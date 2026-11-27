"""공통 OCR 엔진 — PP-OCRv6 문자 영역 검출 + korean PP-OCRv5 인식, ONNX Runtime 추론, OpenCV 전처리.

모델 (PaddlePaddle 공식 ONNX, Hugging Face `PaddlePaddle/<이름>_onnx`):
  · 검출: PP-OCRv6_small_det (기본) / PP-OCRv6_medium_det  — 환경변수 OCR_DET_MODEL 로 바꾼다
  · 인식: korean_PP-OCRv5_mobile_rec
  모델 파일은 models/detection/<이름>/inference.onnx, models/recognition/<이름>/inference.onnx (install.py 가 받는다).

반환 형식은 예전 Apple Vision 판과 호환된다: {text, cx, cy, src="ocr"} + confidence, box(네 꼭짓점), bbox(x1,y1,x2,y2).
좌표는 항상 **원본 스크린샷 해상도** 기준이다(축소·영역 자르기 후 다시 환산).

모델은 프로세스에서 한 번만 올린다(get_engine). 같은 이미지(같은 바이트)·같은 영역은 다시 추론하지 않고 캐시를 돌려준다.
GPU 는 OCR_PROVIDER=gpu 일 때만 쓴다(CUDA → DirectML → CoreML 중 설치된 것). 기본은 CPU.
"""
from __future__ import annotations

import hashlib
import math
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path

import cv2
import numpy as np
import yaml

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
DET_MODELS = ("PP-OCRv6_small_det", "PP-OCRv6_medium_det")
REC_MODEL = "korean_PP-OCRv5_mobile_rec"
DEFAULT_DET = os.environ.get("OCR_DET_MODEL", "PP-OCRv6_small_det")

# 검출 입력 긴 변 상한. 폰 스크린샷(1080x2400)은 원본 그대로 들어간다(32 배수로만 맞춤).
DET_MAX_SIDE = int(os.environ.get("OCR_DET_MAX_SIDE", "2400"))
REC_HEIGHT = 48
REC_BATCH = 8
CACHE_SIZE = 16


class OCRNotReady(RuntimeError):
    pass


def model_paths(det_name: str = DEFAULT_DET) -> dict[str, Path]:
    return {
        "det": MODELS / "detection" / det_name / "inference.onnx",
        "det_yml": MODELS / "detection" / det_name / "inference.yml",
        "rec": MODELS / "recognition" / REC_MODEL / "inference.onnx",
        "rec_yml": MODELS / "recognition" / REC_MODEL / "inference.yml",
    }


def _providers() -> list[str]:
    import onnxruntime as ort
    avail = ort.get_available_providers()
    if os.environ.get("OCR_PROVIDER", "cpu").lower() == "gpu":
        for p in ("CUDAExecutionProvider", "DmlExecutionProvider", "CoreMLExecutionProvider"):
            if p in avail:
                return [p, "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


def _to_bgr(img) -> np.ndarray:
    """PIL.Image / numpy(BGR 또는 BGRA·회색) / PNG 바이트 → BGR uint8."""
    if isinstance(img, (bytes, bytearray)):
        arr = cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            raise ValueError("이미지 디코딩 실패")
        return arr
    if isinstance(img, np.ndarray):
        if img.ndim == 2:
            return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        if img.shape[2] == 4:
            return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        return img
    # PIL
    rgb = np.asarray(img.convert("RGB"))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


class OCREngine:
    """검출·인식 ONNX 세션을 메모리에 들고 반복 사용한다. 스레드 안전(추론은 락 하나로 직렬화)."""

    def __init__(self, det_name: str = DEFAULT_DET):
        import onnxruntime as ort
        paths = model_paths(det_name)
        missing = [str(p) for k, p in paths.items() if not p.exists()]
        if missing:
            raise OCRNotReady("OCR 모델 파일 없음 — install 을 먼저 실행: " + ", ".join(missing))
        det_cfg = yaml.safe_load(paths["det_yml"].read_text(encoding="utf-8"))
        rec_cfg = yaml.safe_load(paths["rec_yml"].read_text(encoding="utf-8"))
        if det_cfg["Global"]["model_name"] != det_name or rec_cfg["Global"]["model_name"] != REC_MODEL:
            raise OCRNotReady("모델 설정 파일 이름이 맞지 않음")
        post = det_cfg["PostProcess"]
        self.thresh = float(post.get("thresh", 0.2))
        self.box_thresh = float(post.get("box_thresh", 0.45))
        self.unclip_ratio = float(post.get("unclip_ratio", 1.4))
        self.max_candidates = int(post.get("max_candidates", 1000))
        # CTC: 0 = blank, 그 뒤 사전, 맨 끝 공백 (PaddleOCR CTCLabelDecode, use_space_char=True)
        self.chars = ["blank"] + list(rec_cfg["PostProcess"]["character_dict"]) + [" "]

        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.log_severity_level = 3   # 변환 모델의 '사용 안 하는 initializer' 경고 숨김
        threads = int(os.environ.get("OCR_THREADS", "0"))
        if threads:
            so.intra_op_num_threads = threads
        prov = _providers()
        self.det = ort.InferenceSession(str(paths["det"]), so, providers=prov)
        self.rec = ort.InferenceSession(str(paths["rec"]), so, providers=prov)
        n_cls = self.rec.get_outputs()[0].shape[-1]
        if isinstance(n_cls, int) and n_cls != len(self.chars):
            raise OCRNotReady(f"인식 모델 출력({n_cls})과 문자 사전({len(self.chars)}) 크기가 다름")
        self.det_name, self.providers = det_name, prov
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, list[dict]] = OrderedDict()
        self.stats = {"calls": 0, "cache_hits": 0, "last_ms": 0.0, "last_conf": 0.0}

    # ---------- 검출 ----------

    def _det_resize(self, img: np.ndarray) -> tuple[np.ndarray, float, float]:
        h, w = img.shape[:2]
        ratio = min(1.0, DET_MAX_SIDE / max(h, w))
        rh = max(32, int(round(h * ratio / 32)) * 32)
        rw = max(32, int(round(w * ratio / 32)) * 32)
        return cv2.resize(img, (rw, rh)), rh / h, rw / w

    def detect(self, img: np.ndarray) -> list[np.ndarray]:
        """문자 영역 네 꼭짓점(원본 좌표, float32 4x2) 목록. 위→아래, 왼→오른 순."""
        h, w = img.shape[:2]
        resized, ry, rx = self._det_resize(img)
        x = resized.astype(np.float32) / 255.0
        x = (x - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
        x = x.transpose(2, 0, 1)[None]
        prob = self.det.run(None, {self.det.get_inputs()[0].name: x})[0][0, 0]
        boxes = self._db_postprocess(prob, w, h)
        return sorted(boxes, key=lambda b: (round(b[:, 1].min() / 10), b[:, 0].min()))

    def _db_postprocess(self, prob: np.ndarray, w: int, h: int) -> list[np.ndarray]:
        bitmap = (prob > self.thresh).astype(np.uint8)
        ph, pw = prob.shape
        contours, _ = cv2.findContours(bitmap, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        out = []
        for c in contours[: self.max_candidates]:
            if len(c) < 3:
                continue
            rect = cv2.minAreaRect(c)
            if min(rect[1]) < 3:
                continue
            score = self._box_score(prob, c)
            if score < self.box_thresh:
                continue
            # unclip: 직사각형을 거리 d = 넓이·ratio/둘레 만큼 바깥으로 넓힌다(PaddleOCR DB unclip 의 사각형 경우와 같음)
            (cx, cy), (rw, rh), ang = rect
            d = rw * rh * self.unclip_ratio / (2 * (rw + rh))
            rect = ((cx, cy), (rw + 2 * d, rh + 2 * d), ang)
            if min(rect[1]) < 5:
                continue
            box = cv2.boxPoints(rect)
            box[:, 0] = np.clip(box[:, 0] * w / pw, 0, w - 1)
            box[:, 1] = np.clip(box[:, 1] * h / ph, 0, h - 1)
            out.append(_order_points(box))
        return out

    @staticmethod
    def _box_score(prob: np.ndarray, contour: np.ndarray) -> float:
        x, y, bw, bh = cv2.boundingRect(contour)
        mask = np.zeros((bh, bw), np.uint8)
        cv2.fillPoly(mask, [contour.reshape(-1, 2) - [x, y]], 1)
        return float(cv2.mean(prob[y:y + bh, x:x + bw], mask)[0])

    # ---------- 인식 ----------

    def recognize(self, crops: list[np.ndarray]) -> list[tuple[str, float]]:
        if not crops:
            return []
        order = np.argsort([c.shape[1] / max(1, c.shape[0]) for c in crops])
        results: list[tuple[str, float]] = [("", 0.0)] * len(crops)
        for s in range(0, len(crops), REC_BATCH):
            idx = order[s:s + REC_BATCH]
            max_ratio = max(320 / REC_HEIGHT, *(crops[i].shape[1] / max(1, crops[i].shape[0]) for i in idx))
            width = int(math.ceil(REC_HEIGHT * max_ratio))
            batch = np.zeros((len(idx), 3, REC_HEIGHT, width), np.float32)
            for j, i in enumerate(idx):
                c = crops[i]
                rw = min(width, int(math.ceil(REC_HEIGHT * c.shape[1] / max(1, c.shape[0]))))
                r = cv2.resize(c, (max(1, rw), REC_HEIGHT)).astype(np.float32)
                batch[j, :, :, :rw] = ((r / 255.0 - 0.5) / 0.5).transpose(2, 0, 1)
            probs = self.rec.run(None, {self.rec.get_inputs()[0].name: batch})[0]
            for j, i in enumerate(idx):
                results[i] = self._ctc_decode(probs[j])
        return results

    def _ctc_decode(self, prob: np.ndarray) -> tuple[str, float]:
        ids, conf = prob.argmax(axis=1), prob.max(axis=1)
        keep = np.ones(len(ids), bool)
        keep[1:] = ids[1:] != ids[:-1]
        keep &= ids != 0
        text = "".join(self.chars[i] for i in ids[keep])
        return text, float(conf[keep].mean()) if keep.any() else 0.0

    # ---------- 전체 ----------

    def ocr(self, img, region: tuple[int, int, int, int] | None = None, min_conf: float = 0.5,
            use_cache: bool = True) -> list[dict]:
        """img 전체 또는 region(x1,y1,x2,y2) 만 읽는다. 결과 좌표는 원본 이미지 기준."""
        bgr = _to_bgr(img)
        H, W = bgr.shape[:2]
        ox = oy = 0
        if region:
            x1, y1, x2, y2 = (max(0, region[0]), max(0, region[1]), min(W, region[2]), min(H, region[3]))
            if x2 <= x1 or y2 <= y1:
                return []
            bgr, ox, oy = bgr[y1:y2, x1:x2], x1, y1
        key = None
        if use_cache:
            key = hashlib.blake2b(bgr.tobytes(), digest_size=16).hexdigest() + f"{bgr.shape}{ox},{oy},{min_conf}"
            with self._lock:
                if key in self._cache:
                    self._cache.move_to_end(key)
                    self.stats["cache_hits"] += 1
                    return [dict(it) for it in self._cache[key]]
        t0 = time.perf_counter()
        with self._lock:
            boxes = self.detect(bgr)
            crops = [_crop_quad(bgr, b) for b in boxes]
            recs = self.recognize(crops)
        items = []
        for box, (text, conf) in zip(boxes, recs):
            text = text.strip()
            if not text or conf < min_conf:
                continue
            q = box + [ox, oy]
            x1, y1 = q.min(axis=0)
            x2, y2 = q.max(axis=0)
            items.append({"text": text, "cx": int(round((x1 + x2) / 2)), "cy": int(round((y1 + y2) / 2)),
                          "src": "ocr", "confidence": round(conf, 4),
                          "box": [[int(round(px)), int(round(py))] for px, py in q],
                          "bbox": [int(x1), int(y1), int(math.ceil(x2)), int(math.ceil(y2))]})
        ms = (time.perf_counter() - t0) * 1000
        with self._lock:
            self.stats["calls"] += 1
            self.stats["last_ms"] = round(ms, 1)
            self.stats["last_conf"] = round(float(np.mean([i["confidence"] for i in items])), 4) if items else 0.0
            if key:
                self._cache[key] = [dict(it) for it in items]
                while len(self._cache) > CACHE_SIZE:
                    self._cache.popitem(last=False)
        return items


def _order_points(pts: np.ndarray) -> np.ndarray:
    """왼위·오위·오아래·왼아래 순."""
    pts = pts.astype(np.float32)
    s, d = pts.sum(axis=1), np.diff(pts, axis=1).ravel()
    return np.array([pts[s.argmin()], pts[d.argmin()], pts[s.argmax()], pts[d.argmax()]], np.float32)


def _crop_quad(img: np.ndarray, quad: np.ndarray) -> np.ndarray:
    """사각형 영역을 펴서 자른다. 세로로 긴 영역(세로쓰기)은 90도 돌린다(PaddleOCR get_rotate_crop_image)."""
    w = int(max(np.linalg.norm(quad[0] - quad[1]), np.linalg.norm(quad[2] - quad[3])))
    h = int(max(np.linalg.norm(quad[0] - quad[3]), np.linalg.norm(quad[1] - quad[2])))
    w, h = max(w, 1), max(h, 1)
    dst = np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float32)
    crop = cv2.warpPerspective(img, cv2.getPerspectiveTransform(quad, dst), (w, h),
                               borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_CUBIC)
    if h / w >= 1.5:
        crop = np.rot90(crop)
    return np.ascontiguousarray(crop)


_ENGINE: OCREngine | None = None
_ENGINE_LOCK = threading.Lock()
_ENGINE_ERROR: str = ""


def get_engine() -> OCREngine:
    """프로세스 전체에서 엔진 하나. 처음 부를 때만 모델을 올린다."""
    global _ENGINE, _ENGINE_ERROR
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                try:
                    _ENGINE = OCREngine()
                    _ENGINE_ERROR = ""
                except Exception as e:
                    _ENGINE_ERROR = str(e)
                    raise
    return _ENGINE


def status() -> dict:
    if _ENGINE is None:
        return {"ready": False, "error": _ENGINE_ERROR, "det_model": DEFAULT_DET, "rec_model": REC_MODEL}
    e = _ENGINE
    return {"ready": True, "error": "", "det_model": e.det_name, "rec_model": REC_MODEL,
            "providers": e.providers, **e.stats}


def ocr_texts(img, region=None, min_conf: float = 0.5) -> list[dict]:
    """예전 phone_login.ocr_texts 와 같은 형식의 목록."""
    return get_engine().ocr(img, region=region, min_conf=min_conf)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("사용: python phone_auto/ocr_engine.py <이미지> [x1 y1 x2 y2]")
        sys.exit(2)
    reg = tuple(map(int, sys.argv[2:6])) if len(sys.argv) >= 6 else None
    data = Path(sys.argv[1]).read_bytes()
    eng = get_engine()
    for it in eng.ocr(data, region=reg):
        print(f"{it['confidence']:.3f} ({it['cx']},{it['cy']}) {it['text']}")
    print(status())
