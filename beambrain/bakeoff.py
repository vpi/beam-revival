"""Driving bake-off: the same tasks in the virtual house for several models, scored automatically.

  python3 -m beambrain.bakeoff [--models hf:a hf:b] [--tasks ball kitchen bed] [--runs 1] [--parallel 2]

Everything runs in one process per trial and uses the real code paths: simulated motor board in the
virtual house -> motor server -> a stand-in for the robot page that streams the head camera -> the
brain's AI driver -> the model on Synthetic. Results go to ~/beam-brain/bakeoff/<timestamp>/
(results.json, report.md and a map of each trial's path).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import socket
import time
from dataclasses import asdict
from pathlib import Path

import aiohttp
from aiohttp import web

from beamctl.server import Config, MotorController, build_app
from beamctl.sim import SimulatedBoard
from beamctl.world import START, World

from .brain import Brain
from .driver import AIDriver, Outcome
from .greeter import Greeter
from .llm import LLM, load_key
from .people import PeopleStore

MODELS = ["hf:deepseek-ai/DeepSeek-V4.1-Flash", "hf:deepseek-ai/DeepSeek-V4.1-Flash-1017",
          "hf:Qwen/Qwen3.8-27B", "hf:zai-org/GLM-5.3-Flash"]
# Per 1M tokens (input, output), from the Synthetic pricing page, for cost estimates.
PRICES = {"hf:deepseek-ai/DeepSeek-V4.1-Flash": (0.06, 0.30), "hf:deepseek-ai/DeepSeek-V4.1-Flash-1017": (0.06, 0.30),
          "hf:Qwen/Qwen3.8-27B": (0.23, 1.10), "hf:zai-org/GLM-5.3-Flash": (0.10, 0.25)}


def _thing(world: World, name: str):
    return next(t for t in world.things if t.name == name)


def _near_facing(board: SimulatedBoard, world: World, name: str, max_dist: float, max_bearing: float) -> tuple[bool, str]:
    t = _thing(world, name)
    # distance to the object's footprint edge, not its centre
    dx = max(abs(board.x - t.x) - t.w / 2, 0)
    dy = max(abs(board.y - t.y) - max(t.w, 0.3) / 2, 0)
    dist = math.hypot(dx, dy)
    bearing = math.degrees(math.atan2(t.y - board.y, t.x - board.x) - board.heading)
    bearing = (bearing + 180) % 360 - 180
    ok = dist <= max_dist and abs(bearing) <= max_bearing
    return ok, f"{dist:.2f} m from the {name.lower()}, bearing {bearing:.0f}°"


TASKS = {
    "ball": {
        "prompt": "Find the red ball and stop in front of it (about 1 meter away, facing it). Then call done.",
        "check": lambda b, w: _near_facing(b, w, "RED BALL", 1.3, 30),
    },
    "kitchen": {
        "prompt": "Go to the kitchen. Call done once you are inside the kitchen.",
        "check": lambda b, w: (w.room_at(b.x, b.y) == "kitchen", f"ended in the {w.room_at(b.x, b.y)}"),
    },
    "bed": {
        "prompt": "Find the bed and stop next to it (about 1 meter away, facing it). Then call done.",
        "check": lambda b, w: _near_facing(b, w, "BED", 1.3, 40),
    },
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Rig:
    """One isolated virtual robot: sim in the house + motor server + camera feed + brain."""

    def __init__(self, start=START):
        self.world = World()
        self.board = SimulatedBoard(world=self.world, start=start)
        self.port = _free_port()
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()

    async def __aenter__(self) -> "Rig":
        path = await self.board.start()
        self.ctrl = MotorController(Config(device=path, brain_key="bakeoff-key-123456", max_linear=0.6))
        self.runner = web.AppRunner(build_app(self.ctrl), access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        self.session = aiohttp.ClientSession()
        self.brain = Brain(None, PeopleStore(Path("/tmp") / f"bakeoff-people-{self.port}.json"), None, Greeter())
        url = f"ws://127.0.0.1:{self.port}/ws?role=brain&key=bakeoff-key-123456"
        self._tasks.append(asyncio.create_task(self.brain.run(url, self._stop)))
        self._tasks.append(asyncio.create_task(self._camera_feed()))
        for _ in range(50):                                  # wait for telemetry and a first frame
            if self.brain.telemetry and self.brain.last_jpeg:
                break
            await asyncio.sleep(0.1)
        return self

    async def _camera_feed(self) -> None:
        """Stands in for the robot page: streams the head camera (3 fps) when the server asks for frames."""
        fps = 0.0
        async with self.session.ws_connect(f"ws://127.0.0.1:{self.port}/ws?role=robot", max_msg_size=8 * 2**20) as ws:
            async def receive():
                nonlocal fps
                async for msg in ws:
                    m = json.loads(msg.data)
                    if m.get("type") == "frames":
                        fps = float(m["fps"])
            rx = asyncio.create_task(receive())
            try:
                import base64
                while not self._stop.is_set():
                    if fps > 0:
                        for cam, (w, h) in (("head", (640, 480)), ("nav", (320, 240))):
                            jpeg = await asyncio.to_thread(self.board.camera_jpeg, cam)
                            await ws.send_str(json.dumps({"type": "frame", "cam": cam, "w": w, "h": h,
                                                          "jpeg": base64.b64encode(jpeg).decode()}, separators=(",", ":")))
                    await asyncio.sleep(1 / max(fps, 3))
            finally:
                rx.cancel()

    async def __aexit__(self, *exc) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.session.close()
        await self.runner.cleanup()
        await self.board.stop()


async def run_trial(model: str, task: str, key: str, out_dir: Path, run: int, max_steps: int,
                    guides: bool = False, big_actions: bool = False) -> dict:
    async with Rig() as rig:
        llm = LLM(model, key, timeout_s=180)      # thinking models can take over a minute on hard steps
        driver = AIDriver(rig.brain, llm, TASKS[task]["prompt"], max_steps=max_steps, allow_say=False,
                          guides=guides, big_actions=big_actions)
        rig.brain.driver = driver
        outcome: Outcome = await driver.run(rig.session)
        ok, detail = TASKS[task]["check"](rig.board, rig.world)
        success = ok and outcome.outcome == "done"
        name = f"{model.split('/')[-1]}_{task}_{run}"
        try:
            import cv2
            img = rig.world.map_image(rig.board.x, rig.board.y, rig.board.heading, trail=rig.board.trail, scale=30)
            cv2.putText(img, f"{model.split('/')[-1]} / {task}: {'SUCCESS' if success else 'FAIL'}", (10, 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 140, 0) if success else (0, 0, 200), 2)
            cv2.imwrite(str(out_dir / f"{name}.png"), img)
        except Exception:
            pass
        price_in, price_out = PRICES.get(model, (0, 0))
        return {
            "model": model, "task": task, "run": run, "success": success, "guides": guides, "big_actions": big_actions,
            "outcome": outcome.outcome, "model_said": outcome.summary, "check": detail,
            "steps": len(outcome.steps), "requests": outcome.requests, "seconds": round(outcome.seconds, 1),
            "bumps": rig.board.bumps, "blocked_moves": outcome.blocked_moves,
            "tokens_in": outcome.tokens_in, "tokens_out": outcome.tokens_out,
            "cost_usd": round(outcome.tokens_in / 1e6 * price_in + outcome.tokens_out / 1e6 * price_out, 5),
            "think_s_median": round(sorted(s.seconds_thinking for s in outcome.steps)[len(outcome.steps) // 2], 2)
            if outcome.steps else None,
            "no_action_steps": sum(s.tool is None for s in outcome.steps),
            "cut_off_steps": sum(s.tool is None and s.finish == "length" for s in outcome.steps),
            "text_calls_recovered": sum(s.recovered for s in outcome.steps),
            "log": [asdict(s) for s in outcome.steps],
            "map": f"{name}.png",
        }


async def quota(key: str) -> dict:
    async with aiohttp.ClientSession() as s, s.get("https://api.synthetic.new/v2/quotas",
                                                   headers={"Authorization": f"Bearer {key}"}) as r:
        return (await r.json())["subscription"]


async def bakeoff(models, tasks, runs, parallel, max_steps, key, guides: bool = False,
                  big_actions: bool = False) -> Path:
    out_dir = Path.home() / "beam-brain" / "bakeoff" / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True)
    trials = [(m, t, r) for r in range(1, runs + 1) for t in tasks for m in models]
    q = await quota(key)
    need = len(trials) * max_steps
    print(f"{len(trials)} trials, up to {need} requests; quota {q['requests']}/{q['limit']} used")
    if q["limit"] - q["requests"] < need:
        print("WARNING: may not fit in the current quota window; trials stop if the quota runs low")
    sem = asyncio.Semaphore(parallel)
    results = []

    async def one(m, t, r):
        async with sem:
            q = await quota(key)
            if q["limit"] - q["requests"] < max_steps + 10:
                print(f"skip {m} {t} #{r}: quota low ({q['requests']}/{q['limit']})")
                return
            t0 = time.time()
            try:
                res = await run_trial(m, t, key, out_dir, r, max_steps, guides, big_actions)
            except Exception as e:
                res = {"model": m, "task": t, "run": r, "success": False, "outcome": "harness_error", "check": str(e)}
            results.append(res)
            (out_dir / "results.json").write_text(json.dumps(results, indent=1))
            print(f"{'PASS' if res['success'] else 'fail'}  {m.split('/')[-1]:28s} {t:8s} #{r}  "
                  f"{res.get('outcome')}, {res.get('steps')} steps, {res.get('seconds')} s, bumps {res.get('bumps')} "
                  f"— {res.get('check')}  ({time.time() - t0:.0f} s)", flush=True)

    await asyncio.gather(*(one(m, t, r) for m, t, r in trials))
    (out_dir / "report.md").write_text(report(results, models, tasks))
    return out_dir


def report(results: list[dict], models: list[str], tasks: list[str]) -> str:
    lines = ["| model | " + " | ".join(tasks) + " | success | avg steps | avg time | bumps | no-action steps | cost |",
             "|---|" + "---|" * (len(tasks) + 6)]
    for m in models:
        rs = [r for r in results if r["model"] == m]
        if not rs:
            continue
        cells = []
        for t in tasks:
            tr = [r for r in rs if r["task"] == t]
            cells.append(" ".join("✅" if r["success"] else "❌" for r in tr) or "—")
        ok = [r for r in rs if r.get("steps") is not None]
        n = max(1, len(ok))
        lines.append(f"| {m.split('/')[-1]} | " + " | ".join(cells) +
                     f" | {sum(r['success'] for r in rs)}/{len(rs)} | {sum(r['steps'] for r in ok) / n:.1f} | "
                     f"{sum(r['seconds'] for r in ok) / n:.0f} s | {sum(r['bumps'] for r in ok)} | "
                     f"{sum(r.get('no_action_steps', 0) for r in ok)} (cut off {sum(r.get('cut_off_steps', 0) for r in ok)}) | "
                     f"${sum(r.get('cost_usd', 0) for r in ok):.4f} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="AI driving bake-off in the virtual house")
    ap.add_argument("--models", nargs="*", default=MODELS)
    ap.add_argument("--tasks", nargs="*", default=list(TASKS))
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--parallel", type=int, default=2)
    ap.add_argument("--max-steps", type=int, default=20)
    ap.add_argument("--guides", action="store_true", help="add angle/distance marks and the breadcrumb map")
    ap.add_argument("--big-actions", action="store_true", help="add approach() and go_through_doorway()")
    args = ap.parse_args()
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    out = asyncio.run(bakeoff(args.models, args.tasks, args.runs, args.parallel, args.max_steps, load_key(),
                              guides=args.guides, big_actions=args.big_actions))
    print("\n" + (out / "report.md").read_text())
    print(f"results in {out}")


if __name__ == "__main__":
    main()
