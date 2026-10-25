"""Follows faces from frame to frame so names don't flicker and enrolment has several samples."""

from __future__ import annotations

import itertools
from collections import Counter, deque
from dataclasses import dataclass, field

from .faces import Face

KEEP_SAMPLES = 8           # recent embeddings per track, used when you name the face
VOTES = 5                  # recent name guesses per track
CONFIRM = 3                # a name counts once it wins this many of the recent guesses
EXPIRE_FRAMES = 6          # forget a track not seen for this many frames (~2 s at 3 fps)


@dataclass
class Track:
    id: int
    face: Face
    last_frame: int
    samples: deque = field(default_factory=lambda: deque(maxlen=KEEP_SAMPLES))
    votes: deque = field(default_factory=lambda: deque(maxlen=VOTES))
    score: float = 0.0

    @property
    def name(self) -> str | None:
        """The confirmed name, or None while unsure / unknown."""
        if not self.votes:
            return None
        name, n = Counter(self.votes).most_common(1)[0]
        return name if name is not None and n >= CONFIRM else None


def iou(a: tuple, b: tuple) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


class Tracker:
    def __init__(self) -> None:
        self.tracks: dict[int, Track] = {}
        self.frame = 0
        self._ids = itertools.count(1)

    def update(self, faces: list[Face]) -> list[Track]:
        """Match this frame's faces to existing tracks (by overlap); returns the tracks seen now."""
        self.frame += 1
        seen: list[Track] = []
        free = dict(self.tracks)
        for f in sorted(faces, key=lambda f: -f.box[2] * f.box[3]):
            best = max(free.values(), key=lambda t: iou(t.face.box, f.box), default=None)
            if best is not None and iou(best.face.box, f.box) > 0.25:
                del free[best.id]
                best.face, best.last_frame = f, self.frame
                track = best
            else:
                track = Track(next(self._ids), f, self.frame)
                self.tracks[track.id] = track
            track.samples.append(f.embedding)
            seen.append(track)
        for t in list(self.tracks.values()):
            if self.frame - t.last_frame > EXPIRE_FRAMES:
                del self.tracks[t.id]
        return seen
