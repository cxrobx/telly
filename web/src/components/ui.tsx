"use client";

import Link from "next/link";
import { useState } from "react";
import { Check, Library, Plus, ThumbsDown, ThumbsUp, TrendingUp, Clapperboard } from "lucide-react";
import { Scores } from "./scores";
import {
  api,
  epLabel,
  fmtDate,
  poster,
  relDay,
  type NewsEvent,
  type Pick,
  type TimelineItem,
  type Undated,
} from "@/lib/api";

export function Section({
  title,
  action,
  children,
}: {
  title: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="section">
      <div className="section-head">
        <h2 className="section-title">{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}

export function Rail({ children, label }: { children: React.ReactNode; label: string }) {
  return (
    <div className="rail" role="list" aria-label={label}>
      {children}
    </div>
  );
}

export function Poster({ path, alt, size = "w342" }: { path: string | null; alt: string; size?: string }) {
  const src = poster(path, size);
  return src ? (
    // eslint-disable-next-line @next/next/no-img-element
    <img className="poster-img" src={src} alt={alt} loading="lazy" />
  ) : (
    <div className="poster-img poster-empty" aria-label={alt}>
      <Clapperboard size={28} />
    </div>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="glass empty">{children}</div>;
}

export function Skeleton({ rows = 1 }: { rows?: number }) {
  return (
    <div className="skeleton-wrap" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton" />
      ))}
    </div>
  );
}

export function Chip({ children, tone = "neutral" }: { children: React.ReactNode; tone?: "neutral" | "accent" | "good" | "bad" }) {
  return (
    <span className="chip" data-tone={tone}>
      {children}
    </span>
  );
}

export function statusTone(status: string | null): "good" | "bad" | "neutral" {
  if (status === "Returning Series" || status === "In Production") return "good";
  if (status === "Canceled" || status === "Ended") return "bad";
  return "neutral";
}

export function EpisodeCard({ item }: { item: TimelineItem }) {
  return (
    <Link href={`/show/${item.tmdb_id}`} className="glass ep-card lift" role="listitem">
      <div className="ep-poster">
        <Poster path={item.poster_path} alt={item.show} size="w185" />
      </div>
      <div className="ep-body">
        <div className="ep-when">{relDay(item.date)}</div>
        <div className="ep-show">{item.show}</div>
        <div className="ep-meta">
          {item.kind === "season_premiere" ? (
            <Chip tone="accent">Season {item.season} premiere</Chip>
          ) : (
            <span>{epLabel(item.season, item.episode)}</span>
          )}
        </div>
        {item.title && item.kind === "episode" && <div className="ep-title">{item.title}</div>}
      </div>
    </Link>
  );
}

const KIND_LABEL: Record<NewsEvent["kind"], string> = {
  renewed: "Renewed",
  canceled: "Canceled",
  ended: "Ended",
  season_dated: "Date set",
  episodes_aired: "Out now",
};

export function EventRow({ ev }: { ev: NewsEvent }) {
  const tone = ev.kind === "canceled" || ev.kind === "ended" ? "bad" : ev.kind === "renewed" ? "good" : "accent";
  return (
    <Link href={`/show/${ev.tmdb_id}`} className="event-row lift">
      <div className="event-thumb">
        <Poster path={ev.poster_path} alt={ev.show} size="w92" />
      </div>
      <div className="event-body">
        <div className="event-top">
          <Chip tone={tone}>{KIND_LABEL[ev.kind]}</Chip>
          <span className="event-when">{fmtDate(ev.when)}</span>
        </div>
        <div className="event-summary">{ev.summary}</div>
      </div>
    </Link>
  );
}

export function UndatedChip({ item }: { item: Undated }) {
  return (
    <Link href={`/show/${item.tmdb_id}`} className="glass undated lift">
      <div className="undated-poster">
        <Poster path={item.poster_path} alt={item.show} size="w92" />
      </div>
      <div>
        <div className="undated-show">{item.show}</div>
        <div className="undated-note">
          Season {item.season} · date TBA
          {item.source && item.source !== "TMDB" ? <> · via {item.source}</> : null}
        </div>
      </div>
    </Link>
  );
}

export function PickCard({
  pick,
  canRequest,
  onRated,
  compact = false,
  trendingBadge = true,
}: {
  pick: Pick;
  canRequest?: boolean;
  onRated?: (value: -1 | 1) => void;
  compact?: boolean;
  trendingBadge?: boolean; // off in the Trending rail, where every card would carry it
}) {
  const [rated, setRated] = useState<0 | 1 | -1>(0);
  const [leaving, setLeaving] = useState(false);
  const [req, setReq] = useState<"idle" | "busy" | "done" | "error">("idle");
  const [err, setErr] = useState<string | null>(null);
  const rate = async (value: -1 | 1) => {
    const next = rated === value ? 0 : value;
    setRated(next);
    await api("/recs/feedback", { body: { tmdb_id: pick.tmdb_id, media_type: pick.media_type, value: next } });
    if (next === -1 && onRated) {
      setLeaving(true); // let it animate away before the list drops it
      setTimeout(() => onRated(-1), 260);
    } else if (next !== 0) onRated?.(next);
  };
  const request = async () => {
    setReq("busy");
    try {
      await api("/request", { body: { tmdb_id: pick.tmdb_id, media_type: pick.media_type } });
      setReq("done");
    } catch (e) {
      setReq("error");
      setErr((e as Error).message);
    }
  };
  const href = pick.media_type === "tv" ? `/show/${pick.tmdb_id}` : undefined;
  const Img = (
    <div className="pick-poster">
      <Poster path={pick.poster_path} alt={pick.title} />
      <div className="pick-badges">
        {pick.in_library && (
          <Chip tone="good">
            <Library size={12} /> On Plex
          </Chip>
        )}
        {pick.trending && trendingBadge && (
          <Chip tone="accent">
            <TrendingUp size={12} /> Trending
          </Chip>
        )}
      </div>
    </div>
  );
  return (
    <article className={`glass pick-card lift ${compact ? "compact" : ""}`} role="listitem" data-leaving={leaving}>
      {href ? <Link href={href}>{Img}</Link> : Img}
      <div className="pick-body">
        <div className="pick-title">
          {pick.title} {pick.year && <span className="pick-year">{pick.year}</span>}
        </div>
        <Scores ratings={pick.ratings} compact />
        <p className="pick-reason">{pick.reason}</p>
        {!compact && (
          <div className="pick-actions">
            <button className="icon-btn" aria-pressed={rated === 1} aria-label="More like this" onClick={() => rate(1)}>
              <ThumbsUp size={16} />
            </button>
            <button className="icon-btn" aria-pressed={rated === -1} aria-label="Not for me" onClick={() => rate(-1)}>
              <ThumbsDown size={16} />
            </button>
            {!pick.in_library && canRequest && (
              <button className="btn btn-small" disabled={req === "busy" || req === "done"} onClick={request}>
                {req === "done" ? (
                  <>
                    <Check size={14} /> Requested
                  </>
                ) : (
                  <>
                    <Plus size={14} /> Request
                  </>
                )}
              </button>
            )}
          </div>
        )}
        {req === "error" && <p className="error-text">{err}</p>}
      </div>
    </article>
  );
}
