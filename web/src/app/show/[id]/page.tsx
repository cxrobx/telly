"use client";

import { useParams } from "next/navigation";
import { useState } from "react";
import { BellPlus, BellOff, ExternalLink, Library } from "lucide-react";
import { useBackdrop } from "@/components/backdrop";
import { Chip, Empty, Poster, Section, Skeleton, statusTone } from "@/components/ui";
import { Scores } from "@/components/scores";
import { api, backdrop, epLabel, fmtDate, relDay, useApi, type ShowDetail } from "@/lib/api";

export default function ShowPage() {
  const { id } = useParams<{ id: string }>();
  const { data, loading, error, setData } = useApi<ShowDetail>(`/shows/${id}`);
  const [busy, setBusy] = useState(false);
  useBackdrop(backdrop(data?.backdrop_path));

  if (error) return <Empty>{error}</Empty>;
  if (loading || !data) return <Skeleton rows={4} />;

  const toggle = async () => {
    setBusy(true);
    await api(`/shows/${data.tmdb_id}/${data.following ? "unfollow" : "follow"}`, { method: "POST" });
    setData({ ...data, following: !data.following, followed_because: data.following ? null : "manual" });
    setBusy(false);
  };
  const upcoming = data.seasons.filter((s) => s.season > (data.last_episode?.season ?? 0));

  return (
    <div className="page">
      <section className="show-hero">
        <div className="show-poster">
          <Poster path={data.poster_path} alt={data.name} size="w500" />
        </div>
        <div className="glass show-panel">
          <h1 className="hero-title">{data.name}</h1>
          <div className="hero-meta">
            <Chip tone={statusTone(data.status)}>{data.status ?? "Unknown"}</Chip>
            {data.year && <span className="muted">{data.year}</span>}
            {data.networks?.[0] && <span className="muted">{data.networks[0]}</span>}
            {data.in_library && (
              <Chip tone="good">
                <Library size={12} /> On Plex
              </Chip>
            )}
          </div>
          <Scores ratings={data.ratings} />
          {data.genres.length > 0 && <div className="muted small">{data.genres.join(" · ")}</div>}
          <div className="show-facts">
            {data.next_episode ? (
              <div>
                <div className="fact-label">Next episode</div>
                <div className="fact-value">
                  {epLabel(data.next_episode.season, data.next_episode.episode)} · {relDay(data.next_episode.air_date)}
                </div>
              </div>
            ) : upcoming.length > 0 ? (
              <div>
                <div className="fact-label">Next season</div>
                <div className="fact-value">
                  Season {upcoming[0].season} · {upcoming[0].air_date ? fmtDate(upcoming[0].air_date, { month: "long", day: "numeric", year: "numeric" }) : "date TBA"}
                </div>
              </div>
            ) : data.renewed_next ? (
              <div>
                <div className="fact-label">Next season</div>
                <div className="fact-value">
                  Season {data.renewed_next.season} · renewed, date TBA
                  {data.renewed_next.source && <span className="muted"> · {data.renewed_next.source}</span>}
                </div>
              </div>
            ) : null}
            {data.last_episode && (
              <div>
                <div className="fact-label">Latest</div>
                <div className="fact-value">
                  {epLabel(data.last_episode.season, data.last_episode.episode)} · {fmtDate(data.last_episode.air_date, { month: "short", day: "numeric", year: "numeric" })}
                </div>
              </div>
            )}
            {data.my_plays > 0 && (
              <div>
                <div className="fact-label">You&apos;ve watched</div>
                <div className="fact-value">
                  {data.my_plays} episode{data.my_plays === 1 ? "" : "s"}
                  {data.last_watched ? ` · last ${fmtDate(data.last_watched)}` : ""}
                </div>
              </div>
            )}
          </div>
          <div className="hero-actions">
            <button className={`btn ${data.following ? "" : "btn-primary"}`} onClick={toggle} disabled={busy}>
              {data.following ? (
                <>
                  <BellOff size={16} /> Following · stop alerts
                </>
              ) : (
                <>
                  <BellPlus size={16} /> Follow for alerts
                </>
              )}
            </button>
          </div>
        </div>
      </section>

      <Section title="Seasons">
        <div className="season-strip">
          {data.seasons.map((s) => (
            <div key={s.season} className="glass season-cell" data-future={s.season > (data.last_episode?.season ?? 0)}>
              <div className="season-num">S{s.season}</div>
              <div className="muted small">{s.air_date ? s.air_date.slice(0, 4) : "TBA"}</div>
              <div className="muted small">{s.episode_count ? `${s.episode_count} ep` : ""}</div>
            </div>
          ))}
          {data.renewed_next && (
            <div className="glass season-cell" data-future="true" title={`Renewed (${data.renewed_next.source ?? "news"})`}>
              <div className="season-num">S{data.renewed_next.season}</div>
              <div className="muted small">TBA</div>
              <div className="muted small">renewed</div>
            </div>
          )}
        </div>
      </Section>

      <Section title="History">
        {data.events.length ? (
          <ul className="glass event-list">
            {data.events.map((e, i) => (
              <li key={i} className="history-row">
                <span className="muted small">{fmtDate(e.when, { month: "short", day: "numeric", year: "numeric" })}</span>
                <span>{e.summary}</span>
                {e.source_url && (
                  <a href={e.source_url} target="_blank" rel="noreferrer" className="source-link" aria-label="Source">
                    <ExternalLink size={14} />
                  </a>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <Empty>Nothing yet. Telly records renewals, premiere dates and new episodes here as they happen.</Empty>
        )}
      </Section>
    </div>
  );
}
