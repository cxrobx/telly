"use client";
import { useMemo, useState } from "react";
import { useApi, type Pick } from "@/lib/api";
import { Empty, PickCard, Skeleton } from "./ui";
type CatalogPick = Pick & { acclaim: number; tmdb_rating: number; tmdb_votes: number; watched: boolean };
type Sort = "acclaim" | "imdb" | "critics" | "audience" | "newest";

export function AnimeCatalog({ canRequest }: { canRequest?: boolean }) {
  const { data, loading, error, reload } = useApi<{ picks: CatalogPick[]; updated_at: string | null }>("/anime/catalog");
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("any");
  const [unwatched, setUnwatched] = useState(false);
  const [sort, setSort] = useState<Sort>("acclaim");
  const [limit, setLimit] = useState(24);
  const picks = useMemo(() => {
    const q = query.trim().toLocaleLowerCase();
    const score = (p: CatalogPick) => sort === "imdb" ? p.ratings?.imdb : sort === "critics" ? p.ratings?.rt_critic
      : sort === "audience" ? p.ratings?.rt_audience : sort === "newest" ? p.year : p.acclaim;
    return (data?.picks ?? []).filter((p) => (!q || p.title.toLocaleLowerCase().includes(q))
      && (kind === "any" || p.media_type === kind) && (!unwatched || !p.watched))
      .sort((a, b) => (score(b) ?? -1) - (score(a) ?? -1) || b.acclaim - a.acclaim);
  }, [data, query, kind, unwatched, sort]);
  return <section className="section" aria-label="Acclaimed anime catalog">
    <div><h2 className="section-title">Acclaimed anime</h2>
      <p className="muted small">Highly rated shows and films, always here to browse. Ranked by TMDB score adjusted for vote count, with at least 200 ratings and a 7.5/10 average. Updated daily.</p>
    </div>
    <div className="catalog-toolbar">
      <label className="search-box catalog-search"><input aria-label="Search acclaimed anime" placeholder="Search the catalog…" value={query} onChange={(e) => { setQuery(e.target.value); setLimit(24); }} /></label>
      <select className="status-select" aria-label="Anime type" value={kind} onChange={(e) => { setKind(e.target.value); setLimit(24); }}><option value="any">Shows &amp; movies</option><option value="tv">Shows</option><option value="movie">Movies</option></select>
      <select className="status-select" aria-label="Catalog sort" value={sort} onChange={(e) => { setSort(e.target.value as Sort); setLimit(24); }}><option value="acclaim">Most acclaimed</option><option value="imdb">IMDb</option><option value="critics">Critics</option><option value="audience">Audience</option><option value="newest">Newest</option></select>
      <label className="catalog-unwatched small"><input type="checkbox" checked={unwatched} onChange={(e) => { setUnwatched(e.target.checked); setLimit(24); }} /> Unwatched only</label>
    </div>
    {error ? <Empty>Couldn&apos;t load the catalog. <button className="btn btn-small" onClick={reload}>Try again</button></Empty>
      : loading ? <Skeleton rows={3} />
      : !data?.updated_at ? <Empty>The acclaimed catalog is being prepared. <button className="btn btn-small" onClick={reload}>Refresh</button></Empty>
      : <><p className="muted small" role="status">{picks.length} titles{unwatched ? " you haven’t watched" : " · watched titles included"}</p>
        {!picks.length ? <Empty>No titles match these filters.</Empty> : <div className="picks-grid" role="list" aria-label="Acclaimed anime">{picks.slice(0, limit).map((p) => <PickCard key={`${p.media_type}-${p.tmdb_id}`} pick={p} canRequest={canRequest} catalogMode />)}</div>}
        {picks.length > limit && <button className="btn" onClick={() => setLimit((n) => n + 24)}>Show more · {picks.length - limit} remaining</button>}
      </>}
  </section>;
}
