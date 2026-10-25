"""Face detection (YuNet) and face embeddings (SFace), both from the OpenCV model zoo.

Embeddings are 128-dim, L2-normalised; two faces are the same person when their cosine
similarity is above ~0.36 (OpenCV's recommended SFace threshold).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

DETECTOR = "face_detection_yunet_2023mar.onnx"
RECOGNIZER = "face_recognition_sface_2021dec.onnx"
SAME_PERSON = 0.363        # SFace cosine threshold from the OpenCV model zoo


@dataclass
class Face:
    box: tuple[int, int, int, int]          # x, y, w, h in frame pixels
    score: float                            # detector confidence
    embedding: np.ndarray                   # (128,) float32, unit length
    crop: np.ndarray = field(repr=False)    # 112x112 aligned face (for thumbnails)


class FaceEngine:
    # min_size 24 px: at 640x480 that's a face a few metres from the robot; smaller is guesswork.
    def __init__(self, models_dir: str | Path, min_score: float = 0.8, min_size: int = 24):
        models = Path(models_dir)
        for name in (DETECTOR, RECOGNIZER):
            if not (models / name).exists():
                raise FileNotFoundError(f"missing model {models / name}")
        self.detector = cv2.FaceDetectorYN.create(str(models / DETECTOR), "", (320, 320), min_score, 0.3, 50)
        self.recognizer = cv2.FaceRecognizerSF.create(str(models / RECOGNIZER), "")
        self.min_size = min_size

    def analyze(self, img: np.ndarray) -> list[Face]:
        h, w = img.shape[:2]
        self.detector.setInputSize((w, h))
        _, rows = self.detector.detect(img)
        faces = []
        for row in [] if rows is None else rows:
            x, y, bw, bh = (int(v) for v in row[:4])
            if bw < self.min_size or bh < self.min_size:
                continue                     # too small to recognise reliably
            crop = self.recognizer.alignCrop(img, row)
            emb = self.recognizer.feature(crop).flatten().astype(np.float32)
            emb /= np.linalg.norm(emb) + 1e-9
            faces.append(Face((max(0, x), max(0, y), bw, bh), float(row[-1]), emb, crop))
        return faces


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))


def thumbnail_jpeg(crop: np.ndarray, size: int = 64) -> bytes:
    ok, buf = cv2.imencode(".jpg", cv2.resize(crop, (size, size)), [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buf.tobytes() if ok else b""
