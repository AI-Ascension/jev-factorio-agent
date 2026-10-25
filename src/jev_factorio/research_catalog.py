"""Version-aware research tree for display: export, validation and progress summary.

The tree comes from the running game (`lua/research_catalog.lua`), so Factorio 1.1,
2.0 and Space Age each show their own technologies. Nothing here plans, forecasts
or authorizes actions; unknown or malformed data stays unknown.

Export once (or keep the research state fresh) from any FLE/RCON setup:

    JEV_RCON_PASSWORD=... python -m jev_factorio.research_catalog \\
        --host 127.0.0.1 --port 27015 --out research-catalog.json [--watch 10]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import sys
import tempfile
import time
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable

SCHEMA = "jev.research.v1"
FILENAME = "research-catalog.json"
MAX_BYTES = 8 * 1024 * 1024
MAX_TECHNOLOGIES = 5000
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
BASE_GOAL = "rocket-silo"


def lua_source() -> str:
    return files("jev_factorio").joinpath("lua/research_catalog.lua").read_text()


def export(send_command: Callable[[str], str]) -> dict:
    """Run the read-only exporter through an RCON `send_command`.

    Returns the raw export (the sidecar format) once `parse` has accepted it.
    """
    raw = send_command("/sc " + lua_source())
    if not raw or raw.startswith("Cannot execute command."):
        raise RuntimeError(f"Research export failed: {(raw or 'no output')[:200]}")
    data = json.loads(raw)
    parse(data)
    return data


def write(path: Path, catalog: dict) -> None:
    """Atomically replace `path` with a raw export; readers never see a partial file."""
    parse(catalog)
    path = Path(path)
    raw = json.dumps(catalog, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_BYTES:
        raise ValueError("Research catalog exceeds the display budget")
    handle, temporary = tempfile.mkstemp(prefix=".research-", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(raw)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def export_sidecar(backend: Any, directory: Path) -> Path | None:
    """Best effort: write the running game's research tree next to the controller's
    telemetry for the dashboard. Never raises; the controller must not depend on it."""
    try:
        instance = getattr(backend, "_instance", None)
        client = getattr(instance, "rcon_client", None)
        if client is None:
            return None
        path = Path(directory) / FILENAME
        write(path, export(client.send_command))
        return path
    except Exception as error:  # display-only side output
        print(f"Research catalog not exported ({type(error).__name__}); dashboard keeps its fallback",
              file=sys.stderr, flush=True)
        return None


def _names(value: Any, limit: int = MAX_TECHNOLOGIES) -> list[str]:
    # Factorio encodes an empty Lua table as {}; treat it as an empty list.
    if isinstance(value, dict) and not value:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError("Invalid name list")
    names = [item for item in value if isinstance(item, str) and NAME.match(item)]
    if len(names) != len(value):
        raise ValueError("Invalid name in list")
    return names


def _count(value: Any) -> int | float | None:
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return value
    return None


def parse(data: Any) -> dict:
    """Validate and normalize an exported catalog; raise ValueError when unusable."""
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ValueError("Not a jev.research.v1 catalog")
    version = data.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Missing or invalid base game version")
    mods = data.get("mods")
    if not isinstance(mods, dict) or len(mods) > 500 or not all(
            isinstance(k, str) and NAME.match(k) and isinstance(v, str) and len(v) <= 32 for k, v in mods.items()):
        raise ValueError("Invalid mod list")
    raw = data.get("technologies")
    if not isinstance(raw, dict) or not raw or len(raw) > MAX_TECHNOLOGIES:
        raise ValueError("Invalid technology table")
    technologies = {}
    for name, tech in raw.items():
        if not NAME.match(name) or not isinstance(tech, dict):
            raise ValueError("Invalid technology entry")
        ingredients = tech.get("ingredients") or []
        if isinstance(ingredients, dict):
            ingredients = []
        if not isinstance(ingredients, list) or len(ingredients) > 64:
            raise ValueError("Invalid research ingredients")
        packs = []
        for entry in ingredients:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not NAME.match(entry["name"]):
                raise ValueError("Invalid research ingredient")
            packs.append(entry["name"])
        trigger = tech.get("trigger")
        if trigger is not None:
            trigger = {key: trigger[key] for key in ("type", "item", "entity", "fluid")
                       if isinstance(trigger, dict) and isinstance(trigger.get(key), str) and NAME.match(trigger[key])}
            count = _count(trigger and tech["trigger"].get("count"))
            if count is not None:
                trigger["count"] = count
            trigger = trigger or None
        technologies[name] = {
            "prerequisites": _names(tech.get("prerequisites", [])),
            "packs": packs,
            "count": _count(tech.get("count")),
            "infinite": type(tech.get("max_level")) is int and tech["max_level"] == 2 ** 32 - 1,
            "trigger": trigger,
            "unlocks": _names(tech.get("unlocks", []), 512),
            "locations": _names(tech.get("locations", []), 64),
            "hidden": tech.get("hidden") is True,
            "enabled": tech.get("enabled") is not False,
            "essential": tech.get("essential") is True,
        }
    science_packs = {}
    raw_packs = data.get("science_packs")
    if raw_packs is None:
        raw_packs = {}
    if not isinstance(raw_packs, dict):
        raise ValueError("Invalid science pack table")
    for pack, row in raw_packs.items():
        if NAME.match(pack) and isinstance(row, dict):
            science_packs[pack] = {"from_start": row.get("from_start") is True,
                                   "unlocked_by": [t for t in _names(row.get("unlocked_by", [])) if t in technologies]}
    state = None
    raw_state = data.get("state")
    if isinstance(raw_state, dict) and type(raw_state.get("tick")) is int and raw_state["tick"] >= 0:
        progress = _count(raw_state.get("progress"))
        current = raw_state.get("current")
        state = {"tick": raw_state["tick"],
                 "researched": [t for t in _names(raw_state.get("researched", [])) if t in technologies],
                 "current": current if isinstance(current, str) and current in technologies else None,
                 "progress": progress if progress is not None and progress <= 1 else None}
    return {"schema": SCHEMA, "version": version, "mods": dict(mods), "technologies": technologies,
            "science_packs": science_packs, "state": state}


def title(name: str) -> str:
    words = name.replace("-", " ").replace("_", " ").strip()
    return words[:1].upper() + words[1:]


def pack_title(pack: str) -> str:
    return title(pack[:-5] if pack.endswith("-pack") else pack)


class Tree:
    """Static structure of one catalog: tiers, the path to the end goal, milestones."""

    def __init__(self, catalog: dict):
        self.catalog = catalog
        self.version = catalog["version"]
        self.mods = catalog["mods"]
        techs = {n: t for n, t in catalog["technologies"].items() if not t["hidden"] and t["enabled"]}
        self.techs = techs
        self._depth: dict[str, int] = {}
        for name in techs:
            self.depth(name)
        self.space_age = "space-age" in self.mods or any(t["locations"] for t in techs.values())
        packs = {pack for tech in techs.values() for pack in tech["packs"]}
        rank_key = {}
        for pack in packs:
            row = catalog["science_packs"].get(pack, {"from_start": False, "unlocked_by": []})
            unlockers = [t for t in row["unlocked_by"] if t in techs]
            if row["from_start"]:
                rank_key[pack] = (0, pack)
            elif unlockers:
                rank_key[pack] = (1 + min(self._depth[t] for t in unlockers), pack)
            else:
                rank_key[pack] = (10 ** 6, pack)  # e.g. base-game space science: made by launching
        self.packs = sorted(packs, key=lambda p: rank_key[p])
        self.rank = {pack: index for index, pack in enumerate(self.packs)}
        self.unlocker = {}
        for pack in self.packs:
            row = catalog["science_packs"].get(pack, {})
            unlockers = sorted((t for t in row.get("unlocked_by", []) if t in techs), key=lambda t: (self._depth[t], t))
            self.unlocker[pack] = unlockers[0] if unlockers else None
        self._tier: dict[str, int] = {}
        for name in techs:
            self.tier(name)
        self.goals = self._goals()
        self.path = self._ancestors(self.goals)
        self.milestones = self._milestones()

    def depth(self, name: str, seen: frozenset = frozenset()) -> int:
        if name in self._depth:
            return self._depth[name]
        if name in seen or name not in self.techs:
            return 0
        parents = [p for p in self.techs[name]["prerequisites"] if p in self.techs]
        value = 1 + max((self.depth(p, seen | {name}) for p in parents), default=-1)
        self._depth[name] = value
        return value

    def tier(self, name: str, seen: frozenset = frozenset()) -> int:
        """Index of the most advanced science pack a technology (or its trigger ancestry) needs."""
        if name in self._tier:
            return self._tier[name]
        if name in seen or name not in self.techs:
            return 0
        tech = self.techs[name]
        if tech["packs"]:
            value = max(self.rank.get(p, 0) for p in tech["packs"])
        else:
            value = max((self.tier(p, seen | {name}) for p in tech["prerequisites"] if p in self.techs), default=0)
        self._tier[name] = value
        return value

    def _finite(self, name: str) -> bool:
        return name in self.techs and not self.techs[name]["infinite"]

    def _essential(self) -> list[str]:
        """The game's own key progression (Factorio 2.0+ `essential`); empty on 1.1."""
        return [n for n, t in self.techs.items() if t["essential"] and self._finite(n)]

    def _goals(self) -> list[str]:
        # Base game: progress runs to the rocket silo. Space Age continues through the
        # planets, so every essential technology is a goal there.
        unlockers = [t for t in self.unlocker.values() if t and self._finite(t)]
        if self.space_age:
            goals = self._essential() or unlockers + [
                n for n, t in self.techs.items() if t["locations"] and self._finite(n)]
        elif self._finite(BASE_GOAL):
            goals = [BASE_GOAL]
        else:
            goals = self._essential() or unlockers
        return sorted(set(goals), key=lambda t: (self._depth[t], t))

    def _ancestors(self, goals: list[str]) -> set[str]:
        path, stack = set(), list(goals)
        while stack:
            name = stack.pop()
            if name in path or name not in self.techs or self.techs[name]["infinite"]:
                continue
            path.add(name)
            stack.extend(self.techs[name]["prerequisites"])
        return path

    def _milestones(self) -> list[dict]:
        """Essential technologies on the path (2.0+), else science-pack unlocks and goals (1.1)."""
        rows = {}
        for pack in self.packs:
            if self.unlocker[pack] is None and self.rank[pack] == 0:
                rows[pack] = {"key": pack, "kind": "research", "title": pack_title(pack), "start": True, "depth": -1}
        keys = [t for t in self._essential() if t in self.path] or [
            t for t in self.unlocker.values() if t in self.path] + self.goals
        for name in keys:
            rows.setdefault(name, {"key": name, "kind": "research", "title": self._label(name),
                                   "depth": self._depth[name]})
        return sorted(rows.values(), key=lambda r: (r["depth"], r["key"]))

    def _label(self, name: str) -> str:
        packs = [pack for pack, tech in self.unlocker.items() if tech == name]
        locations = self.techs[name]["locations"] if name in self.techs else []
        return pack_title(packs[0]) if packs else title(locations[0]) if locations else title(name)

    def summary(self, researched: set[str], current: str | None, progress: float | None,
                seen: dict[str, int | None], *, allow_trigger: bool = True) -> dict:
        tiers = []
        for pack in self.packs:
            members = [t for t in self.path if self._tier[t] == self.rank[pack]]
            if members:
                tiers.append({"pack": pack, "title": pack_title(pack),
                              "done": sum(1 for t in members if t in researched), "total": len(members)})
        available = sorted(t for t in self.path if t not in researched
                           and all(p in researched for p in self.techs[t]["prerequisites"] if p in self.techs))
        milestones = []
        for row in self.milestones:
            row = {k: v for k, v in row.items() if k != "depth"}
            done = row.get("start") or row["key"] in researched
            row["state"] = "done" if done else "pending"
            row["tick"] = seen.get(row["key"]) if done and not row.get("start") else None
            milestones.append(row)
        now = None
        if current is None and allow_trigger:
            triggers = [name for name in available if self.techs[name]["trigger"] is not None]
            if triggers:
                current = min(triggers, key=lambda name: (self._depth[name], name))
                progress = None  # Eligibility does not establish trigger progress.
        if current in self.techs:
            tech = self.techs[current]
            pack = self.packs[self._tier[current]] if self.packs else None
            now = {"name": current, "title": title(current), "pack": pack,
                   "progress": progress, "trigger": tech["trigger"], "on_path": current in self.path}
        return {"version": self.version, "space_age": self.space_age,
                "goal": self._label(self.goals[-1]) if self.goals else None,
                "tiers": tiers, "current": now, "available": len(available),
                "path_done": sum(1 for t in self.path if t in researched), "path_total": len(self.path),
                "milestones": milestones}


def read(path: Path) -> dict:
    """Read a sidecar written by `write`; regular files only, bounded."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("Research catalog is not a bounded regular file")
        return parse(json.loads(stream.read(MAX_BYTES + 1)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export the running game's research tree for the dashboard (read-only)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=27015)
    parser.add_argument("--out", type=Path, default=Path(FILENAME))
    parser.add_argument("--watch", type=float, help="Re-export every N seconds to keep research state fresh")
    args = parser.parse_args(argv)
    password = os.environ.get("JEV_RCON_PASSWORD")
    if password is None:
        parser.error("Set JEV_RCON_PASSWORD (not accepted on the command line)")
    if args.watch is not None and (not math.isfinite(args.watch) or args.watch < 2):
        parser.error("--watch must be finite and at least 2 seconds")
    from factorio_rcon import RCONClient

    client = RCONClient(args.host, args.port, password)
    while True:
        raw = export(client.send_command)
        write(args.out, raw)
        catalog = parse(raw)
        state = catalog["state"] or {}
        print(f"{args.out}: Factorio {catalog['version']}, {len(catalog['technologies'])} technologies, "
              f"{len(state.get('researched', []))} researched", flush=True)
        if args.watch is None:
            return 0
        time.sleep(args.watch)


if __name__ == "__main__":
    sys.exit(main())
