"use client";

import Link from "next/link";
import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { BellOff, Check, Plus, Search, X } from "lucide-react";
import { useBackdrop } from "@/components/backdrop";
import { Chip, Empty, Poster, Section, Skeleton, statusTone } from "@/components/ui";
import { Scores } from "@/components/scores";
import { api, backdrop, epLabel, fmtDate, relDay, useApi, type Followed } from "@/lib/api";

type Result = { tmdb_id: number; name: string; first_aired: string | null; overview: string; following: boolean };

const WHY: Record<Followed["followed_because"], string> = {
  watchlist: "On your Plex watchlist",
  inferred: "From your watch history",
  manual: "Added by you",
};

function SearchBox({ onFollowed }: { onFollowed: () => void }) {
  const params = useSearchParams();
  const ref = useRef<HTMLInputElement>(null);
  const [q, setQ] = useState("");
  const [results, setResults] = useState<Result[]>([]);
  const [busy, setBusy] = useState<number | null>(null);
  useEffect(() => {
    if (params.get("search")) ref.current?.focus();
  }, [params]);
  const searchable = q.trim().length >= 2;
  useEffect(() => {
    if (!searchable) return;
    const t = setTimeout(() => api<{ results: Result[] }>(`/search?q=${encodeURIComponent(q)}`).then((r) => setResults(r.results)), 250);
    return () => clearTimeout(t);
  }, [q, searchable]);
  const shown = searchable ? results : [];
  const follow = async (r: Result) => {
    setBusy(r.tmdb_id);
    await api(`/shows/${r.tmdb_id}/follow`, { method: "POST" });
    setResults((rs) => rs.map((x) => (x.tmdb_id === r.tmdb_id ? { ...x, following: true } : x)));
    setBusy(null);
    onFollowed();
  };
  return (
    <div className="search">
      <label className="glass search-box">
        <Search size={18} />
        <input ref={ref} value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find a show to follow" aria-label="Find a show to follow" />
        {q && (
          <button className="icon-btn ghost" aria-label="Clear" onClick={() => setQ("")}>
            <X size={16} />
          </button>
        )}
      </label>
      {shown.length > 0 && (
        <ul className="glass search-results">
          {shown.map((r) => (
            <li key={r.tmdb_id} className="search-row">
              <Link href={`/show/${r.tmdb_id}`} className="search-name">
                {r.name} {r.first_aired && <span className="muted">{r.first_aired.slice(0, 4)}</span>}
              </Link>
              <button className="btn btn-small" disabled={r.following || busy === r.tmdb_id} onClick={() => follow(r)}>
                {r.following ? (
                  <>
                    <Check size={14} /> Following
                  </>
                ) : (
                  <>
                    <Plus size={14} /> Follow
                  </>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

const ALIVE = new Set(["Returning Series", "In Production", "Planned", "Pilot"]);

function nextLine(s: Followed): { text: string; tone: "accent" | "muted" } {
  if (s.next_episode) {
    const e = s.next_episode;
    return { text: `${epLabel(e.season, e.episode)} · ${relDay(e.air_date)}`, tone: "accent" };
  }
  const n = s.next_season;
  if (n?.air_date) return { text: `Season ${n.season} · ${fmtDate(n.air_date, { month: "short", day: "numeric", year: "numeric" })}`, tone: "accent" };
  if (n) return { text: `Season ${n.season} renewed · date TBA${n.source && n.source !== "TMDB" ? ` · ${n.source}` : ""}`, tone: "accent" };
  const last = s.last_episode?.season;
  if (s.status === "Canceled") return { text: last ? `Canceled after season ${last}` : "Canceled", tone: "muted" };
  if (s.status === "Ended") return { text: last ? `Ended after season ${last}` : "Ended", tone: "muted" };
  return { text: "No new season announced yet", tone: "muted" };
}

function statusLabel(s: Followed) {
  if (s.next_episode) return "Airing";
  if (s.next_season) return "Renewed";
  return s.status === "Returning Series" ? "Returning" : (s.status ?? "Unknown");
}

function FollowCard({ show, onUnfollow }: { show: Followed; onUnfollow: () => void }) {
  const line = nextLine(show);
  return (
    <article className="glass pick-card follow-card lift" role="listitem">
      <Link href={`/show/${show.tmdb_id}`}>
        <div className="pick-poster">
          <Poster path={show.poster_path} alt={show.name} />
          <div className="pick-badges">
            <Chip tone={show.next_episode || show.next_season ? "accent" : statusTone(show.status)}>{statusLabel(show)}</Chip>
          </div>
        </div>
      </Link>
      <div className="pick-body">
        <Link href={`/show/${show.tmdb_id}`} className="pick-title">
          {show.name} {show.year && <span className="pick-year">{show.year}</span>}
        </Link>
        <Scores ratings={show.ratings} compact />
        <p className={`follow-next ${line.tone}`}>{line.text}</p>
        <div className="pick-actions">
          <span className="follow-why">{WHY[show.followed_because]}</span>
          <button className="icon-btn follow-unfollow" onClick={onUnfollow} aria-label={`Unfollow ${show.name}`} title="Unfollow">
            <BellOff size={15} />
          </button>
        </div>
      </div>
    </article>
  );
}

export default function FollowingPage() {
  const { data, loading, reload, setData } = useApi<{ shows: Followed[] }>("/following");
  const groups = useMemo(() => {
    const shows = data?.shows ?? [];
    const airing = shows.filter((s) => s.next_episode).sort((a, b) => a.next_episode!.air_date.localeCompare(b.next_episode!.air_date));
    const back = shows
      .filter((s) => !s.next_episode && (ALIVE.has(s.status ?? "") || s.next_season))
      .sort((a, b) => {
        const ad = a.next_season?.air_date ?? "9999", bd = b.next_season?.air_date ?? "9999";
        return ad.localeCompare(bd) || Number(!a.next_season) - Number(!b.next_season) || a.name.localeCompare(b.name);
      });
    const done = shows.filter((s) => !airing.includes(s) && !back.includes(s));
    return [
      { key: "airing", title: "Airing now", shows: airing },
      { key: "back", title: "Coming back", shows: back },
      { key: "done", title: "Ended & canceled", shows: done },
    ].filter((g) => g.shows.length);
  }, [data]);
  const hero = groups[0]?.shows.find((s) => s.backdrop_path);
  useBackdrop(backdrop(hero?.backdrop_path, "w780"));

  const unfollow = async (id: number) => {
    setData((d) => (d ? { shows: d.shows.filter((s) => s.tmdb_id !== id) } : d));
    await api(`/shows/${id}/unfollow`, { method: "POST" });
  };
  return (
    <div className="page">
      <header className="page-head">
        <h1 className="page-title">Following</h1>
        <p className="page-sub">
          You get alerts for these: new episodes, premiere dates, renewals and cancellations.
        </p>
      </header>
      <Suspense>
        <SearchBox onFollowed={reload} />
      </Suspense>
      {loading || !data ? (
        <Skeleton rows={5} />
      ) : data.shows.length === 0 ? (
        <Empty>You&apos;re not following anything yet. Search above, or add shows to your Plex watchlist.</Empty>
      ) : (
        groups.map((g) => (
          <Section key={g.key} title={g.title} action={<span className="muted small">{g.shows.length}</span>}>
            <div className="picks-grid follow-grid" role="list" aria-label={g.title}>
              {g.shows.map((s) => (
                <FollowCard key={s.tmdb_id} show={s} onUnfollow={() => unfollow(s.tmdb_id)} />
              ))}
            </div>
          </Section>
        ))
      )}
    </div>
  );
}
