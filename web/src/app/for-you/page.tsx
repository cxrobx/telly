"use client";

import Link from "next/link";
import { useMemo, useState, useSyncExternalStore } from "react";
import { ArrowDownWideNarrow, Text } from "lucide-react";
import { AnimeCatalog } from "@/components/anime-catalog";
import { useBackdrop } from "@/components/backdrop";
import { Empty, PickCard, Skeleton } from "@/components/ui";
import { backdrop, useApi, type Me, type Pick } from "@/lib/api";

const FILTERS = [
  { key: "any", label: "Everything" },
  { key: "tv", label: "Shows" },
  { key: "movie", label: "Movies" },
  { key: "anime", label: "Anime" },
] as const;

// "Best match" is Telly's own order (fit with your history, reranked by Haiku). The rest sort
// the same picks by an outside score; a pick without that score goes last, not away.
const SORTS = [
  { key: "match", label: "Best match", value: () => 0 },
  { key: "imdb", label: "IMDb", value: (p: Pick) => p.ratings?.imdb ?? null },
  { key: "critics", label: "Critics", value: (p: Pick) => p.ratings?.rt_critic ?? null },
  { key: "audience", label: "Audience", value: (p: Pick) => p.ratings?.rt_audience ?? null },
  { key: "newest", label: "Newest", value: (p: Pick) => p.year ?? null },
] as const;
type SortKey = (typeof SORTS)[number]["key"];
const SORT_STORE = "telly.forYou.sort";

// The choice is a per-browser convenience in localStorage (falling back to memory in a private
// window). useSyncExternalStore renders "match" on the server and the saved choice after
// hydration, so the prerendered page and the browser never disagree.
let memorySort: SortKey | null = null;
const sortListeners = new Set<() => void>();
const isSort = (v: unknown): v is SortKey => SORTS.some((s) => s.key === v);

function readSort(): SortKey {
  try {
    const v = localStorage.getItem(SORT_STORE);
    if (isSort(v)) return v;
  } catch {
    /* storage blocked */
  }
  return memorySort ?? "match";
}

function writeSort(k: SortKey) {
  memorySort = k;
  try {
    localStorage.setItem(SORT_STORE, k);
  } catch {
    /* private window: remembered for this visit only */
  }
  sortListeners.forEach((l) => l());
}

function subscribeSort(cb: () => void) {
  sortListeners.add(cb);
  return () => sortListeners.delete(cb);
}

// Summaries (the text under each card's scores) start hidden; the choice is remembered the same way.
const SUMMARY_STORE = "telly.forYou.summaries";
let memorySummaries: boolean | null = null;

function readSummaries(): boolean {
  try {
    const v = localStorage.getItem(SUMMARY_STORE);
    if (v === "1" || v === "0") return v === "1";
  } catch {
    /* storage blocked */
  }
  return memorySummaries ?? false;
}

function writeSummaries(on: boolean) {
  memorySummaries = on;
  try {
    localStorage.setItem(SUMMARY_STORE, on ? "1" : "0");
  } catch {
    /* private window: remembered for this visit only */
  }
  sortListeners.forEach((l) => l());
}

export default function ForYouPage() {
  const [media, setMedia] = useState<(typeof FILTERS)[number]["key"]>("any");
  const [animeView, setAnimeView] = useState<"catalog" | "picks">("catalog");
  const catalog = media === "anime" && animeView === "catalog";
  const sort = useSyncExternalStore(subscribeSort, readSort, () => "match" as SortKey);
  const summaries = useSyncExternalStore(subscribeSort, readSummaries, () => false);
  const { data, loading, setData } = useApi<{ picks: Pick[] }>(`/recs?media=${media}`);
  const { data: me } = useApi<Me>("/me");

  const picks = useMemo(() => {
    const list = data?.picks ?? [];
    if (sort === "match") return list;
    const score = SORTS.find((s) => s.key === sort)!.value;
    // stable: ties (and unscored picks, which go last) keep Telly's own order
    return list
      .map((p, i) => ({ p, i, v: score(p) }))
      .sort((a, b) => (a.v == null ? 1 : 0) - (b.v == null ? 1 : 0) || (b.v ?? 0) - (a.v ?? 0) || a.i - b.i)
      .map((x) => x.p);
  }, [data, sort]);

  useBackdrop(backdrop(picks[0]?.backdrop_path, "w780"));

  const drop = (p: Pick) =>
    setData((d) => (d ? { picks: d.picks.filter((x) => !(x.tmdb_id === p.tmdb_id && x.media_type === p.media_type)) } : d));

  return (
    <div className="page">
      <header className="page-head">
        <h1 className="page-title">For you</h1>
        {catalog ? <p className="page-sub">The best-rated anime shows and films, refreshed daily. Watched ones are marked.</p> : <p className="page-sub">
          Picked from what you&apos;ve watched on Plex, how you rated it on <Link href="/history" className="inline-link">History</Link> and your IMDb ratings, plus what&apos;s trending this week.
          Thumbs up for more like it, thumbs down and it&apos;s gone. <Link href="/taste" className="inline-link">Tune your taste</Link>
        </p>}
        <div className="toolbar">
          <div className="pills" role="tablist" aria-label="Filter">
            {FILTERS.map((f) => (
              <button key={f.key} role="tab" aria-selected={media === f.key} className="pill" onClick={() => setMedia(f.key)}>
                {f.label}
              </button>
            ))}
          </div>
          <div className="toolbar-end">
            <div className="sort" role="radiogroup" aria-label="Sort by">
              <ArrowDownWideNarrow size={16} aria-hidden className="sort-icon" />
              {SORTS.map((s) => (
                <button
                  key={s.key}
                  role="radio"
                  aria-checked={sort === s.key}
                  className="sort-opt"
                  onClick={() => writeSort(s.key)}
                >
                  {s.key === "match" && catalog ? "Most acclaimed" : s.label}
                </button>
              ))}
            </div>
            <div className="sort">
              <button className="sort-opt sort-toggle" aria-pressed={summaries} onClick={() => writeSummaries(!summaries)}>
                <Text size={14} aria-hidden /> Summaries
              </button>
            </div>
          </div>
        </div>
        {media === "anime" && <div className="pills" role="tablist" aria-label="Anime view">
          <button className="pill" role="tab" aria-selected={animeView === "catalog"} onClick={() => setAnimeView("catalog")}>Acclaimed catalog</button>
          <button className="pill" role="tab" aria-selected={animeView === "picks"} onClick={() => setAnimeView("picks")}>Your picks</button>
        </div>}
      </header>
      {catalog ? <AnimeCatalog canRequest={me?.can_request} sort={sort} summaries={summaries} /> : loading || !data ? (
        <Skeleton rows={3} />
      ) : picks.length === 0 ? (
        <Empty>
          No picks yet. Tell Telly a few things you liked on <Link href="/taste">Your taste</Link> and they&apos;re built in about a
          minute; after that they refresh every night.
        </Empty>
      ) : (
        <div className="picks-grid" role="list" aria-label="Recommendations">
          {picks.map((p) => (
            <PickCard
              key={`${p.media_type}-${p.tmdb_id}`}
              pick={p}
              canRequest={me?.can_request}
              showReason={summaries}
              onRated={(v) => v === -1 && drop(p)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
