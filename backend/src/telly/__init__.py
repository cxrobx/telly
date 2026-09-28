"""Telly CLI: `telly migrate | sync | poll [--hot] | infer | deliver | plexdata | recs | renewals | run | events | serve`."""

from __future__ import annotations

import argparse
import logging


def main() -> None:
    p = argparse.ArgumentParser(prog="telly")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="alembic upgrade head")
    sub.add_parser("sync", help="Plex members + play history")
    pp = sub.add_parser("poll", help="TMDB poll + change detection")
    pp.add_argument("--hot", action="store_true", help="only shows airing around today")
    sub.add_parser("infer", help="add inferred follows")
    sub.add_parser("deliver", help="send pending alerts")
    sub.add_parser("plexdata", help="library, watchlists, Overseerr ids")
    sub.add_parser("recs", help="rebuild recommendations")
    sub.add_parser("renewals", help="weekly news renewal check (needs headless Claude)")
    pt = sub.add_parser("taste-add", help="record that someone likes a title (e.g. told plexbot)")
    pt.add_argument("plex_id", type=int)
    pt.add_argument("media_type", choices=["tv", "movie"])
    pt.add_argument("tmdb_id", type=int)
    pt.add_argument("--source", default="told", choices=["told", "mentioned", "overseerr"])
    pt.add_argument("--title", default="")
    pr = sub.add_parser("ratings", help="refresh IMDb / Rotten Tomatoes scores")
    pr.add_argument("--force", action="store_true", help="ignore the weekly freshness window")
    sub.add_parser("run", help="sync, poll, infer, deliver (the nightly job)")
    pe = sub.add_parser("events", help="list recent events")
    pe.add_argument("--limit", type=int, default=20)
    ps = sub.add_parser("serve", help="run the API + scheduler")
    ps.add_argument("--port", type=int, default=8940)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.cmd == "migrate":
        from alembic import command
        from alembic.config import Config
        command.upgrade(Config("alembic.ini"), "head")
    elif args.cmd == "sync":
        from .jobs import run_sync
        print(run_sync())
    elif args.cmd == "poll":
        from .jobs import run_poll
        r = run_poll(hot_only=args.hot)
        print(f"polled={r.polled} baselined={r.baselined} errors={r.errors}")
        for e in r.events:
            print("  event:", e)
    elif args.cmd == "infer":
        from .jobs import run_infer
        print(run_infer())
    elif args.cmd == "deliver":
        from .jobs import run_deliver
        r = run_deliver()
        print(f"sent={r.sent} failed={r.failed}")
        for line in r.lines:
            print("  ", line)
    elif args.cmd == "plexdata":
        from .jobs import run_plexdata
        print(run_plexdata())
    elif args.cmd == "recs":
        from .jobs import run_recs
        print(run_recs())
    elif args.cmd == "renewals":
        from .jobs import run_renewals
        print(run_renewals())
    elif args.cmd == "taste-add":
        from . import taste
        from .db import session_scope
        with session_scope() as s:
            if args.source == "told":  # History: a Liked it rating
                from . import history
                print(history.record(s, None, args.plex_id, args.tmdb_id, args.media_type, liked=True))
            else:
                print("added" if taste.add(s, args.plex_id, args.tmdb_id, args.media_type, args.source,
                                           args.title) else "already there")
    elif args.cmd == "ratings":
        from .jobs import run_ratings
        print(run_ratings(force=args.force))
    elif args.cmd == "run":
        from .jobs import run_all
        for r in run_all():
            print(r)
    elif args.cmd == "events":
        from sqlalchemy import select
        from .db import session_scope
        from .models import Event, Title
        with session_scope() as s:
            q = (select(Event, Title.name).join(Title, (Title.tmdb_id == Event.tmdb_id)
                 & (Title.media_type == "tv"), isouter=True)
                 .order_by(Event.detected_at.desc()).limit(args.limit))
            for ev, name in s.execute(q):
                print(ev.detected_at.isoformat(timespec="minutes"), name, ev.kind, ev.payload)
    elif args.cmd == "serve":
        import uvicorn
        uvicorn.run("telly.app:app", host="0.0.0.0", port=args.port)
