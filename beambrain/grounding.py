"""Ask a vision model where something is in an image, as a box. Used by the driver's big actions,
and by `python3 -m beambrain.grounding` to measure how accurately each model draws boxes.

Boxes are fractions of the image (0..1, x from the left, y from the top), which every model can
produce from a plain-language request; we don't rely on any model-specific grounding format.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import re
import statistics
from dataclasses import dataclass

import aiohttp

from .llm import LLM, APIError, image_part, load_key

LOCATE_PROMPT = """Find {target} in this camera image.
Reply with ONLY a JSON object, no other text:
{{"found": true, "box": [x1, y1, x2, y2]}}
where the box tightly encloses it, as fractions of the image width and height (0 to 1; x from the left
edge, y from the top edge). If it is not visible, reply {{"found": false}}."""


@dataclass
class Box:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def bottom(self) -> float:
        return self.y1


def parse_box(text: str) -> tuple[bool, Box | None]:
    """Parse the model's reply. Accepts fractions 0..1, or 0..1000 style coordinates."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return False, None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        nums = re.findall(r"-?\d+(?:\.\d+)?", m.group(0))
        data = {"found": "false" not in m.group(0).lower(), "box": [float(n) for n in nums[:4]]}
    if not data.get("found") or not data.get("box") or len(data["box"]) != 4:
        return False, None
    b = [float(v) for v in data["box"]]
    scale = 1000.0 if max(b) > 1.5 else 1.0
    b = [min(1.0, max(0.0, v / scale)) for v in b]
    x0, x1 = sorted((b[0], b[2]))
    y0, y1 = sorted((b[1], b[3]))
    return True, Box(x0, y0, x1, y1)


async def locate(llm: LLM, session: aiohttp.ClientSession, jpeg_b64: str, target: str):
    """Returns (found, box, reply); the reply carries timing and token counts."""
    r = await llm.chat(session, [{"role": "user", "content": [
        {"type": "text", "text": LOCATE_PROMPT.format(target=target)},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + jpeg_b64}}]}], max_tokens=8000)
    found, box = parse_box(r.text)
    return found, box, r


# --- accuracy probe ------------------------------------------------------------

def _views():
    from beamctl.world import World
    w = World()
    th = {t.name: t for t in w.things}
    # (description, thing, pose): each target clearly in view, at a range of distances and angles.
    return w, [
        ("the red ball", th["RED BALL"], (6.0, 4.5, math.radians(10))),
        ("the red ball", th["RED BALL"], (7.5, 4.0, math.radians(45))),
        ("the red ball", th["RED BALL"], (8.5, 4.6, math.radians(45))),
        ("the bed", th["BED"], (2.5, 2.5, math.radians(-115))),
        ("the fridge", th["FRIDGE"], (7.0, 1.6, math.radians(-10))),
        ("the couch", th["COUCH"], (6.5, 4.8, math.radians(-25))),
    ]


async def probe(models: list[str], key: str, runs: int = 2) -> None:
    from beamctl.world import World  # noqa: F401
    w, views = _views()
    async with aiohttp.ClientSession() as s:
        for model in models:
            llm = LLM(model, key, timeout_s=180)
            errs, misses, secs = [], 0, []
            for desc, thing, (x, y, h) in views:
                gt = w.project(thing, x, y, h)
                jpeg = base64.b64encode(w.camera_jpeg(x, y, h)).decode()
                for _ in range(runs):
                    try:
                        found, box, reply = await locate(llm, s, jpeg, desc)
                    except (APIError, aiohttp.ClientError, asyncio.TimeoutError):
                        misses += 1
                        continue
                    secs.append(reply.seconds)
                    if not found or box is None:
                        misses += 1
                        continue
                    # Error in what matters for driving: horizontal centre (as degrees) and bottom edge.
                    fov = 68.0
                    cx_gt = (gt[0] + gt[2]) / 2 / 640
                    deg_err = abs(math.degrees(math.atan((box.cx - 0.5) * 2 * math.tan(math.radians(fov / 2))))
                                  - math.degrees(math.atan((cx_gt - 0.5) * 2 * math.tan(math.radians(fov / 2)))))
                    bottom_err = abs(box.bottom - gt[3] / 480)
                    errs.append((deg_err, bottom_err))
            n = len(views) * runs
            if errs:
                print(f"{model.split('/')[-1]:28s} found {n - misses}/{n}  "
                      f"angle error median {statistics.median(e[0] for e in errs):4.1f}° (worst {max(e[0] for e in errs):4.1f}°)  "
                      f"bottom-edge error median {statistics.median(e[1] for e in errs) * 100:4.1f}% of height  "
                      f"{statistics.median(secs):4.1f} s/answer", flush=True)
            else:
                print(f"{model.split('/')[-1]:28s} found 0/{n}", flush=True)


def main() -> None:
    from .bakeoff import MODELS
    ap = argparse.ArgumentParser(description="How accurately do models draw boxes around things?")
    ap.add_argument("--models", nargs="*", default=MODELS)
    ap.add_argument("--runs", type=int, default=2)
    a = ap.parse_args()
    asyncio.run(probe(a.models, load_key(), a.runs))


if __name__ == "__main__":
    main()
