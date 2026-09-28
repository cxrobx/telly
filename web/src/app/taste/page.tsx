"use client";

import Link from "next/link";
import { useMemo } from "react";
import { useBackdrop } from "@/components/backdrop";
import { Empty, Poster, Section, Skeleton } from "@/components/ui";
import { ImdbImport, Tell } from "@/components/taste";
import { Memories } from "@/components/memories";
import { api, backdrop, useApi } from "@/lib/api";

type Item = {
  tmdb_id: number;
  media_type: "tv" | "movie";
  title: string;
  poster_path: string | null;
  sources: string[];
  rating: number | null;
  use_for_picks: boolean;
  noted_at: string | null;
};

// Strongest first. Requests and chat mentions are interest, not proof of liking, so they
// count a little and are the ones most worth switching off.
const GROUPS: { source: string; title: string; note: string }[] = [
  { source: "told", title: "You told Telly", note: "Counts as much as your favourite shows on Plex." },
  { source: "imdb_rating", title: "Your IMDb ratings", note: "7 and up shape your picks; 4 and under count against." },
  { source: "overseerr", title: "Requested in Overseerr", note: "Counts a little: a request can be a try-out or for a friend." },
  { source: "mentioned", title: "Came up in plexbot chats", note: "Counts a little: mostly fix-it questions, not reviews." },
  { source: "imdb_watchlist", title: "Your IMDb watchlist", note: "Never shapes picks; just isn't recommended back to you." },
];

function TasteCard({ item, onToggle }: { item: Item; onToggle: (use: boolean) => void }) {
  return (
    <article className="glass pick-card taste-card" role="listitem" data-off={!item.use_for_picks}>
      <Link href={item.media_type === "tv" ? `/show/${item.tmdb_id}` : "#"} className="pick-poster" aria-label={item.title}>
        <Poster path={item.poster_path} alt={item.title} size="w185" />
      </Link>
      <div className="pick-body">
        <div className="pick-title">{item.title}</div>
        <div className="taste-meta">
          {item.rating != null && <span className="taste-rating">IMDb: you gave it {item.rating}/10</span>}
          {item.sources.length > 1 && <span className="muted">also {item.sources.slice(1).map((s) => GROUPS.find((g) => g.source === s)?.title.toLowerCase().replace("your ", "")).join(", ")}</span>}
        </div>
        {item.sources[0] !== "imdb_watchlist" && (
          <label className="taste-toggle">
            <button role="switch" aria-checked={item.use_for_picks} className="toggle toggle-small" onClick={() => onToggle(!item.use_for_picks)} aria-label={`Use ${item.title} for my picks`}>
              <span className="toggle-knob" />
            </button>
            <span className="small">{item.use_for_picks ? "Shapes my picks" : "Not my taste right now"}</span>
          </label>
        )}
      </div>
    </article>
  );
}

export default function TastePage() {
  const { data, loading, reload, setData } = useApi<{ items: Item[] }>("/taste/items");
  const grouped = useMemo(
    () => GROUPS.map((g) => ({ ...g, items: (data?.items ?? []).filter((i) => i.sources[0] === g.source) })).filter((g) => g.items.length),
    [data],
  );
  useBackdrop(backdrop(null));

  const toggle = async (item: Item, use: boolean) => {
    setData((d) => (d ? { items: d.items.map((i) => (i === item ? { ...i, use_for_picks: use } : i)) } : d));
    await api("/taste/use", { body: { tmdb_id: item.tmdb_id, media_type: item.media_type, use } });
  };

  return (
    <div className="page">
      <header className="page-head">
        <h1 className="page-title">Your taste</h1>
        <p className="page-sub">
          What shapes your picks. Your Plex watch history, what you tell Telly, and your IMDb ratings count most. Requests and chat
          mentions count a little, and you can switch any title off. Changes show up in tonight&apos;s picks (your first ones are built right away).
        </p>
      </header>

      <Section title="Tell Telly what you liked">
        <Tell onTold={reload} />
      </Section>

      <Section title="IMDb">
        <ImdbImport onImported={reload} />
      </Section>

      <Section title="What Telly remembers">
        <Memories />
      </Section>

      {loading || !data ? (
        <Skeleton rows={3} />
      ) : grouped.length === 0 ? (
        <Empty>Nothing here yet beyond your Plex history. Tell Telly about something above, or bring in your IMDb.</Empty>
      ) : (
        grouped.map((g) => (
          <Section key={g.source} title={g.title} action={<span className="muted small">{g.items.length}</span>}>
            <p className="muted small group-note">{g.note}</p>
            <div className="picks-grid taste-grid" role="list" aria-label={g.title}>
              {g.items.map((i) => (
                <TasteCard key={`${i.media_type}-${i.tmdb_id}`} item={i} onToggle={(u) => toggle(i, u)} />
              ))}
            </div>
          </Section>
        ))
      )}
    </div>
  );
}
