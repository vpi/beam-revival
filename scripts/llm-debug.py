"""Show how a model answers at different token budgets (finish reason, content vs. reasoning).

  python3 scripts/llm-debug.py hf:Qwen/Qwen3.8-27B [more models...]
"""

import asyncio
import sys

import aiohttp

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])
from beambrain.llm import LLM, load_key  # noqa: E402


async def main(models: list[str]) -> None:
    key = load_key()
    async with aiohttp.ClientSession() as s:
        for model in models:
            for mt in (10, 1000):
                r = await LLM(model, key).chat(s, [{"role": "user", "content": "Reply with just: OK"}], max_tokens=mt)
                ch = r.raw["choices"][0]
                msg = ch["message"]
                reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
                print(f"{model} max_tokens={mt}: finish={ch.get('finish_reason')} content={msg.get('content')!r} "
                      f"reasoning_chars={len(reasoning)} usage={r.raw.get('usage')} {r.seconds:.1f}s")


asyncio.run(main(sys.argv[1:]))
