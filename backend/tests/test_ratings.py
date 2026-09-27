"""IMDb / Rotten Tomatoes scores (ratings.py). OMDb shapes are the live ones from 2026-09-27."""

from __future__ import annotations

from datetime import timedelta

from telly import ratings
from telly.models import Follow, Rating, Recommendation, Title, User, utcnow
from telly.recs import for_user

A = 111

SUCCESSION = {"Response": "True", "imdbRating": "8.8", "imdbVotes": "346,930", "Metascore": "N/A",
              "Ratings": [{"Source": "Internet Movie Database", "Value": "8.8/10"}]}
A_MOVIE = {"Response": "True", "imdbRating": "7.4", "imdbVotes": "1,204", "Metascore": "71",
           "Ratings": [{"Source": "Internet Movie Database", "Value": "7.4/10"},
                       {"Source": "Rotten Tomatoes", "Value": "92%"}, {"Source": "Metacritic", "Value": "71/100"}]}


def test_parse_omdb_series_has_imdb_but_no_rt():
    assert ratings.parse_omdb(SUCCESSION) == {"imdb": 8.8, "imdb_votes": 346930, "metacritic": None,
                                              "rt_critic": None}


def test_parse_omdb_movie_has_rt():
    assert ratings.parse_omdb(A_MOVIE) == {"imdb": 7.4, "imdb_votes": 1204, "metacritic": 71, "rt_critic": 92}


def test_parse_omdb_all_missing():
    assert ratings.parse_omdb({"imdbRating": "N/A", "imdbVotes": "N/A"}) == {
        "imdb": None, "imdb_votes": None, "metacritic": None, "rt_critic": None}


class FakeOMDb:
    def __init__(self, by_id, search=None, seasons=None):
        self.by_id, self.calls = by_id, []
        self.search, self.seasons = search or {}, seasons or {}  # seasons: {(imdb_id, n): episodes}

    def get(self, imdb_id):
        self.calls.append(imdb_id)
        d = self.by_id.get(imdb_id)
        return ratings.parse_omdb(d) if d else None

    def search_series(self, title):
        return self.search.get(title, [])

    def total_seasons(self, imdb_id):
        return max((n for i, n in self.seasons if i == imdb_id), default=None)

    def season(self, imdb_id, n):
        return self.seasons.get((imdb_id, n), [])


class FakeTMDB:
    def __init__(self, ids, shows=None):
        self.ids, self.calls, self.shows = ids, [], shows or {}

    def imdb_id(self, media_type, tmdb_id):
        self.calls.append((media_type, tmdb_id))
        return self.ids.get((media_type, tmdb_id))

    def tv(self, tmdb_id):
        return self.shows.get(tmdb_id)


def _world(s):
    s.add(User(plex_id=A, pms_account_id=1, username="a"))
    s.flush()
    s.add_all([Title(tmdb_id=1, media_type="tv", name="Succession", imdb_id="tt7660850"),
               Follow(plex_id=A, tmdb_id=1, source="inferred"),
               Recommendation(plex_id=A, tmdb_id=2, media_type="movie", rank=1, score=1, title="A Movie", reason="r")])
    s.commit()


def test_refresh_uses_known_ids_looks_up_missing_ones_and_is_weekly(s):
    _world(s)
    omdb, tmdb = FakeOMDb({"tt7660850": SUCCESSION, "tt0000002": A_MOVIE}), FakeTMDB({("movie", 2): "tt0000002"})
    r = ratings.refresh(s, tmdb, omdb)
    assert r.refreshed == 2
    assert tmdb.calls == [("movie", 2)]                    # the show's id came from its TMDB poll
    assert s.get(Rating, (1, "tv")).imdb == 8.8 and s.get(Rating, (1, "tv")).rt_critic is None
    assert s.get(Rating, (2, "movie")).rt_critic == 92
    r2 = ratings.refresh(s, tmdb, omdb)
    assert r2.refreshed == 0 and r2.skipped_fresh == 2 and len(omdb.calls) == 2
    s.get(Rating, (1, "tv")).fetched_at = utcnow() - timedelta(days=8)
    s.commit()
    assert ratings.refresh(s, tmdb, omdb).refreshed == 1   # stale again after a week


# Live MDBList responses, 2026-09-27 (trimmed to the ratings array).
FALLOUT_NEW_HOST = {"ratings": [
    {"source": "imdb", "value": 8.3, "score": 83, "votes": 404242, "url": 146},
    {"source": "tomatoes", "value": 94, "score": 94, "votes": 204, "url": "/tv/fallout", "fresh": 1},
    {"source": "popcorn", "value": 96, "score": 96, "votes": None, "url": "/tv/fallout"},
    {"source": "letterboxd", "value": None, "score": None, "votes": None, "url": None}]}
SUCCESSION_LEGACY_HOST = {"ratings": [
    {"source": "tomatoes", "value": 95, "score": 95, "votes": 345, "url": "/tv/succession"},
    {"source": "tomatoesaudience", "value": 88, "score": 88, "votes": None},
    {"source": "letterboxd", "value": 0, "score": None, "votes": None, "url": None}]}


def test_parse_mdblist_both_hosts_audience_names():
    assert ratings.parse_mdblist(FALLOUT_NEW_HOST) == {"rt_critic": 94, "rt_audience": 96,
                                                      "imdb": 8.3, "imdb_votes": 404242}
    assert ratings.parse_mdblist(SUCCESSION_LEGACY_HOST) == {"rt_critic": 95, "rt_audience": 88,
                                                             "imdb": None, "imdb_votes": None}
    assert ratings.parse_mdblist({"ratings": [{"source": "tomatoes", "value": None}]}) is None


def _mdb(critic, audience, imdb=None, votes=None):
    return {"rt_critic": critic, "rt_audience": audience, "imdb": imdb, "imdb_votes": votes}


def test_rotten_tomatoes_comes_from_the_rt_source_for_series(s):
    _world(s)
    ratings.refresh(s, FakeTMDB({}), FakeOMDb({"tt7660850": SUCCESSION}),
                    rt_source=lambda mt, tid: _mdb(95, 88) if mt == "tv" else None)
    row = s.get(Rating, (1, "tv"))
    assert (row.imdb, row.rt_critic, row.rt_audience) == (8.8, 95, 88)


def test_a_movie_keeps_omdbs_critic_score_when_mdblist_has_only_the_audience(s):
    _world(s)
    ratings.refresh(s, FakeTMDB({("movie", 2): "tt0000002"}), FakeOMDb({"tt0000002": A_MOVIE}),
                    rt_source=lambda mt, tid: _mdb(None, 81))
    row = s.get(Rating, (2, "movie"))
    assert (row.rt_critic, row.rt_audience) == (92, 81)


def test_a_title_with_no_imdb_id_is_remembered_not_retried_nightly(s):
    _world(s)
    tmdb = FakeTMDB({})
    ratings.refresh(s, tmdb, FakeOMDb({"tt7660850": SUCCESSION}))
    assert ratings.refresh(s, tmdb, FakeOMDb({})).refreshed == 0
    assert tmdb.calls == [("movie", 2)]


def test_picks_carry_their_scores_and_unscored_picks_carry_none(s):
    _world(s)
    ratings.refresh(s, FakeTMDB({("movie", 2): "tt0000002"}), FakeOMDb({"tt0000002": A_MOVIE}))
    pick = for_user(s, A)[0]
    assert pick["ratings"]["imdb"] == 7.4 and pick["ratings"]["rt_critic"] == 92
    s.add(Recommendation(plex_id=A, tmdb_id=3, media_type="tv", rank=2, score=1, title="Unscored", reason="r"))
    s.commit()
    assert for_user(s, A)[1]["ratings"] is None


def test_mdblist_fills_imdb_when_omdb_has_none_yet_but_never_overrides_it(s):
    _world(s)  # Neagley, 2026-09-27: OMDb "N/A" a week after release, MDBList 7.8
    omdb = FakeOMDb({"tt7660850": {**SUCCESSION, "imdbRating": "N/A", "imdbVotes": "N/A"},
                     "tt0000002": A_MOVIE})
    ratings.refresh(s, FakeTMDB({("movie", 2): "tt0000002"}), omdb,
                    rt_source=lambda mt, tid: _mdb(94, 83, imdb=7.8, votes=13003) if mt == "tv"
                    else _mdb(None, None, imdb=1.0, votes=5))
    assert (s.get(Rating, (1, "tv")).imdb, s.get(Rating, (1, "tv")).imdb_votes) == (7.8, 13003)
    assert s.get(Rating, (2, "movie")).imdb == 7.4  # OMDb had one: kept


MONSTER = {"Title": "Monster", "Year": "2022–", "imdbID": "tt13207736", "Type": "series"}
OLD_MONSTER = {"Title": "Monster", "Year": "2004–2005", "imdbID": "tt0434706", "Type": "series"}
LIZZIE = {"name": "Monster: The Lizzie Borden Story", "first_air_date": "2026-09-17"}


def _season_world(s):
    s.add(User(plex_id=A, pms_account_id=1, username="a"))
    s.flush()
    s.add(Recommendation(plex_id=A, tmdb_id=9, media_type="tv", rank=1, score=1, title="Lizzie", reason="r"))
    s.commit()


def test_a_season_listed_as_its_own_show_gets_its_seasons_imdb_score(s):
    _season_world(s)
    omdb = FakeOMDb({"tt13207736": {**SUCCESSION, "imdbRating": "7.7"}},
                    search={"Monster": [MONSTER, OLD_MONSTER]},
                    seasons={("tt13207736", 3): [{"Released": "2025-10-03", "imdbRating": "7.3"}],
                             ("tt13207736", 4): [{"Released": "2026-09-17", "imdbRating": "7.0"},
                                                 {"Released": "2026-09-17", "imdbRating": "7.5"},
                                                 {"Released": "2026-09-17", "imdbRating": "N/A"}]})
    ratings.refresh(s, FakeTMDB({}, shows={9: LIZZIE}), omdb)
    row = s.get(Rating, (9, "tv"))
    assert (row.imdb_id, row.imdb, row.imdb_votes) == ("tt13207736", 7.2, None)


def test_a_season_with_no_rated_episodes_falls_back_to_the_series_score(s):
    _season_world(s)  # the real state on 2026-09-27: season 4 listed, nothing rated yet
    omdb = FakeOMDb({"tt13207736": {**SUCCESSION, "imdbRating": "7.7", "imdbVotes": "226,197"}},
                    search={"Monster": [MONSTER]},
                    seasons={("tt13207736", 4): [{"Released": "N/A", "imdbRating": "N/A"},
                                                 {"Released": "2026-09-17", "imdbRating": "N/A"}]})
    ratings.refresh(s, FakeTMDB({}, shows={9: LIZZIE}), omdb)
    assert s.get(Rating, (9, "tv")).imdb == 7.7


def test_an_ambiguous_parent_series_gets_no_score_rather_than_a_wrong_one(s):
    _season_world(s)
    other = {**MONSTER, "imdbID": "tt9999999"}
    omdb = FakeOMDb({"tt13207736": SUCCESSION, "tt9999999": SUCCESSION}, search={"Monster": [MONSTER, other]})
    ratings.refresh(s, FakeTMDB({}, shows={9: LIZZIE}), omdb)
    row = s.get(Rating, (9, "tv"))
    assert row.imdb_id is None and row.imdb is None


def test_a_show_without_a_colon_is_never_matched_to_a_parent(s):
    _season_world(s)
    omdb = FakeOMDb({}, search={"Neagley": [MONSTER]})
    ratings.refresh(s, FakeTMDB({}, shows={9: {"name": "Neagley", "first_air_date": "2026-09-16"}}), omdb)
    assert s.get(Rating, (9, "tv")).imdb_id is None


def test_spans():
    assert ratings._spans("2022–", 2026) and ratings._spans("2004–2005", 2005)
    assert not ratings._spans("2004–2005", 2026) and not ratings._spans("2027–", 2026)
    assert ratings._spans("2026", 2026) and not ratings._spans("2020", 2026)
