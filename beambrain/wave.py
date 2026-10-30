"""Rocky step 4: a wave wakes him, like "hey Rocky". Runs on the brain; no picture goes anywhere.

At 3 frames a second a waving hand can't be followed, but it lands somewhere new in every frame. So a wave is:
change beside a face (where a raised hand is) in most of the last few frames, while the person stays where they
are (walking up to him moves the face) and the rest of the picture doesn't change (the robot turning, or a light
switched on, changes all of it). A hand in front of the face often hides it from the face finder for a moment, so
the face's last place is used for up to a second, but at least one of those frames must show the face (someone
walking close by, face turned away, also changes a lot beside where their face was).
"""

from __future__ import annotations

from collections import deque

import cv2
import numpy as np

WIDTH = 160            # frames are compared at this width: plenty for a hand, cheap
PIXEL_CHANGE = 25      # grey levels a pixel must change by to count as moving
ZONE_MOVING = 0.05     # share of a hand zone that must be moving (a wave from a few metres moves ~5-7 %)
SCENE_MOVING = 0.35    # beyond this share of the whole picture moving, it's the camera or the light, not a hand
FACE_MOVED = 0.15      # the face moved this many face-widths, or changed size this much: the person is moving
FACE_HOLD_S = 1.0      # a face hidden (by the hand) for up to this long is taken to be where it was
FRAMES = 5             # look at this many frame-to-frame changes...
NEEDED = 3             # ...and want a moving hand zone in this many
MAX_GAP_S = 1.0        # frames further apart than this aren't compared
COOLDOWN_S = 10.0      # one wave, one wake
MISS, HIDDEN, SEEN = 0, 1, 2   # a frame: no hand moving; a hand moving beside a hidden face; ...beside a seen one


class WaveDetector:
    def __init__(self) -> None:
        self.prev: np.ndarray | None = None
        self.prev_t = 0.0
        self.face: tuple[int, int, int, int] | None = None   # the nearest face, full-frame pixels
        self.face_t = 0.0
        self.hits: deque = deque(maxlen=FRAMES)
        self.last_wave = -COOLDOWN_S
        self.last_score = self.last_scene = self.last_moved = 0.0   # the latest measurements, for tuning

    def reset(self) -> None:
        self.prev = self.face = None
        self.hits.clear()

    def _person_moved(self, face: tuple[int, int, int, int]) -> float:
        if self.face is None:
            return 0.0
        (x, y, w, h), (px, py, pw, ph) = face, self.face
        shift = np.hypot(x + w / 2 - px - pw / 2, y + h / 2 - py - ph / 2) / max(pw, 1)
        return max(shift, abs(w - pw) / max(pw, 1))

    def update(self, img: np.ndarray, faces: list[tuple[int, int, int, int]], now: float) -> bool:
        """img: a BGR head-camera frame; faces: their boxes (x, y, w, h) in its pixels. True once per wave."""
        scale = WIDTH / img.shape[1]
        grey = cv2.GaussianBlur(cv2.cvtColor(cv2.resize(img, None, fx=scale, fy=scale), cv2.COLOR_BGR2GRAY), (5, 5), 0)
        prev, prev_t = self.prev, self.prev_t
        self.prev, self.prev_t = grey, now
        self.last_moved = 0.0
        if faces:
            face = max(faces, key=lambda b: b[2] * b[3])          # the nearest person
            self.last_moved = self._person_moved(face)
            self.face, self.face_t = face, now
        elif self.face is not None and now - self.face_t <= FACE_HOLD_S:
            face = self.face                                     # hidden behind the hand, probably
        else:
            face = self.face = None
        if prev is None or prev.shape != grey.shape or now - prev_t > MAX_GAP_S:
            self.hits.clear()
            return False
        moving = cv2.absdiff(grey, prev) > PIXEL_CHANGE
        self.last_scene = float(moving.mean())
        self.last_score = 0.0
        if self.last_moved > FACE_MOVED:
            self.hits.clear()                                    # walking about: start again once they stop
            return False
        if self.last_scene > SCENE_MOVING or face is None:
            self.hits.append(MISS)
            return False
        x, y, w, h = (int(v * scale) for v in face)
        top, bottom = max(0, int(y - 1.5 * h)), y + 2 * h     # a raised hand: from above the head to the shoulders
        zones = [moving[top:bottom, max(0, x - 2 * w):max(0, x - w // 4)],        # beside the face, either side
                 moving[top:bottom, x + w + w // 4:x + 3 * w]]
        self.last_score = max((float(z.mean()) for z in zones if z.size), default=0.0)
        self.hits.append(MISS if self.last_score <= ZONE_MOVING else SEEN if faces else HIDDEN)
        # At least one hit with the face in sight and still: a person walking close, face turned away, isn't a wave.
        if (sum(h != MISS for h in self.hits) >= NEEDED and SEEN in self.hits
                and now - self.last_wave >= COOLDOWN_S):
            self.last_wave = now
            self.hits.clear()
            return True
        return False
