"""Home Assistant for Rocky, through HA's built-in MCP Server (the "Model Context Protocol Server" integration).

Rocky's AI model gets HA's Assist tools (turn on/off, set a light, read states, ...) and calls them when someone
asks for something in the home. HA decides what's reachable: only entities exposed to Assist, as the HA user that
owns the token. Make that a non-admin user named after Rocky, so HA's logbook shows the AI did it.

The token is read from a file (default ~/beam-brain/ha.key) and never logged or printed. Who may use the home
tools is decided in rocky.py and brain.py, not here.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import aiohttp

log = logging.getLogger("beambrain.home")

DEFAULT_KEY_FILE = Path.home() / "beam-brain" / "ha.key"
MAX_RESULT_CHARS = 8000      # HA's live context of a whole home is ~20 KB; the AI only needs the start
TOOLS_TTL_S = 10 * 60        # re-read the tool list now and then (HA adds tools when integrations change)
LIST_TIMEOUT = aiohttp.ClientTimeout(total=3)   # HA answers in milliseconds; longer means it's busy or away
RETRY_S = 60                 # after HA failed to list its tools, carry on without asking it for this long


class HomeError(Exception):
    pass


class HomeAssistant:
    def __init__(self, url: str, token: str, timeout_s: float = 10):
        """url: HA's MCP endpoint, e.g. http://homeassistant.local:8123/api/mcp"""
        self.url = url
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"}
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._tools: list[dict] = []
        self._tools_at = 0.0
        self._retry_at = 0.0                 # after a failed tool list, don't ask HA again before this
        self._ids = 0

    async def _rpc(self, session: aiohttp.ClientSession, method: str, params: dict,
                   timeout: aiohttp.ClientTimeout | None = None) -> dict:
        self._ids += 1
        body = {"jsonrpc": "2.0", "id": self._ids, "method": method, "params": params}
        async with session.post(self.url, json=body, headers=self._headers, timeout=timeout or self._timeout) as r:
            text = await r.text()
            if r.status != 200:
                raise HomeError(f"HTTP {r.status}")       # not the body: it can echo the request
            if r.headers.get("Content-Type", "").startswith("text/event-stream"):
                lines = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
                text = lines[-1] if lines else ""
        data = json.loads(text)
        if "error" in data:
            raise HomeError(str(data["error"].get("message", "error"))[:200])
        return data["result"]

    async def tools(self, session: aiohttp.ClientSession) -> list[dict]:
        """HA's tools in the OpenAI function-calling format. Asked for before every reply that may use them, so a
        slow or unreachable HA mustn't hold Rocky up: a short wait, then the last list (or none) for a minute."""
        now = time.monotonic()
        if now < self._retry_at or (self._tools and now - self._tools_at <= TOOLS_TTL_S):
            if not self._tools:
                raise HomeError("not answering (will try again shortly)")
            return self._tools
        try:
            result = await self._rpc(session, "tools/list", {}, LIST_TIMEOUT)
        except Exception:
            self._retry_at = time.monotonic() + RETRY_S
            if self._tools:
                return self._tools                        # HA had them a moment ago: they're still right
            raise
        self._tools = [{"type": "function", "function": {"name": t["name"],
                                                         "description": t.get("description", ""),
                                                         "parameters": t.get("inputSchema") or {"type": "object"}}}
                       for t in result.get("tools", [])]
        self._tools_at = time.monotonic()
        return self._tools

    def knows(self, name: str) -> bool:
        return any(t["function"]["name"] == name for t in self._tools)

    async def call(self, session: aiohttp.ClientSession, name: str, args: dict) -> tuple[bool, str]:
        """Run one tool. Returns (worked, what HA said), never raises: the AI hears about failures."""
        if not self.knows(name):
            return False, f"No such tool: {name}"
        try:
            result = await self._rpc(session, "tools/call", {"name": name, "arguments": args})
        except Exception as e:
            log.warning("Home Assistant %s failed: %s", name, e)
            return False, "Home Assistant didn't answer."
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        return not result.get("isError"), text[:MAX_RESULT_CHARS]


def load_token(path: str | Path | None = None) -> str | None:
    p = Path(path) if path else DEFAULT_KEY_FILE
    try:
        return p.read_text().strip() or None
    except OSError:
        return None
