"""Minimal OpenAI-compatible chat client (Synthetic, or any similar provider) for the brain.

Uses aiohttp only. The API key is read from a file (default ~/beam-brain/ai.key) or the
SYNTHETIC_API_KEY environment variable, and is never logged or printed.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

DEFAULT_BASE = "https://api.synthetic.new/openai/v1"
DEFAULT_KEY_FILE = Path.home() / "beam-brain" / "ai.key"


def load_key(path: str | Path | None = None) -> str:
    key = os.environ.get("SYNTHETIC_API_KEY", "").strip()
    if key:
        return key
    p = Path(path) if path else DEFAULT_KEY_FILE
    try:
        key = p.read_text().strip()
    except OSError:
        raise SystemExit(f"No API key: put it in {p} (chmod 600) or set SYNTHETIC_API_KEY") from None
    if not key:
        raise SystemExit(f"{p} is empty")
    return key


def image_part(jpeg: bytes) -> dict:
    """An OpenAI-style image content part (data URL)."""
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}}


@dataclass
class Reply:
    text: str
    tool_calls: list[dict] = field(default_factory=list)    # [{"name": .., "arguments": {..}}]
    seconds: float = 0.0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    raw: dict = field(default_factory=dict, repr=False)


class APIError(Exception):
    pass


class LLM:
    def __init__(self, model: str, key: str, base: str = DEFAULT_BASE, timeout_s: float = 60):
        self.model, self.base = model, base.rstrip("/")
        self._headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)

    async def chat(self, session: aiohttp.ClientSession, messages: list[dict], tools: list[dict] | None = None,
                   max_tokens: int = 400, temperature: float = 0.2) -> Reply:
        body: dict = {"model": self.model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        t0 = time.monotonic()
        async with session.post(f"{self.base}/chat/completions", json=body, headers=self._headers,
                                timeout=self._timeout) as r:
            text = await r.text()
            if r.status != 200:
                raise APIError(f"HTTP {r.status}: {text[:300]}")
        data = json.loads(text)
        msg = data["choices"][0]["message"]
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {"_unparsed": fn.get("arguments")}
            calls.append({"name": fn.get("name"), "arguments": args})
        usage = data.get("usage") or {}
        return Reply(msg.get("content") or "", calls, time.monotonic() - t0,
                     usage.get("prompt_tokens"), usage.get("completion_tokens"), data)

    async def models(self, session: aiohttp.ClientSession) -> list[str]:
        async with session.get(f"{self.base}/models", headers=self._headers, timeout=self._timeout) as r:
            text = await r.text()
            if r.status != 200:
                raise APIError(f"HTTP {r.status}: {text[:300]}")
        return sorted(m["id"] for m in json.loads(text).get("data", []))
