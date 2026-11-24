# -*- coding: utf-8 -*-
"""
안드로이드용 pytesseract 대체 모듈 (SlideCapture Pro Android)

PC 엔진(slide_rebuild.py)은 pytesseract.image_to_data() 로 tesseract 실행 파일을 호출한다.
폰에는 tesseract 실행 파일이 없으므로, 같은 Tesseract 엔진을 안드로이드용으로 만든
Tesseract4Android(TessBaseAPI)를 Chaquopy로 호출하고 hOCR 결과를 pytesseract 와 같은
딕셔너리 형식으로 바꿔 돌려준다. 엔진 코드는 한 줄도 바꾸지 않고 그대로 쓴다.

필요한 함수만 구현: image_to_data(image, lang, config, output_type=Output.DICT)
"""
import re
import threading
from html.parser import HTMLParser

import numpy as np

__all__ = ["Output", "image_to_data", "set_data_path", "set_backend", "pytesseract"]


class Output:
    DICT = "dict"
    STRING = "string"


class _Cmd:
    """pytesseract.pytesseract.tesseract_cmd 호환용 (폰에서는 사용하지 않음)"""
    tesseract_cmd = ""


pytesseract = _Cmd()

_DATA_PATH = None       # tessdata 폴더의 '부모' 폴더 경로 (앱이 시작할 때 지정)
_BACKEND = None
_local = threading.local()


def set_data_path(path):
    global _DATA_PATH
    _DATA_PATH = str(path)


def set_backend(backend):
    """테스트용: hocr(gray, lang, psm, oem, variables) 메서드를 가진 객체로 바꿔 끼운다."""
    global _BACKEND
    _BACKEND = backend


class _JavaBackend:
    """Tesseract4Android(TessBaseAPI) 호출. 스레드마다 언어별 인스턴스를 따로 둔다(엔진이 2개 언어를 동시에 읽음)."""

    def __init__(self):
        from java import jclass  # Chaquopy
        self.TessBaseAPI = jclass("com.googlecode.tesseract.android.TessBaseAPI")

    def _api(self, lang, oem):
        apis = getattr(_local, "apis", None)
        if apis is None:
            apis = _local.apis = {}
        key = (lang, oem)
        api = apis.get(key)
        if api is None:
            if not _DATA_PATH:
                raise RuntimeError("Tesseract 데이터 경로가 지정되지 않았습니다.")
            api = self.TessBaseAPI()
            if not api.init(_DATA_PATH, lang, oem):
                raise RuntimeError(f"Tesseract 초기화 실패 (언어 데이터 확인): {lang}")
            apis[key] = api
        return api

    def hocr(self, gray, lang, psm, oem, variables):
        api = self._api(lang, oem)
        api.setPageSegMode(int(psm))
        for k, v in variables.items():
            api.setVariable(str(k), str(v))
        h, w = gray.shape[:2]
        api.setImage(np.ascontiguousarray(gray, dtype=np.uint8).tobytes(), int(w), int(h), 1, int(w))
        try:
            text = api.getHOCRText(0)
        finally:
            api.clear()
        return str(text) if text is not None else ""


def _backend():
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = _JavaBackend()
    return _BACKEND


def _parse_config(config):
    psm, oem, variables = 3, 1, {}
    toks = (config or "").split()
    i = 0
    while i < len(toks):
        t = toks[i]
        if t == "--psm" and i + 1 < len(toks):
            psm = int(toks[i + 1])
            i += 1
        elif t == "--oem" and i + 1 < len(toks):
            oem = int(toks[i + 1])
            i += 1
        elif t == "-c" and i + 1 < len(toks) and "=" in toks[i + 1]:
            k, v = toks[i + 1].split("=", 1)
            variables[k] = v
            i += 1
        i += 1
    # Tesseract4Android의 언어 데이터(tessdata_fast)는 LSTM 전용 -> 기본(3)은 LSTM(1)로
    if oem not in (0, 1, 2):
        oem = 1
    return psm, oem, variables


_BBOX = re.compile(r"bbox\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)")
_CONF = re.compile(r"x_wconf\s+(-?[\d.]+)")
_LINE_CLASSES = {"ocr_line", "ocrx_line", "ocr_caption", "ocr_textfloat", "ocr_header"}


class _HocrParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.block = self.par = self.line = self.word = 0
        self.stack = []            # 열린 태그별 class
        self.cur = None            # 현재 단어
        self.depth_word = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "") or ""
        self.stack.append(cls)
        if cls == "ocr_carea":
            self.block += 1
            self.par = self.line = 0
        elif cls == "ocr_par":
            self.par += 1
            self.line = 0
        elif cls in _LINE_CLASSES:
            self.line += 1
            self.word = 0
        elif cls == "ocrx_word":
            self.word += 1
            title = a.get("title", "") or ""
            m = _BBOX.search(title)
            c = _CONF.search(title)
            x0, y0, x1, y1 = (int(v) for v in m.groups()) if m else (0, 0, 0, 0)
            self.cur = dict(block=self.block, par=max(1, self.par), line=max(1, self.line), word=self.word,
                            left=x0, top=y0, width=max(0, x1 - x0), height=max(0, y1 - y0),
                            conf=float(c.group(1)) if c else -1.0, text="")
            self.depth_word = len(self.stack)

    def handle_endtag(self, tag):
        if self.cur is not None and self.depth_word == len(self.stack):
            self.rows.append(self.cur)
            self.cur = None
            self.depth_word = None
        if self.stack:
            self.stack.pop()

    def handle_data(self, data):
        if self.cur is not None:
            self.cur["text"] += data


def parse_hocr(hocr):
    """hOCR 문자열 -> pytesseract.image_to_data(output_type=DICT) 와 같은 형식의 딕셔너리"""
    p = _HocrParser()
    p.feed(hocr or "")
    p.close()
    keys = ["level", "page_num", "block_num", "par_num", "line_num", "word_num",
            "left", "top", "width", "height", "conf", "text"]
    d = {k: [] for k in keys}
    for r in p.rows:
        d["level"].append(5)
        d["page_num"].append(1)
        d["block_num"].append(r["block"])
        d["par_num"].append(r["par"])
        d["line_num"].append(r["line"])
        d["word_num"].append(r["word"])
        d["left"].append(r["left"])
        d["top"].append(r["top"])
        d["width"].append(r["width"])
        d["height"].append(r["height"])
        d["conf"].append(r["conf"])
        d["text"].append(r["text"].strip())
    return d


def _to_gray(image):
    if hasattr(image, "convert"):           # PIL.Image
        return np.asarray(image.convert("L"))
    a = np.asarray(image)
    if a.ndim == 3:
        import cv2
        a = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
    return a


def image_to_data(image, lang="eng", config="", nice=0, output_type=Output.STRING, timeout=0, pandas_config=None):
    psm, oem, variables = _parse_config(config)
    gray = _to_gray(image)
    d = parse_hocr(_backend().hocr(gray, lang, psm, oem, variables))
    if output_type == Output.DICT:
        return d
    lines = ["\t".join(d.keys())]
    for i in range(len(d["text"])):
        lines.append("\t".join(str(d[k][i]) for k in d))
    return "\n".join(lines)
