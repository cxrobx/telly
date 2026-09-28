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
  when: string;
  show: string;
  tmdb_id: number;
  kind: "renewed" | "canceled" | "ended" | "season_dated" | "episodes_aired";
  summary: string;
  payload: Record<string, unknown>;
};

export type Ratings = {
  imdb: number | null;
  imdb_votes: number | null;
  imdb_id: string | null;
  rt_critic: number | null;
  rt_audience: number | null;
} | null;

export type Pick = {
  tmdb_id: number;
  media_type: "tv" | "movie";
  title: string;
  year: number | null;
  reason: string;
  because: string[];
  trending: boolean;
  in_library: boolean;
  anime?: boolean;
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
  for_you: Pick[];
  trending: Pick[];
  needs_taste: boolean; // nothing to base picks on yet, and they haven't closed the prompt
  building: boolean; // their first picks are being built right now
};

export type Timeline = { from: string; to: string; dated: TimelineItem[]; renewed_no_date: Undated[] };

export type Followed = Art & {
  tmdb_id: number;
  name: string;
  year: number | null;
  status: string | null;
  followed_because: "watchlist" | "inferred" | "manual";
  next_episode: { season: number; episode: number; air_date: string; name: string } | null;
  last_episode: { season: number; episode: number; air_date: string; name: string } | null;
  next_season: { season: number; air_date: string | null; source: string | null } | null;
  ratings?: Ratings;
};

export type ShowDetail = Art & {
  tmdb_id: number;
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
