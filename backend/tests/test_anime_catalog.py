from datetime import timedelta
import httpx
import pytest
from telly import anime
from telly.models import AnimeCatalog, LibraryItem, Play, Rating, User, WatchEntry, utcnow
from telly.clients.tmdb import TMDBClient


def title(i, score=8.5, votes=1000, **kw):
    return {"id": i, "name": f"Example Anime {i}", "genre_ids": [16], "original_language": "ja",
            "vote_average": score, "vote_count": votes, "overview": "An example story.", **kw}


class CatalogTMDB:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail
    def acclaimed_anime(self, kind, page):
        self.calls.append((kind, page))
        if self.fail:
            raise httpx.ConnectError("offline")
        return ([title(1), title(2, score=9, votes=200), title(3, votes=20), title(4, adult=True),
                 title(5, score=6), title(6, original_language="en")] if page == 1 else [title(1)])


def test_catalog_rank_quality_dedup_cache_and_outage(s):
    tmdb = CatalogTMDB()
    assert anime.refresh(s, tmdb) == 4  # same id can be a different show and movie
    assert len(tmdb.calls) == 10
    snapshot = s.get(AnimeCatalog, 1)
    assert snapshot.picks[0]["tmdb_id"] == 1  # established acclaim beats a small high score
    assert all(p["tmdb_id"] in {1, 2} for p in snapshot.picks)
    anime.refresh(s, tmdb)
    assert len(tmdb.calls) == 10
    before = snapshot.picks
    with pytest.raises(httpx.ConnectError):
        anime.refresh(s, CatalogTMDB(fail=True), force=True)
    assert s.get(AnimeCatalog, 1).picks == before


def test_catalog_includes_watched_and_marks_only_the_callers_history(s):
    s.add_all([User(plex_id=111, pms_account_id=111), User(plex_id=222, pms_account_id=222)])
    s.flush()
    anime.refresh(s, CatalogTMDB())
    s.add_all([Play(history_key="example-play", plex_id=111, tmdb_id=1, media_type="tv", rating_key="example", viewed_at=utcnow()),
               WatchEntry(plex_id=111, tmdb_id=2, media_type="movie", title="Example", status="finished"),
               LibraryItem(tmdb_id=1, media_type="tv", rating_key="example"),
               Rating(tmdb_id=1, media_type="tv", imdb=9.0, imdb_id="tt1234567")])
    s.flush()
    a, b = anime.for_user(s, 111), anime.for_user(s, 222)
    assert len(a["picks"]) == len(b["picks"]) == 4
    assert sum(p["watched"] for p in a["picks"]) == 2
    assert not any(p["watched"] for p in b["picks"])
    row = next(p for p in a["picks"] if p["tmdb_id"] == 1 and p["media_type"] == "tv")
    assert row["in_library"] and row["ratings"]["imdb"] == 9.0
    assert row["tmdb_rating"] == 8.5  # distinct rating sources


def test_catalog_missing_snapshot_and_empty_refresh(s):
    assert anime.for_user(s, 111) == {"picks": [], "updated_at": None}
    class Empty:
        def acclaimed_anime(self, *a):
            return []
    with pytest.raises(ValueError):
        anime.refresh(s, Empty())


def test_acclaimed_discovery_queries_only_public_criteria():
    def respond(request):
        assert request.url.path == "/3/discover/movie"
        assert dict(request.url.params) == {"with_genres": "16", "with_original_language": "ja",
            "include_adult": "false", "sort_by": "vote_average.desc", "page": "3",
            "vote_count.gte": "200", "vote_average.gte": "7.5"}
        return httpx.Response(200, json={"results": [title(1)]})
    tmdb = TMDBClient(client=httpx.Client(base_url="https://api.themoviedb.org/3", transport=httpx.MockTransport(respond)))
    assert tmdb.acclaimed_anime("movie", 3)[0]["id"] == 1
