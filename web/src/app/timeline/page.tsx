"use client";

import Link from "next/link";
import { useMemo } from "react";
import { useBackdrop } from "@/components/backdrop";
import { Chip, Empty, Poster, Section, Skeleton, UndatedChip } from "@/components/ui";
import { backdrop, epLabel, useApi, type Timeline, type TimelineItem } from "@/lib/api";

function monthKey(iso: string) {
  return iso.slice(0, 7);
}

function monthLabel(key: string) {
  return new Date(`${key}-15T12:00:00`).toLocaleDateString("en-US", { month: "long", year: "numeric" });
}

function Day({ iso }: { iso: string }) {
  const d = new Date(`${iso}T12:00:00`);
  const today = new Date().toISOString().slice(0, 10) === iso;
  return (
    <div className="tl-day" data-today={today}>
      <span className="tl-dow">{d.toLocaleDateString("en-US", { weekday: "short" })}</span>
      <span className="tl-num">{d.getDate()}</span>
    </div>
  );
}

function Item({ it }: { it: TimelineItem }) {
  return (
    <Link href={`/show/${it.tmdb_id}`} className="glass tl-item lift">
      <div className="tl-poster">
        <Poster path={it.poster_path} alt={it.show} size="w92" />
      </div>
      <div className="tl-body">
        <div className="tl-show">{it.show}</div>
        {it.kind === "season_premiere" ? (
          <Chip tone="accent">Season {it.season} premiere</Chip>
        ) : (
          <div className="tl-ep">
            {epLabel(it.season, it.episode)}
            {it.title ? ` · ${it.title}` : ""}
          </div>
        )}
      </div>
    </Link>
  );
}

export default function TimelinePage() {
  const { data, loading } = useApi<Timeline>("/timeline?days=240");
  const months = useMemo(() => {
    const m = new Map<string, Map<string, TimelineItem[]>>();
    for (const it of data?.dated ?? []) {
      const mk = monthKey(it.date);
      if (!m.has(mk)) m.set(mk, new Map());
      const days = m.get(mk)!;
      days.set(it.date, [...(days.get(it.date) ?? []), it]);
    }
    return [...m.entries()];
  }, [data]);
  useBackdrop(backdrop((data?.dated[0] ?? data?.renewed_no_date[0])?.backdrop_path, "w780"));

  if (loading || !data) return <Skeleton rows={4} />;
  return (
    <div className="page">
      <header className="page-head">
        <h1 className="page-title">Timeline</h1>
        <p className="page-sub">What&apos;s coming for the shows you follow, through the next eight months.</p>
      </header>

      {months.length === 0 ? (
        <Empty>No dates announced yet for your shows. The renewals below will land here the moment they get one.</Empty>
      ) : (
        <div className="tl-strip" role="list" aria-label="Upcoming by month">
          {months.map(([mk, days]) => (
            <section key={mk} className="tl-month" role="listitem" aria-label={monthLabel(mk)}>
              <h2 className="tl-month-title">{monthLabel(mk)}</h2>
              <div className="tl-days">
                {[...days.entries()].map(([iso, items]) => (
                  <div key={iso} className="tl-row">
                    <Day iso={iso} />
                    <div className="tl-items">
                      {items.map((it) => (
                        <Item key={`${it.tmdb_id}-${it.season}-${it.episode}`} it={it} />
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </section>
          ))}
        </div>
      )}

      {data.renewed_no_date.length > 0 && (
        <Section title="Renewed · date TBA">
          <div className="undated-grid">
            {data.renewed_no_date.map((u) => (
              <UndatedChip key={`${u.tmdb_id}-${u.season}`} item={u} />
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}
