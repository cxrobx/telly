"""Taste beyond Plex (taste.py): IMDb CSV, Overseerr requests, plexbot mentions → recs."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from telly import recs, taste
from telly.models import ImdbRow, Play, TasteSignal, Title, User

A, B = 111, 222

# IMDb's export headers (ratings; watchlist has Position first). Verify against a real export.
RATINGS_CSV = (
    "﻿Const,Your Rating,Date Rated,Title,Original Title,URL,Title Type,IMDb Rating,Runtime (mins),Year,Genres,Num Votes,Release Date,Directors\n"
    "tt0306414,10,2024-03-02,The Wire,The Wire,https://www.imdb.com/title/tt0306414/,TV Series,9.3,60,2002,Crime,400000,2002-06-02,\n"
    "tt2861424,8,2023-01-10,Rick and Morty,Rick and Morty,https://www.imdb.com/title/tt2861424/,TV Series,9.1,23,2013,Animation,600000,2013-12-02,\n"
    "tt0110912,3,2022-06-01,Some Movie,Some Movie,https://x,Movie,8.9,154,1994,Crime,2000000,1994-10-14,Someone\n"
    "tt9999999,9,2022-06-01,An Episode,An Episode,https://x,TV Episode,8.0,50,2020,Drama,100,2020-01-01,\n"
)
WATCHLIST_CSV = (
    "Position,Const,Created,Modified,Description,Title,Original Title,URL,Title Type,IMDb Rating,Runtime (mins),Year,Genres,Num Votes,Release Date,Directors,Your Rating,Date Rated\n"
    "1,tt1234567,2025-01-01,2025-01-01,,Want This,Want This,https://x,Movie,7.0,100,2024,Drama,1000,2024-01-01,,,\n"
)


def test_parse_ratings_export():
    kind, rows = taste.parse_imdb_csv(RATINGS_CSV)
    assert kind == "rating" and len(rows) == 4
    assert rows[0] == {"imdb_id": "tt0306414", "title": "The Wire", "title_type": "TV Series", "rating": 10,
                       "noted_at": datetime(2024, 3, 2, tzinfo=timezone.utc)}


def test_parse_watchlist_export_is_detected_by_its_position_column():
    kind, rows = taste.parse_imdb_csv(WATCHLIST_CSV)
    assert kind == "watchlist" and rows[0]["imdb_id"] == "tt1234567" and rows[0]["rating"] is None


@pytest.mark.parametrize("text,msg", [("", "empty"), ("name,year\nx,1\n", "no Const column"),
                                      ("Const,Your Rating,Title\ntt1,,X\n", "no ratings")])
def test_parse_refuses_what_isnt_an_imdb_export(text, msg):
    with pytest.raises(taste.ImdbImportError, match=msg):
        taste.parse_imdb_csv(text)


class FakeTMDB:
    def __init__(self):
        self.finds = []

    def find_imdb(self, imdb_id):
        self.finds.append(imdb_id)
        return {"tt2861424": {"tv_results": [{"id": 60625}], "movie_results": []},
                "tt0110912": {"tv_results": [], "movie_results": [{"id": 680}]}}.get(imdb_id, {})

    def tv(self, tmdb_id):
        return {"name": f"Show {tmdb_id}"}

    def movie(self, tmdb_id):
        return {"title": f"Movie {tmdb_id}"}


@pytest.fixture
def two(s):
    s.add_all([User(plex_id=A, pms_account_id=1, username="a", overseerr_id=1),
               User(plex_id=B, pms_account_id=B, username="b")])
    s.flush()
    s.add(Title(tmdb_id=1438, media_type="tv", name="The Wire", imdb_id="tt0306414"))
    s.commit()
    return s


def test_import_matches_known_ids_first_and_skips_episodes(two):
    s = two
    kind, rows = taste.parse_imdb_csv(RATINGS_CSV)
    assert taste.store_imdb(s, A, kind, rows) == {"kind": "rating", "rows": 4, "added": 4, "updated": 0}
    tmdb = FakeTMDB()
    assert taste.resolve_imdb(s, tmdb) == {"matched": 3, "none": 0, "skipped": 1}
    assert "tt0306414" not in tmdb.finds          # The Wire was already known locally
    sigs = {(x.tmdb_id, x.media_type): x.rating for x in s.scalars(select(TasteSignal))}
    assert sigs == {(1438, "tv"): 10, (60625, "tv"): 8, (680, "movie"): 3}
    assert taste.store_imdb(s, A, kind, rows)["added"] == 0   # re-upload is a no-op


def test_imdb_ratings_seed_by_score_and_low_ratings_only_exclude(two):
    s = two
    taste.store_imdb(s, A, *taste.parse_imdb_csv(RATINGS_CSV))
    taste.resolve_imdb(s, FakeTMDB())
    s.commit()
    seeds = {x.name: x.weight for x in recs.seeds_for(s, A)}
    assert seeds["The Wire"] > seeds["Rick and Morty"] > 0     # 10 outweighs 8
    assert "Some Movie" not in seeds                           # a 3 is not taste
    assert {(1438, "tv"), (60625, "tv"), (680, "movie")} <= recs._excluded(s, A)


def test_watchlist_entries_exclude_but_do_not_seed(two):
    s = two
    s.add(TasteSignal(plex_id=A, tmdb_id=5, media_type="movie", source="imdb_watchlist", title="Want This"))
    s.commit()
    assert (5, "movie") in recs._excluded(s, A)
    assert "Want This" not in {x.name for x in recs.seeds_for(s, A)}


class FakeOverseerr:
    def requests_of(self, oid):
        assert oid == 1  # only the member who has an Overseerr account is asked
        return [{"tmdb_id": 1438, "media_type": "tv", "created_at": datetime(2025, 12, 3, tzinfo=timezone.utc)},
                {"tmdb_id": 999, "media_type": "movie", "created_at": None}]


def test_overseerr_requests_become_that_members_signals_only(two):
    s = two
    assert taste.sync_overseerr_requests(s, FakeOverseerr(), FakeTMDB()) == 2
    rows = list(s.scalars(select(TasteSignal)))
    assert {r.plex_id for r in rows} == {A}
    assert {r.title for r in rows} == {"The Wire", "Movie 999"}
    assert taste.sync_overseerr_requests(s, FakeOverseerr(), FakeTMDB()) == 0


def _watched(s, pid, tmdb_id, n):
    s.add(Title(tmdb_id=tmdb_id, media_type="tv", name=f"Watched {tmdb_id}"))
    s.flush()
    for e in range(n):
        s.add(Play(history_key=f"{tmdb_id}-{e}", plex_id=pid, media_type="tv", tmdb_id=tmdb_id, rating_key="x",
                   season=1, episode=e + 1, viewed_at=datetime.now(timezone.utc)))


def test_told_is_foundational_but_a_request_is_only_weak_interest(two):
    s = two
    _watched(s, A, 50, 3)                                     # three episodes watched on Plex
    taste.add(s, A, 1438, "tv", "overseerr", "The Wire")      # requested, maybe for a friend
    taste.add(s, A, 60625, "tv", "told", "Rick and Morty")    # "I loved it"
    s.commit()
    w = {x.name: x.weight for x in recs.seeds_for(s, A)}
    assert w["Rick and Morty"] >= w["Watched 50"] > w["The Wire"] > 0


def test_switching_a_title_off_drops_it_from_taste_but_never_recommends_it_back(two):
    s = two
    taste.add(s, A, 1438, "tv", "overseerr", "The Wire")
    s.commit()
    assert taste.set_use(s, A, 1438, "tv", False) == 1
    s.commit()
    assert "The Wire" not in {x.name for x in recs.seeds_for(s, A)}
    assert (1438, "tv") in recs._excluded(s, A)
    assert (1438, "tv") not in recs._excluded(s, B)          # someone else's taste is theirs
    assert taste.add(s, A, 1438, "tv", "overseerr") is False  # saying it again…
    assert s.get(TasteSignal, (A, 1438, "tv", "overseerr")).use_for_picks is True  # …turns it back on


def test_listing_groups_sources_per_title_strongest_first(two):
    s = two
    taste.add(s, A, 1438, "tv", "overseerr", "The Wire")
    taste.add(s, A, 1438, "tv", "told", "The Wire")
    taste.add(s, A, 5, "movie", "mentioned", "Live by Night")
    s.commit()
    items = taste.listing(s, A)
    assert [(i["title"], i["sources"]) for i in items] == [("The Wire", ["told", "overseerr"]),
                                                           ("Live by Night", ["mentioned"])]


def test_plex_plays_still_count_alongside_signals(two):
    s = two
    s.add(Play(history_key="1", plex_id=A, media_type="tv", tmdb_id=1438, rating_key="x", season=1,
               episode=1, viewed_at=datetime.now(timezone.utc)))
    taste.add(s, A, 1438, "tv", "overseerr", "The Wire")
    s.commit()
    assert [x.name for x in recs.seeds_for(s, A)].count("The Wire") == 1


# ── record_taste: a title typed in chat → the entry they mean ────────────────────────────────

DAREDEVIL = [  # TMDB /search/multi order for "daredevil"
    {"id": 202555, "media_type": "tv", "name": "Daredevil: Born Again", "first_air_date": "2025-03-04"},
    {"id": 61889, "media_type": "tv", "name": "Marvel's Daredevil", "first_air_date": "2015-04-10"},
    {"id": 9480, "media_type": "movie", "title": "Daredevil", "release_date": "2003-02-14"},
]


def test_something_they_watched_beats_tmdbs_order():
    p = taste.pick_title(DAREDEVIL, {(61889, "tv")}, "daredevil")["pick"]
    assert (p["tmdb_id"], p["watched"]) == (61889, True)


def test_an_exact_name_wins_when_nothing_is_watched():
    assert taste.pick_title(DAREDEVIL, set(), "Daredevil")["pick"]["tmdb_id"] == 9480


def test_year_and_type_narrow_it():
    assert taste.pick_title(DAREDEVIL, set(), "daredevil", media_type="tv")["pick"]["tmdb_id"] == 202555
    assert taste.pick_title(DAREDEVIL, set(), "daredevil", year=2015)["pick"]["tmdb_id"] == 61889


def test_two_same_name_titles_nobody_watched_is_a_question_not_a_guess():
    two_shogun = [{"id": 126308, "media_type": "tv", "name": "Shōgun", "first_air_date": "2024-02-27"},
                  {"id": 1990, "media_type": "tv", "name": "Shogun", "first_air_date": "1980-09-15"}]
    same = [{**two_shogun[0], "name": "Shogun"}, two_shogun[1]]
    found = taste.pick_title(same, set(), "shogun")
    assert found["pick"] is None and len(found["candidates"]) == 2
    assert taste.pick_title(same, set(), "shogun", year=2024)["pick"]["tmdb_id"] == 126308


def test_known_keys_are_plays_and_taste(two):
    s = two
    s.add(Play(history_key="h1", plex_id=A, media_type="tv", tmdb_id=61889, rating_key="r",
               viewed_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    taste.add(s, A, 9480, "movie", "told", "Daredevil")
    assert taste.known_keys(s, A) == {(61889, "tv"), (9480, "movie")}
    assert taste.known_keys(s, B) == set()


def test_record_likes_as_told_and_dislikes_as_a_thumbs_down(two):
    s = two
    assert taste.record(s, FakeTMDB(), A, 61889, "tv", True)["title"] == "Show 61889"
    assert s.get(TasteSignal, (A, 61889, "tv", "told")) is not None
    taste.record(s, FakeTMDB(), A, 9480, "movie", False)
    assert s.get(TasteSignal, (A, 9480, "movie", "told")) is None
    assert (9480, "movie") in recs._excluded(s, A)


def test_a_watched_spinoff_never_stands_in_for_the_show_they_named():
    got = [{"id": 94997, "media_type": "tv", "name": "House of the Dragon", "first_air_date": "2022-08-21"},
           {"id": 1399, "media_type": "tv", "name": "Game of Thrones", "first_air_date": "2011-04-17"}]
    assert taste.pick_title(got, {(94997, "tv")}, "Game of Thrones")["pick"]["tmdb_id"] == 1399
