"""Compare hosted models for robot use: can they see, judge left/right, call tools, and how fast?

  python3 -m beambrain.probe [--models hf:a hf:b ...] [--runs 3]

Results are printed as a table and saved to ~/beam-brain/probe-results.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from pathlib import Path

import aiohttp
import cv2
import numpy as np

from .llm import LLM, APIError, image_part, load_key

CANDIDATES = [
    "hf:Qwen/Qwen3.8-27B",
    "hf:deepseek-ai/DeepSeek-V4.1-Flash",
    "hf:deepseek-ai/DeepSeek-V4.1-Flash-1017",
    "hf:zai-org/GLM-5.3-Flash",
    "hf:moonshotai/Kimi-K3",
    "syn:small:vision",
    "syn:large:vision",
    "infinite:small:vision",
]

ROBOT_TOOLS = [
    {"type": "function", "function": {
        "name": "turn", "description": "Turn the robot in place. Positive degrees = turn LEFT, negative = turn RIGHT.",
        "parameters": {"type": "object", "properties": {"degrees": {"type": "number"}}, "required": ["degrees"]}}},
    {"type": "function", "function": {
        "name": "drive", "description": "Drive straight. Positive meters = forward, negative = backward.",
        "parameters": {"type": "object", "properties": {"meters": {"type": "number"}}, "required": ["meters"]}}},
    {"type": "function", "function": {
        "name": "say", "description": "Say something out loud through the robot's speaker.",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}},
]


def scene(ball_side: str) -> bytes:
    """A simple room as the robot's camera would see it: floor, wall, a red ball on one side."""
    img = np.full((480, 640, 3), (200, 205, 210), np.uint8)              # wall
    cv2.rectangle(img, (0, 300), (640, 480), (90, 110, 140), -1)          # floor
    cv2.rectangle(img, (250, 120), (390, 300), (60, 80, 120), -1)          # a door straight ahead
    x = 120 if ball_side == "left" else 520
    cv2.circle(img, (x, 360), 45, (30, 30, 220), -1)                       # red ball (BGR)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes()


def kitchen_view() -> bytes:
    """The virtual house's kitchen as the head camera sees it (fridge and stove in view)."""
    import math
    from beamctl.world import World
    return World().camera_jpeg(7.0, 1.6, math.radians(-10))


# Generous: these models think before answering, and the thinking counts against max_tokens.
# A small budget gets cut off mid-thought with an empty answer (finish_reason "length").
BUDGET = 2000


def _result(r, ok: bool, detail: str) -> dict:
    return {"ok": ok, "seconds": r.seconds, "detail": detail, "in": r.prompt_tokens,
            "out": r.completion_tokens, "thinking": (r.raw.get("usage") or {}).get("reasoning_tokens"),
            "finish": r.raw["choices"][0].get("finish_reason")}


async def run_test(llm: LLM, session, name: str) -> dict:
    """One test; returns {"ok": bool, "seconds": float, "detail": str, tokens...}."""
    if name == "ping":
        r = await llm.chat(session, [{"role": "user", "content": "Reply with just: OK"}], max_tokens=BUDGET)
        return _result(r, "ok" in r.text.lower(), r.text.strip()[:60])
    if name == "describe":
        r = await llm.chat(session, [{"role": "user", "content": [
            {"type": "text", "text": "In one short sentence: what room is this robot camera looking at, and what's in it?"},
            image_part(kitchen_view())]}], max_tokens=BUDGET)
        ok = any(w in r.text.lower() for w in ("kitchen", "fridge", "stove", "refrigerator"))
        return _result(r, ok, r.text.strip()[:90])
    if name == "left_right":
        side = "left"
        r = await llm.chat(session, [{"role": "user", "content": [
            {"type": "text", "text": "This is a robot's camera view. Is the red ball on the left or the right side "
                                     "of the image? Answer with one word: left or right."},
            image_part(scene(side))]}], max_tokens=BUDGET)
        ans = r.text.strip().lower()
        return _result(r, side in ans and "right" not in ans, ans[:30])
    if name == "tool_call":
        side = "right"
        r = await llm.chat(session, [
            {"role": "system", "content": "You control a small home robot. Each turn you see its camera and call "
                                          "exactly one tool. Turning: positive degrees = left, negative = right."},
            {"role": "user", "content": [
                {"type": "text", "text": "Task: get to the red ball. Here is the current camera view. "
                                         "Call the single best next tool."},
                image_part(scene(side))]}], tools=ROBOT_TOOLS, max_tokens=BUDGET)
        call = r.tool_calls[0] if r.tool_calls else None
        ok = bool(call and call["name"] == "turn" and float(call["arguments"].get("degrees", 0)) < 0)
        detail = f"{call['name']}({json.dumps(call['arguments'])})" if call else f"no tool call: {r.text.strip()[:60]}"
        return _result(r, ok, detail)
    raise ValueError(name)


TESTS = ["ping", "describe", "left_right", "tool_call"]


async def probe(models: list[str], runs: int, key: str) -> dict:
    results: dict = {}
    async with aiohttp.ClientSession() as session:
        listed = await LLM("", key).models(session)
        print(f"Synthetic lists {len(listed)} models:")
        for m in listed:
            print("  ", m)
        for model in models:
            if not model.startswith("syn:") and model not in listed:
                print(f"\n{model}: not listed, skipping")
                continue
            print(f"\n=== {model}")
            llm = LLM(model, key)
            results[model] = {}
            for test in TESTS:
                rows = []
                for _ in range(runs):
                    try:
                        rows.append(await run_test(llm, session, test))
                    except (APIError, aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError) as e:
                        rows.append({"ok": False, "seconds": None, "detail": f"error: {str(e)[:120]}"})
                passed = sum(r["ok"] for r in rows)
                secs = [r["seconds"] for r in rows if r["seconds"] is not None]
                med = statistics.median(secs) if secs else None
                think = [r.get("thinking") or 0 for r in rows if r.get("seconds") is not None]
                med_think = int(statistics.median(think)) if think else None
                results[model][test] = {"passed": passed, "runs": runs, "median_s": med,
                                        "median_thinking_tokens": med_think, "examples": rows}
                fin = ",".join(sorted({str(r.get("finish")) for r in rows}))
                print(f"  {test:10s} {passed}/{runs}  {('%.1f s' % med) if med else '  —  '}  "
                      f"think~{med_think}  [{fin}]  e.g. {rows[0]['detail']}")
    return results


def table(results: dict) -> str:
    lines = [f"{'model':42s} " + " ".join(f"{t:>11s}" for t in TESTS)]
    for model, res in results.items():
        cells = []
        for t in TESTS:
            r = res[t]
            cells.append(f"{r['passed']}/{r['runs']} {('%4.1fs' % r['median_s']) if r['median_s'] else '   — '}")
        lines.append(f"{model:42s} " + " ".join(f"{c:>11s}" for c in cells))
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Compare hosted models for the robot brain")
    ap.add_argument("--models", nargs="*", default=CANDIDATES)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--key-file")
    args = ap.parse_args()
    results = asyncio.run(probe(args.models, args.runs, load_key(args.key_file)))
    print("\n" + table(results))
    out = Path.home() / "beam-brain" / "probe-results.json"
    out.write_text(json.dumps(results, indent=1))
    print(f"\nsaved {out}")


if __name__ == "__main__":
    main()
