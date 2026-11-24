# -*- coding: utf-8 -*-
"""
SlideCapture Pro Android - PPT 변환 진입점

안드로이드 변환 서비스(별도 프로세스)가 호출한다. PC 버전의 ppt_worker.run_job 과 slide_rebuild 엔진을
그대로 쓰고, 진행 상황은 Kotlin 쪽 리포터 객체(onMessage(String))로 JSON 문자열을 넘긴다.
"""
import json
import os

import pytesseract
import slide_rebuild
from ppt_worker import run_job


class _Reporter:
    """ppt_worker 의 Queue.put() 대신 Kotlin 콜백으로 전달"""

    def __init__(self, cb):
        self.cb = cb

    def put(self, msg):
        try:
            self.cb.onMessage(json.dumps(list(msg), ensure_ascii=False, default=str))
        except Exception:
            pass


def setup(data_dir, font_regular="", font_bold=""):
    """앱 시작 시 1회: Tesseract 데이터 경로(tessdata의 부모 폴더)와 검증용 한글 글꼴 지정"""
    pytesseract.set_data_path(data_dir)
    fonts = []
    if font_regular and os.path.exists(font_regular):
        fonts.append((font_regular, font_bold if font_bold and os.path.exists(font_bold) else font_regular, 0))
    # 안드로이드 기본 한글 글꼴(기기마다 있을 수 있음)
    fonts.append(("/system/fonts/NotoSansCJK-Regular.ttc", "/system/fonts/NotoSansCJK-Regular.ttc", 1))
    for f in reversed(fonts):
        if f not in slide_rebuild._FONT_PATHS:
            slide_rebuild._FONT_PATHS.insert(0, f)
    return True


def convert(job_json, reporter):
    """job_json: PC 버전 ppt_worker.run_job 과 같은 항목(JSON 문자열)"""
    job = json.loads(job_json)
    job.setdefault("tess", "")
    run_job(job, _Reporter(reporter))
    return True
