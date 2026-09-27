"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { ArrowRight, Clapperboard, Film, Heart, MessageCircle, Star, Upload } from "lucide-react";
import { api, useApi } from "@/lib/api";

type Taste = {
  plex_titles: number;
  told: number;
  overseerr: number;
  mentioned: number;
  switched_off: number;
  imdb_ratings: number;
  imdb_watchlist: number;
  imdb_pending: number;
  imdb_unmatched: number;
};

// IMDb has no API or sign-in for personal data, so its CSV export is the only way in.
export function ImdbImport({ onImported }: { onImported?: () => void }) {
  const { data, reload } = useApi<Taste>("/taste");
  const [msg, setMsg] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const file = useRef<HTMLInputElement>(null);
  const pending = data?.imdb_pending ?? 0;

  // while matches run in the background, check back every 2 s; refresh the list when done
  useEffect(() => {
    if (!pending) return;
    const t = setTimeout(() => {
      reload();
      onImported?.();
    }, 2000);
    return () => clearTimeout(t);
  }, [pending, data, reload, onImported]);

  const upload = async (f: File) => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api<{ kind: string; rows: number; added: number }>("/taste/imdb", { body: { csv: await f.text() } });
      setMsg({ tone: "ok", text: `Got your IMDb ${r.kind === "watchlist" ? "watchlist" : "ratings"}: ${r.rows} titles (${r.added} new).` });
      reload();
    } catch (e) {
      setMsg({ tone: "error", text: (e as Error).message });
    } finally {
      setBusy(false);
      if (file.current) file.current.value = "";
    }
  };

  return (
    <div className="glass imdb-card">
      <div className="taste-import-head">
        <div>
          <div className="setting-name">
            {data?.imdb_ratings ? `${data.imdb_ratings} ratings` : "No ratings yet"}
            {data?.imdb_watchlist ? ` · ${data.imdb_watchlist} on your watchlist` : ""}
          </div>
          <ol className="small taste-steps">
            <li>
              On imdb.com open <strong>Your Ratings</strong> (profile menu) → <strong>⋯</strong> → <strong>Export</strong>. IMDb prepares
              a CSV under <strong>Exports</strong>; download it.
            </li>
            <li>Upload it here, and your watchlist export too if you like. Re-uploading later only adds what&apos;s new.</li>
          </ol>
        </div>
        <label className={`btn ${busy ? "" : "btn-primary"} taste-upload`}>
          <Upload size={16} /> {busy ? "Reading…" : "Upload IMDb CSV"}
          <input ref={file} type="file" accept=".csv,text/csv" hidden disabled={busy} onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
        </label>
      </div>
      {pending > 0 && <p className="small accent-text">Matching {pending} titles to TMDB…</p>}
      {msg && <p className={`small ${msg.tone === "error" ? "error-text" : "muted"}`}>{msg.text}</p>}
      {!pending && (data?.imdb_unmatched ?? 0) > 0 && (
        <p className="small muted">{data!.imdb_unmatched} IMDb titles couldn&apos;t be matched (usually shorts or very obscure titles).</p>
      )}
    </div>
  );
}

// Settings: a summary of what shapes someone's picks, and the way to change it.
export function TasteSection() {
  const { data } = useApi<Taste>("/taste");
  const rows: { icon: typeof Film; label: string; value: number | undefined; note: string }[] = [
    { icon: Film, label: "Plex history", value: data?.plex_titles, note: "counts most" },
    { icon: Heart, label: "You told Telly", value: data?.told, note: "counts most" },
    { icon: Star, label: "IMDb ratings", value: data?.imdb_ratings, note: "counts by your score" },
    { icon: Clapperboard, label: "Overseerr requests", value: data?.overseerr, note: "counts a little" },
    { icon: MessageCircle, label: "From plexbot chats", value: data?.mentioned, note: "counts a little" },
  ];
  return (
    <div className="glass settings-card">
      {rows.map((r) => (
        <div className="setting taste-row" key={r.label}>
          <r.icon size={18} />
          <div className="setting-text">
            <div className="setting-name">{r.label}</div>
            <div className="muted small">{r.note}</div>
          </div>
          <div className="taste-count">{r.value ?? "–"}</div>
        </div>
      ))}
      <div className="taste-import">
        <Link href="/taste" className="btn btn-primary">
          Tune your taste <ArrowRight size={16} />
        </Link>
        {(data?.switched_off ?? 0) > 0 && <p className="small muted">{data!.switched_off} titles switched off.</p>}
      </div>
    </div>
  );
}
