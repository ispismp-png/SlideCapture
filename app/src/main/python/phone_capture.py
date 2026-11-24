# -*- coding: utf-8 -*-
"""
SlideCapture Pro Android - 캡처 이미지 정리

- 폰 카메라 촬영: 사진에서 화면(모니터·프로젝터 스크린) 테두리를 찾아 사다리꼴을 반듯하게 펴고 슬라이드만 잘라낸다.
- USB HDMI 캡처 보드: PC 버전과 같은 방식으로 좌우 검은 여백(필러박스)을 화면 경계에서 정확히 잘라낸다.
"""
import json

import cv2
import numpy as np

from ppt_worker import read_image, trim_side_bars

_ASPECTS = [(16, 9), (16, 10), (4, 3), (3, 2), (21, 9)]


def _order(pts):
    pts = np.asarray(pts, np.float32).reshape(4, 2)
    s = pts.sum(1)
    d = np.diff(pts, axis=1).ravel()
    tl, br = pts[np.argmin(s)], pts[np.argmax(s)]
    tr, bl = pts[np.argmin(d)], pts[np.argmax(d)]
    return np.array([tl, tr, br, bl], np.float32)


def _quad_ok(q, W, H, min_area):
    q = _order(q)
    if not cv2.isContourConvex(q.reshape(-1, 1, 2).astype(np.int32)):
        return False
    area = cv2.contourArea(q)
    if area < min_area:
        return False
    # 각 변 길이 비교(너무 찌그러진 사각형 제외)
    w1, w2 = np.linalg.norm(q[1] - q[0]), np.linalg.norm(q[2] - q[3])
    h1, h2 = np.linalg.norm(q[3] - q[0]), np.linalg.norm(q[2] - q[1])
    if min(w1, w2) < 0.45 * max(w1, w2) or min(h1, h2) < 0.45 * max(h1, h2):
        return False
    return True


def _line_x(p1, p2, p3, p4):
    """두 직선(p1-p2, p3-p4)의 교점"""
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    x4, y4 = p4
    d = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(d) < 1e-6:
        return None
    a = x1 * y2 - y1 * x2
    b = x3 * y4 - y3 * x4
    return np.array([(a * (x3 - x4) - (x1 - x2) * b) / d, (a * (y3 - y4) - (y1 - y2) * b) / d], np.float32)


def _quad_from_contour(c):
    """윤곽의 볼록 껍질에서 가장 긴 네 변을 직선으로 연장해 꼭짓점을 구한다.
    슬라이드 모서리 쪽이 어두운 색(제목 띠 등)이라 모서리가 잘려 보여도 정확한 꼭짓점을 복원한다."""
    hull = cv2.convexHull(c).reshape(-1, 2).astype(np.float32)
    peri = cv2.arcLength(hull.reshape(-1, 1, 2), True)
    ap = cv2.approxPolyDP(hull.reshape(-1, 1, 2), 0.002 * peri, True).reshape(-1, 2).astype(np.float32)
    n = len(ap)
    if n < 4:
        return None
    if n == 4:
        return ap
    edges = [(np.linalg.norm(ap[(i + 1) % n] - ap[i]), i) for i in range(n)]
    keep = sorted(i for _, i in sorted(edges, reverse=True)[:4])
    pts = []
    for k in range(4):
        i, j = keep[k], keep[(k + 1) % 4]
        x = _line_x(ap[i], ap[(i + 1) % n], ap[j], ap[(j + 1) % n])
        if x is None:
            return None
        pts.append(x)
    return np.array(pts, np.float32)


def _touches_border(q, w, h):
    """사각형이 사진 가장자리 두 곳 이상에 닿으면 화면 테두리가 아니라 슬라이드 안쪽 영역일 가능성이 크다
    (화면만 꽉 차게 찍은 사진·스크린샷). 이때는 보정하지 않는다."""
    m = 0.02
    xs, ys = q[:, 0], q[:, 1]
    sides = int(xs.min() <= m * w) + int(xs.max() >= (1 - m) * w) + int(ys.min() <= m * h) + int(ys.max() >= (1 - m) * h)
    return sides >= 2


def find_screen(img):
    """사진에서 화면 사각형(4점, 원본 좌표)을 찾는다. 못 찾으면 None"""
    H, W = img.shape[:2]
    s = 900.0 / max(H, W)
    small = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else img.copy()
    if s >= 1:
        s = 1.0
    h, w = small.shape[:2]
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    min_area = 0.18 * w * h
    cands = []

    def collect(mask):
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:6]:
            if cv2.contourArea(c) < min_area:
                break
            q4 = _quad_from_contour(c)
            if q4 is not None and _quad_ok(q4, w, h, min_area) and \
                    np.all((q4 > -0.05 * max(w, h)) & (q4 < 1.05 * max(w, h))):
                cands.append((cv2.contourArea(_order(q4)), _order(q4)))
                continue
            peri = cv2.arcLength(c, True)
            for eps in (0.015, 0.025, 0.04):
                ap = cv2.approxPolyDP(c, eps * peri, True)
                if len(ap) == 4 and _quad_ok(ap, w, h, min_area):
                    cands.append((cv2.contourArea(ap), _order(ap)))
                    break
            else:
                # 모서리가 둥글거나 일부 가려진 경우: 볼록 껍질의 최소 사각형
                hull = cv2.convexHull(c)
                if cv2.contourArea(hull) >= min_area:
                    box = cv2.boxPoints(cv2.minAreaRect(hull))
                    fill = cv2.contourArea(c) / max(1.0, cv2.contourArea(box))
                    if fill > 0.85 and _quad_ok(box, w, h, min_area):
                        cands.append((cv2.contourArea(box) * 0.9, _order(box)))

    # 1) 밝은 화면 영역(화면은 주변보다 밝다) - 찾으면 이것을 우선 사용
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    collect(th)
    if cands:
        area, q = max(cands, key=lambda a: a[0])
        return None if area > 0.97 * w * h or _touches_border(q, w, h) else q / s
    # 2) 테두리(엣지) 기반 (어두운 배경 슬라이드 등)
    ed = cv2.Canny(gray, 40, 120)
    ed = cv2.dilate(ed, np.ones((3, 3), np.uint8), iterations=2)
    ed = cv2.morphologyEx(ed, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    cnts, _ = cv2.findContours(ed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    fill = np.zeros_like(ed)
    cv2.drawContours(fill, cnts, -1, 255, -1)
    collect(fill)
    if not cands:
        return None
    # 사진 전체를 거의 다 덮는 사각형(이미 화면만 찍힌 경우)은 보정 불필요
    area, q = max(cands, key=lambda a: a[0])
    if area > 0.97 * w * h or _touches_border(q, w, h):
        return None
    return q / s


def _target_size(q, max_w=1920):
    w = (np.linalg.norm(q[1] - q[0]) + np.linalg.norm(q[2] - q[3])) / 2.0
    h = (np.linalg.norm(q[3] - q[0]) + np.linalg.norm(q[2] - q[1])) / 2.0
    r = w / max(1.0, h)
    best = min(_ASPECTS, key=lambda a: abs(r - a[0] / a[1]))
    if abs(r - best[0] / best[1]) / r < 0.08:      # 흔한 화면 비율이면 그 비율로 맞춤
        r = best[0] / float(best[1])
    out_w = int(min(max_w, max(960, w)))
    return out_w, int(round(out_w / r))


def rectify(img, quad=None, max_w=1920):
    """화면 사각형을 반듯한 직사각형으로 편다"""
    if quad is None:
        quad = find_screen(img)
    if quad is None:
        return img, None
    q = _order(quad)
    ow, oh = _target_size(q, max_w)
    dst = np.array([[0, 0], [ow - 1, 0], [ow - 1, oh - 1], [0, oh - 1]], np.float32)
    M = cv2.getPerspectiveTransform(q, dst)
    out = cv2.warpPerspective(img, M, (ow, oh), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return out, q


def _read_oriented(path):
    """폰 카메라 JPEG의 EXIF 회전 정보를 반영해 읽는다"""
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            return cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
    except Exception:
        return read_image(path)


def process_capture(in_path, out_path, source="camera", correct=True, trim=True):
    """캡처 이미지를 정리해 out_path(PNG)에 저장. 결과 정보를 JSON 문자열로 돌려준다(Kotlin에서 사용).
    source: camera(폰 카메라: 화면 보정) / board(USB 캡처보드: 검은 여백 제거) / import(기존 파일: 이전 버전 검정 줄만 제거)"""
    img = _read_oriented(in_path) if source == "camera" else read_image(in_path)
    if img is None:
        return json.dumps({"ok": False, "error": "이미지를 읽을 수 없습니다."}, ensure_ascii=False)
    info = {"ok": True, "source": source, "in": [int(img.shape[1]), int(img.shape[0])], "rectified": False,
            "trimmed": False}
    if source == "camera" and correct:
        out, q = rectify(img)
        if q is not None:
            img = out
            info["rectified"] = True
            info["quad"] = [[round(float(x), 1), round(float(y), 1)] for x, y in q]
    if trim:
        x1, x2 = trim_side_bars(img, saved=(source != "board"))
        if x1 > 0 or x2 < img.shape[1]:
            img = img[:, x1:x2].copy()
            info["trimmed"] = True
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        return json.dumps({"ok": False, "error": "PNG 저장 실패"}, ensure_ascii=False)
    buf.tofile(str(out_path))
    info["out"] = [int(img.shape[1]), int(img.shape[0])]
    return json.dumps(info, ensure_ascii=False)


def change_score(path_a, path_b):
    """두 캡처의 차이(0~1). 기존파일열기 중복 판단 보조용"""
    a, b = read_image(path_a), read_image(path_b)
    if a is None or b is None:
        return 1.0
    a = cv2.resize(cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), (64, 36), interpolation=cv2.INTER_AREA)
    b = cv2.resize(cv2.cvtColor(b, cv2.COLOR_BGR2GRAY), (64, 36), interpolation=cv2.INTER_AREA)
    return float(np.mean(np.abs(a.astype(np.int16) - b.astype(np.int16)) > 18))
