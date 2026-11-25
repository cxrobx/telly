"use client";

import { useCallback, useEffect, useState } from "react";

export type Art = { poster_path: string | null; backdrop_path: string | null; networks?: string[] };

export type TimelineItem = Art & {
  date: string;
  show: string;
  tmdb_id: number;
  season: number;
  episode: number;
  kind: "episode" | "season_premiere";
  title: string | null;
};

export type Undated = Art & {
  show: string;
  tmdb_id: number;
  season: number;
  note: string;
  source?: string | null;
  source_url?: string | null;
};

export type NewsEvent = Art & {
  event_id: number; // what /whats-new/{event_id}/dismiss | watched | restore take
  watchable: boolean; // a release (episodes out, or in Plex): it can be marked watched
  when: string;
  show: string;
  tmdb_id: number;
  kind: "renewed" | "canceled" | "ended" | "season_dated" | "episodes_aired" | "in_plex";
  summary: string;
  payload: Record<string, unknown>;
};

/** POST /whats-new/{event_id}/dismiss | watched (backend news.py). */
export type NewsDismissed = {
  ok: true;
  event_id: number;
  reason: "dismissed" | "watched";
  history?: { title: string; tmdb_id: number; media_type: "tv"; status: string | null; changed: boolean };
};

export type Ratings = {
  imdb: number | null;
  imdb_votes: number | null;
  imdb_id: string | null;
  rt_url?: string | null;
  rt_critic: number | null;
  rt_audience: number | null;
} | null;

export type Pick = {
  tmdb_id: number;
  media_type: "tv" | "movie";
  title: string;
  year: number | null;
  released?: string | null;
  new_kind?: string;
  kind_label?: string;
  reason: string;
  because: string[];
  trending: boolean;
  in_library: boolean;
  anime?: boolean;
  watched?: boolean;
  tmdb_rating?: number;
  tmdb_votes?: number;
  acclaim?: number;
  poster_path: string | null;
  backdrop_path: string | null;
  overview: string;
  ratings?: Ratings;
};

export type Me = {
  plex_id: number;
  username: string;
  is_owner: boolean;
  watchlist_connected: boolean;
  can_request: boolean;
  discord_linked: boolean;
  discord_name: string | null;
  discord_dms: boolean;
  phone_topic: string | null;
  ntfy_url: string;
  following: number;
};

export type Home = {
  hero: TimelineItem | null;
  airing_soon: TimelineItem[];
  whats_new: NewsEvent[];
  whats_new_picks: Pick[];
  for_you: Pick[];
  recent_picks: Pick[];
  trending: Pick[];
  needs_taste: boolean; // nothing to base picks on yet, and they haven't closed the prompt
  building: boolean; // their first picks are being built right now
};

export type Timeline = { from: string; to: string; dated: TimelineItem[]; renewed_no_date: Undated[] };

/** Radarr/Sonarr status (backend arr.py): about the title, never who requested it. */
export type Download = {
  state: "downloading" | "missing" | "available" | "unmonitored" | "not_in_library";
  label: string;
  tone: "good" | "accent" | "bad" | "muted";
  progress: number | null;
  eta: string | null;
  quality: string | null;
  missing?: number;
};

export type Followed = Art & {
  download?: Download | null;
  tmdb_id: number;
  name: string;
  year: number | null;
  status: string | null;
  followed_because: "watchlist" | "inferred" | "manual";
  airing: boolean;
  next_episode: { season: number; episode: number; air_date: string; name: string } | null;
  last_episode: { season: number; episode: number; air_date: string; name: string } | null;
  next_season: { season: number; air_date: string | null; source: string | null } | null;
  ratings?: Ratings;
};

export type ShowDetail = Art & {
  download?: Download | null;
  tmdb_id: number;
  imdb_id: string | null;
  name: string;
  year: number | null;
  status: string | null;
  genres: string[];
  seasons: { season: number; air_date: string | null; episode_count: number; name: string }[];
  last_episode: { season: number; episode: number; air_date: string; name: string } | null;
  next_episode: { season: number; episode: number; air_date: string; name: string } | null;
  following: boolean;
  followed_because: string | null;
  my_plays: number;
  last_watched: string | null;
  in_library: boolean;
  renewed_next: { season: number; source: string | null; source_url: string | null } | null;
  ratings?: Ratings;
  events: { when: string; kind: string; summary: string; source_url: string | null }[];
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init?: { method?: string; body?: unknown }): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method: init?.method ?? (init?.body !== undefined ? "POST" : "GET"),
    headers: init?.body !== undefined || init?.method === "POST" ? { "Content-Type": "application/json" } : undefined,
    body: init?.body !== undefined ? JSON.stringify(init.body) : init?.method === "POST" ? "{}" : undefined,
    credentials: "same-origin",
  });
  if (res.status === 401 && typeof window !== "undefined" && !location.pathname.startsWith("/signin")) {
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination -- module-level, no router here; a full load clears stale state
    location.href = "/signin";
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, (data as { detail?: string }).detail ?? res.statusText);
  return data as T;
}

export function useApi<T>(path: string | null) {
  // State is tagged with the path it belongs to, so "loading" is derived (data for another
  // path, or none yet) instead of being set synchronously inside the effect.
  const [state, setState] = useState<{ path: string | null; data: T | null; error: string | null }>({
    path: null,
    data: null,
    error: null,
  });
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    if (!path) return;
    let live = true;
    api<T>(path)
      .then((data) => live && setState({ path, data, error: null }))
      .catch((e: Error) => live && setState({ path, data: null, error: e.message }));
    return () => {
      live = false;
    };
  }, [path, nonce]);
  const current = state.path === path;
  const setData = useCallback(
    (next: T | null | ((prev: T | null) => T | null)) =>
      setState((s) => ({ ...s, data: typeof next === "function" ? (next as (p: T | null) => T | null)(s.data) : next })),
    [],
  );
  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { data: current ? state.data : null, error: current ? state.error : null, loading: !current, reload, setData };
}

const IMG = "https://image.tmdb.org/t/p";
export const poster = (p: string | null | undefined, size = "w342") => (p ? `${IMG}/${size}${p}` : null);
export const backdrop = (p: string | null | undefined, size = "w1280") => (p ? `${IMG}/${size}${p}` : null);

export function fmtDate(iso: string, opts: Intl.DateTimeFormatOptions = { month: "short", day: "numeric" }) {
  return new Date(`${iso}T12:00:00`).toLocaleDateString("en-US", opts);
}

export function relDay(iso: string) {
  const today = new Date();
  today.setHours(12, 0, 0, 0);
  const d = new Date(`${iso}T12:00:00`);
  const days = Math.round((d.getTime() - today.getTime()) / 86400000);
  if (days === 0) return "Today";
  if (days === 1) return "Tomorrow";
  if (days === -1) return "Yesterday";
  if (days > 1 && days < 7) return d.toLocaleDateString("en-US", { weekday: "long" });
  return fmtDate(iso);
}

export const epLabel = (s: number, e: number) => `S${s} · E${e}`;

export type WatchRating = "not_for_me" | "liked" | "loved";
export type WatchStatus = "caught_up" | "finished" | "watching" | "stalled" | "dropped";

export type HistoryItem = Art & {
  tmdb_id: number;
  media_type: "tv" | "movie";
  name: string;
  year: number | null;
  status: WatchStatus;
  plex_status: WatchStatus | null; // what Plex alone says; null when never played there
  status_set: boolean; // they chose the status
  rating: WatchRating | null;
  source: "plex" | "manual";
  episodes: number;
  aired: number | null;
  last_watched: string | null;
  added_at: string | null;
  ratings?: Ratings;
};
