# -*- coding: utf-8 -*-
"""
SlideCapture Pro - PPT 변환 작업 프로세스

화면(GUI)과 분리된 별도 프로세스에서 PPT 변환을 실행한다.
변환 중에도 캡처·미리보기·불러오기 등 다른 메뉴를 그대로 쓸 수 있게 하기 위함.
진행 상황은 multiprocessing Queue 로 GUI에 전달한다:
  ("progress", 현재, 전체, 파일명) / ("done", 결과 dict) / ("error", 메시지, 상세)
"""
import json
import shutil
import traceback
from pathlib import Path

import cv2
import numpy as np


def trim_side_bars(frame, saved=False):
    """좌우 검은 여백(필러박스)을 화면 경계에서 정확히 잘라 (x1, x2)를 돌려준다. 자를 게 없으면 (0, w).
    - 캡처 시: 양쪽 모두 화면 폭 1.5%(최소 8px) 이상의 완전한 검정 띠가 있을 때만 자른다(어두운 슬라이드 보호).
    - saved=True: 이전 버전이 남긴 3~12px의 좌우 대칭 검정 줄도 잘라낸다(이미 저장된 캡처용).
    검정 띠와 화면 사이의 어두운 경계 띠(최대 3px)도 함께 자른다. 남는 폭이 55% 미만이면 자르지 않는다."""
    h, w = frame.shape[:2]
    if w < 200 or h < 100:
        return 0, w
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    is_bar = np.mean(gray <= 24, axis=0) >= 0.97       # HDMI 제한 범위(검정=16)·잡음까지 감안
    left = 0
    while left < w and is_bar[left]:
        left += 1
    right = 0
    while right < w and is_bar[w - 1 - right]:
        right += 1
    min_bar = max(8, int(w * 0.015))
    ok = left >= min_bar and right >= min_bar
    if not ok and saved:
        ok = 3 <= left <= 12 and 3 <= right <= 12 and abs(left - right) <= 3
    if not ok:
        return 0, w
    x1, x2 = left, w - right
    if x2 - x1 < w * 0.55:
        return 0, w
    colmean = gray.mean(axis=0)
    # 경계의 어두운 띠: 바로 안쪽 화면보다 확연히 어두운 열만 추가로 자른다(최대 3px)
    for _ in range(3):
        inner = colmean[x1 + 2:x1 + 8].mean() if x1 + 8 < x2 else 255
        if colmean[x1] < 0.6 * inner and colmean[x1] < 120:
            x1 += 1
        else:
            break
    for _ in range(3):
        inner = colmean[x2 - 8:x2 - 2].mean() if x2 - 8 > x1 else 255
        if colmean[x2 - 1] < 0.6 * inner and colmean[x2 - 1] < 120:
            x2 -= 1
        else:
            break
    return int(x1), int(x2)


def read_image(path):
    """한글·공백이 들어간 경로도 읽을 수 있게 np.fromfile + imdecode 사용. 실패하면 None"""
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        if data.size == 0:
            return None
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception:
        return None


def run_job(job, q):
    """job: frames, out, workdir, app, auto_aspect, ocr, tables, vectors, keep_bg, inpaint, exact, tess"""
    work = Path(job["workdir"])
    rebuilder = None
    try:
        from pptx import Presentation
        from pptx.util import Inches
        from slide_rebuild import SlideRebuilder

        frames = [Path(f) for f in job["frames"]]
        total = len(frames)

        def load(path):
            img = read_image(path)
            if img is not None and job.get("auto_aspect", True):
                x1, x2 = trim_side_bars(img, saved=True)   # 이전에 저장된 캡처의 좌우 검정 줄 제거
                if x1 > 0 or x2 < img.shape[1]:
                    img = img[:, x1:x2].copy()
            return img

        work.mkdir(parents=True, exist_ok=True)
        first = None
        for f in frames:
            first = load(f)
            if first is not None:
                break
        if first is None:
            raise ValueError("읽을 수 있는 캡처 이미지가 없습니다.")
        prs = Presentation()
        prs.slide_width = Inches(13.333333)
        fh, fw = first.shape[:2]
        prs.slide_height = int(prs.slide_width * fh / fw)
        blank = prs.slide_layouts[6]
        exact = bool(job["exact"])
        keep_bg = bool(job["keep_bg"])
        rebuilder = SlideRebuilder(ocr_enabled=job["ocr"], tables=job["tables"], vectors=job["vectors"],
                                   keep_bg=keep_bg, inpaint=job["inpaint"], tess_path=job["tess"],
                                   workdir=work, exact=exact)
        mode = "원본 동일(투명 텍스트)" if exact else ("배경 유지(안전)" if keep_bg else "완전 복원")
        report = {"application": job["app"], "mode": mode, "slides": [], "skipped": []}
        n_out = 0
        for n, path in enumerate(frames, 1):
            q.put(("progress", n, total, path.name))
            img = load(path)
            if img is None:      # 변환 중에 삭제되었거나 읽을 수 없는 파일은 건너뜀
                report["skipped"].append(str(path))
                continue
            n_out += 1
            slide = prs.slides.add_slide(blank)
            st = rebuilder.build(prs, slide, img, f"{n:03d}")
            note = (f"{job['app']} 복원 결과: 글자 {st['ocr_lines']}줄 중 텍스트 상자 {st['text_boxes']}개, "
                    f"원본 유지 {st.get('kept_original', 0)}줄, "
                    f"표 {len(st['tables'])}개(셀 글자 {st['table_text_items']}개), 도형 {st['shapes']}개, "
                    f"선 {st['lines']}개, 아이콘(프리폼) {st.get('freeforms', 0)}개, "
                    f"그림 조각 {st['pictures']}개. ")
            if exact:
                note += "화면은 원본 캡처 그대로이며 글자는 보이지 않는 텍스트(검색·복사용)입니다."
            else:
                note += ("인식 결과를 원본 글자 모양과 비교해 일치하는 줄만 텍스트로 바꾸고, "
                         "오인식이 의심되는 줄은 원본 그림 그대로 두었습니다. "
                         "도형·선·아이콘은 원본과 같은 위치·크기·색의 편집 가능한 객체이며, "
                         "사진처럼 복잡한 영역만 그림으로 남겼습니다.")
            slide.notes_slide.notes_text_frame.text = note
            report["slides"].append({"slide": n_out, "image": path.name,
                                     "resolution": [img.shape[1], img.shape[0]], **st})
        if n_out == 0:
            raise ValueError("변환할 수 있는 캡처 이미지가 없습니다.")
        q.put(("saving", total, total, ""))
        prs.save(job["out"])
        rebuilder.cleanup()
        report_path = Path(job["out"]).with_suffix(".json")
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        q.put(("done", {"out": job["out"], "report": str(report_path), "slides": n_out, "mode": mode,
                        "skipped": report["skipped"]}))
    except Exception as e:
        if rebuilder:
            try:
                rebuilder.cleanup()
            except Exception:
                pass
        q.put(("error", str(e), traceback.format_exc()))
    finally:
        shutil.rmtree(work, ignore_errors=True)
