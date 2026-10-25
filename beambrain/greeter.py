"""When to say hello: once per person per cooldown, only for confirmed names."""

from __future__ import annotations

import random
import time

PHRASES = ["Hi {name}!", "Hello {name}!", "Hey {name}, nice to see you!", "Good to see you, {name}!"]


class Greeter:
    def __init__(self, cooldown_s: float = 30 * 60, phrases: list[str] | None = None, rng: random.Random | None = None):
        self.cooldown_s = cooldown_s
        self.phrases = phrases or PHRASES
        self.last: dict[str, float] = {}
        self.rng = rng or random.Random()

    def maybe_greet(self, name: str, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        if now - self.last.get(name, float("-inf")) < self.cooldown_s:
            return None
        self.last[name] = now
        return self.rng.choice(self.phrases).format(name=name)
