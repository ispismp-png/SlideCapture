# -*- coding: utf-8 -*-
"""
SlideCapture Pro 재구성 엔진 (프로그램 버전은 slidecapture_pro.py 의 VERSION)

- OCR 2단계 인식 + "다시 그려 비교" 검증. 원본과 일치가 확인된 글자만 텍스트로 바꾸고
  나머지는 원본 픽셀 그대로 둔다(깨짐 방지). exact=True 이면 원본 그대로 + 투명 텍스트층.
- 도형(사각형/둥근사각형/원)·선(커넥터)·아이콘/차트 조각(프리폼, 구멍 포함)을 네이티브 객체로 만든다.
  '원본 배경 유지' 모드에서도 배경 캡처 위에 같은 위치·크기·색으로 겹쳐 올려 화면은 원본과 같게 유지.
  사진처럼 색이 섞인 영역은 PNG 그림으로 둔다.

캡처 이미지 1장 -> 편집 가능한 PPT 슬라이드 1장

기존 방식의 문제
  1) 윤곽선 검출(Canny/Hough)로 만든 사각형·선이 원본 배경 위에 겹쳐 그려져 이중선/잡선 발생
  2) 글자 크기를 OCR 박스 높이로만 추정 -> 폭이 안 맞아 글자가 겹치거나 줄바꿈
  3) 한글 글꼴(East Asian)을 지정하지 않아 PowerPoint에서 글꼴이 바뀜
  4) 표에 기본 표 스타일(파란 헤더/줄무늬)이 적용되어 원본과 다르게 보임
  5) 텍스트 제거를 TELEA inpainting 으로만 처리해 번짐 발생

새 방식 (완전 복원 모드)
  OCR -> 글자 제거(깨끗한 이미지) -> 색 영역 분할로 '채움색이 있는 도형'(사각형/둥근사각형/원/그라데이션/테두리)
  -> 선·표 격자 검출 -> 도형으로 설명되지 않는 나머지(차트·아이콘 등)만 투명 PNG 조각
  -> 글자는 폭 맞춤 글꼴 크기로 편집 가능한 텍스트 상자
"""
import math
import re
from pathlib import Path

import cv2
import numpy as np
from lxml import etree
from PIL import Image, ImageDraw, ImageFont
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

FONT_NAME = "맑은 고딕"
# (보통 글꼴, 굵은 글꼴, ttc 인덱스) - 글자 폭 측정과 '다시 그려 비교' 검증에 사용
_FONT_PATHS = [
    (r"C:\Windows\Fonts\malgun.ttf", r"C:\Windows\Fonts\malgunbd.ttf", 0),
    ("/usr/share/fonts/truetype/nanum/NanumGothic.ttf", "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 1),
]
NO_STYLE_NO_GRID = "{2D5ABB26-0587-4C30-8999-92F81FD0307C}"

# 검증 기준 (낮추면 더 많은 글자를 편집 가능 텍스트로 바꾸지만 원본과 달라질 위험이 커짐)
MIN_CONF = 45          # 줄 단위 OCR 신뢰도
MIN_SHAPE_F1 = 0.80    # 원본 글자 모양 vs 인식 글자를 다시 그린 모양의 일치도(줄 전체)
MIN_CHAR_F1 = 0.60     # 글자 하나하나의 최저 일치도(한 글자만 틀려도 걸러냄)
MAX_FOREIGN = 0.035    # 글자 상자 안에서 배경·글자색이 아닌 픽셀(그림·아이콘) 비율
MIN_RING_BG = 0.72     # 글자 상자 바깥 띠가 배경 단색인 비율


# ---------------------------------------------------------------- 공통 유틸
def _rgb(bgr):
    b, g, r = [int(round(float(v))) for v in bgr]
    return RGBColor(max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))


def _dist(a, b):
    return int(np.abs(np.asarray(a, np.int16) - np.asarray(b, np.int16)).max())


def _mode_color(pix):
    """가장 많이 쓰인 색(배경색)을 반환. pix: Nx3 uint8 BGR"""
    q = (pix // 8).astype(np.int32)
    key = (q[:, 0] * 32 + q[:, 1]) * 32 + q[:, 2]
    k = np.bincount(key).argmax()
    return np.median(pix[key == k], axis=0)


def _cluster(vals, tol):
    vals = sorted(int(v) for v in vals)
    groups = []
    for v in vals:
        if not groups or v - groups[-1][-1] > tol:
            groups.append([v])
        else:
            groups[-1].append(v)
    return [int(round(sum(g) / len(g))) for g in groups]


def _is_textish(t):
    return any(c.isalnum() or 0xAC00 <= ord(c) <= 0xD7A3 or 0x3131 <= ord(c) <= 0x318E for c in t)


def _ring(img, x, y, w, h, p):
    """(x,y,w,h) 상자 바깥 p 픽셀 두께의 띠 픽셀(Nx3)"""
    H, W = img.shape[:2]
    x1, y1 = max(0, x - p), max(0, y - p)
    x2, y2 = min(W, x + w + p), min(H, y + h + p)
    big = img[y1:y2, x1:x2]
    m = np.ones(big.shape[:2], bool)
    m[max(0, y - y1):max(0, y - y1) + h, max(0, x - x1):max(0, x - x1) + w] = False
    ring = big[m]
    return ring if len(ring) >= 10 else big.reshape(-1, 3)


def _seg_dist(pix, bg, fg):
    """픽셀이 배경색~글자색 사이(안티앨리어싱 포함) 선분에서 얼마나 떨어져 있는지 (Nx... -> N...)"""
    bg = np.asarray(bg, np.float32)
    fg = np.asarray(fg, np.float32)
    v = fg - bg
    vv = float((v * v).sum())
    p = pix.astype(np.float32) - bg
    if vv < 1:
        return np.abs(p).max(-1)
    t = np.clip((p * v).sum(-1) / vv, 0, 1)
    return np.abs(p - t[..., None] * v).max(-1)


class TextMeter:
    """글자 폭 측정·다시 그리기: 실제 글꼴(맑은 고딕)이 있으면 정확히, 없으면 근사치."""

    def __init__(self):
        self.reg = self.bold = None
        for rp, bp, idx in _FONT_PATHS:
            if not Path(rp).exists():
                continue
            try:
                self.reg = ImageFont.truetype(rp, 100, index=idx)
            except Exception:
                continue
            try:
                self.bold = ImageFont.truetype(bp, 100, index=idx) if Path(bp).exists() else self.reg
            except Exception:
                self.bold = self.reg
            break
        self._cache = {}

    @property
    def ok(self):
        return self.reg is not None

    def _f(self, bold):
        return self.bold if bold else self.reg

    def width100(self, text, bold=False):
        f = self._f(bold) if self.ok else None
        if f is not None:
            try:
                return float(f.getlength(text))
            except Exception:
                pass
        return sum(100.0 if ord(c) >= 0x1100 else 55.0 for c in text)

    def ink(self, text, bold=False, spc=0.0):
        """100px 크기로 그린 글자의 잉크 마스크(잉크 영역만 잘라냄)와 글꼴 기준 위치 정보.
        spc: 글자 사이 추가 간격(100px 기준 px) - PowerPoint 문자 간격(spc)과 같은 효과"""
        spc = round(float(spc), 1)
        key = (text, bool(bold), spc)
        if key in self._cache:
            return self._cache[key]
        res = None
        if self.ok:
            f = self._f(bold)
            asc, desc = f.getmetrics()
            wid = int(self.width100(text, bold) + max(0.0, spc) * len(text)) + 40
            im = Image.new("L", (max(10, wid), asc + desc + 40), 0)
            dr = ImageDraw.Draw(im)
            if abs(spc) < 0.05:
                dr.text((20, 20), text, font=f, fill=255)
            else:
                xx = 20.0
                for ch in text:
                    dr.text((xx, 20), ch, font=f, fill=255)
                    xx += f.getlength(ch) + spc
            a = np.asarray(im) > 110
            ys, xs = np.nonzero(a)
            if len(xs):
                x1, x2, y1, y2 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
                # cy_off: 잉크 중심이 글꼴 줄 상자(ascent+descent) 중심보다 얼마나 아래인지(100px 기준)
                res = dict(mask=a[y1:y2, x1:x2], w=x2 - x1, h=y2 - y1, lx=x1 - 20,
                           cy_off=((y1 + y2) / 2.0 - 20) - (asc + desc) / 2.0, line=asc + desc)
        self._cache[key] = res
        return res


# ---------------------------------------------------------------- OCR
def _tess(tess_path):
    import pytesseract
    if tess_path and Path(tess_path).exists():
        pytesseract.pytesseract.tesseract_cmd = tess_path
    return pytesseract


def _vertical_rules(img):
    """세로 구분선(표·상자 테두리) 마스크: 서로 다른 칸의 글자가 한 줄로 합쳐지지 않게 하는 용도"""
    im = img.astype(np.int16)
    off = 3
    a = np.abs(im[:, off:-off] - im[:, :-2 * off]).max(2) > 30
    b = np.abs(im[:, off:-off] - im[:, 2 * off:]).max(2) > 30
    vm = np.zeros(img.shape[:2], np.uint8)
    vm[:, off:-off] = (a & b)
    vm = cv2.morphologyEx(vm, cv2.MORPH_OPEN, np.ones((24, 1), np.uint8))
    return vm


def _rule_between(vrule, x1, x2, y1, y2):
    """x1~x2 사이에 줄 높이보다 위아래로 길게 이어진 세로선이 있으면 True (글자 획은 제외)"""
    x1, x2 = int(x1), int(x2)
    if x2 <= x1:
        return False
    h = max(1, y2 - y1)
    ya, yb = max(0, int(y1 - 0.35 * h)), min(vrule.shape[0], int(y2 + 0.35 * h))
    return bool(vrule[ya:yb, x1:x2].all(axis=0).any())


def _detect_words(img, pyt, lang):
    """1단계: 글자 위치 찾기 (2배 확대, 희소 텍스트 모드). 인식 글자는 2단계에서 다시 읽는다."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    med = cv2.medianBlur(gray, 15)
    on = (cv2.absdiff(gray, med) > 22).astype(np.uint8)
    rules = cv2.morphologyEx(on, cv2.MORPH_OPEN, np.ones((1, 45), np.uint8)) | \
        cv2.morphologyEx(on, cv2.MORPH_OPEN, np.ones((45, 1), np.uint8))
    rules = cv2.dilate(rules, np.ones((3, 3), np.uint8))
    gray = np.where(rules > 0, med, gray).astype(np.uint8)
    scale = 2.0 if img.shape[1] <= 2200 else 1.0
    g = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC) if scale != 1.0 else gray
    d = pyt.image_to_data(Image.fromarray(g), lang=lang, config="--oem 3 --psm 11",
                          output_type=pyt.Output.DICT)
    words = []
    for i, raw in enumerate(d["text"]):
        t = raw.strip()
        try:
            conf = float(d["conf"][i])
        except Exception:
            conf = -1
        if not t or conf < 10 or not _is_textish(t):
            continue
        x, y = int(d["left"][i] / scale), int(d["top"][i] / scale)
        w, h = max(1, int(round(d["width"][i] / scale))), max(1, int(round(d["height"][i] / scale)))
        if h < 7 or h > 160 or w < 3:
            continue
        words.append(dict(t=t, x=x, y=y, w=w, h=h, c=conf))
    return words


def _ink_bridge(img, x1, x2, y1, y2, h):
    """두 조각 사이(x1~x2)에 OCR이 놓친 글자 잉크가 이어져 있는지: 빈 구간이 글자 높이보다 짧으면 True"""
    x1, x2, y1, y2 = int(x1), int(x2), int(y1), int(y2)
    if x2 <= x1:
        return True
    reg = img[max(0, y1):max(y1 + 1, y2), x1:x2]
    if reg.size == 0:
        return False
    bg = _mode_color(reg.reshape(-1, 3))
    col = (np.abs(reg.astype(np.int16) - bg).max(2) > 40).any(axis=0)
    if col.mean() < 0.25:
        return False
    run = best = 0
    for v in col:
        run = 0 if v else run + 1
        best = max(best, run)
    return best <= 0.9 * h


def _group_lines(words, vrule, img=None):
    """같은 높이대·가까운 단어를 한 줄로. 세로 구분선을 넘거나 간격이 크면 끊는다."""
    words = sorted(words, key=lambda a: a["x"])
    lines = []
    for wd in words:
        cy = wd["y"] + wd["h"] / 2.0
        best = None
        for ln in lines:
            hh = max(min(wd["h"], ln["h"]), 0.6 * max(wd["h"], ln["h"]))
            if abs(cy - ln["cy"]) > 0.45 * max(wd["h"], ln["h"]):
                continue
            if max(wd["h"], ln["h"]) > 1.8 * min(wd["h"], ln["h"]):
                continue
            gap = wd["x"] - ln["x2"]
            if not (-0.5 * hh <= gap <= 1.25 * hh):
                continue
            if gap > 1 and _rule_between(vrule, ln["x2"], wd["x"], max(ln["y"], wd["y"]),
                                         min(ln["y2"], wd["y"] + wd["h"])):
                continue
            best = ln
            break
        if best is None:
            lines.append(dict(words=[wd], x=wd["x"], y=wd["y"], x2=wd["x"] + wd["w"], y2=wd["y"] + wd["h"],
                              cy=cy, h=wd["h"]))
        else:
            best["words"].append(wd)
            best["x2"] = max(best["x2"], wd["x"] + wd["w"])
            best["y"] = min(best["y"], wd["y"])
            best["y2"] = max(best["y2"], wd["y"] + wd["h"])
            best["cy"] = (best["y"] + best["y2"]) / 2.0
            best["h"] = float(np.median([q["h"] for q in best["words"]]))
    # 같은 줄인데 따로 잡힌 조각(겹치거나 가까운 것)을 반복해서 합친다
    changed = True
    while changed:
        changed = False
        lines.sort(key=lambda a: (a["x"], a["y"]))
        for i in range(len(lines)):
            a = lines[i]
            for j in range(i + 1, len(lines)):
                b = lines[j]
                ov = min(a["y2"], b["y2"]) - max(a["y"], b["y"])
                hmin, hmax = min(a["y2"] - a["y"], b["y2"] - b["y"]), max(a["y2"] - a["y"], b["y2"] - b["y"])
                inside = (a["y"] - 3 <= b["y"] and b["y2"] <= a["y2"] + 3) or \
                         (b["y"] - 3 <= a["y"] and a["y2"] <= b["y2"] + 3)
                if ov < 0.55 * hmin or (hmax > 1.7 * hmin and not (inside and hmax <= 4 * hmin)):
                    continue
                gap = max(a["x"], b["x"]) - min(a["x2"], b["x2"])
                hh = max(a["y2"] - a["y"], b["y2"] - b["y"])
                if gap > 1.1 * hh:
                    # 사이에 OCR이 놓친 글자가 이어져 있으면 합친다(굵은 한글이 조각나 인식되는 경우)
                    if gap > 4.0 * hh or img is None or not _ink_bridge(
                            img, min(a["x2"], b["x2"]), max(a["x"], b["x"]),
                            max(a["y"], b["y"]), min(a["y2"], b["y2"]), hh):
                        continue
                if gap > 1 and _rule_between(vrule, min(a["x2"], b["x2"]), max(a["x"], b["x"]),
                                             max(a["y"], b["y"]), min(a["y2"], b["y2"])):
                    continue
                a["words"] += b["words"]
                a["x"], a["y"] = min(a["x"], b["x"]), min(a["y"], b["y"])
                a["x2"], a["y2"] = max(a["x2"], b["x2"]), max(a["y2"], b["y2"])
                a["cy"] = (a["y"] + a["y2"]) / 2.0
                a["h"] = float(np.median([q["h"] for q in a["words"]]))
                del lines[j]
                changed = True
                break
            if changed:
                break
    return lines


_BULLETS = set("ㆍ·•『』「」[]|:;*-‐~¦ㅣ")


def _tidy(txt):
    """줄 앞의 글머리표 오인식('『', 'ㆍ', '|' 등)은 '•'로, 줄 끝의 외톨이 기호는 제거"""
    parts = txt.split(" ")
    if len(parts) > 1 and len(parts[0]) == 1 and parts[0] in _BULLETS:
        parts[0] = "•"
    while len(parts) > 1 and len(parts[-1]) == 1 and parts[-1] in _BULLETS:
        parts.pop()
    return " ".join(parts)


_HG = "가-힣"


def _variants(t):
    """흔한 OCR 오류를 고친 후보들: 가운뎃점(·)을 ':'·'-'·'ㆍ'로 읽음, 넓은 자간 때문에 생긴 띄어쓰기.
    어느 후보가 맞는지는 '다시 그려 비교'로 결정한다."""
    out = [t]
    dot = re.sub(r"(?<=[%s])\s?[:ㆍ;]\s?(?=[%s])" % (_HG, _HG), "·", t)
    dot2 = re.sub(r"(?<=[%s])[\-.](?=[%s])" % (_HG, _HG), "·", dot)
    toks = t.split(" ")
    singles = sum(1 for a in toks if len(a) == 1 and re.match("[%s]" % _HG, a))
    if len(toks) > 2 and singles >= 0.5 * len(toks):   # 자간이 넓어 글자마다 띄어 읽힌 경우만
        joined = toks[0]
        for a, b in zip(toks, toks[1:]):
            one = (len(a) == 1 and re.match("[%s]" % _HG, a)) or (len(b) == 1 and re.match("[%s]" % _HG, b))
            both = re.search("[%s]$" % _HG, a) and re.match("[%s]" % _HG, b)
            joined += ("" if (one and both) else " ") + b
    else:
        joined = t
    if len(t) > 1 and t[0] in _BULLETS and t[0] not in "-(":   # 붙어서 읽힌 글머리표
        for b in ("• " + t[1:].lstrip(), "· " + t[1:].lstrip(), "ㆍ" + t[1:].lstrip()):
            if b not in out:
                out.append(b)
    for v in (dot, dot2, joined, re.sub(r"(?<=[%s])\s?[:ㆍ;]\s?(?=[%s])" % (_HG, _HG), "·", joined)):
        if v not in out:
            out.append(v)
    return out


def _recognize_lines(img, cands, pyt, lang):
    """2단계: 줄마다 잘라 '어두운 글자/흰 바탕'으로 정규화·확대한 뒤 세로로 쌓아 한 번에 다시 인식.
    흰 글자/어두운 배경도 같은 조건으로 읽히고, 한 줄 단위로 읽어 한글 띄어쓰기가 정확해진다."""
    H, W = img.shape[:2]
    TH = 44                         # 정규화한 줄 높이(px)
    items = []
    for ci, ln in enumerate(cands):
        x, y, w, h = int(ln["x"]), int(ln["y"]), int(ln["x2"] - ln["x"]), int(ln["y2"] - ln["y"])
        bg = _mode_color(_ring(img, x, y, w, h, max(3, int(h * 0.3))))
        px, py = int(0.45 * h) + 3, int(0.25 * h) + 2
        x0, y0, x1, y1 = max(0, x - px), max(0, y - py), min(W, x + w + px), min(H, y + h + py)
        crop = img[y0:y1, x0:x1]
        if crop.size == 0:
            continue
        dist = np.abs(crop.astype(np.int16) - bg.astype(np.int16)).max(2).astype(np.float32)
        # 상자 테두리·표 선 같은 긴 직선은 지워서 '|' 같은 오인식을 막는다
        on = (dist > 40).astype(np.uint8)
        L = max(12, int(1.15 * h))
        rule = cv2.morphologyEx(on, cv2.MORPH_OPEN, np.ones((L, 1), np.uint8)) | \
            cv2.morphologyEx(on, cv2.MORPH_OPEN, np.ones((1, max(L, int(2.5 * h))), np.uint8))
        dist[cv2.dilate(rule, np.ones((3, 3), np.uint8)) > 0] = 0
        c = float(np.percentile(dist, 99.5))
        if c < 28:
            continue
        norm = 255 - np.clip(dist * (255.0 / (0.8 * c)), 0, 255)
        s = TH / float(max(6, h))
        norm = cv2.resize(norm.astype(np.uint8), None, fx=s, fy=s,
                          interpolation=cv2.INTER_CUBIC if s > 1 else cv2.INTER_AREA)
        items.append(dict(ln=ln, idx=ci, x0=x0, y0=y0, s=s, im=norm))
    out = []
    CH = 30
    for k in range(0, len(items), CH):
        chunk = items[k:k + CH]
        gap = 36
        cw = max(it["im"].shape[1] for it in chunk) + 2 * gap
        chh = sum(it["im"].shape[0] + gap for it in chunk) + gap
        canvas = np.full((chh, cw), 255, np.uint8)
        oy = gap
        for it in chunk:
            hh, ww = it["im"].shape
            canvas[oy:oy + hh, gap:gap + ww] = it["im"]
            it["oy"], it["ox"], it["words"] = oy, gap, []
            oy += hh + gap
        d = pyt.image_to_data(Image.fromarray(canvas), lang=lang,
                              config="--oem 3 --psm 6 -c preserve_interword_spaces=1",
                              output_type=pyt.Output.DICT)
        for i, raw in enumerate(d["text"]):
            t = raw.strip()
            if not t:
                continue
            try:
                conf = float(d["conf"][i])
            except Exception:
                conf = -1
            if conf < 0:
                continue
            cy = d["top"][i] + d["height"][i] / 2.0
            it = next((q for q in chunk if q["oy"] <= cy < q["oy"] + q["im"].shape[0]), None)
            if it is None:
                continue
            s = it["s"]
            it["words"].append(dict(t=t, c=conf,
                                    x=it["x0"] + (d["left"][i] - it["ox"]) / s,
                                    y=it["y0"] + (d["top"][i] - it["oy"]) / s,
                                    w=d["width"][i] / s, h=d["height"][i] / s,
                                    key=(d["block_num"][i], d["par_num"][i], d["line_num"][i])))
        for it in chunk:
            ws = it["words"]
            if not ws:
                continue
            # 한 줄에서 여러 줄로 읽힌 경우 가장 큰(원래) 줄만 사용
            keys = {}
            for q in ws:
                keys.setdefault(q["key"], []).append(q)
            ws = max(keys.values(), key=lambda g: sum(len(q["t"]) for q in g))
            ws.sort(key=lambda q: q["x"])
            hmed = float(np.median([q["h"] for q in ws]))
            gaps = [b["x"] - (a["x"] + a["w"]) for a, b in zip(ws, ws[1:])]
            single = sum(1 for q in ws if len(q["t"]) == 1 and 0xAC00 <= ord(q["t"]) <= 0xD7A3)
            # 띄어쓰기 후보 여러 개(기준 간격별)를 만들어 두고, 검증 단계에서 원본 모양과 가장 맞는 것을 고른다
            thrs = [0.2 * hmed, 0.1 * hmed, 0.32 * hmed]
            if gaps and single >= 0.5 * len(ws):
                thrs.insert(0, max(0.3 * hmed, float(np.median(gaps)) + 0.22 * hmed))
            texts = []
            for thr in thrs:
                t_ = ws[0]["t"]
                for g_, b in zip(gaps, ws[1:]):
                    t_ += (" " if g_ > thr else "") + b["t"]
                t_ = _tidy(t_)
                if t_ not in texts:
                    texts.append(t_)
            txt = texts[0]
            if not _is_textish(txt):
                continue
            n = sum(len(q["t"]) for q in ws)
            conf = sum(q["c"] * len(q["t"]) for q in ws) / max(1, n)
            x1 = min(q["x"] for q in ws)
            y1 = min(q["y"] for q in ws)
            x2 = max(q["x"] + q["w"] for q in ws)
            y2 = max(q["y"] + q["h"] for q in ws)
            out.append(dict(text=txt, x=int(round(x1)), y=int(round(y1)), w=max(1, int(round(x2 - x1))),
                            h=max(1, int(round(y2 - y1))), hmed=hmed, conf=conf, cid=it["idx"], texts=texts))
    return out


def ocr_lines(img, tess_path="", lang="kor+eng"):
    """글자 줄 목록: [{text,x,y,w,h,hmed,conf}] (원본 픽셀 좌표)"""
    pyt = _tess(tess_path)
    words = _detect_words(img, pyt, lang)
    cands = _group_lines(words, _vertical_rules(img), img)
    if lang != "kor":
        # 한글 위주 모델로 한 번 더 읽어 후보로 둔다(굵은 한글이 영문으로 오인식되는 문제 보완). 두 번 읽기는 병렬 실행.
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(_recognize_lines, img, cands, pyt, lang)
            f2 = ex.submit(_recognize_lines, img, cands, pyt, "kor")
            lines, kor_lines = f1.result(), f2.result()
        alt = {}
        for a in kor_lines:
            alt.setdefault(a["cid"], []).append(a)
        for a in lines:
            a["alts"] = [dict(a)] + [b for b in alt.get(a["cid"], [])
                                     if abs(b["x"] - a["x"]) + abs(b["y"] - a["y"]) < 0.6 * a["h"] + 4]
    else:
        lines = _recognize_lines(img, cands, pyt, lang)
    # 겹치는 줄(같은 글자를 두 번 찾은 경우) 제거: 신뢰도 높은 쪽 유지
    lines.sort(key=lambda a: -a["conf"] * len(a["text"]))
    kept = []
    for a in lines:
        dup = False
        for b in kept:
            ix = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
            iy = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
            if ix > 0 and iy > 0 and ix * iy > 0.4 * min(a["w"] * a["h"], b["w"] * b["h"]):
                dup = True
                break
        if not dup:
            kept.append(a)
    kept.sort(key=lambda a: (a["y"], a["x"]))
    return kept


# ---------------------------------------------------------------- 엔진
class SlideRebuilder:
    def __init__(self, ocr_enabled=True, tables=True, vectors=True, keep_bg=False,
                 inpaint=True, tess_path="", workdir=None, exact=False):
        self.ocr_enabled = ocr_enabled
        # exact=True: 원본 캡처 그대로 + 보이지 않는 텍스트층(검색·복사용). 화면은 원본과 100% 동일
        self.exact = exact
        self.use_tables = tables
        self.use_vectors = vectors
        self.keep_bg = keep_bg
        self.inpaint = inpaint
        self.tess_path = tess_path
        self.workdir = Path(workdir) if workdir else Path(".")
        self.meter = TextMeter()

    # ---------- 글자 색/굵기, 글자 제거
    @staticmethod
    def _ring_bg(img, ln):
        """글자 박스 바깥 테두리 띠의 최빈색 = 글자 뒤 배경색 (굵은 글자도 안전)"""
        h_img, w_img = img.shape[:2]
        p = max(3, int(ln["h"] * 0.3))
        x1, y1 = max(0, ln["x"] - p), max(0, ln["y"] - p)
        x2, y2 = min(w_img, ln["x"] + ln["w"] + p), min(h_img, ln["y"] + ln["h"] + p)
        big = img[y1:y2, x1:x2]
        m = np.ones(big.shape[:2], bool)
        ix1, iy1 = ln["x"] - x1, ln["y"] - y1
        m[max(0, iy1):iy1 + ln["h"], max(0, ix1):ix1 + ln["w"]] = False
        ring = big[m]
        if len(ring) < 10:
            ring = big.reshape(-1, 3)
        return _mode_color(ring)

    def _style_of(self, img, ln):
        h_img, w_img = img.shape[:2]
        pad = max(2, int(ln["h"] / 8))
        x1, y1 = max(0, ln["x"] - pad), max(0, ln["y"] - pad)
        x2, y2 = min(w_img, ln["x"] + ln["w"] + pad), min(h_img, ln["y"] + ln["h"] + pad)
        crop = img[y1:y2, x1:x2].reshape(-1, 3)
        if crop.size == 0:
            ln.update(bg=np.array([255, 255, 255.]), fg=np.array([0, 0, 0.]), bold=False)
            return
        bg = self._ring_bg(img, ln)
        dist = np.abs(crop.astype(np.int16) - bg).max(1)
        mx = int(dist.max())
        if mx < 35:
            fg = np.array([0, 0, 0.]) if bg.mean() > 128 else np.array([255, 255, 255.])
            bold = False
        else:
            sel = dist >= 0.8 * mx
            fg = np.median(crop[sel], axis=0)
            tight = img[ln["y"]:ln["y"] + ln["h"], ln["x"]:ln["x"] + ln["w"]]
            ink = (np.abs(tight.astype(np.int16) - bg).max(2) > 0.5 * mx).astype(np.uint8)
            bold = False
            if ink.sum() > 20:
                dt = cv2.distanceTransform(ink, cv2.DIST_L2, 3)
                stem = 2.0 * float(np.percentile(dt[ink > 0], 85))
                bold = (stem / max(1.0, ln["hmed"] * 0.9)) > 0.135
        ln.update(bg=bg, fg=fg, bold=bold)

    def _verify(self, img, ln):
        """인식 후보(kor+eng / kor)마다 '같은 글꼴로 다시 그려 원본과 비교'하고 가장 잘 맞는 후보를 고른다.
        통과(ok=True)한 줄만 원본에서 지우고 편집 가능한 텍스트로 바꾼다. 나머지는 원본 픽셀을 그대로 둔다."""
        best = None
        styled = {}
        for ai, cand in enumerate(ln.get("alts") or [ln]):
            cand_texts = []
            for t0 in cand.get("texts") or [cand["text"]]:
                for vv in _variants(t0):
                    if vv not in cand_texts:
                        cand_texts.append(vv)
            for vi, txt in enumerate(cand_texts):
                c = dict(cand)
                c.pop("alts", None)
                c["text"] = txt
                if cand is not ln:
                    if ai not in styled:
                        self._style_of(img, c)
                        styled[ai] = (c["bg"], c["fg"], c["bold"])
                    c["bg"], c["fg"], c["bold"] = styled[ai]
                self._verify_one(img, c)
                # 같은 점수면 OCR 원문(첫 후보)을 우선
                key = (c["ok"], round(c.get("score", -9), 3) - 0.01 * (vi > 0) - 0.002 * ai)
                if best is None or key > best[0]:
                    best = (key, c)
        c = best[1]
        ln.clear()
        ln.update(c)

    def _verify_one(self, img, ln):
        H, W = img.shape[:2]
        x, y, w, h = ln["x"], ln["y"], ln["w"], ln["h"]
        bg, fg = np.asarray(ln["bg"], float), np.asarray(ln["fg"], float)
        contrast = _dist(bg, fg)
        ln.update(ok=False, why="", f1=0.0, px=None, spc=0.0, score=-9)
        if contrast < 40:
            ln["why"] = "대비 부족"
            return
        if ln["conf"] < MIN_CONF:
            ln["why"] = "OCR 신뢰도 낮음"
            ln["score"] = -5 + ln["conf"] / 100.0
            return
        ring = _ring(img, x, y, w, h, max(2, int(h * 0.18)))
        if float(np.mean(np.abs(ring.astype(np.int16) - bg).max(1) <= 28)) < MIN_RING_BG:
            ln["why"] = "배경이 단색 아님"
            return
        x1, y1, x2, y2 = max(0, x - 2), max(0, y - 2), min(W, x + w + 2), min(H, y + h + 2)
        crop = img[y1:y2, x1:x2]
        dbg = np.abs(crop.astype(np.int16) - bg).max(2)
        sd = _seg_dist(crop, bg, fg)
        # 캡처 보드의 색 번짐(크로마 서브샘플링)을 감안해 여유 있게 판단
        foreign = float(np.mean((sd > 70) & (dbg > 40)))
        if foreign > MAX_FOREIGN:
            ln["why"] = "글자 외 그림 포함"
            return
        # 잉크는 OCR 상자보다 넓게 보되(잘린 획 포함), 상자 중심부에 걸친 덩어리만 이 줄의 글자로 본다
        p = max(3, int(0.35 * h))
        x1, y1, x2, y2 = max(0, x - p), max(0, y - p), min(W, x + w + p), min(H, y + h + p)
        crop = img[y1:y2, x1:x2]
        dbg = np.abs(crop.astype(np.int16) - bg).max(2)
        sd = _seg_dist(crop, bg, fg)
        ink0 = ((dbg > 0.45 * contrast) & (sd <= 70)).astype(np.uint8)
        n, lab, st, _ = cv2.connectedComponentsWithStats(ink0, connectivity=8)
        cy0, cy1 = y + 0.25 * h - y1, y + 0.75 * h - y1
        cx0, cx1 = x - x1, x + w - x1
        ink = np.zeros(ink0.shape, bool)
        for i in range(1, n):
            bx, by, bw, bh, _a = st[i]
            if by + bh >= cy0 and by <= cy1 and bx + bw >= cx0 and bx <= cx1 and bh <= 2.2 * h:
                ink |= lab == i
        ys, xs = np.nonzero(ink)
        if len(xs) < 15:
            ln["why"] = "글자 픽셀 부족"
            return
        ox1, ox2, oy1, oy2 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
        om = ink[oy1:oy2, ox1:ox2]
        ow, oh = int(ox2 - ox1), int(oy2 - oy1)
        ln["ink"] = (x1 + ox1, y1 + oy1, ow, oh)
        text = ln["text"]
        if not self.meter.ok:   # 글꼴 파일이 없으면 모양 검증 불가 -> 원본 유지(안전)
            ln["why"] = "검증용 글꼴 없음"
            return
        best = None
        ker = np.ones((3, 3), np.uint8)
        nd = 1
        omd = cv2.dilate(om.astype(np.uint8), ker, iterations=nd) > 0
        for bold in (False, True):
            r0 = self.meter.ink(text, bold)
            if r0 is None:
                continue
            k = oh / float(r0["h"])                      # 100px -> 원본 크기 배율
            spc = (ow / k - r0["w"]) / max(1, len(text) - 1) if len(text) > 1 else 0.0
            spc = float(np.clip(spc, -10, 70))
            r = self.meter.ink(text, bold, spc) if abs(spc) >= 0.5 else r0
            if r is None:
                continue
            rm = cv2.resize(r["mask"].astype(np.uint8) * 255, (ow, oh), interpolation=cv2.INTER_AREA) > 100
            if rm.sum() < 5:
                continue
            rmd = cv2.dilate(rm.astype(np.uint8), ker, iterations=nd) > 0
            hit_r = rm & omd
            hit_o = om & rmd
            prec = float(hit_r.sum()) / rm.sum()
            rec = float(hit_o.sum()) / max(1, om.sum())
            f1 = 2 * prec * rec / max(1e-6, prec + rec)
            # 글자별(세로 띠) 일치도의 최저값: 한 글자만 틀려도 잡아낸다
            nch = max(1, len(text.replace(" ", "")))
            nb = max(1, min(nch, ow // max(3, int(0.45 * oh))))
            worst = 1.0
            for bi in range(nb):
                xa, xb = bi * ow // nb, (bi + 1) * ow // nb
                rs, os_ = rm[:, xa:xb].sum(), om[:, xa:xb].sum()
                if rs + os_ < 0.02 * oh * (xb - xa):
                    continue
                pp = float(hit_r[:, xa:xb].sum()) / max(1, rs)
                rr = float(hit_o[:, xa:xb].sum()) / max(1, os_)
                worst = min(worst, 2 * pp * rr / max(1e-6, pp + rr))
            dens = max(1e-3, float(om.mean())) / max(1e-3, float(rm.mean()))
            score = 0.6 * f1 + 0.4 * worst - 0.3 * abs(math.log(dens))
            score += 0.04 if bool(bold) == bool(ln.get("bold")) else 0.0   # 획 두께 분석 결과 우선
            # 글자 폭/높이 비율이 크게 다르면(글자 수 오인식) 감점
            aspect = (r["w"] / float(r["h"])) / (ow / float(oh))
            if spc <= -9.5 or spc >= 69.5 or not (0.8 <= aspect <= 1.25):
                score -= 0.3
            if best is None or score > best[0]:
                best = (score, f1, worst, bold, k, spc, r)
        if best is None:
            ln["why"] = "다시 그리기 실패"
            return
        score, f1, worst, bold, k, spc, r = best
        px = 100.0 * k
        ln.update(f1=f1, worst=worst, score=score, bold=bold, px=px, spc=spc * k,
                  cy=y1 + (oy1 + oy2) / 2.0, cy_off=r["cy_off"] * k, line_px=r["line"] * k, lx=r["lx"] * k)
        # 아주 작은 글자(화면 캡처 속 UI 글자 등)는 모양 비교가 부정확하므로 더 엄격하게
        if oh < 11:
            ln["why"] = "글자가 너무 작음"
            return
        strict = oh < 15
        need_f1 = max(MIN_SHAPE_F1, 0.9) if strict else MIN_SHAPE_F1
        need_ch = max(MIN_CHAR_F1, 0.75) if strict else MIN_CHAR_F1
        if f1 < need_f1 or worst < need_ch or score < need_f1 - 0.1:
            ln["why"] = "모양 불일치(오인식 의심)"
            return
        ln["ok"] = True

    @staticmethod
    def _neighbor_check(lines):
        """주변 줄 대부분이 '원본 유지'인 곳(화면 캡처·그림 속 작은 글자 영역)의 줄은 통과했어도 원본 유지로 돌린다."""
        flags = [ln["ok"] for ln in lines]
        for i, a in enumerate(lines):
            if not flags[i]:
                continue
            r = 2.5 * a["h"]
            nb = []
            for j, b in enumerate(lines):
                if i == j:
                    continue
                dx = max(0, max(a["x"], b["x"]) - min(a["x"] + a["w"], b["x"] + b["w"]))
                dy = max(0, max(a["y"], b["y"]) - min(a["y"] + a["h"], b["y"] + b["h"]))
                if dx <= r and dy <= r:
                    nb.append(flags[j])
            if len(nb) >= 3 and nb.count(False) >= 0.6 * len(nb):
                a["ok"] = False
                a["why"] = "주변이 그림·화면 캡처 영역"

    @staticmethod
    def _erase(img, lines, protect=()):
        """검증 통과한 줄의 글자 픽셀만 배경색으로 칠한다(번짐 없음).
        표 선·테두리 같은 긴 직선과, 원본으로 남겨 둘 다른 줄의 글자는 보존한다."""
        out = img.copy()
        H, W = img.shape[:2]
        guard = np.zeros((H, W), np.uint8)
        for q in protect:
            gx, gy, gw, gh = q["x"], q["y"], q["w"], q["h"]
            e = max(2, int(0.25 * gh))
            guard[max(0, gy - e):gy + gh + e, max(0, gx - 1):gx + gw + 1] = 1
            if q.get("ink"):
                gx, gy, gw, gh = q["ink"]
                guard[max(0, gy - 1):gy + gh + 1, max(0, gx - 1):gx + gw + 1] = 1
        for ln in lines:
            ix, iy, iw, ih = ln["ink"]
            h = ih
            p = max(2, int(0.15 * h))
            # 검증 잉크 상자 + OCR 상자(아래로 떨어진 'ㅡ' 같은 획 포함)
            ux1, uy1 = min(ix, ln["x"]), min(iy, ln["y"])
            ux2, uy2 = max(ix + iw, ln["x"] + ln["w"]), max(iy + ih, ln["y"] + ln["h"])
            x1, y1 = max(0, ux1 - p), max(0, uy1 - p)
            x2, y2 = min(W, ux2 + p), min(H, uy2 + p)
            c = out[y1:y2, x1:x2]
            if c.size == 0:
                continue
            bg = np.asarray(ln["bg"], float)
            dbg = np.abs(c.astype(np.int16) - bg).max(2)
            sd = _seg_dist(c, bg, ln["fg"])
            m = ((dbg > 10) & (sd <= 72)).astype(np.uint8)
            L = max(25, int(1.6 * h))
            keep = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((1, L), np.uint8)) | \
                cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((L, 1), np.uint8))
            # 진짜 선(표 선·테두리)은 글자 영역 밖으로 이어지거나 아주 길다. 글자 획('ㅡ'가 이어진 것)은 지운다.
            if keep.any():
                kn, klab, kst, _ = cv2.connectedComponentsWithStats(keep, connectivity=8)
                kh, kw = keep.shape
                for kk in range(1, kn):
                    bx, by, bw, bh, _a = kst[kk]
                    # 지우는 영역의 가장자리까지 이어지면 바깥으로 계속되는 선으로 본다
                    outside = bx <= 0 or by <= 0 or bx + bw >= kw or by + bh >= kh
                    if not outside and max(bw, bh) < 3.5 * h:
                        keep[klab == kk] = 0
            # 이 줄의 글자 덩어리(가운데 띠에 걸친 것)는 옆 줄 보호 영역과 겹쳐도 지운다(아랫부분만 남는 문제 방지)
            own = np.zeros_like(m)
            mk = m & (1 - keep)
            cn, clab, cst, _ = cv2.connectedComponentsWithStats(mk, connectivity=8)
            ba, bb = iy + 0.2 * ih - y1, iy + 0.8 * ih - y1
            for ci in range(1, cn):
                bx, by, bw, bh, _a = cst[ci]
                if by <= bb and by + bh >= ba and bx + bw >= ix - x1 and bx <= ix + iw - x1 and bh <= 1.3 * ih:
                    own[clab == ci] = 1
            own = cv2.dilate(own, np.ones((3, 3), np.uint8))
            m = cv2.dilate(m & (1 - keep), np.ones((3, 3), np.uint8)) & (sd <= 90).astype(np.uint8) & (1 - keep)
            m &= (1 - guard[y1:y2, x1:x2]) | own
            c[m.astype(bool)] = np.clip(bg, 0, 255).astype(np.uint8)
        return out

    # ---------- 색 영역 분할 -> 도형 후보
    def _segment(self, clean):
        h, w = clean.shape[:2]
        lap = cv2.Laplacian(cv2.cvtColor(clean, cv2.COLOR_BGR2GRAY), cv2.CV_16S)
        noisy = float(np.median(np.abs(lap))) > 1.0
        den = cv2.medianBlur(clean, 3) if noisy else clean
        mask = np.zeros((h + 2, w + 2), np.uint8)
        tol = (5, 5, 5) if noisy else (4, 4, 4)
        flags = 4 | cv2.FLOODFILL_MASK_ONLY | (255 << 8)
        step = 7
        shapes = []
        for y in range(3, h - 3, step):
            for x in range(3, w - 3, step):
                if mask[y + 1, x + 1]:
                    continue
                prev = mask.copy()
                _, _, _, rect = cv2.floodFill(den, mask, (x, y), 0, tol, tol, flags)
                rx, ry, rw, rh = rect
                if rw < 8 or rh < 8 or min(rw, rh) <= 5:
                    continue
                sub = (mask[ry + 1:ry + rh + 1, rx + 1:rx + rw + 1] !=
                       prev[ry + 1:ry + rh + 1, rx + 1:rx + rw + 1]).astype(np.uint8)
                area = int(sub.sum())
                if area < 250:
                    continue
                cnts, _ = cv2.findContours(sub, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                if not cnts:
                    continue
                c = max(cnts, key=cv2.contourArea)
                fill = np.zeros_like(sub)
                cv2.drawContours(fill, [c], -1, 1, -1)
                farea = int(fill.sum())
                extent = farea / float(rw * rh)
                solidity = area / float(max(1, farea))
                if extent < 0.74 or solidity < 0.35:
                    continue  # 윤곽선(링)·불규칙 형태는 도형으로 보지 않음
                pix = clean[ry:ry + rh, rx:rx + rw][sub.astype(bool)]
                mean = pix.mean(0)
                std = float(pix.std(0).max())
                fillspec = ("solid", mean)
                if std > 9:
                    fillspec = self._gradient_of(clean[ry:ry + rh, rx:rx + rw], fill.astype(bool))
                    if fillspec is None:
                        continue
                missing = rw * rh - farea
                if abs(extent - 0.785) < 0.04:
                    kind, r = "oval", 0
                elif missing / float(rw * rh) < 0.004:
                    kind, r = "rect", 0
                else:
                    r = min(math.sqrt(missing / (4 - math.pi)), min(rw, rh) / 2.0)
                    kind = "round" if r >= 3 else "rect"
                shapes.append(dict(kind=kind, x=rx, y=ry, w=rw, h=rh, r=r, fill=fillspec,
                                   border=None, mask=fill))
                if len(shapes) > 400:
                    return shapes
        return shapes

    @staticmethod
    def _gradient_of(crop, region):
        """가로/세로 선형 그라데이션이면 양 끝색을 반환, 아니면 None."""
        h, w = region.shape
        best = None
        for axis, n in ((1, w), (0, h)):
            k = max(2, int(n * 0.12))
            if axis == 1:
                a, b = region[:, :k], region[:, -k:]
                ca, cb = crop[:, :k][a], crop[:, -k:][b]
            else:
                a, b = region[:k, :], region[-k:, :]
                ca, cb = crop[:k, :][a], crop[-k:, :][b]
            if len(ca) < 8 or len(cb) < 8:
                continue
            c1, c2 = ca.mean(0), cb.mean(0)
            d = _dist(c1, c2)
            if best is None or d > best[0]:
                best = (d, axis, c1, c2)
        if best is None or best[0] < 25:
            return None
        return ("grad", best[2], best[3], "h" if best[1] == 1 else "v")

    def _finish_shapes(self, clean, shapes, bgcolor):
        out = []
        H, W = clean.shape[:2]
        for s in shapes:
            if s["fill"][0] == "solid" and _dist(s["fill"][1], bgcolor) < 6 and \
                    s["w"] * s["h"] > 0.4 * W * H:
                continue  # 페이지 배경과 같은 큰 면은 슬라이드 배경으로 대체
            s["border"] = self._border_of(clean, s, bgcolor)
            out.append(s)
        out.sort(key=lambda a: (-(a["w"] * a["h"]), a["y"], a["x"]))
        return out

    def _border_of(self, clean, s, parent_hint):
        H, W = clean.shape[:2]
        x, y, w, h = s["x"], s["y"], s["w"], s["h"]
        if s["fill"][0] != "solid" or min(w, h) < 20:
            return None

        def sides(o):
            cols = []
            xa, xb = x + int(w * .2), x + int(w * .8)
            ya, yb = y + int(h * .2), y + int(h * .8)
            if y - o >= 0:
                cols.append(np.median(clean[y - o, xa:xb], axis=0))
            if y + h - 1 + o < H:
                cols.append(np.median(clean[y + h - 1 + o, xa:xb], axis=0))
            if x - o >= 0:
                cols.append(np.median(clean[ya:yb, x - o], axis=0))
            if x + w - 1 + o < W:
                cols.append(np.median(clean[ya:yb, x + w - 1 + o], axis=0))
            return cols

        c1 = sides(1)
        if len(c1) < 3:
            return None
        cand = np.median(np.array(c1), axis=0)
        if max(_dist(c, cand) for c in c1) > 30:
            return None
        fill = s["fill"][1]
        if _dist(cand, fill) < 35:
            return None
        t = 1
        for o in range(2, 7):
            cs = sides(o)
            if cs and all(_dist(c, cand) <= 30 for c in cs):
                t = o
            else:
                break
        outer = sides(t + 3)
        if not outer:
            return None
        parent = np.median(np.array(outer), axis=0)
        if _dist(parent, cand) < 20:
            return None
        if _dist((np.asarray(fill, float) + parent) / 2, cand) < 30 and t == 1:
            return None
        return (cand, t)

    # ---------- 캔버스(도형만으로 다시 그린 그림)
    @staticmethod
    def _shape_mask(s, grow=0):
        w, h, r = s["w"] + 2 * grow, s["h"] + 2 * grow, s["r"] + grow
        m = np.zeros((h, w), np.uint8)
        if s["kind"] == "oval":
            cv2.ellipse(m, (w // 2, h // 2), (max(1, w // 2), max(1, h // 2)), 0, 0, 360, 255, -1)
        elif s["kind"] == "round":
            r = int(max(1, min(r, min(w, h) // 2)))
            cv2.rectangle(m, (r, 0), (w - 1 - r, h - 1), 255, -1)
            cv2.rectangle(m, (0, r), (w - 1, h - 1 - r), 255, -1)
            for cx, cy in ((r, r), (w - 1 - r, r), (r, h - 1 - r), (w - 1 - r, h - 1 - r)):
                cv2.circle(m, (cx, cy), r, 255, -1)
        else:
            m[:] = 255
        return m

    def _render(self, shapes, W, H, bgcolor):
        canvas = np.empty((H, W, 3), np.uint8)
        canvas[:] = np.clip(bgcolor, 0, 255).astype(np.uint8)

        def paint(x, y, m, colorimg):
            h, w = m.shape
            x0, y0 = max(0, x), max(0, y)
            x1, y1 = min(W, x + w), min(H, y + h)
            if x1 <= x0 or y1 <= y0:
                return
            mm = m[y0 - y:y1 - y, x0 - x:x1 - x] > 0
            region = canvas[y0:y1, x0:x1]
            if colorimg.ndim == 1:
                region[mm] = colorimg
            else:
                region[mm] = colorimg[y0 - y:y1 - y, x0 - x:x1 - x][mm]

        for s in shapes:
            if s.get("border"):
                col, t = s["border"]
                g = int(t)
                paint(s["x"] - g, s["y"] - g, self._shape_mask(s, g), np.clip(col, 0, 255).astype(np.uint8))
            m = self._shape_mask(s)
            if s["fill"][0] == "solid":
                paint(s["x"], s["y"], m, np.clip(s["fill"][1], 0, 255).astype(np.uint8))
            else:
                _, c1, c2, axis = s["fill"]
                n = s["w"] if axis == "h" else s["h"]
                t = np.linspace(0, 1, max(2, n))[:, None]
                ramp = (np.asarray(c1) * (1 - t) + np.asarray(c2) * t).astype(np.uint8)
                if axis == "h":
                    img = np.broadcast_to(ramp[None, :, :], (s["h"], s["w"], 3))
                else:
                    img = np.broadcast_to(ramp[:, None, :], (s["h"], s["w"], 3))
                paint(s["x"], s["y"], m, np.ascontiguousarray(img))
        return canvas

    # ---------- 선 / 표 격자
    @staticmethod
    def _thin_line_mask(img, off=5, thr=25):
        """주변보다 확 다른 '가는 띠'(두께 ~4px 이하) 픽셀만 추출. 면의 경계는 제외된다."""
        im = img.astype(np.int16)
        H, W = img.shape[:2]
        hm = np.zeros((H, W), np.uint8)
        vm = np.zeros((H, W), np.uint8)
        a = np.abs(im[off:-off] - im[:-2 * off]).max(2) > thr      # 위쪽과 다름
        b = np.abs(im[off:-off] - im[2 * off:]).max(2) > thr       # 아래쪽과 다름
        hm[off:-off] = (a & b)
        a = np.abs(im[:, off:-off] - im[:, :-2 * off]).max(2) > thr
        b = np.abs(im[:, off:-off] - im[:, 2 * off:]).max(2) > thr
        vm[:, off:-off] = (a & b)
        return hm, vm

    @staticmethod
    def _line_components(res, horizontal):
        k = np.ones((1, 30), np.uint8) if horizontal else np.ones((30, 1), np.uint8)
        op = cv2.morphologyEx(res, cv2.MORPH_OPEN, k)
        n, lab, st, _ = cv2.connectedComponentsWithStats(op, connectivity=8)
        comps = []
        for i in range(1, n):
            x, y, w, h, a = st[i]
            thick = h if horizontal else w
            length = w if horizontal else h
            if thick <= 7 and length >= 30:
                comps.append(dict(x=int(x), y=int(y), w=int(w), h=int(h), label=i, lab=lab))
        return comps

    def _find_tables(self, hc, vc, W, H, hm, vm):
        if not hc or not vc:
            return []
        m = np.zeros((H, W), np.uint8)
        for c in hc:
            m[c["y"]:c["y"] + c["h"], c["x"]:c["x"] + c["w"]] = 1
        for c in vc:
            m[c["y"]:c["y"] + c["h"], c["x"]:c["x"] + c["w"]] = 1
        n, _, st, _ = cv2.connectedComponentsWithStats(cv2.dilate(m, np.ones((9, 9), np.uint8)), connectivity=8)
        tables = []
        for i in range(1, n):
            bx, by, bw, bh, _a = st[i]
            if bw < W * .12 or bh < H * .05:
                continue
            hs = [c for c in hc if c["x"] >= bx - 6 and c["x"] + c["w"] <= bx + bw + 6 and
                  c["y"] >= by - 6 and c["y"] + c["h"] <= by + bh + 6]
            vs = [c for c in vc if c["x"] >= bx - 6 and c["x"] + c["w"] <= bx + bw + 6 and
                  c["y"] >= by - 6 and c["y"] + c["h"] <= by + bh + 6]
            ys = _cluster([c["y"] + c["h"] // 2 for c in hs], 5)
            xs = _cluster([c["x"] + c["w"] // 2 for c in vs], 5)
            if len(ys) < 3 or len(xs) < 3 or len(ys) > 41 or len(xs) > 21:
                continue
            if min(np.diff(xs)) < 14 or min(np.diff(ys)) < 12:
                continue
            # 셀 단위 검증: 모든 셀의 네 변이 실제 선으로 둘러싸여 있어야 표로 인정
            good = total = 0
            for r in range(len(ys) - 1):
                for c in range(len(xs) - 1):
                    x0, x1, y0, y1 = xs[c], xs[c + 1], ys[r], ys[r + 1]
                    total += 1
                    top = hm[max(0, y0 - 3):y0 + 4, x0:x1 + 1].max(0).mean()
                    bot = hm[max(0, y1 - 3):y1 + 4, x0:x1 + 1].max(0).mean()
                    lef = vm[y0:y1 + 1, max(0, x0 - 3):x0 + 4].max(1).mean()
                    rig = vm[y0:y1 + 1, max(0, x1 - 3):x1 + 4].max(1).mean()
                    if min(top, bot, lef, rig) >= 0.6:
                        good += 1
            if total == 0 or good < 0.9 * total:
                continue
            thick = float(np.median([c["h"] for c in hs] + [c["w"] for c in vs]))
            tables.append(dict(x=xs[0], y=ys[0], w=xs[-1] - xs[0], h=ys[-1] - ys[0], xs=xs, ys=ys,
                               thick=thick, hs=hs, vs=vs))
        tables.sort(key=lambda a: -a["w"] * a["h"])
        kept = []
        for t in tables:
            if any(t["x"] >= q["x"] - 4 and t["y"] >= q["y"] - 4 and t["x"] + t["w"] <= q["x"] + q["w"] + 4 and
                   t["y"] + t["h"] <= q["y"] + q["h"] + 4 for q in kept):
                continue
            kept.append(t)
        return kept[:6]

    # ---------- 나머지(차트·아이콘 등) -> 투명 PNG 조각
    def _pieces(self, clean, res, tag):
        H, W = res.shape
        pieces = []
        for dil in (7, 25, 61):
            d = cv2.dilate(res, np.ones((dil, dil), np.uint8))
            n, lab, st, _ = cv2.connectedComponentsWithStats(d, connectivity=8)
            if n - 1 <= 60 or dil == 61:
                break
        for i in range(1, n):
            x, y, w, h, _a = st[i]
            sub = res[y:y + h, x:x + w] & (lab[y:y + h, x:x + w] == i).astype(np.uint8)
            if int(sub.sum()) < 40:
                continue
            alpha = cv2.dilate(sub, np.ones((3, 3), np.uint8)) * 255
            rgba = np.dstack([clean[y:y + h, x:x + w], alpha]).astype(np.uint8)
            path = self.workdir / f"_piece_{tag}_{len(pieces):03d}.png"
            cv2.imencode(".png", rgba)[1].tofile(str(path))
            pieces.append(dict(x=int(x), y=int(y), w=int(w), h=int(h), path=path, sub=sub))
        return pieces

    # ---------- PPT 출력 도우미
    @staticmethod
    def _set_run_font(run, pt, color, bold, spc_pt=0.0, invisible=False):
        f = run.font
        f.name = FONT_NAME
        f.size = Pt(pt)
        f.bold = bool(bold)
        f.color.rgb = _rgb(color)
        rPr = run._r.get_or_add_rPr()
        rPr.set("lang", "ko-KR")
        rPr.set("altLang", "en-US")
        rPr.set("kern", "0")
        if abs(spc_pt) >= 0.05:
            rPr.set("spc", str(int(round(spc_pt * 100))))   # 문자 간격(1/100 pt)
        if invisible:
            clr = rPr.find(qn("a:solidFill")).find(qn("a:srgbClr"))
            etree.SubElement(clr, qn("a:alpha")).set("val", "0")
        for tag in ("a:ea", "a:cs"):
            el = rPr.find(qn(tag))
            if el is None:
                el = etree.SubElement(rPr, qn(tag))
            el.set("typeface", FONT_NAME)

    @staticmethod
    def _cell_border(cell, color, w_emu):
        tcPr = cell._tc.get_or_add_tcPr()
        for tag in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):
            for old in tcPr.findall(qn(tag)):
                tcPr.remove(old)
        for idx, tag in enumerate(("a:lnL", "a:lnR", "a:lnT", "a:lnB")):
            ln = etree.Element(qn(tag))
            if color is None:
                ln.set("w", "0")
                etree.SubElement(ln, qn("a:noFill"))
            else:
                ln.set("w", str(int(w_emu)))
                ln.set("cap", "flat")
                ln.set("cmpd", "sng")
                ln.set("algn", "ctr")
                sf = etree.SubElement(ln, qn("a:solidFill"))
                clr = etree.SubElement(sf, qn("a:srgbClr"))
                c = _rgb(color)
                clr.set("val", "%02X%02X%02X" % (c[0], c[1], c[2]))
                pd = etree.SubElement(ln, qn("a:prstDash"))
                pd.set("val", "solid")
            tcPr.insert(idx, ln)

    def _fit_pt(self, text, width_px, px_per_pt_inv, hmed):
        w100 = max(1.0, self.meter.width100(text))
        px = 100.0 * width_px / w100
        has_hangul = any(0xAC00 <= ord(c) <= 0xD7A3 for c in text)
        hi = (1.18 if has_hangul else 1.55) * hmed   # OCR 박스가 글자보다 넓게 잡힌 경우의 과대 확대 방지
        px = max(0.55 * hmed, min(hi, px))
        return px * px_per_pt_inv

    # ---------- 메인: 슬라이드 1장 구성
    # ---------- 도형·선·아이콘 -> 네이티브 객체
    def _vector_layer(self, clean, img, bgcolor, rejected, tables, tag):
        """도형(사각형/둥근사각형/원) + 선(커넥터) + 아이콘·차트 조각(프리폼, 안 되면 PNG).
        배경 유지 모드: 원본과 거의 같게 다시 그려지는 객체만 만들고, 도형에 가려지는 세부(글자·가는 선 등)는
        원본 픽셀 그대로 도형 위에 다시 올려 화면이 원본과 같게 유지되도록 한다."""
        H, W = clean.shape[:2]
        keep_bg = self.keep_bg
        shapes, hlines, vlines, pieces = [], [], [], []
        if self.use_vectors:
            raw = self._segment(clean)
            shapes = self._finish_shapes(clean, raw, bgcolor)

            def inside(s, t):
                return (s["x"] >= t["x"] - 6 and s["y"] >= t["y"] - 6 and
                        s["x"] + s["w"] <= t["x"] + t["w"] + 6 and s["y"] + s["h"] <= t["y"] + t["h"] + 6)
            shapes = [s for s in shapes if not any(inside(s, t) for t in tables)]
            shapes = [s for s in shapes if not self._in_photo(clean, s)]   # 사진 속을 억지로 도형화하지 않음
            if keep_bg:
                shapes = [s for s in shapes if self._shape_faithful(clean, s)]
        canvas = self._render(shapes, W, H, bgcolor)
        res = (np.abs(clean.astype(np.int16) - canvas.astype(np.int16)).max(2) > 26).astype(np.uint8)
        for t in tables:
            res[max(0, t["y"] - 6):t["y"] + t["h"] + 7, max(0, t["x"] - 6):t["x"] + t["w"] + 7] = 0
        # 도형이 덮는 영역(배경 유지 모드에서 이 안의 세부는 반드시 도형 위에 다시 그려야 함)
        covered = np.zeros((H, W), np.uint8)
        for s_ in shapes:
            g = int(s_["border"][1]) + 1 if s_.get("border") else 1
            m = self._shape_mask(s_, g)
            x0, y0 = s_["x"] - g, s_["y"] - g
            xa, ya, xb, yb = max(0, x0), max(0, y0), min(W, x0 + m.shape[1]), min(H, y0 + m.shape[0])
            if xb > xa and yb > ya:
                covered[ya:yb, xa:xb] |= (m[ya - y0:yb - y0, xa - x0:xb - x0] > 0).astype(np.uint8)
        # 원본으로 남길 글자 줄: 픽셀 조각으로 쪼개지 않고 '원본 그대로의 사각 그림'으로 붙인다(획 끊김 방지)
        keep_rects = []
        for ln in rejected:
            if ln.get("why") in ("글자 외 그림 포함", "배경이 단색 아님", "주변이 그림·화면 캡처 영역"):
                continue        # 아이콘·그림을 글자로 잘못 읽은 경우: 글자 사각형으로 묶지 않고 도형/아이콘으로 처리
            pp, pv = max(2, int(0.2 * ln["h"])), max(3, int(0.4 * ln["h"]))
            kx1, ky1 = max(0, ln["x"] - pp), max(0, ln["y"] - pv)
            kx2, ky2 = min(W, ln["x"] + ln["w"] + pp), min(H, ln["y"] + ln["h"] + pv)
            if kx2 - kx1 < 3 or ky2 - ky1 < 3:
                continue
            if not res[ky1:ky2, kx1:kx2].any():
                continue        # 도형으로 이미 똑같이 표현되는 영역
            res[ky1:ky2, kx1:kx2] = 0
            if keep_bg and not covered[ky1:ky2, kx1:kx2].any():
                continue        # 배경 그림에 그대로 보이므로 따로 올릴 필요 없음
            keep_rects.append((kx1, ky1, kx2, ky2))
        raw_res = res.copy()
        explained = np.zeros((H, W), np.uint8)   # 네이티브 객체로 다시 그려진 픽셀
        hc = self._line_components(res, True)
        vc = self._line_components(res, False)
        # 선 -> 커넥터
        for comps, horiz in ((hc, True), (vc, False)):
            for c in comps:
                lab = c["lab"]
                ys_, xs_ = np.nonzero(lab[c["y"]:c["y"] + c["h"], c["x"]:c["x"] + c["w"]] == c["label"])
                if len(ys_) == 0:
                    continue
                px_ = clean[c["y"] + ys_, c["x"] + xs_]
                col = np.median(px_, axis=0)
                if keep_bg and float(np.mean(np.abs(px_.astype(np.int16) - col).max(1) > 40)) > 0.15:
                    continue    # 색이 고르지 않은 선(그라데이션·점선 등)은 원본 그대로
                t = c["h"] if horiz else c["w"]
                if horiz:
                    seg = (c["x"], c["y"] + t / 2.0, c["x"] + c["w"], c["y"] + t / 2.0)
                else:
                    seg = (c["x"] + t / 2.0, c["y"], c["x"] + t / 2.0, c["y"] + c["h"])
                (hlines if horiz else vlines).append(dict(seg=seg, t=t, color=col))
                ya, yb = max(0, c["y"] - 1), c["y"] + c["h"] + 1
                xa, xb = max(0, c["x"] - 1), c["x"] + c["w"] + 1
                explained[ya:yb, xa:xb] |= res[ya:yb, xa:xb]
                res[ya:yb, xa:xb] = 0
        res = cv2.morphologyEx(res, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        for p0 in self._pieces(clean, res, tag):
            # 묶음 전체가 안 되면 서로 떨어진 덩어리별로 다시 시도(아이콘 여러 개가 한 조각으로 묶인 경우)
            parts = [p0]
            if self._vectorize(clean, p0) is None:
                parts = self._split_piece(clean, p0, tag) or [p0]
            for p in parts:
                ff = p.get("ff") or self._vectorize(clean, p)
                if ff is not None:
                    p["ff"] = ff
                    explained[p["y"]:p["y"] + p["h"], p["x"]:p["x"] + p["w"]] |= p["sub"]
                    pieces.append(p)
                elif not keep_bg or covered[p["y"]:p["y"] + p["h"], p["x"]:p["x"] + p["w"]].any():
                    if "path" not in p:
                        p["path"] = self._save_piece(clean, p, tag)
                    pieces.append(p)          # 사진·복잡한 그림: PNG 그대로
                    explained[p["y"]:p["y"] + p["h"], p["x"]:p["x"] + p["w"]] |= p["sub"]
        if keep_bg:
            # 도형에 가려지는데 아직 다시 그려지지 않은 세부(가는 선·점선·작은 점 등)는 원본 픽셀 그대로 올린다
            lost = raw_res & covered & (1 - cv2.dilate(explained, np.ones((3, 3), np.uint8)))
            for p in self._pieces(clean, lost, tag + "d"):
                pieces.append(p)
        for k, (kx1, ky1, kx2, ky2) in enumerate(keep_rects):
            path = self.workdir / f"_keep_{tag}_{k:03d}.png"
            cv2.imencode(".png", img[ky1:ky2, kx1:kx2])[1].tofile(str(path))
            pieces.append(dict(x=kx1, y=ky1, w=kx2 - kx1, h=ky2 - ky1, path=path))
        return shapes, hlines, vlines, pieces

    def _split_piece(self, clean, p, tag):
        """조각을 서로 떨어진 덩어리로 나눈다(덩어리가 1개뿐이면 None)"""
        sub = p["sub"]
        n, lab, st, _ = cv2.connectedComponentsWithStats(cv2.dilate(sub, np.ones((3, 3), np.uint8)), connectivity=8)
        if n <= 2 or n > 400:
            return None
        out = []
        for i in range(1, n):
            bx, by, bw, bh, _a = st[i]
            m = sub[by:by + bh, bx:bx + bw] & (lab[by:by + bh, bx:bx + bw] == i).astype(np.uint8)
            if int(m.sum()) < 12:
                continue
            out.append(dict(x=p["x"] + int(bx), y=p["y"] + int(by), w=int(bw), h=int(bh), sub=m))
        return out

    def _save_piece(self, clean, p, tag):
        self._pc = getattr(self, "_pc", 0) + 1
        alpha = cv2.dilate(p["sub"], np.ones((3, 3), np.uint8)) * 255
        rgba = np.dstack([clean[p["y"]:p["y"] + p["h"], p["x"]:p["x"] + p["w"]], alpha]).astype(np.uint8)
        path = self.workdir / f"_piece_{tag}_s{self._pc:04d}.png"
        cv2.imencode(".png", rgba)[1].tofile(str(path))
        return path

    def _in_photo(self, clean, s):
        """도형 바깥 테두리 띠가 얼룩덜룩하면(사진 속 둥근 물체 등) 도형으로 만들지 않는다"""
        ring = _ring(clean, s["x"], s["y"], s["w"], s["h"], 4)
        if len(ring) < 20:
            return False
        med = np.median(ring, axis=0)
        near = np.abs(ring.astype(np.int16) - med).max(1) <= 30
        if near.mean() >= 0.6:
            return False
        # 두세 가지 단색(다른 도형·배경과 맞닿은 경우)이면 사진이 아님
        q = (ring // 16).astype(np.int32)
        key = (q[:, 0] * 16 + q[:, 1]) * 16 + q[:, 2]
        cnt = np.sort(np.bincount(key))[::-1]
        return cnt[:3].sum() < 0.75 * len(ring)

    def _shape_faithful(self, clean, s):
        """배경 유지 모드: 도형을 올려도 화면이 원본과 같은지(채우기 색·영역이 원본과 일치하는지) 확인"""
        m = self._shape_mask(s) > 0
        crop = clean[s["y"]:s["y"] + s["h"], s["x"]:s["x"] + s["w"]]
        if crop.shape[:2] != m.shape:
            return False
        if s["fill"][0] == "solid":
            ref = np.asarray(s["fill"][1], float)
            d = np.abs(crop.astype(np.float32) - ref).max(2)
        else:
            _, c1, c2, axis = s["fill"]
            n = s["w"] if axis == "h" else s["h"]
            t = np.linspace(0, 1, max(2, n))[:, None]
            ramp = np.asarray(c1, float) * (1 - t) + np.asarray(c2, float) * t
            ref = ramp[None, :, :] if axis == "h" else ramp[:, None, :]
            d = np.abs(crop.astype(np.float32) - ref).max(2)
        dm = d[m]
        if len(dm) == 0:
            return False
        flat = dm[dm <= 26]          # 글자·아이콘 등 세부(위에 다시 올림)를 뺀 바탕 부분
        if len(flat) < 0.55 * len(dm):
            return False
        return float(np.median(flat)) <= (5 if s["fill"][0] == "solid" else 8)

    def _vectorize(self, clean, p):
        """조각이 단색 몇 개로 이루어진 아이콘·차트면 색별 윤곽선(구멍 포함) 프리폼으로 만든다.
        사진처럼 색이 섞여 있거나 다시 그린 결과가 원본과 다르면 None -> PNG로 둔다."""
        x, y, w, h, sub = p["x"], p["y"], p["w"], p["h"], p["sub"]
        crop = clean[y:y + h, x:x + w]
        mk = sub > 0
        pix = crop[mk].astype(np.int16)
        if len(pix) < 20:
            return None
        q = (pix // 12)
        key = (q[:, 0] * 32 + q[:, 1]) * 32 + q[:, 2]
        uk, cnt = np.unique(key, return_counts=True)
        palette = []
        covered = 0
        for kk in uk[np.argsort(-cnt)]:
            sel = key == kk
            col = np.median(pix[sel], axis=0)
            if all(_dist(col, c) > 30 for c in palette):
                palette.append(col)
                if len(palette) > 6:
                    return None
            covered += int(sel.sum())
            if covered >= 0.97 * len(pix):
                break
        pal = np.array(palette, np.int16)
        dd = np.abs(pix[:, None, :] - pal[None, :, :]).max(2)
        lab = dd.argmin(1)
        dmin = dd.min(1)
        if float(np.mean(dmin > 45)) > 0.04 or float(dmin.mean()) > 16:
            return None                 # 그라데이션·사진
        labimg = np.full((h, w), -1, np.int32)
        labimg[mk] = lab
        S = 4                           # 4배 확대해서 윤곽을 따면 반 픽셀 오차 없이 원래 크기에 맞는다
        polys = []
        recon = np.full((h, w), -1, np.int32)
        npts = 0
        order = np.argsort([-(labimg == i).sum() for i in range(len(pal))])
        for ci in order:
            mc = (labimg == ci).astype(np.uint8)
            if mc.sum() < 3:
                continue
            big = cv2.resize(mc, (w * S, h * S), interpolation=cv2.INTER_NEAREST)
            cnts, hier = cv2.findContours(big, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
            if hier is None:
                continue
            hier = hier[0]
            subs = []
            outers = [i for i in range(len(cnts)) if hier[i][3] < 0]
            outers.sort(key=lambda i: -abs(cv2.contourArea(cnts[i])))
            rmask = np.zeros((h * S, w * S), np.uint8)
            for oi in outers:
                if abs(cv2.contourArea(cnts[oi])) < 2 * S * S:
                    continue
                o = cv2.approxPolyDP(cnts[oi], 0.45 * S, True).reshape(-1, 2)
                if len(o) < 3:
                    continue
                subs.append((o, False))
                cv2.fillPoly(rmask, [o], 1)
                ch = hier[oi][2]
                while ch >= 0:          # 구멍(도넛·고리의 안쪽) - 무시하지 않고 그대로 뚫는다
                    if abs(cv2.contourArea(cnts[ch])) >= 1.0 * S * S:
                        hp = cv2.approxPolyDP(cnts[ch], 0.45 * S, True).reshape(-1, 2)
                        if len(hp) >= 3:
                            subs.append((hp, True))
                            cv2.fillPoly(rmask, [hp], 0)
                    ch = hier[ch][0]
            if not subs:
                continue
            small = cv2.resize(rmask, (w, h), interpolation=cv2.INTER_AREA) > 0
            recon[small] = ci
            npts += sum(len(a) for a, _ in subs)
            # 좌표: 확대 이미지의 픽셀 경계 -> 원래 좌표(슬라이드 px)
            conv = []
            for pts, hole in subs:
                f = pts.astype(np.float64)
                f = (f + 0.5) / S
                area = 0.5 * float(np.sum(f[:, 0] * np.roll(f[:, 1], -1) - np.roll(f[:, 0], -1) * f[:, 1]))
                # 바깥선과 구멍의 방향을 반대로: 채우기 규칙(nonzero/even-odd)에 상관없이 구멍이 뚫린다
                if (area > 0) == hole:
                    f = f[::-1]
                conv.append([(x + float(a), y + float(b)) for a, b in f])
            polys.append(dict(color=pal[ci].astype(float), paths=conv))
        if not polys or npts > 6000 or len(polys) > 8:
            return None
        # 검증: 다시 그린 결과가 원본 조각과 거의 같아야 함
        bad = (recon[mk] < 0) | (recon[mk] != labimg[mk])
        over = (recon >= 0) & ~cv2.dilate(sub, np.ones((3, 3), np.uint8)).astype(bool)
        if float(bad.mean()) > 0.06 or over.sum() > 0.06 * mk.sum() + 4:
            return None
        return polys

    def _write_freeform(self, slide, polys, sx, sy):
        n = 0
        for poly in polys:
            paths = [pth for pth in poly["paths"] if len(pth) >= 3]
            if not paths:
                continue
            x0, y0 = paths[0][0]
            fb = slide.shapes.build_freeform(x0, y0, scale=(sx, sy))
            fb.add_line_segments(paths[0][1:], close=True)
            for pth in paths[1:]:
                fb.move_to(*pth[0])
                fb.add_line_segments(pth[1:], close=True)
            sh = fb.convert_to_shape()
            sh.fill.solid()
            sh.fill.fore_color.rgb = _rgb(poly["color"])
            sh.line.fill.background()
            sh.shadow.inherit = False
            st_el = sh._element.find(qn("p:style"))
            if st_el is not None:
                sh._element.remove(st_el)
            sh.name = f"아이콘 조각 {n + 1}"
            n += 1
        return n

    def build(self, prs, slide, img, tag):
        H, W = img.shape[:2]
        sx = prs.slide_width / float(W)
        sy = prs.slide_height / float(H)
        pt_per_px = prs.slide_width / 12700.0 / W
        stats = dict(ocr_lines=0, text_boxes=0, tables=[], table_text_items=0, shapes=0, lines=0, pictures=0,
                     kept_original=0)

        all_lines = ocr_lines(img, self.tess_path) if self.ocr_enabled else []
        for ln in all_lines:
            self._style_of(img, ln)
        stats["ocr_lines"] = len(all_lines)

        if self.exact:
            return self._build_exact(prs, slide, img, tag, all_lines, sx, sy, pt_per_px, stats)

        for ln in all_lines:
            self._verify(img, ln)
        self._neighbor_check(all_lines)
        lines = [ln for ln in all_lines if ln["ok"]]
        rejected = [ln for ln in all_lines if not ln["ok"]]
        stats["kept_original"] = len(rejected)
        stats["kept_original_text"] = [f'{ln["text"]} ({ln["why"]})' for ln in rejected][:80]
        do_erase = self.inpaint or not self.keep_bg
        clean = self._erase(img, lines, rejected) if (lines and do_erase) else img.copy()

        small = cv2.resize(clean, (160, 90), interpolation=cv2.INTER_AREA).reshape(-1, 3)
        bgcolor = _mode_color(small)
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = _rgb(bgcolor)

        shapes, hlines, vlines, pieces, tables = [], [], [], [], []
        full_mode = not self.keep_bg
        # 표 격자는 도형 분할과 무관하게 '가는 선' 마스크에서 먼저 찾는다.
        if self.use_tables and lines and not self.keep_bg:
            thm, tvm = self._thin_line_mask(clean)
            tables = self._find_tables(self._line_components(thm, True), self._line_components(tvm, False),
                                       W, H, thm, tvm)

        if self.keep_bg:
            # 배경: 원본 캡처(검증된 글자만 지운 것). 그 위에 같은 위치·크기·색의 편집 가능한 객체를 겹쳐 올린다.
            p = self.workdir / f"_bg_{tag}.png"
            cv2.imencode(".png", clean)[1].tofile(str(p))
            slide.shapes.add_picture(str(p), 0, 0, width=prs.slide_width, height=prs.slide_height)
            self._tmp = getattr(self, "_tmp", []) + [p]
        if self.use_vectors or not self.keep_bg:
            shapes, hlines, vlines, pieces = self._vector_layer(clean, img, bgcolor, rejected, tables, tag)

        # ----- 쓰기: 도형 -> 조각 -> 선 -> 표 -> 글자 (아래에서 위 순서)
        for s in shapes:
            self._write_shape(slide, s, sx, sy)
        stats["shapes"] = len(shapes)
        n_pic = n_ff = 0
        for p in pieces:
            if p.get("ff"):
                n_ff += self._write_freeform(slide, p["ff"], sx, sy)
                continue
            slide.shapes.add_picture(str(p["path"]), int(p["x"] * sx), int(p["y"] * sy),
                                     width=int(p["w"] * sx), height=int(p["h"] * sy))
            self._tmp = getattr(self, "_tmp", []) + [p["path"]]
            n_pic += 1
        stats["pictures"] = n_pic
        stats["freeforms"] = n_ff
        for l in hlines + vlines:
            x1, y1, x2, y2 = l["seg"]
            cn = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy))
            cn.line.color.rgb = _rgb(l["color"])
            cn.line.width = Emu(int(max(6350, l["t"] * sx)))
            st_el = cn._element.find(qn("p:style"))
            if st_el is not None:
                cn._element.remove(st_el)
        stats["lines"] = len(hlines) + len(vlines)

        used = set()
        for t in tables:
            n_items = self._write_table(slide, t, lines, used, clean, bgcolor, sx, sy, pt_per_px, W, H)
            stats["table_text_items"] += n_items
            stats["tables"].append(dict(bounds=[t["x"], t["y"], t["w"], t["h"]], rows=len(t["ys"]) - 1,
                                        columns=len(t["xs"]) - 1))
        if self.keep_bg and not do_erase:
            lines = []     # 글자를 지우지 않은 배경 위에 텍스트를 겹치면 이중 글자가 되므로 만들지 않음
        rest = [ln for i, ln in enumerate(lines) if i not in used]
        # 줄마다 별도 텍스트 상자: 원본 위치·크기를 가장 정확히 재현 (여러 줄 합치기는 위치 오차를 키움)
        blocks = [[ln] for ln in sorted(rest, key=lambda a: (a["y"], a["x"]))]
        for b in blocks:
            self._write_block(slide, b, shapes if full_mode else [], sx, sy, pt_per_px)
        stats["text_boxes"] = len(blocks)
        return stats

    def _build_exact(self, prs, slide, img, tag, lines, sx, sy, pt_per_px, stats):
        p = self.workdir / f"_bg_{tag}.png"
        cv2.imencode(".png", img)[1].tofile(str(p))
        slide.shapes.add_picture(str(p), 0, 0, width=prs.slide_width, height=prs.slide_height)
        self._tmp = getattr(self, "_tmp", []) + [p]
        n = 0
        for ln in lines:
            if ln["conf"] < 40:
                continue
            px = ln["hmed"] / 0.78
            if self.meter.ok:
                w100 = self.meter.width100(ln["text"])
                px = float(np.clip(100.0 * ln["w"] / max(1.0, w100), 0.6 * px, 1.4 * px))
            tb = slide.shapes.add_textbox(int(ln["x"] * sx), int((ln["y"] + ln["h"] / 2.0 - px * 0.7) * sy),
                                          int(ln["w"] * 1.1 * sx + 1), int(px * 1.4 * sy))
            tf = tb.text_frame
            tf.word_wrap = False
            tf.auto_size = MSO_AUTO_SIZE.NONE
            tf.vertical_anchor = MSO_ANCHOR.MIDDLE
            tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
            r = tf.paragraphs[0].add_run()
            r.text = ln["text"]
            self._set_run_font(r, max(1.0, round(px * pt_per_px * 2) / 2.0), (0, 0, 0), False, invisible=True)
            n += 1
        stats["text_boxes"] = n
        return stats

    def cleanup(self):
        for p in getattr(self, "_tmp", []):
            try:
                Path(p).unlink()
            except OSError:
                pass
        self._tmp = []

    # ---------- 도형 쓰기
    def _write_shape(self, slide, s, sx, sy):
        x, y, w, h = s["x"], s["y"], s["w"], s["h"]
        bt = 0
        if s.get("border"):
            bt = s["border"][1]
        kind = {"rect": MSO_SHAPE.RECTANGLE, "round": MSO_SHAPE.ROUNDED_RECTANGLE, "oval": MSO_SHAPE.OVAL}[s["kind"]]
        g = bt / 2.0
        sh = slide.shapes.add_shape(kind, int((x - g) * sx), int((y - g) * sy),
                                    int((w + 2 * g) * sx), int((h + 2 * g) * sy))
        if s["kind"] == "round":
            sh.adjustments[0] = max(0.0, min(0.5, s["r"] / float(min(w, h))))
        f = s["fill"]
        if f[0] == "solid":
            sh.fill.solid()
            sh.fill.fore_color.rgb = _rgb(f[1])
        else:
            sh.fill.gradient()
            sh.fill.gradient_angle = 0 if f[3] == "h" else 270
            st = sh.fill.gradient_stops
            st[0].color.rgb = _rgb(f[1])
            st[1].color.rgb = _rgb(f[2])
        if s.get("border"):
            sh.line.color.rgb = _rgb(s["border"][0])
            sh.line.width = Emu(int(max(6350, bt * sx)))
        else:
            sh.line.fill.background()
        sh.shadow.inherit = False
        st_el = sh._element.find(qn("p:style"))
        if st_el is not None:
            sh._element.remove(st_el)   # 테마 그림자/효과 참조 제거(렌더러별 그림자 차이 방지)

    # ---------- 글자 상자
    @staticmethod
    def _merge_blocks(lines):
        """같은 왼쪽 정렬·비슷한 크기·좁은 줄간격의 줄은 한 문단 상자로 합친다."""
        lines = sorted(lines, key=lambda a: (a["x"], a["y"]))
        used = [False] * len(lines)
        blocks = []
        for i, a in enumerate(lines):
            if used[i]:
                continue
            blk = [a]
            used[i] = True
            cur = a
            while True:
                nxt = None
                for j, b in enumerate(lines):
                    if used[j]:
                        continue
                    gap = b["y"] - (cur["y"] + cur["h"])
                    if (abs(b["x"] - cur["x"]) <= 0.5 * cur["hmed"] and -0.2 * cur["h"] <= gap <= 0.6 * cur["h"] and
                            abs(b["hmed"] - cur["hmed"]) <= 0.25 * cur["hmed"] and
                            np.abs(np.asarray(b["fg"]) - np.asarray(cur["fg"])).max() < 60 and
                            b["bold"] == cur["bold"]):
                        nxt = j
                        break
                if nxt is None:
                    break
                used[nxt] = True
                blk.append(lines[nxt])
                cur = lines[nxt]
            blocks.append(blk)
        blocks.sort(key=lambda b: (b[0]["y"], b[0]["x"]))
        return blocks

    def _write_block(self, slide, blk, shapes, sx, sy, pt_per_px):
        """검증된 한 줄 -> 텍스트 상자. 글꼴 크기·문자 간격·위치를 원본 잉크 영역에 맞춘다."""
        ln = blk[0]
        px = ln["px"]
        pt = max(1.0, round(px * pt_per_px, 1))
        ix, iy, iw, ih = ln["ink"]
        left = ix - ln["lx"]
        width = iw + ln["lx"] + max(4.0, 0.6 * px)
        line_px = ln["line_px"]
        top = ln["cy"] - ln["cy_off"] - line_px / 2.0   # 글꼴 줄 상자의 중심 = 잉크 중심 - cy_off
        align = PP_ALIGN.LEFT
        # 완전 복원 모드: 도형 안에서 가운데 정렬된 글자는 도형 폭 기준 가운데 정렬
        cx, cyy = ix + iw / 2.0, iy + ih / 2.0
        cont = None
        for s in shapes:
            if s["x"] <= cx <= s["x"] + s["w"] and s["y"] <= cyy <= s["y"] + s["h"] and s["w"] >= iw:
                if cont is None or s["w"] * s["h"] < cont["w"] * cont["h"]:
                    cont = s
        if cont is not None and abs(cx - (cont["x"] + cont["w"] / 2.0)) <= max(3.0, 0.02 * cont["w"]) and \
                iw < 0.9 * cont["w"]:
            align, left, width = PP_ALIGN.CENTER, cont["x"], cont["w"]
        tb = slide.shapes.add_textbox(int(round(left * sx)), int(round(top * sy)),
                                      int(round(width * sx)), int(round(line_px * sy)))
        tf = tb.text_frame
        tf.word_wrap = False
        tf.auto_size = MSO_AUTO_SIZE.NONE
        # 가운데 맞춤: 프로그램(PowerPoint/LibreOffice)마다 줄 높이 계산이 조금 달라도 세로 오차가 가장 작다
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        p = tf.paragraphs[0]
        p.alignment = align
        p.line_spacing = 1.0
        p.space_before = p.space_after = Pt(0)
        r = p.add_run()
        r.text = ln["text"]
        self._set_run_font(r, pt, ln["fg"], ln["bold"], spc_pt=ln["spc"] * pt_per_px)

    # ---------- 표 쓰기
    def _write_table(self, slide, t, lines, used, clean, bgcolor, sx, sy, pt_per_px, W, H):
        xs, ys = t["xs"], t["ys"]
        cols, rows = len(xs) - 1, len(ys) - 1
        gf = slide.shapes.add_table(rows, cols, int(t["x"] * sx), int(t["y"] * sy),
                                    int(t["w"] * sx), int(t["h"] * sy))
        tbl = gf.table
        tblPr = tbl._tbl.tblPr
        tblPr.set("firstRow", "0")
        tblPr.set("bandRow", "0")
        sid = tblPr.find(qn("a:tableStyleId"))
        if sid is None:
            sid = etree.SubElement(tblPr, qn("a:tableStyleId"))
        sid.text = NO_STYLE_NO_GRID
        for c in range(cols):
            tbl.columns[c].width = Emu(int((xs[c + 1] - xs[c]) * sx))
        for r in range(rows):
            tbl.rows[r].height = Emu(int((ys[r + 1] - ys[r]) * sy))
        # 격자선 색
        samples = []
        for c in t["hs"][:12]:
            samples.append(np.median(clean[c["y"]:c["y"] + c["h"], c["x"]:c["x"] + c["w"]].reshape(-1, 3), axis=0))
        for c in t["vs"][:12]:
            samples.append(np.median(clean[c["y"]:c["y"] + c["h"], c["x"]:c["x"] + c["w"]].reshape(-1, 3), axis=0))
        line_col = np.median(np.array(samples), axis=0) if samples else np.array([150, 150, 150.])
        line_w = int(max(6350, t["thick"] * sx))
        # 글자 배정
        cell_lines = {}
        for i, ln in enumerate(lines):
            cx, cy = ln["x"] + ln["w"] / 2.0, ln["y"] + ln["h"] / 2.0
            ci = next((k for k in range(cols) if xs[k] <= cx < xs[k + 1]), None)
            ri = next((k for k in range(rows) if ys[k] <= cy < ys[k + 1]), None)
            if ci is None or ri is None:
                continue
            cell_lines.setdefault((ri, ci), []).append(ln)
            used.add(i)
        n_items = 0
        for r in range(rows):
            for c in range(cols):
                cell = tbl.cell(r, c)
                x0, x1, y0, y1 = xs[c], xs[c + 1], ys[r], ys[r + 1]
                inner = clean[y0 + int((y1 - y0) * .2):max(y0 + 2, y1 - int((y1 - y0) * .2)),
                              x0 + int((x1 - x0) * .1):max(x0 + 2, x1 - int((x1 - x0) * .1))]
                fillc = _mode_color(inner.reshape(-1, 3)) if inner.size else np.asarray(bgcolor)
                if self.keep_bg or _dist(fillc, bgcolor) < 8:
                    cell.fill.background()
                else:
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = _rgb(fillc)
                self._cell_border(cell, None if self.keep_bg else line_col, line_w)
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                cell.margin_top = cell.margin_bottom = 0
                cell.margin_left = cell.margin_right = Pt(3)
                raw_items = sorted(cell_lines.get((r, c), []), key=lambda a: (a["y"], a["x"]))
                if not raw_items:
                    continue
                n_items += len(raw_items)
                rows_ = []
                for a in sorted(raw_items, key=lambda q: q["y"] + q["h"] / 2.0):
                    if rows_ and abs((a["y"] + a["h"] / 2.0) - rows_[-1][0]) < 0.5 * max(a["h"], rows_[-1][1][0]["h"]):
                        rows_[-1][1].append(a)
                    else:
                        rows_.append([a["y"] + a["h"] / 2.0, [a]])
                items = []
                for _cy, grp in rows_:
                    grp.sort(key=lambda q: q["x"])
                    b = dict(grp[0])
                    for a in grp[1:]:
                        gap = a["x"] - (b["x"] + b["w"])
                        b["text"] += ("" if gap < 0.22 * b["h"] else " ") + a["text"]
                        b["w"] = max(b["x"] + b["w"], a["x"] + a["w"]) - b["x"]
                        b["y"] = min(b["y"], a["y"])
                        b["h"] = max(b["h"], a["h"])
                    items.append(b)
                cw = x1 - x0
                ch_pt = (y1 - y0) * pt_per_px
                hm = float(np.median([a["hmed"] for a in items]))
                pt = float(np.median([self._fit_pt(a["text"], a["w"], pt_per_px, hm) for a in items])) * 0.97
                pt = max(6.0, min(pt, (ch_pt / max(1, len(items))) / 1.45))
                pt = round(pt * 2) / 2.0
                tf = cell.text_frame
                tf.word_wrap = True
                a0 = items[0]
                cxm = a0["x"] + a0["w"] / 2.0
                if abs(cxm - (x0 + x1) / 2.0) <= 0.08 * cw:
                    al, ml, mr = PP_ALIGN.CENTER, 0, 0
                elif (x1 - (a0["x"] + a0["w"])) < 0.12 * cw and (a0["x"] - x0) > 0.25 * cw:
                    al, ml, mr = PP_ALIGN.RIGHT, 0, (x1 - (a0["x"] + a0["w"])) * pt_per_px
                else:
                    al, ml, mr = PP_ALIGN.LEFT, min(max(0, a0["x"] - x0), 0.3 * cw) * pt_per_px, 0
                cell.margin_left = Pt(max(0, ml))
                cell.margin_right = Pt(max(0, mr))
                for k, a in enumerate(items):
                    p = tf.paragraphs[0] if k == 0 else tf.add_paragraph()
                    p.alignment = al
                    run = p.add_run()
                    run.text = a["text"]
                    self._set_run_font(run, pt, a["fg"], a["bold"])
        return n_items
