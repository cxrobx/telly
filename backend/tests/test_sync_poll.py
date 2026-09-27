"""Sync + poll against an in-memory DB with fake Plex/TMDB (shapes from spec §2)."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import func, select

from telly.models import Event, Follow, Play, PlexItem, Title, User
from telly.poll import poll, titles_to_poll
from telly.sync import SyncReport, infer_follows, sync_history, sync_users

from .conftest import FakePlex, FakeTMDB, ep, show_item

ACCOUNTS = [{"id": 1, "name": "cxrobx"}, {"id": 222222222, "name": "alexis"}]


def tmdb_tv(status="Returning Series", seasons=((1, 10), (2, 10)), last=(2, 10), extra_season=None):
    ss = [{"season_number": n, "air_date": f"202{n}-01-01", "episode_count": c} for n, c in seasons]
    if extra_season:
        ss.append({"season_number": extra_season, "air_date": None, "episode_count": 0})
    return {"id": 103516, "name": "Strange New Worlds", "status": status, "seasons": ss,
            "first_air_date": "2022-05-05", "genres": [{"name": "Sci-Fi"}], "networks": [],
            "last_episode_to_air": {"season_number": last[0], "episode_number": last[1],
                                    "air_date": "2026-09-24", "name": "x"},
            "next_episode_to_air": None}


def synced(s, history, items=None, accounts=ACCOUNTS, page_size=200):
    plex = FakePlex(accounts, history, items or {"4385": show_item(103516)})
    r = SyncReport()
    sync_users(s, plex, r)
    sync_history(s, plex, r, page_size=page_size)
    s.commit()
    return plex, r


# ── identity ─────────────────────────────────────────────────────────────


def test_owner_account_1_maps_to_their_plex_tv_id(s):
    synced(s, [ep(1, 1, 4385, "SNW", 2, 1), ep(2, 222222222, 4385, "SNW", 2, 1)])
    owners = {p.plex_id for p in s.scalars(select(Play))}
    assert owners == {FakePlex.OWNER, 222222222}
    assert s.get(User, FakePlex.OWNER).is_owner and s.get(User, FakePlex.OWNER).pms_account_id == 1


def test_plays_from_accounts_not_on_the_server_are_dropped(s):
    _, r = synced(s, [ep(1, 999, 4385, "SNW", 2, 1)])
    assert r.skipped_unknown_account == 1
    assert s.scalar(select(func.count()).select_from(Play)) == 0


def test_removed_member_is_marked_and_comes_back_if_re_added(s):
    synced(s, [])
    synced(s, [], accounts=[{"id": 1, "name": "cxrobx"}])
    assert s.get(User, 222222222).removed_at is not None
    synced(s, [])
    assert s.get(User, 222222222).removed_at is None


# ── history ──────────────────────────────────────────────────────────────


def test_resync_is_idempotent_and_guids_are_fetched_once_per_show(s):
    hist = [ep(i, 1, 4385, "SNW", 2, i) for i in range(1, 6)]
    plex, r1 = synced(s, hist)
    assert r1.new_plays == 5 and plex.item_calls == ["4385"]
    plex2, r2 = synced(s, hist)
    assert r2.new_plays == 0 and plex2.item_calls == []


def test_pagination_reaches_older_pages_then_stops_at_known_ones(s):
    hist = [ep(i, 1, 4385, "SNW", 1, i) for i in range(1, 8)]
    _, r = synced(s, hist, page_size=3)
    assert r.new_plays == 7
    newer = [ep(100, 1, 4385, "SNW", 2, 1)] + hist
    _, r2 = synced(s, newer, page_size=3)
    assert r2.new_plays == 1


def test_show_gone_from_library_is_kept_unmatched(s):
    _, r = synced(s, [ep(1, 1, 7777, "Deleted Show", 1, 1)], items={})
    assert r.unmatched_items == 1
    assert s.get(PlexItem, "7777").match_status == "missing"
    assert s.scalar(select(Play.tmdb_id)) is None
    assert s.scalar(select(func.count()).select_from(Title)) == 0


# ── poll + events ────────────────────────────────────────────────────────


def test_first_poll_baselines_then_a_renewal_alerts_exactly_once(s):
    synced(s, [ep(1, 1, 4385, "SNW", 2, 1)])
    tmdb = FakeTMDB({103516: tmdb_tv()})
    r = poll(s, tmdb)
    assert r.baselined == 1 and r.events == []
    assert s.get(Title, (103516, "tv")).name == "Strange New Worlds"

    tmdb.shows[103516] = tmdb_tv(extra_season=3)
    assert poll(s, tmdb).events == []           # seen once: pending
    assert len(poll(s, tmdb).events) == 1       # seen twice: renewed
    assert poll(s, tmdb).events == []           # stable afterwards
    evs = list(s.scalars(select(Event)))
    assert [(e.kind, e.payload["season"]) for e in evs] == [("renewed", 3)]


def test_the_same_renewal_via_two_paths_is_stored_once(s):
    synced(s, [ep(1, 1, 4385, "SNW", 2, 1)])
    tmdb = FakeTMDB({103516: tmdb_tv(status="Ended")})
    poll(s, tmdb)
    tmdb.shows[103516] = tmdb_tv(status="Returning Series")
    poll(s, tmdb), poll(s, tmdb)                 # renewed via status
    tmdb.shows[103516] = tmdb_tv(status="Returning Series", extra_season=3)
    poll(s, tmdb), poll(s, tmdb)                 # same renewal via the new season row
    assert s.scalar(select(func.count()).select_from(Event)) == 1


def test_a_show_tmdb_no_longer_knows_is_recorded_not_fatal(s):
    synced(s, [ep(1, 1, 4385, "SNW", 2, 1)])
    r = poll(s, FakeTMDB({}))
    assert r.errors == {103516: "not_found"}
    assert s.get(Title, (103516, "tv")).poll_error == "not_found"


def test_hot_poll_only_picks_shows_airing_around_today(s):
    today = date(2026, 9, 27)
    s.add_all([
        Title(tmdb_id=1, media_type="tv", name="airs today", next_episode={"air_date": "2026-09-27"},
              last_polled_at=datetime(2026, 9, 26, tzinfo=timezone.utc)),
        Title(tmdb_id=2, media_type="tv", name="next week", next_episode={"air_date": "2026-10-04"},
              last_polled_at=datetime(2026, 9, 26, tzinfo=timezone.utc)),
        Title(tmdb_id=3, media_type="tv", name="never polled"),
        Title(tmdb_id=4, media_type="movie", name="a movie"),
    ])
    s.commit()
    assert sorted(t.name for t in titles_to_poll(s, hot_only=True, today=today)) == [
        "airs today", "never polled"]


# ── inferred follows ─────────────────────────────────────────────────────


def _polled(s, history, tv=None):
    synced(s, history)
    poll(s, FakeTMDB({103516: tv or tmdb_tv()}))
    r = SyncReport()
    infer_follows(s, r)
    s.commit()
    return r


def test_three_episodes_of_the_latest_season_is_a_follow(s):
    r = _polled(s, [ep(i, 1, 4385, "SNW", 2, i) for i in range(1, 4)])
    assert r.new_follows == 1
    assert s.get(Follow, (FakePlex.OWNER, 103516)).source == "inferred"


def test_two_episodes_is_not_enough(s):
    assert _polled(s, [ep(i, 1, 4385, "SNW", 2, i) for i in range(1, 3)]).new_follows == 0


def test_old_seasons_only_needs_half_of_what_aired(s):
    # 20 aired; 3 from season 1 only → 15% → no. 10 → 50% → yes.
    assert _polled(s, [ep(i, 1, 4385, "SNW", 1, i) for i in range(1, 4)]).new_follows == 0
    r = SyncReport()
    synced(s, [ep(i, 1, 4385, "SNW", 1, i) for i in range(1, 11)])
    infer_follows(s, r)
    assert r.new_follows == 1


def test_ended_shows_are_still_followed_so_a_revival_reaches_people(s):
    r = _polled(s, [ep(i, 1, 4385, "SNW", 2, i) for i in range(1, 11)], tv=tmdb_tv(status="Ended"))
    assert r.new_follows == 1


def test_unfollow_is_never_overridden_by_inference(s):
    synced(s, [ep(i, 1, 4385, "SNW", 2, i) for i in range(1, 4)])
    s.add(Follow(plex_id=FakePlex.OWNER, tmdb_id=103516, source="manual", state="unfollowed"))
    s.commit()
    poll(s, FakeTMDB({103516: tmdb_tv()}))
    r = SyncReport()
    infer_follows(s, r)
    assert r.new_follows == 0
    assert s.get(Follow, (FakePlex.OWNER, 103516)).state == "unfollowed"


def test_removed_members_get_no_new_follows(s):
    synced(s, [ep(i, 222222222, 4385, "SNW", 2, i) for i in range(1, 4)])
    synced(s, [], accounts=[{"id": 1, "name": "cxrobx"}])
    poll(s, FakeTMDB({103516: tmdb_tv()}))
    r = SyncReport()
    infer_follows(s, r)
    assert r.new_follows == 0
