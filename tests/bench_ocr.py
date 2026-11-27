"""OCR 성능 측정 — 검출 모델(PP-OCRv6 small/medium) 비교, 속도·정확도 기록.

정확도: 정답이 있는 합성 화면(실제 앱과 같은 1080x2400, 한글 UI 문구·상품명·금액·주문번호, 크기 24~64px,
        저대비 회색 글씨 포함)을 그려 줄 단위로 비교한다. 문자 정확도 = 1 - CER.
속도:   실제 폰 스크린샷 폴더(--real) 의 전체 화면 / 부분 화면(하단 400px) 처리 시간.

사용:  python tests/bench_ocr.py --real "<스크린샷 폴더>" --out docs/OCR_BENCHMARK.md
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import random
import statistics
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "phone_auto"))
import ocr_engine  # noqa: E402

FONT_CANDIDATES = [
    ("/System/Library/Fonts/AppleSDGothicNeo.ttc", [0, 2, 6]),
    ("/System/Library/Fonts/Supplemental/AppleGothic.ttf", [0]),
    ("C:/Windows/Fonts/malgun.ttf", [0]),
    ("C:/Windows/Fonts/malgunbd.ttf", [0]),
    ("/usr/share/fonts/truetype/nanum/NanumGothic.ttf", [0]),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", [1]),
]

KOREAN = ["주문을 완료했어요!", "주문완료", "결제하기", "바로구매", "장바구니", "모두 동의합니다", "로그아웃", "기본배송지",
          "일반배송 구매하기", "옵션을 선택해 주세요", "상품을 찾을 수 없어요", "이전 페이지", "홈으로 가기", "앱카드 결제",
          "현대카드 앱으로 쉽고 빠르게", "결제 비밀번호 인증", "정보 보호를 위해 아래 인증 절차를 진행해 주세요.",
          "사람인지 확인하십시오", "로카페이", "신용카드", "일시불", "전액사용", "닫기", "마이", "확인", "나중에",
          "헤트라스 수선화 디퓨저", "라운드랩 1025 독도 토너", "토리든 다이브인 세럼", "메디힐 마데카소사이드 패드",
          "닥터지 레드 블레미쉬 클리어 수딩 크림", "에스트라 아토베리어365 크림", "아이소이 잡티 세럼",
          "롬앤 쥬시 래스팅 틴트", "웨이크메이크 소프트 블러링 아이 팔레트", "클리오 킬커버 파운웨어 쿠션",
          "CJ ONE 포인트는 10P 단위로 사용하실 수 있어요.", "장바구니가 달라졌어요!", "배송지 변경",
          "오늘드림", "무료배송", "쿠폰 적용", "올리브영 쇼핑 큐레이터 활동의 일환으로", "헤이즐넛", "선택 취소"]


def numeric_samples(rng: random.Random) -> list[str]:
    out = []
    for _ in range(40):
        kind = rng.randrange(5)
        if kind == 0:
            out.append(f"{rng.randrange(1000, 300000):,}원")
        elif kind == 1:
            out.append("Y" + "".join(str(rng.randrange(10)) for _ in range(13)))
        elif kind == 2:
            out.append(f"{rng.randrange(0, 30000):,}P")
        elif kind == 3:
            out.append(f"A0000002{rng.randrange(10000, 99999)}")
        else:
            out.append(f"- {rng.randrange(100, 9000):,}")
    return out


def fonts() -> list[tuple[str, int]]:
    out = []
    for path, idxs in FONT_CANDIDATES:
        if os.path.exists(path):
            out += [(path, i) for i in idxs]
    return out


def make_screens(n: int, seed: int = 7) -> list[tuple[Image.Image, list[dict]]]:
    """화면 한 장에 줄 12~16개. 정답 = 줄 글자와 그 상자."""
    rng = random.Random(seed)
    fs = fonts()
    if not fs:
        raise SystemExit("한글 글꼴을 찾지 못함 — FONT_CANDIDATES 에 경로 추가")
    screens = []
    for _ in range(n):
        bg = rng.choice([(255, 255, 255), (248, 248, 248), (245, 246, 250)])
        img = Image.new("RGB", (1080, 2400), bg)
        d = ImageDraw.Draw(img)
        y, truth = 120, []
        lines = rng.sample(KOREAN, 8) + rng.sample(numeric_samples(rng), 6)
        rng.shuffle(lines)
        for text in lines:
            size = rng.choice([24, 28, 32, 36, 42, 48, 56, 64])
            path, idx = rng.choice(fs)
            font = ImageFont.truetype(path, size, index=idx)
            color = rng.choice([(17, 17, 17), (51, 51, 51), (119, 119, 119), (153, 153, 153), (0, 0, 0)])
            x = rng.randrange(40, 120)
            box = d.textbbox((x, y), text, font=font)
            if box[2] > 1040:
                continue
            if rng.random() < 0.15:   # 버튼: 진한 바탕 흰 글씨
                d.rounded_rectangle((box[0] - 30, box[1] - 20, box[2] + 30, box[3] + 20), 12, fill=(30, 30, 30))
                color = (255, 255, 255)
            d.text((x, y), text, font=font, fill=color)
            truth.append({"text": text, "bbox": box, "numeric": not any("가" <= ch <= "힣" for ch in text)})
            y = box[3] + rng.randrange(60, 110)
            if y > 2250:
                break
        screens.append((img, truth))
    return screens


def cer(ref: str, hyp: str) -> float:
    a, b = ref.replace(" ", ""), hyp.replace(" ", "")
    dp = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, dp[0] = dp[0], i
        for j, cb in enumerate(b, 1):
            prev, dp[j] = dp[j], min(dp[j] + 1, dp[j - 1] + 1, prev + (ca != cb))
    return dp[len(b)] / max(1, len(a))


def match(truth: list[dict], items: list[dict]) -> list[dict]:
    """정답 줄마다 중심이 상자 안에 들어오는 OCR 항목을 왼→오른으로 이어 붙여 비교한다."""
    rows = []
    for t in truth:
        x1, y1, x2, y2 = t["bbox"]
        hits = sorted((it for it in items if x1 - 30 <= it["cx"] <= x2 + 30 and y1 - 15 <= it["cy"] <= y2 + 15),
                      key=lambda it: it["cx"])
        hyp = " ".join(h["text"] for h in hits)
        center_err = None
        if hits:
            hx1 = min(h["bbox"][0] for h in hits); hy1 = min(h["bbox"][1] for h in hits)
            hx2 = max(h["bbox"][2] for h in hits); hy2 = max(h["bbox"][3] for h in hits)
            center_err = abs((hx1 + hx2) / 2 - (x1 + x2) / 2) + abs((hy1 + hy2) / 2 - (y1 + y2) / 2)
        rows.append({"ref": t["text"], "hyp": hyp, "numeric": t["numeric"],
                     "exact": hyp.replace(" ", "") == t["text"].replace(" ", ""), "cer": cer(t["text"], hyp),
                     "center_err": center_err})
    return rows


def summarize(rows: list[dict]) -> dict:
    def part(rs):
        if not rs:
            return {}
        chars = sum(len(r["ref"].replace(" ", "")) for r in rs)
        errs = sum(r["cer"] * len(r["ref"].replace(" ", "")) for r in rs)
        ce = [r["center_err"] for r in rs if r["center_err"] is not None]
        return {"lines": len(rs), "line_exact": round(sum(r["exact"] for r in rs) / len(rs) * 100, 2),
                "char_acc": round((1 - errs / chars) * 100, 3), "missed": sum(1 for r in rs if not r["hyp"]),
                "center_err_px_mean": round(statistics.mean(ce), 1) if ce else None}
    return {"korean": part([r for r in rows if not r["numeric"]]), "numeric": part([r for r in rows if r["numeric"]])}


def time_real(engine, files: list[Path]) -> dict:
    full, region = [], []
    for f in files:
        data = f.read_bytes()
        t = time.perf_counter(); engine.ocr(data, use_cache=False); full.append((time.perf_counter() - t) * 1000)
        t = time.perf_counter(); engine.ocr(data, region=(0, 2000, 1080, 2400), use_cache=False)
        region.append((time.perf_counter() - t) * 1000)
    f = lambda xs: {"mean_ms": round(statistics.mean(xs), 1), "p95_ms": round(sorted(xs)[int(len(xs) * .95) - 1], 1),
                    "max_ms": round(max(xs), 1)} if xs else {}
    return {"images": len(files), "full": f(full), "region_bottom400": f(region)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", help="실제 폰 스크린샷 폴더(속도 측정)")
    ap.add_argument("--synthetic", type=int, default=100)
    ap.add_argument("--out", default=str(ROOT / "docs" / "OCR_BENCHMARK.json"))
    a = ap.parse_args()
    reals = sorted(p for p in Path(a.real).rglob("*.png")) if a.real else []
    reals = [p for p in reals if Image.open(p).size == (1080, 2400)]
    screens = make_screens(a.synthetic)
    result = {"env": {"os": platform.platform(), "machine": platform.machine(), "python": platform.python_version(),
                      "cpu_count": os.cpu_count(), "onnxruntime": __import__("onnxruntime").__version__,
                      "fonts": [Path(p).name + f"#{i}" for p, i in fonts()]}, "models": {}}
    for det in ocr_engine.DET_MODELS:
        eng = ocr_engine.OCREngine(det)
        eng.ocr(screens[0][0], use_cache=False)   # 워밍업
        rows, times = [], []
        for img, truth in screens:
            t = time.perf_counter()
            items = eng.ocr(img, use_cache=False, min_conf=0.0)
            times.append((time.perf_counter() - t) * 1000)
            rows += match(truth, items)
        result["models"][det] = {"synthetic": summarize(rows), "synthetic_full_ms_mean": round(statistics.mean(times), 1),
                                 "real": time_real(eng, reals),
                                 "worst": sorted((r for r in rows if not r["exact"]), key=lambda r: -r["cer"])[:15]}
        print(det, json.dumps(result["models"][det]["synthetic"], ensure_ascii=False),
              result["models"][det]["real"], flush=True)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
