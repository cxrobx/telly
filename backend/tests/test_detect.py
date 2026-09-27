"""Change-detection invariants (spec §6). Each test is one real-world TMDB situation."""

from __future__ import annotations

from telly.detect import FactState, detect, observe


def tv(status="Returning Series", seasons=((1, "2024-01-01", 8),), last=(1, 8), next_=None) -> dict:
    """A minimal TMDB /tv/{id} payload. seasons = (number, air_date, episode_count)."""
    return {
        "status": status,
        "seasons": [{"season_number": 0, "air_date": None, "episode_count": 3}]
        + [{"season_number": n, "air_date": d, "episode_count": c} for n, d, c in seasons],
        "last_episode_to_air": {"season_number": last[0], "episode_number": last[1]} if last else None,
        "next_episode_to_air": next_,
    }


def run(*snapshots, confirm=2):
    """Poll a show once per snapshot; the first poll is its baseline. Returns events per poll."""
    facts: dict[str, FactState] = {}
    return [
        detect(42, snap, facts, first_seen=(i == 0), confirm=confirm)
        for i, snap in enumerate(snapshots)
    ]


def kinds(polls):
    return [[e.kind for e in p] for p in polls]


# ── baseline ─────────────────────────────────────────────────────────────


def test_first_poll_is_a_baseline_and_emits_nothing():
    polls = run(tv(status="Ended", seasons=((1, "2020-01-01", 8), (2, None, 0)), last=(1, 8)))
    assert polls == [[]]


def test_unchanged_show_stays_quiet():
    assert kinds(run(tv(), tv(), tv())) == [[], [], []]


def test_observe_ignores_specials_and_only_dates_upcoming_seasons():
    obs = observe(tv(seasons=((1, "2024-01-01", 8), (2, "2027-03-01", 0)), last=(1, 8)))
    assert obs["max_season"] == "2"
    assert "season:1:air_date" not in obs
    assert obs["season:2:air_date"] == "2027-03-01"


# ── renewals ─────────────────────────────────────────────────────────────


def test_new_season_row_is_a_renewal_after_two_polls():
    before = tv(seasons=((1, "2024-01-01", 8),))
    after = tv(seasons=((1, "2024-01-01", 8), (2, None, 0)))
    polls = run(before, after, after)
    assert kinds(polls) == [[], [], ["renewed"]]
    assert polls[2][0].payload == {"season": 2, "via": "season_added"}
    assert polls[2][0].dedupe_key == "renewed:42:2"


def test_season_row_that_flips_back_never_alerts():
    before = tv(seasons=((1, "2024-01-01", 8),))
    flip = tv(seasons=((1, "2024-01-01", 8), (2, None, 0)))
    assert kinds(run(before, flip, before, flip, before)) == [[], [], [], [], []]


def test_ended_show_coming_back_is_a_renewal():
    ended = tv(status="Ended")
    back = tv(status="Returning Series")
    polls = run(ended, back, back)
    assert kinds(polls) == [[], [], ["renewed"]]
    assert polls[2][0].payload == {"season": 2, "via": "status"}


def test_status_flip_and_new_season_together_are_one_renewal():
    ended = tv(status="Ended")
    back = tv(status="Returning Series", seasons=((1, "2024-01-01", 8), (2, None, 0)))
    polls = run(ended, back, back)
    assert kinds(polls) == [[], [], ["renewed"]]
    assert polls[2][0].dedupe_key == "renewed:42:2"


def test_status_flip_then_season_row_later_share_one_dedupe_key():
    ended = tv(status="Ended")
    back = tv(status="Returning Series")
    row = tv(status="Returning Series", seasons=((1, "2024-01-01", 8), (2, None, 0)))
    polls = run(ended, back, back, row, row)
    keys = [e.dedupe_key for p in polls for e in p]
    assert keys == ["renewed:42:2", "renewed:42:2"]  # the DB unique constraint keeps one


def test_a_season_row_that_already_aired_is_not_a_renewal():
    before = tv(seasons=((1, "2024-01-01", 8),), last=(1, 8))
    backfilled = tv(seasons=((1, "2024-01-01", 8), (2, "2025-01-01", 8)), last=(2, 8))
    assert all(e.kind != "renewed" for p in run(before, backfilled, backfilled) for e in p)


# ── cancellations / endings ───────────────────────────────────────────────


def test_cancellation_needs_two_polls():
    polls = run(tv(), tv(status="Canceled"), tv(status="Canceled"))
    assert kinds(polls) == [[], [], ["canceled"]]
    assert polls[2][0].payload == {"after_season": 1}


def test_ended():
    assert kinds(run(tv(), tv(status="Ended"), tv(status="Ended"))) == [[], [], ["ended"]]


def test_canceled_to_ended_cleanup_is_not_news():
    assert kinds(run(tv(status="Canceled"), tv(status="Ended"), tv(status="Ended"))) == [[], [], []]


# ── season dates ─────────────────────────────────────────────────────────


def test_upcoming_season_getting_a_date():
    undated = tv(seasons=((1, "2024-01-01", 8), (2, None, 0)))
    dated = tv(seasons=((1, "2024-01-01", 8), (2, "2027-01-14", 0)))
    polls = run(undated, dated, dated)
    assert kinds(polls) == [[], [], ["season_dated"]]
    assert polls[2][0].payload == {"season": 2, "air_date": "2027-01-14", "previous": None}


def test_premiere_date_moving_is_a_new_event():
    a = tv(seasons=((1, "2024-01-01", 8), (2, "2027-01-14", 0)))
    b = tv(seasons=((1, "2024-01-01", 8), (2, "2027-02-04", 0)))
    polls = run(a, b, b)
    assert polls[2][0].payload == {"season": 2, "air_date": "2027-02-04", "previous": "2027-01-14"}


def test_renewal_with_a_date_emits_both():
    before = tv(seasons=((1, "2024-01-01", 8),))
    after = tv(seasons=((1, "2024-01-01", 8), (2, "2027-01-14", 0)))
    assert sorted(kinds(run(before, after, after))[2]) == ["renewed", "season_dated"]


# ── episodes ─────────────────────────────────────────────────────────────


def test_weekly_episode_alerts_on_first_sight():
    s = ((1, "2024-01-01", 8),)
    polls = run(tv(seasons=s, last=(1, 3)), tv(seasons=s, last=(1, 4)))
    assert polls[1][0].kind == "episodes_aired"
    assert polls[1][0].payload == {"season": 1, "from_episode": 4, "to_episode": 4, "premiere": False}


def test_binge_drop_is_one_event_covering_the_whole_season():
    s = ((1, "2024-01-01", 8), (2, "2026-10-01", 8))
    polls = run(tv(seasons=s, last=(1, 8)), tv(seasons=s, last=(2, 8)))
    assert polls[1] and polls[1][0].payload == {
        "season": 2, "from_episode": 1, "to_episode": 8, "premiere": True,
    }


def test_brand_new_show_first_episode_is_a_premiere():
    unaired = tv(status="In Production", seasons=((1, "2026-10-01", 8),), last=None)
    aired = tv(status="Returning Series", seasons=((1, "2026-10-01", 8),), last=(1, 1))
    polls = run(unaired, aired)
    assert [e.kind for e in polls[1]] == ["episodes_aired"]
    assert polls[1][0].payload["premiere"] is True


def test_backwards_correction_is_silent():
    s = ((1, "2024-01-01", 8),)
    assert kinds(run(tv(seasons=s, last=(1, 5)), tv(seasons=s, last=(1, 4)))) == [[], []]


def test_same_episode_event_is_stable_across_repolls():
    s = ((1, "2024-01-01", 8),)
    polls = run(tv(seasons=s, last=(1, 3)), tv(seasons=s, last=(1, 4)), tv(seasons=s, last=(1, 4)))
    assert [e.dedupe_key for p in polls for e in p] == ["aired:42:1:4"]
