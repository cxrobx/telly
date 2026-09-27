"""Change detection: TMDB snapshot → confirmed facts → show events. Pure; no I/O.

TMDB is community-edited, so a status or a season row can flip and flip back. Every fact
therefore carries a pending value that must be seen on `confirm` consecutive polls before it
is promoted; only promotions produce events. The one exception is `last_ep`, which TMDB
derives from air dates rather than from edits, so it confirms on first sight.

A show seen for the first time is a baseline: its facts are confirmed as-is and nothing is
emitted, so adding a show (or the first import) never floods anyone with alerts.

Fact values are strings. A fact confirmed as "no value" (e.g. a season with no date yet) is
stored as "", which keeps it distinct from a fact never confirmed at all (None).
"""

from __future__ import annotations

from dataclasses import dataclass

ALIVE = {"Returning Series", "In Production", "Planned", "Pilot"}
DEAD = {"Ended", "Canceled"}

INSTANT_KEYS = {"last_ep"}


@dataclass
class FactState:
    confirmed: str | None = None
    pending: str | None = None
    pending_seen: int = 0


@dataclass(frozen=True)
class Detected:
    kind: str
    payload: dict
    dedupe_key: str


def _enc(v: object) -> str:
    return "" if v is None else str(v)


def observe(tv: dict) -> dict[str, str]:
    """Pull the tracked facts out of a TMDB /tv/{id} response."""
    regular = [s for s in (tv.get("seasons") or []) if (s.get("season_number") or 0) > 0]
    last = tv.get("last_episode_to_air")
    last_aired_season = last["season_number"] if last else 0

    obs = {
        "status": _enc(tv.get("status")),
        "max_season": _enc(max((s["season_number"] for s in regular), default=0)),
    }
    if last:
        obs["last_ep"] = f"{last['season_number']}:{last['episode_number']}"
    for s in regular:
        n = s["season_number"]
        if n > last_aired_season:  # only upcoming seasons have a date worth watching
            obs[f"season:{n}:air_date"] = _enc(s.get("air_date"))
    return obs


def step(
    facts: dict[str, FactState], obs: dict[str, str], *, first_seen: bool, confirm: int
) -> list[tuple[str, str | None, str]]:
    """Advance `facts` in place with one observation. Returns promotions (key, old, new);
    old is None when the key had never been confirmed before."""
    promotions: list[tuple[str, str | None, str]] = []
    for key, value in obs.items():
        f = facts.setdefault(key, FactState())
        if first_seen:
            f.confirmed, f.pending, f.pending_seen = value, None, 0
            continue
        if f.confirmed is not None and value == f.confirmed:
            f.pending, f.pending_seen = None, 0
            continue
        if f.pending_seen and value == f.pending:
            f.pending_seen += 1
        else:
            f.pending, f.pending_seen = value, 1
        need = 1 if key in INSTANT_KEYS else confirm
        if f.pending_seen >= need:
            promotions.append((key, f.confirmed, value))
            f.confirmed, f.pending, f.pending_seen = value, None, 0
    return promotions


def _ep(v: str | None) -> tuple[int, int]:
    if not v:
        return (0, 0)
    s, e = v.split(":")
    return (int(s), int(e))


def events_for(
    tmdb_id: int, promotions: list[tuple[str, str | None, str]], facts: dict[str, FactState]
) -> list[Detected]:
    """Turn promotions into events. `facts` is the state *after* the step."""
    last_aired_season = _ep(facts["last_ep"].confirmed if "last_ep" in facts else None)[0]
    out: list[Detected] = []

    for key, old, new in promotions:
        if key == "status":
            if old is None:
                continue
            if old in DEAD and new in ALIVE:
                season = last_aired_season + 1
                out.append(Detected("renewed", {"season": season, "via": "status"},
                                    f"renewed:{tmdb_id}:{season}"))
            elif old in ALIVE and new == "Canceled":
                out.append(Detected("canceled", {"after_season": last_aired_season},
                                    f"canceled:{tmdb_id}:{last_aired_season}"))
            elif old in ALIVE and new == "Ended":
                out.append(Detected("ended", {"after_season": last_aired_season},
                                    f"ended:{tmdb_id}:{last_aired_season}"))

        elif key == "max_season":
            if old is None:
                continue
            o, n = int(old or 0), int(new or 0)
            if n > o and n > last_aired_season:
                out.append(Detected("renewed", {"season": n, "via": "season_added"},
                                    f"renewed:{tmdb_id}:{n}"))

        elif key.startswith("season:") and key.endswith(":air_date"):
            season = int(key.split(":")[1])
            if new and new != old and season > last_aired_season:
                out.append(Detected("season_dated",
                                    {"season": season, "air_date": new, "previous": old or None},
                                    f"season_dated:{tmdb_id}:{season}:{new}"))

        elif key == "last_ep":
            (s0, e0), (s1, e1) = _ep(old), _ep(new)
            if (s1, e1) <= (s0, e0):
                continue  # TMDB correction backwards; nothing aired
            start = 1 if s1 > s0 else e0 + 1
            out.append(Detected("episodes_aired",
                                {"season": s1, "from_episode": start, "to_episode": e1,
                                 "premiere": start == 1},
                                f"aired:{tmdb_id}:{s1}:{e1}"))
    return out


def detect(
    tmdb_id: int, tv: dict, facts: dict[str, FactState], *, first_seen: bool, confirm: int = 2
) -> list[Detected]:
    """One poll of one show: update `facts` in place and return the events it produced."""
    promotions = step(facts, observe(tv), first_seen=first_seen, confirm=confirm)
    seen: set[str] = set()
    out = []
    for ev in events_for(tmdb_id, promotions, facts):  # a status flip and a new season row
        if ev.dedupe_key not in seen:                  # can report the same renewal together
            seen.add(ev.dedupe_key)
            out.append(ev)
    return out
