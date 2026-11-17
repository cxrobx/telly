"use client";
import { useMemo, useState } from "react";
import { Search } from "lucide-react";
import { useApi, type Pick } from "@/lib/api";
import { Empty, PickCard, Skeleton } from "./ui";
type CatalogPick = Pick & { acclaim: number; tmdb_rating: number; tmdb_votes: number; watched: boolean };
// The page's sort bar drives this list; its "Best match" slot reads "Most acclaimed" here.
type Sort = "match" | "imdb" | "critics" | "audience" | "newest";
const KINDS = [
  { key: "any", label: "All" },
  { key: "tv", label: "Shows" },
  { key: "movie", label: "Movies" },
] as const;
const PAGE = 24;

export function AnimeCatalog({ canRequest, sort, summaries }: { canRequest?: boolean; sort: Sort; summaries: boolean }) {
  const { data, loading, error, reload } = useApi<{ picks: CatalogPick[]; updated_at: string | null }>("/anime/catalog");
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<(typeof KINDS)[number]["key"]>("any");
  const [unwatched, setUnwatched] = useState(false);
  const [limit, setLimit] = useState(PAGE);
  const picks = useMemo(() => {
    const q = query.trim().toLocaleLowerCase();
    const score = (p: CatalogPick) => sort === "imdb" ? p.ratings?.imdb : sort === "critics" ? p.ratings?.rt_critic
      : sort === "audience" ? p.ratings?.rt_audience : sort === "newest" ? p.year : p.acclaim;
    return (data?.picks ?? []).filter((p) => (!q || p.title.toLocaleLowerCase().includes(q))
      && (kind === "any" || p.media_type === kind) && (!unwatched || !p.watched))
      .sort((a, b) => (score(b) ?? -1) - (score(a) ?? -1) || b.acclaim - a.acclaim);
  }, [data, query, kind, unwatched, sort]);
  const reset = () => setLimit(PAGE);

  return <section className="section catalog" aria-label="Acclaimed anime catalog">
    <div className="catalog-bar">
      <label className="search-box catalog-search">
        <Search size={15} aria-hidden />
        <input aria-label="Search acclaimed anime" placeholder="Search anime" value={query} onChange={(e) => { setQuery(e.target.value); reset(); }} />
      </label>
      <div className="sort" role="radiogroup" aria-label="Type">
        {KINDS.map((k) => (
          <button key={k.key} role="radio" aria-checked={kind === k.key} className="sort-opt" onClick={() => { setKind(k.key); reset(); }}>{k.label}</button>
        ))}
      </div>
      <div className="sort">
        <button className="sort-opt" aria-pressed={unwatched} onClick={() => { setUnwatched(!unwatched); reset(); }}>Unwatched only</button>
      </div>
      {data?.updated_at && <p className="catalog-count" role="status">{picks.length} titles</p>}
    </div>
    {error ? <Empty>Couldn&apos;t load the catalog. <button className="btn btn-small" onClick={reload}>Try again</button></Empty>
      : loading ? <Skeleton rows={3} />
      : !data?.updated_at ? <Empty>The acclaimed catalog is being prepared. <button className="btn btn-small" onClick={reload}>Refresh</button></Empty>
      : !picks.length ? <Empty>No titles match these filters.</Empty>
      : <>
        <div className="picks-grid" role="list" aria-label="Acclaimed anime">
          {picks.slice(0, limit).map((p) => <PickCard key={`${p.media_type}-${p.tmdb_id}`} pick={p} canRequest={canRequest} catalogMode showReason={summaries} />)}
        </div>
        {picks.length > limit && <button className="btn" onClick={() => setLimit((n) => n + PAGE)}>Show more · {picks.length - limit} remaining</button>}
      </>}
  </section>;
}
