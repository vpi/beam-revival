"""Print the step-by-step log of bake-off trials.

  python3 scripts/bakeoff-log.py [results.json] [--task kitchen] [--model GLM]
Defaults to the newest run in ~/beam-brain/bakeoff/.
"""

import argparse
import json
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("results", nargs="?")
ap.add_argument("--task")
ap.add_argument("--model")
a = ap.parse_args()
path = Path(a.results) if a.results else sorted((Path.home() / "beam-brain" / "bakeoff").glob("*/results.json"))[-1]
def model_matches(want: str | None, model: str) -> bool:
    if not want:
        return True
    if want.startswith("="):                      # exact model name, e.g. =DeepSeek-V4.1-Flash
        return model.split("/")[-1].lower() == want[1:].lower()
    return want.lower() in model.lower()


for r in json.loads(path.read_text()):
    if (a.task and r["task"] != a.task) or not model_matches(a.model, r["model"]):
        continue
    print(f"=== {r['model'].split('/')[-1]} / {r['task']}: {r['outcome']} — {r.get('check')}")
    if r.get("model_said"):
        print(f"    model said: {r['model_said'][:120]}")
    for s in r.get("log", []):
        args = {k: v for k, v in s["args"].items() if k != "why"}
        print(f"  {s['n']:2d} {str(s['tool']):6s} {json.dumps(args)[:28]:28s} "
              f"why: {str(s['args'].get('why', ''))[:58]:58s} -> {s['result'][:42]} ({s['seconds_thinking']:.0f}s"
              f"{', ' + s['finish'] if s.get('finish') and s['finish'] not in ('stop', 'tool_calls') else ''})")
