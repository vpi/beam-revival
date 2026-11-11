"""Named places on the floor map: "kitchen", "watch TV"... A place is a spot and the way to face there, saved from
where the robot stands (the driving page's Map card), with one or more names. The dock is always a place
("dock", "charger" unless renamed): going there means docking.

Kept with the map on the brain only (~/beam-brain/maps/places.json): it's part of the floor plan of a home. The map's
origin is the docked pose, so places stay put when the map is made again, as long as the dock doesn't move.

A place may name the Home Assistant presence sensors in its room (e.g. an mmWave radar's occupancy sensor): the
brain then knows which places someone is at now (brain.py), shows it on the driving page and tells Rocky.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

DOCK = "dock"
DOCK_NAMES = ["dock", "charger"]
MAX_NAMES, MAX_NAME_LEN, MAX_PLACES, MAX_SENSORS = 6, 40, 50, 4
SENSOR_ID = re.compile(r"^binary_sensor\.[a-z0-9_]{1,120}$")


def clean_names(text) -> list[str]:
    """"Living room, lounge" -> ["living room", "lounge"]: lower case, single spaces, no duplicates."""
    parts = text if isinstance(text, list) else re.split(r"[,;\n]", str(text or ""))
    out = []
    for p in parts:
        n = " ".join(re.sub(r"[^\w' -]", " ", str(p)).lower().split())[:MAX_NAME_LEN]
        if n and n not in out:
            out.append(n)
    return out[:MAX_NAMES]


class Places:
    def __init__(self, path: Path | None):
        self.path = path
        self.dock_names = list(DOCK_NAMES)
        self.items: list[dict] = []                   # {"id", "names", "x", "y", "th"} (metres, radians)
        if path is not None and path.exists():
            try:
                d = json.loads(path.read_text())
                self.dock_names = clean_names(d.get("dock_names")) or list(DOCK_NAMES)
                self.items = [p for p in d.get("places", []) if clean_names(p.get("names"))]
            except (OSError, ValueError, AttributeError):
                pass

    def save(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"dock_names": self.dock_names, "places": self.items}, indent=1) + "\n")

    def _taken(self, names: list[str], but: str | None = None) -> str | None:
        for p in self.summary():
            if p["id"] != but:
                for n in names:
                    if n in p["names"]:
                        return n
        return None

    def add(self, names, pose) -> dict | str:
        """A new place where the robot stands; or why not."""
        names = clean_names(names)
        if not names:
            return "Give the place a name."
        if len(self.items) >= MAX_PLACES:
            return "Too many places: delete one first."
        if (n := self._taken(names)):
            return f'"{n}" is already a place\'s name.'
        p = {"id": uuid.uuid4().hex[:8], "names": names,
             "x": round(pose[0], 3), "y": round(pose[1], 3), "th": round(pose[2], 4)}
        self.items.append(p)
        self.save()
        return p

    def rename(self, pid: str, names) -> str | None:
        names = clean_names(names)
        if not names:
            return "A place needs at least one name."
        if (n := self._taken(names, pid)):
            return f'"{n}" is already a place\'s name.'
        if pid == DOCK:
            self.dock_names = names
        else:
            p = next((p for p in self.items if p["id"] == pid), None)
            if p is None:
                return "No such place."
            p["names"] = names
        self.save()
        return None

    def set_sensors(self, pid: str, text) -> str | None:
        """The Home Assistant presence sensors in a place's room ("binary_sensor.kitchen_presence, ..."), so the
        brain knows when someone is there. Empty: none. Returns why not, or None."""
        p = next((p for p in self.items if p["id"] == pid), None)
        if p is None:
            return "Only a saved place (not the charger) can have presence sensors."
        ids = list(dict.fromkeys(s.strip().lower() for s in re.split(r"[,;\s]+", str(text or "")) if s.strip()))
        bad = [s for s in ids if not SENSOR_ID.match(s)]
        if bad:
            return f'"{bad[0][:60]}" isn\'t a Home Assistant binary sensor (binary_sensor.something).'
        if len(ids) > MAX_SENSORS:
            return f"At most {MAX_SENSORS} sensors per place."
        if ids:
            p["sensors"] = ids
        else:
            p.pop("sensors", None)
        self.save()
        return None

    def sensors(self) -> dict[str, list[str]]:
        """{place id: its presence sensors}, for the places that have any."""
        return {p["id"]: p["sensors"] for p in self.items if p.get("sensors")}

    def remove(self, pid: str) -> bool:
        n = len(self.items)
        self.items = [p for p in self.items if p["id"] != pid]
        self.save()
        return len(self.items) < n

    def summary(self) -> list[dict]:
        """For the driving page and Rocky: the dock first."""
        return [{"id": DOCK, "names": self.dock_names, "dock": True}] + [dict(p) for p in self.items]

    def all_names(self) -> list[str]:
        return [n for p in self.summary() for n in p["names"]]

    def find(self, name_or_id: str) -> dict | None:
        """By id, or by any of its names (case and "the" don't matter)."""
        key = " ".join(re.sub(r"^\s*the\s+", "", str(name_or_id or "").lower()).split())
        for p in self.summary():
            if p["id"] == name_or_id or key in p["names"]:
                return p
        return None

    @staticmethod
    def pose(p: dict) -> tuple[float, float, float]:
        return p["x"], p["y"], p["th"]

    @staticmethod
    def label(p: dict) -> str:
        return p["names"][0]
