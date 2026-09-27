"""Tables. Identity is the plex.tv account id (spec §1); every per-person row hangs off it."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes even for timezone=True columns; they're stored as UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class User(Base):
    """A member of the Plex server. Rows come from PMS /accounts, not from sign-in."""

    __tablename__ = "users"

    plex_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pms_account_id: Mapped[int] = mapped_column(BigInteger, unique=True)  # 1 for the owner
    username: Mapped[str] = mapped_column(String, default="")
    is_owner: Mapped[bool] = mapped_column(default=False)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set only by Sign in with Plex. Fernet-encrypted (crypto.py); used for the watchlist only.
    plex_token_enc: Mapped[str | None] = mapped_column(String)
    signed_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    overseerr_id: Mapped[int | None] = mapped_column(Integer)  # for Request, when they have one


class Title(Base):
    """A show or movie, keyed by TMDB id. TV rows also carry the latest TMDB snapshot."""

    __tablename__ = "titles"

    tmdb_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_type: Mapped[str] = mapped_column(String, primary_key=True)  # "tv" | "movie"
    name: Mapped[str] = mapped_column(String, default="")
    year: Mapped[int | None] = mapped_column(Integer)
    poster_path: Mapped[str | None] = mapped_column(String)
    backdrop_path: Mapped[str | None] = mapped_column(String)
    genres: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str | None] = mapped_column(String)
    networks: Mapped[list] = mapped_column(JSON, default=list)
    seasons: Mapped[list] = mapped_column(JSON, default=list)  # [{season, air_date, episode_count}]
    last_episode: Mapped[dict | None] = mapped_column(JSON)  # {season, episode, air_date, name}
    next_episode: Mapped[dict | None] = mapped_column(JSON)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    poll_error: Mapped[str | None] = mapped_column(String)
    imdb_id: Mapped[str | None] = mapped_column(String)


class PlexItem(Base):
    """PMS ratingKey (show or movie) → TMDB id. Cached so each item's guids are fetched once."""

    __tablename__ = "plex_items"

    rating_key: Mapped[str] = mapped_column(String, primary_key=True)
    media_type: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String, default="")
    tmdb_id: Mapped[int | None] = mapped_column(Integer)
    match_status: Mapped[str] = mapped_column(String)  # matched | no_tmdb_guid | missing


class Play(Base):
    """One play from PMS history. history_key is PMS's own id, so re-syncing is idempotent."""

    __tablename__ = "plays"

    history_key: Mapped[str] = mapped_column(String, primary_key=True)
    plex_id: Mapped[int] = mapped_column(ForeignKey("users.plex_id"), index=True)
    media_type: Mapped[str] = mapped_column(String)
    tmdb_id: Mapped[int | None] = mapped_column(Integer, index=True)
    rating_key: Mapped[str] = mapped_column(String)  # the show's (episodes) or movie's key
    season: Mapped[int | None] = mapped_column(Integer)
    episode: Mapped[int | None] = mapped_column(Integer)
    viewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Follow(Base):
    """Who follows which show. `state=unfollowed` is sticky: inference never overrides it."""

    __tablename__ = "follows"

    plex_id: Mapped[int] = mapped_column(ForeignKey("users.plex_id"), primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String)  # watchlist | inferred | manual
    state: Mapped[str] = mapped_column(String, default="following")  # following | unfollowed
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ShowFact(Base):
    """One tracked fact about a show (status, max season, a season's date, last aired ep),
    with a pending value that must hold for N polls before it's confirmed. See detect.py."""

    __tablename__ = "show_facts"

    tmdb_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String, primary_key=True)
    confirmed: Mapped[str | None] = mapped_column(String)
    pending: Mapped[str | None] = mapped_column(String)
    pending_seen: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Event(Base):
    """A show-level change. dedupe_key makes each real-world event exist once, however many
    polls or paths (status flip vs. new season row) report it."""

    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("dedupe_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String)  # renewed | canceled | ended | season_dated | episodes_aired
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class IdentityLink(Base):
    """plexbot requester (surface, external id) → plex_id. Rows come only from a proven
    source: Overseerr's own plexId (web) or a completed /link (discord). Spec §4."""

    __tablename__ = "identity_links"

    surface: Mapped[str] = mapped_column(String, primary_key=True)  # web | discord
    external_id: Mapped[str] = mapped_column(String, primary_key=True)
    plex_id: Mapped[int] = mapped_column(ForeignKey("users.plex_id"), index=True)
    source: Mapped[str] = mapped_column(String)  # overseerr | link | admin (owner ↔ plexbot's configured admin)
    display_name: Mapped[str] = mapped_column(String, default="")
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LinkCode(Base):
    """One-time /link code bound to a Discord id (10-minute TTL, single use)."""

    __tablename__ = "link_codes"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    discord_id: Mapped[str] = mapped_column(String)
    discord_name: Mapped[str] = mapped_column(String, default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    pin_id: Mapped[int | None] = mapped_column(BigInteger)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AlertPref(Base):
    __tablename__ = "alert_prefs"

    plex_id: Mapped[int] = mapped_column(ForeignKey("users.plex_id"), primary_key=True)
    discord_enabled: Mapped[bool] = mapped_column(default=True)
    ntfy_topic: Mapped[str | None] = mapped_column(String)


class Delivery(Base):
    """One alert to one person on one channel. The unique constraint is the
    'never double-ping' guarantee (spec §6): a row exists before the send is attempted."""

    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("plex_id", "event_id", "channel"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plex_id: Mapped[int] = mapped_column(ForeignKey("users.plex_id"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    channel: Mapped[str] = mapped_column(String)  # discord | ntfy
    status: Mapped[str] = mapped_column(String, default="pending")  # pending | sent | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WatchlistItem(Base):
    """A person's Plex watchlist (plex.tv), mirrored nightly from their stored token."""

    __tablename__ = "watchlist_items"

    plex_id: Mapped[int] = mapped_column(ForeignKey("users.plex_id"), primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_type: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, default="")
    added_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LibraryItem(Base):
    """What's on the Plex server, by TMDB id (for "In your library" vs "Request")."""

    __tablename__ = "library"

    tmdb_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_type: Mapped[str] = mapped_column(String, primary_key=True)
    rating_key: Mapped[str] = mapped_column(String)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Recommendation(Base):
    """Precomputed picks for one person. Rebuilt nightly; `reason` is one sentence."""

    __tablename__ = "recommendations"

    plex_id: Mapped[int] = mapped_column(ForeignKey("users.plex_id"), primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_type: Mapped[str] = mapped_column(String, primary_key=True)
    rank: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column()
    title: Mapped[str] = mapped_column(String, default="")
    year: Mapped[int | None] = mapped_column(Integer)
    poster_path: Mapped[str | None] = mapped_column(String)
    backdrop_path: Mapped[str | None] = mapped_column(String)
    overview: Mapped[str] = mapped_column(String, default="")
    reason: Mapped[str] = mapped_column(String, default="")
    because: Mapped[list] = mapped_column(JSON, default=list)  # seed titles
    trending: Mapped[bool] = mapped_column(default=False)
    in_library: Mapped[bool] = mapped_column(default=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Feedback(Base):
    """+1 / -1 on a title. -1 hides it for good; +1 makes it a taste seed."""

    __tablename__ = "feedback"

    plex_id: Mapped[int] = mapped_column(ForeignKey("users.plex_id"), primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_type: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String, default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RenewalCheck(Base):
    """Last news check for a show TMDB hasn't caught up on (renewals.py)."""

    __tablename__ = "renewal_checks"

    tmdb_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    result: Mapped[dict] = mapped_column(JSON, default=dict)


class Rating(Base):
    """Outside scores for a title (ratings.py). IMDb from OMDb; Rotten Tomatoes from OMDb for
    movies and MDBList for series (OMDb has no RT for series: 0 of 12 checked, 2026-09-27)."""

    __tablename__ = "ratings"

    tmdb_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_type: Mapped[str] = mapped_column(String, primary_key=True)
    imdb_id: Mapped[str | None] = mapped_column(String)
    imdb: Mapped[float | None] = mapped_column()          # 0-10
    imdb_votes: Mapped[int | None] = mapped_column(Integer)
    rt_critic: Mapped[int | None] = mapped_column(Integer)    # Tomatometer, 0-100
    rt_audience: Mapped[int | None] = mapped_column(Integer)  # Popcornmeter, 0-100
    metacritic: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)



class TasteSignal(Base):
    """Something that says a person likes (or knows) a title, besides Plex plays (taste.py).

    source, strongest first: told (they said they liked it, in Telly or to plexbot) |
    imdb_rating | overseerr (they requested it: interest, not proof they like it) | mentioned
    (it came up in their plexbot chats) | imdb_watchlist. `rating` is their own 1-10 score where
    the source has one. use_for_picks=False keeps it out of their taste (it still isn't
    recommended back to them)."""

    __tablename__ = "taste_signals"

    plex_id: Mapped[int] = mapped_column(ForeignKey("users.plex_id"), primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_type: Mapped[str] = mapped_column(String, primary_key=True)
    source: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, default="")
    rating: Mapped[int | None] = mapped_column(Integer)
    noted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    use_for_picks: Mapped[bool] = mapped_column(default=True, server_default="1")
    poster_path: Mapped[str | None] = mapped_column(String)


class ImdbRow(Base):
    """A row from someone's IMDb CSV export, kept until its IMDb id is matched to TMDB."""

    __tablename__ = "imdb_rows"

    plex_id: Mapped[int] = mapped_column(ForeignKey("users.plex_id"), primary_key=True)
    imdb_id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, primary_key=True)  # rating | watchlist
    title: Mapped[str] = mapped_column(String, default="")
    title_type: Mapped[str] = mapped_column(String, default="")
    rating: Mapped[int | None] = mapped_column(Integer)
    noted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tmdb_id: Mapped[int | None] = mapped_column(Integer)
    media_type: Mapped[str | None] = mapped_column(String)
    match: Mapped[str] = mapped_column(String, default="pending")  # pending | matched | none | skipped
