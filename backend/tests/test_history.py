"""Watch history (history.py): statuses from Plex, what people say about titles, and how their
ratings shape picks (recs.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from telly import history, recs
from telly.db import make_engine
from telly.models import Play, Title, User, WatchEntry

NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)
A, B = 111, 222
SEASONS = [{"season": 1, "episode_count": 10, "air_date": "2024-01-01"},
           {"season": 2, "episode_count": 10, "air_date": "2025-01-01"}]


class FakeTMDB:
    def __init__(self, by_seed=None):
        self.by_seed = by_seed or {}

    def tv(self, tmdb_id):
        return {"name": f"Show {tmdb_id}", "first_air_date": "2020-05-01", "poster_path": f"/{tmdb_id}.jpg"}

    def movie(self, tmdb_id):
        return {"title": f"Movie {tmdb_id}", "release_date": "2019-01-01", "poster_path": None}

    def recommendations(self, media_type, tmdb_id):
        return self.by_seed.get((media_type, tmdb_id), [])


def show(s, tmdb_id, name, status="Returning Series", last=(2, 10)):
    s.add(Title(tmdb_id=tmdb_id, media_type="tv", name=name, status=status, seasons=SEASONS,
                last_episode={"season": last[0], "episode": last[1], "air_date": "2025-03-01"},
                last_polled_at=NOW))


_hk = 0


def watch(s, pid, tmdb_id, eps, days_ago, mt="tv"):
    """eps: [(season, episode)] (ignored for a movie)."""
    global _hk
    for season, episode in eps if mt == "tv" else [(None, None)]:
        _hk += 1
        s.add(Play(history_key=str(_hk), plex_id=pid, media_type=mt, tmdb_id=tmdb_id, rating_key="x",
                   season=season, episode=episode, viewed_at=NOW - timedelta(days=days_ago)))


def season(n, upto=10):
    return [(n, e) for e in range(1, upto + 1)]


@pytest.fixture
def two(s):
    s.add_all([User(plex_id=A, pms_account_id=1, username="a", overseerr_id=1),
               User(plex_id=B, pms_account_id=B, username="b", overseerr_id=2)])
    s.flush()
    show(s, 1, "Caught Up")
    show(s, 2, "All Done", status="Ended")
    show(s, 3, "Midway")
    show(s, 4, "Went Quiet")
    s.add(Title(tmdb_id=9, media_type="movie", name="A Movie"))
    watch(s, A, 1, season(1) + season(2), 5)
    watch(s, A, 2, season(1) + season(2), 400)
    watch(s, A, 3, season(1, 4), 10)
    watch(s, A, 4, season(1, 3), 200)
    watch(s, A, 9, [], 30, mt="movie")
    s.commit()
    return s


def by_name(s, pid=A):
    return {w.name: w for w in history.watched(s, pid, NOW)}


def test_status_comes_from_plex(two):
    w = by_name(two)
    assert {n: x.status for n, x in w.items()} == {
        "Caught Up": "caught_up", "All Done": "finished", "Midway": "watching",
        "Went Quiet": "stalled", "A Movie": "finished"}
    assert (w["Midway"].episodes, w["Midway"].aired) == (4, 20)
    assert not any(x.status_set for x in w.values())
    assert by_name(two, B) == {}  # someone else's plays are theirs


def test_a_stalled_show_is_never_assumed_dropped(two):
    assert history.watched(two, A, NOW + timedelta(days=3650))[0].status != "dropped"
    assert by_name(two)["Went Quiet"].status == "stalled"


def test_what_they_say_wins_over_plex_and_can_be_undone(two):
    s = two
    history.set_status(s, None, A, 4, "tv", "dropped")
    history.rate(s, None, A, 1, "tv", 2)
    s.commit()
    w = by_name(s)
    assert (w["Went Quiet"].status, w["Went Quiet"].plex_status, w["Went Quiet"].status_set) == ("dropped", "stalled", True)
    assert w["Caught Up"].rating == 2
    watch(s, A, 4, [(1, 4)], 1)  # a new Plex play never overwrites what they said
    s.commit()
    assert by_name(s)["Went Quiet"].status == "dropped"
    history.set_status(s, None, A, 4, "tv", None)
    s.commit()
    assert by_name(s)["Went Quiet"].status == "watching"
    assert s.get(WatchEntry, (A, 4, "tv")) is None  # nothing said about a Plex title: no row


def test_something_watched_elsewhere_is_added_with_its_art(two):
    s = two
    out = history.add(s, FakeTMDB(), A, 500, "tv")
    s.commit()
    assert out["title"] == "Show 500"
    w = by_name(s)["Show 500"]
    assert (w.status, w.source, w.year, w.poster_path, w.episodes) == ("finished", "manual", 2020, "/500.jpg", 0)
    history.rate(s, FakeTMDB(), A, 500, "tv", None)  # clearing a rating keeps what they added
    assert s.get(WatchEntry, (A, 500, "tv")) is not None
    assert history.remove(s, A, 500, "tv") == {"ok": True, "still_in_history": False}
    assert history.remove(s, A, 1, "tv")["still_in_history"] is True
    with pytest.raises(history.HistoryError):
        history.rate(s, FakeTMDB(), A, 500, "tv", 5)
    with pytest.raises(history.HistoryError):
        history.set_status(s, FakeTMDB(), A, 500, "tv", "binged")


def test_listing_is_newest_first_with_words_for_ratings(two):
    s = two
    history.rate(s, None, A, 3, "tv", -1)
    s.commit()
    items = history.listing(s, A)
    assert [i["name"] for i in items][:2] == ["Caught Up", "Midway"]
    assert next(i for i in items if i["name"] == "Midway")["rating"] == "not_for_me"


# ── Ratings → picks ──────────────────────────────────────────────────────────


def test_loved_beats_everything_and_liked_beats_most_watching(two):
    s = two
    history.rate(s, None, A, 2, "tv", 2)   # watched long ago, loved
    history.rate(s, None, A, 3, "tv", 1)   # four episodes, liked
    s.commit()
    seeds = recs.seeds_for(s, A, NOW)
    kinds = {x.name: x.kind for x in seeds}
    assert seeds[0].name == "All Done" and kinds["All Done"] == "loved"
    w = {x.name: x.weight for x in seeds}
    assert w["Midway"] >= w["Caught Up"] * recs.LIKED
    assert kinds["Caught Up"] == "watched"


def test_disliked_and_dropped_shows_are_not_taste(two):
    s = two
    history.rate(s, None, A, 1, "tv", -1)
    history.set_status(s, None, A, 4, "tv", "dropped")
    s.commit()
    names = {x.name for x in recs.seeds_for(s, A, NOW)}
    assert "Caught Up" not in names and "Went Quiet" not in names


def test_negatives_are_not_for_me_and_early_drops_only(two):
    s = two
    history.rate(s, None, A, 1, "tv", -1)                 # not for me
    history.set_status(s, None, A, 4, "tv", "dropped")    # 3 of 20 aired: 15%, past the early line
    history.set_status(s, None, A, 3, "tv", "dropped")    # 4 of 20: 20%, a real try
    history.add(s, FakeTMDB(), A, 600, "tv", status="dropped")  # dropped elsewhere
    s.commit()
    neg = {x.name: (x.kind, x.weight) for x in recs.negatives_for(s, A, NOW)}
    assert neg == {"Caught Up": ("not_for_me", recs.NOT_FOR_ME),
                   "Show 600": ("dropped", recs.DROPPED_EARLY)}
    assert "Went Quiet" not in neg  # 3 episodes in: past the early line
    assert recs.negatives_for(s, B, NOW) == []


def rec(id_, name, votes=500, rating=8.0):
    return {"id": id_, "name": name, "vote_count": votes, "vote_average": rating,
            "first_air_date": "2024-01-01", "overview": ""}


def test_a_dislike_pushes_lookalikes_down_but_never_hides_them(two):
    s = two
    history.rate(s, None, A, 2, "tv", 2)
    history.rate(s, None, A, 3, "tv", -1)
    s.commit()
    tmdb = FakeTMDB({("tv", 2): [rec(70, "Lookalike"), rec(71, "Fresh")],
                     ("tv", 3): [rec(70, "Lookalike")]})
    seeds, neg = recs.seeds_for(s, A, NOW), recs.negatives_for(s, A, NOW)
    pool = {c.title: c for c in recs.candidates(s, tmdb, A, seeds, {}, neg)}
    assert pool["Lookalike"].unlike == ["Midway"]
    assert pool["Fresh"].score > pool["Lookalike"].score > 0
    base = {c.title: c for c in recs.candidates(s, tmdb, A, seeds, {})}
    assert pool["Lookalike"].score == pytest.approx(base["Lookalike"].score * recs.NEG_FLOOR)
    assert pool["Fresh"].score == pytest.approx(base["Fresh"].score)


def test_the_model_gets_taste_in_words(two):
    s = two
    history.rate(s, None, A, 2, "tv", 2)
    history.rate(s, None, A, 3, "tv", -1)
    s.commit()
    lines = recs.taste_lines(recs.seeds_for(s, A, NOW), recs.negatives_for(s, A, NOW))
    assert "Loved: All Done (show)" in lines
    assert "Didn't like (avoid more of the same): Midway (show)" in lines
    assert "Midway" not in lines.split("Didn't like")[0]


def test_rated_titles_are_never_recommended_back(two):
    s = two
    history.add(s, FakeTMDB(), A, 800, "movie", rating=1)
    s.commit()
    assert (800, "movie") in recs._excluded(s, A)
    assert (800, "movie") not in recs._excluded(s, B)


# ── Migration: `told` becomes Liked it ──────────────────────────────────────


def test_migration_moves_told_into_history(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "migrations"))
    cfg.attributes["configure_logger"] = False
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "492e70d4bb96")
        conn.execute(text("INSERT INTO users (plex_id, pms_account_id, username, is_owner, first_seen) "
                          "VALUES (111, 1, 'a', 1, '2026-01-01 00:00:00')"))
        conn.execute(text("INSERT INTO plays (history_key, plex_id, media_type, tmdb_id, rating_key, viewed_at) "
                          "VALUES ('h', 111, 'tv', 2, 'x', '2026-01-01 00:00:00')"))
        for tmdb_id, use in ((1, 1), (2, 1), (3, 0), (2, 0)):
            src = "told" if (tmdb_id, use) != (2, 0) else "overseerr"
            conn.execute(text(
                "INSERT INTO taste_signals (plex_id, tmdb_id, media_type, source, title, use_for_picks) "
                f"VALUES (111, {tmdb_id}, 'tv', '{src}', 'T{tmdb_id}', {use})"))
        command.upgrade(cfg, "head")
        rows = conn.execute(text("SELECT tmdb_id, rating, source FROM watch_entries ORDER BY tmdb_id")).all()
        left = conn.execute(text("SELECT source FROM taste_signals")).scalars().all()
    assert [tuple(r) for r in rows] == [(1, 1, "manual"), (2, 1, "plex"), (3, None, "manual")]
    assert left == ["overseerr"]


def test_a_long_show_dropped_after_real_viewing_is_not_an_early_drop(two):
    s = two
    show(s, 37854, "Long Runner", last=(2, 10))
    s.get(Title, (37854, "tv")).seasons = [{"season": 1, "episode_count": 1000}, {"season": 2, "episode_count": 10}]
    watch(s, A, 37854, season(1, 26), 90)
    show(s, 38000, "Long Sampler", last=(2, 10))
    s.get(Title, (38000, "tv")).seasons = [{"season": 1, "episode_count": 1000}, {"season": 2, "episode_count": 10}]
    watch(s, A, 38000, season(1, 4), 90)
    history.set_status(s, None, A, 37854, "tv", "dropped")
    history.set_status(s, None, A, 38000, "tv", "dropped")
    s.commit()
    w = by_name(s)
    assert not w["Long Runner"].dropped_early   # 26 episodes: a real try, however long the show
    assert w["Long Sampler"].dropped_early      # 4 of 1,010: sampled and left
