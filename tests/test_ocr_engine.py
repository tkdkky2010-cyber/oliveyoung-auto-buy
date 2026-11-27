"""OCR 엔진: 반환 형식, 한글·숫자 인식, 위치 정확도, 영역 좌표 환산, 캐시."""
import os

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

import ocr_engine

FONTS = ["/System/Library/Fonts/AppleSDGothicNeo.ttc", "C:/Windows/Fonts/malgun.ttf",
         "/usr/share/fonts/truetype/nanum/NanumGothic.ttf", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"]
FONT = next((f for f in FONTS if os.path.exists(f)), None)
pytestmark = pytest.mark.skipif(not ocr_engine.model_paths()["det"].exists(), reason="모델 미설치")


@pytest.fixture(scope="module")
def eng():
    return ocr_engine.get_engine()


def screen(lines):
    img = Image.new("RGB", (1080, 2400), "white")
    d = ImageDraw.Draw(img)
    boxes = []
    for text, xy, size in lines:
        font = ImageFont.truetype(FONT, size)
        d.text(xy, text, font=font, fill=(20, 20, 20))
        boxes.append(d.textbbox(xy, text, font=font))
    return img, boxes


def test_format_and_compat(eng):
    img = np.full((200, 800, 3), 255, np.uint8)
    import cv2
    cv2.putText(img, "16,200", (30, 130), cv2.FONT_HERSHEY_SIMPLEX, 3, (0, 0, 0), 6)
    items = eng.ocr(img, use_cache=False)
    assert items and items[0]["text"] == "16,200"
    it = items[0]
    assert {"text", "cx", "cy", "src", "confidence", "box", "bbox"} <= set(it)
    assert it["src"] == "ocr" and 0 < it["confidence"] <= 1 and len(it["box"]) == 4


@pytest.mark.skipif(FONT is None, reason="한글 글꼴 없음")
def test_korean_digits_and_position(eng):
    lines = [("주문을 완료했어요!", (60, 300), 48), ("주문번호 Y2611272780526", (60, 500), 36),
             ("결제하기", (400, 2150), 44), ("12,190원", (700, 900), 40), ("모두 동의합니다", (80, 1320), 34)]
    img, boxes = screen(lines)
    items = eng.ocr(img, use_cache=False)
    texts = [i["text"].replace(" ", "") for i in items]
    for (text, _, _), (x1, y1, x2, y2) in zip(lines, boxes):
        hit = next(i for i in items if i["text"].replace(" ", "") == text.replace(" ", ""))
        assert abs(hit["cx"] - (x1 + x2) / 2) <= 12 and abs(hit["cy"] - (y1 + y2) / 2) <= 12, (text, hit)
    assert len(texts) == len(lines)


@pytest.mark.skipif(FONT is None, reason="한글 글꼴 없음")
def test_region_coordinates_are_original(eng):
    img, boxes = screen([("바로구매", (700, 2150), 44), ("장바구니", (150, 2150), 44)])
    full = {i["text"]: i for i in eng.ocr(img, use_cache=False)}
    part = {i["text"]: i for i in eng.ocr(img, region=(0, 2000, 1080, 2400), use_cache=False)}
    for k in ("바로구매", "장바구니"):
        assert abs(full[k]["cx"] - part[k]["cx"]) <= 3 and abs(full[k]["cy"] - part[k]["cy"]) <= 3


def test_cache_hits(eng):
    img = np.full((100, 300, 3), 255, np.uint8)
    before = eng.stats["cache_hits"]
    eng.ocr(img)
    eng.ocr(img)
    assert eng.stats["cache_hits"] == before + 1


def test_status_ready(eng):
    s = ocr_engine.status()
    assert s["ready"] and s["det_model"] == "PP-OCRv6_small_det" and s["rec_model"] == "korean_PP-OCRv5_mobile_rec"
