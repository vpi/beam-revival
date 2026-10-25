"""The people the robot knows: names, face embeddings and a small thumbnail. Stored locally only."""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import numpy as np

from .faces import SAME_PERSON, similarity

MAX_EMBEDDINGS = 30          # per person; more angles/lighting = better recognition


def clean_name(name: str) -> str:
    return " ".join(str(name).split())[:40]


class PeopleStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.people: dict[str, dict] = {}
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            for name, p in raw.items():
                p["embeddings"] = [np.asarray(e, dtype=np.float32) for e in p["embeddings"]]
                self.people[name] = p

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {n: {**p, "embeddings": [e.tolist() for e in p["embeddings"]]} for n, p in self.people.items()}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.chmod(tmp, 0o600)                 # face data: owner-only
        tmp.replace(self.path)

    def add(self, name: str, embeddings: list[np.ndarray], thumb_jpeg: bytes | None = None) -> str:
        name = clean_name(name)
        if not name or not embeddings:
            raise ValueError("need a name and at least one face")
        p = self.people.setdefault(name, {"embeddings": [], "thumb": None, "added": time.time()})
        p["embeddings"] = (p["embeddings"] + list(embeddings))[-MAX_EMBEDDINGS:]
        if thumb_jpeg:
            p["thumb"] = base64.b64encode(thumb_jpeg).decode()
        self.save()
        return name

    def forget(self, name: str) -> bool:
        if self.people.pop(clean_name(name), None) is None:
            return False
        self.save()
        return True

    def match(self, emb: np.ndarray, threshold: float = SAME_PERSON) -> tuple[str | None, float]:
        best_name, best = None, -1.0
        for name, p in self.people.items():
            for e in p["embeddings"]:
                s = similarity(emb, e)
                if s > best:
                    best_name, best = name, s
        return (best_name, best) if best >= threshold else (None, best)

    def summary(self) -> list[dict]:
        return [{"name": n, "samples": len(p["embeddings"]), "thumb": p.get("thumb")}
                for n, p in sorted(self.people.items())]
