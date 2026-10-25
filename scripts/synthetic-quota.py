"""Print the Synthetic subscription quota (never prints the key).  python3 scripts/synthetic-quota.py"""

import asyncio
import json
import sys

import aiohttp

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])
from beambrain.llm import load_key  # noqa: E402


async def main() -> None:
    key = load_key()
    async with aiohttp.ClientSession() as s:
        for url in ("https://api.synthetic.new/v2/quotas", "https://api.synthetic.new/v1/quotas",
                    "https://api.synthetic.new/openai/v1/quotas"):
            async with s.get(url, headers={"Authorization": f"Bearer {key}"}) as r:
                text = await r.text()
                print(url, r.status)
                if r.status == 200:
                    try:
                        print(json.dumps(json.loads(text), indent=1))
                    except ValueError:
                        print(text[:500])
                    return


asyncio.run(main())
