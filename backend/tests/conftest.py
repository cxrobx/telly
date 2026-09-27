from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from telly import models  # noqa: F401
from telly.db import Base, make_engine


@pytest.fixture
def s() -> Session:
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine, expire_on_commit=False)()
    yield session
    session.close()


class FakePlex:
    """Mimics PlexClient with the live shapes from spec §2."""

    OWNER = 495743363

    def __init__(self, accounts, history, items):
        self._accounts, self._history, self._items = accounts, history, items
        self.item_calls: list[str] = []

    def owner_plex_id(self):
        return self.OWNER

    def accounts(self):
        return self._accounts

    def history_page(self, start, size=200):
        return len(self._history), self._history[start:start + size]

    def item(self, rating_key):
        self.item_calls.append(rating_key)
        return self._items.get(rating_key)


def ep(hk, account, show_key, show, season, episode, t=1790000000):
    return {"historyKey": f"/status/sessions/history/{hk}", "accountID": account,
            "type": "episode", "grandparentKey": f"/library/metadata/{show_key}",
            "grandparentTitle": show, "parentIndex": season, "index": episode, "viewedAt": t}


def show_item(tmdb_id, year=2022):
    return {"year": year, "Guid": [{"id": "imdb://tt1"}, {"id": f"tmdb://{tmdb_id}"}]}


class FakeTMDB:
    def __init__(self, shows):
        self.shows = shows  # tmdb_id -> payload (mutable between polls)

    def tv(self, tmdb_id):
        return self.shows.get(tmdb_id)
