"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { Check, Heart, Plus, Search, ThumbsDown, ThumbsUp, Trash2, X } from "lucide-react";
import { useBackdrop } from "@/components/backdrop";
import { Chip, Empty, Poster, Section, Skeleton } from "@/components/ui";
import { api, backdrop, fmtDate, useApi, type HistoryItem, type WatchRating, type WatchStatus } from "@/lib/api";

type Hit = { tmdb_id: number; media_type: "tv" | "movie"; name: string; year: string; poster_path: string | null };

// Three labelled levels, not stars: coarse scales get many more ratings, and the middle of a
// 5- or 10-point scale is mostly noise. Liked vs. loved is the split worth keeping.
const RATINGS: { key: WatchRating; label: string; icon: typeof Heart; tone: "bad" | "good" | "accent" }[] = [
  { key: "not_for_me", label: "Not for me", icon: ThumbsDown, tone: "bad" },
  { key: "liked", label: "Liked it", icon: ThumbsUp, tone: "good" },
  { key: "loved", label: "Loved it", icon: Heart, tone: "accent" },
];

const STATUS_LABEL: Record<WatchStatus, string> = {
  caught_up: "Caught up",
  finished: "Finished",
  watching: "Watching",
  stalled: "Still watching?",
  dropped: "Dropped",
};

const FILTERS = [
  { key: "all", label: "All" },
  { key: "tv", label: "Shows" },
  { key: "movie", label: "Movies" },
  { key: "unrated", label: "Not rated" },
] as const;

const key = (x: { tmdb_id: number; media_type: string }) => `${x.media_type}-${x.tmdb_id}`;

function AddBox({ known, onAdded }: { known: Set<string>; onAdded: () => void }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const searchable = q.trim().length >= 2;
  useEffect(() => {
    if (!searchable) return;
    const t = setTimeout(() => api<{ results: Hit[] }>(`/search/titles?q=${encodeURIComponent(q)}`).then((r) => setHits(r.results)), 250);
    return () => clearTimeout(t);
  }, [q, searchable]);
  const add = async (h: Hit) => {
    setBusy(key(h));
    await api("/history/add", { body: { tmdb_id: h.tmdb_id, media_type: h.media_type } });
    setBusy(null);
    onAdded();
  };
  const shown = searchable ? hits : [];
  return (
    <div className="search">
      <label className="glass search-box">
        <Search size={18} />
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Add something you watched elsewhere" aria-label="Add something you watched elsewhere" />
        {q && (
          <button className="icon-btn ghost" aria-label="Clear" onClick={() => setQ("")}>
            <X size={16} />
          </button>
        )}
      </label>
      {shown.length > 0 && (
        <ul className="glass search-results">
          {shown.map((h) => {
            const there = known.has(key(h));
            return (
              <li key={key(h)} className="search-row">
                <span className="search-name">
                  {h.name} <span className="muted">{h.year} · {h.media_type === "tv" ? "show" : "movie"}</span>
                </span>
                <button className="btn btn-small" disabled={there || busy === key(h)} onClick={() => add(h)}>
                  {there ? (
                    <>
                      <Check size={14} /> In history
                    </>
                  ) : (
                    <>
                      <Plus size={14} /> Watched
                    </>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function progress(it: HistoryItem): string {
  const when = it.last_watched ?? it.added_at;
  const day = when ? fmtDate(when, { month: "short", day: "numeric", year: "numeric" }) : null;
  if (it.source === "manual") return day ? `Added by you · ${day}` : "Added by you";
  if (it.media_type === "movie") return day ? `Watched ${day}` : "Watched";
  const eps = it.aired ? `${it.episodes} of ${it.aired} episodes` : `${it.episodes} episode${it.episodes === 1 ? "" : "s"}`;
  return day ? `${eps} · ${day}` : eps;
}

function Rate({ item, onRate }: { item: HistoryItem; onRate: (r: WatchRating | null) => void }) {
  const current = RATINGS.find((r) => r.key === item.rating);
  return (
    <div className="rate">
      <div className="rate-btns" role="radiogroup" aria-label={`How was ${item.name}?`}>
        {RATINGS.map((r) => (
          <button
            key={r.key}
            role="radio"
            aria-checked={item.rating === r.key}
            aria-label={r.label}
            title={item.rating === r.key ? `${r.label} (tap to clear)` : r.label}
            className="rate-btn"
            data-tone={r.tone}
            onClick={() => onRate(item.rating === r.key ? null : r.key)}
          >
            <r.icon size={15} fill={item.rating === r.key && r.key === "loved" ? "currentColor" : "none"} />
          </button>
        ))}
      </div>
      <span className="rate-label" data-tone={current?.tone}>
        {current?.label ?? "How was it?"}
      </span>
    </div>
  );
}

function StatusPicker({ item, onStatus }: { item: HistoryItem; onStatus: (s: WatchStatus | null) => void }) {
  // "" = let Plex decide (the poster's badge says what it decided); only for what Plex has seen
  const value = item.status_set ? item.status : item.plex_status ? "" : "finished";
  return (
    <select
      className="status-select"
      value={value}
      aria-label={`Status of ${item.name}`}
      onChange={(e) => onStatus((e.target.value || null) as WatchStatus | null)}
    >
      {item.plex_status && <option value="">From Plex</option>}
      <option value="watching">Watching</option>
      <option value="finished">Finished</option>
      <option value="dropped">Dropped</option>
    </select>
  );
}

function HistoryCard({
  item,
  onRate,
  onStatus,
  onRemove,
}: {
  item: HistoryItem;
  onRate: (r: WatchRating | null) => void;
  onStatus: (s: WatchStatus | null) => void;
  onRemove: () => void;
}) {
  const art = (
    <div className="pick-poster">
      <Poster path={item.poster_path} alt={item.name} />
      <div className="pick-badges">
        <Chip tone={item.status === "stalled" ? "accent" : item.status === "dropped" ? "bad" : "neutral"}>{STATUS_LABEL[item.status]}</Chip>
        {item.media_type === "movie" && <Chip>Movie</Chip>}
      </div>
    </div>
  );
  return (
    <article className="glass pick-card follow-card history-card lift" role="listitem" data-rating={item.rating ?? "none"}>
      {item.media_type === "tv" ? <Link href={`/show/${item.tmdb_id}`}>{art}</Link> : art}
      <div className="pick-body">
        {item.media_type === "tv" ? (
          <Link href={`/show/${item.tmdb_id}`} className="pick-title">
            {item.name} {item.year && <span className="pick-year">{item.year}</span>}
          </Link>
        ) : (
          <span className="pick-title">
            {item.name} {item.year && <span className="pick-year">{item.year}</span>}
          </span>
        )}
        <p className="history-line">{progress(item)}</p>
        <Rate item={item} onRate={onRate} />
        <div className="pick-actions">
          <StatusPicker item={item} onStatus={onStatus} />
          {item.source === "manual" && (
            <button className="icon-btn follow-unfollow" onClick={onRemove} aria-label={`Remove ${item.name} from history`} title="Remove">
              <Trash2 size={14} />
            </button>
          )}
        </div>
      </div>
    </article>
  );
}

export default function HistoryPage() {
  const { data, loading, reload, setData } = useApi<{ items: HistoryItem[] }>("/history");
  const [filter, setFilter] = useState<(typeof FILTERS)[number]["key"]>("all");
  const items = useMemo(() => data?.items ?? [], [data]);
  const unrated = items.filter((i) => !i.rating && i.status !== "watching" && i.status !== "stalled").length;

  const groups = useMemo(() => {
    const shown = items.filter((i) =>
      filter === "all" ? true : filter === "unrated" ? !i.rating : i.media_type === filter,
    );
    const of = (...st: WatchStatus[]) => shown.filter((i) => st.includes(i.status));
    return [
      { key: "watching", title: "Watching", note: null, items: of("watching", "caught_up") },
      { key: "stalled", title: "Still watching?", note: "Nothing on Plex for two months. Finished it somewhere else, or moved on?", items: of("stalled") },
      { key: "finished", title: "Finished", note: null, items: of("finished") },
      { key: "dropped", title: "Dropped", note: null, items: of("dropped") },
    ].filter((g) => g.items.length);
  }, [items, filter]);
  const hero = items.find((i) => i.backdrop_path && i.rating === "loved") ?? items.find((i) => i.backdrop_path);
  useBackdrop(backdrop(hero?.backdrop_path, "w780"));

  const patch = (it: HistoryItem, change: Partial<HistoryItem>) =>
    setData((d) => (d ? { items: d.items.map((x) => (key(x) === key(it) ? { ...x, ...change } : x)) } : d));

  const rate = async (it: HistoryItem, rating: WatchRating | null) => {
    patch(it, { rating });
    await api("/history/rate", { body: { tmdb_id: it.tmdb_id, media_type: it.media_type, rating } });
  };
  const setStatus = async (it: HistoryItem, status: WatchStatus | null) => {
    patch(it, { status: status ?? it.plex_status ?? "finished", status_set: status !== null });
    await api("/history/status", { body: { tmdb_id: it.tmdb_id, media_type: it.media_type, status } });
  };
  const remove = async (it: HistoryItem) => {
    setData((d) => (d ? { items: d.items.filter((x) => key(x) !== key(it)) } : d));
    await api("/history/remove", { body: { tmdb_id: it.tmdb_id, media_type: it.media_type } });
  };

  return (
    <div className="page">
      <header className="page-head">
        <h1 className="page-title">History</h1>
        <p className="page-sub">
          Everything you&apos;ve watched on Plex, plus anything you add from elsewhere. Rate what you&apos;ve seen: what you loved counts
          most in <Link href="/for-you" className="inline-link">your picks</Link>, and Not for me steers them away from more of the same.
          {unrated > 0 && <span className="muted"> {unrated} finished or caught up, not rated yet.</span>}
        </p>
        <div className="toolbar">
          <div className="pills" role="tablist" aria-label="Filter">
            {FILTERS.map((f) => (
              <button key={f.key} role="tab" aria-selected={filter === f.key} className="pill" onClick={() => setFilter(f.key)}>
                {f.label}
              </button>
            ))}
          </div>
        </div>
      </header>
      <AddBox known={new Set(items.map(key))} onAdded={reload} />
      {loading || !data ? (
        <Skeleton rows={5} />
      ) : items.length === 0 ? (
        <Empty>Nothing yet. Watch something on Plex, or add what you&apos;ve watched elsewhere above.</Empty>
      ) : groups.length === 0 ? (
        <Empty>Nothing here with this filter.</Empty>
      ) : (
        groups.map((g) => (
          <Section key={g.key} title={g.title} action={<span className="muted small">{g.items.length}</span>}>
            {g.note && <p className="muted small history-note">{g.note}</p>}
            <div className="picks-grid follow-grid" role="list" aria-label={g.title}>
              {g.items.map((it) => (
                <HistoryCard
                  key={key(it)}
                  item={it}
                  onRate={(r) => rate(it, r)}
                  onStatus={(s) => setStatus(it, s)}
                  onRemove={() => remove(it)}
                />
              ))}
            </div>
          </Section>
        ))
      )}
    </div>
  );
}
