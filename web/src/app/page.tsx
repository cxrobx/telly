"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowRight, CalendarClock, X } from "lucide-react";
import { useBackdrop } from "@/components/backdrop";
import { Tell } from "@/components/taste";
import { Chip, EpisodeCard, EventRow, PickCard, Rail, Section, Skeleton, Empty, Toast, type NewsAction } from "@/components/ui";
import { api, backdrop, epLabel, fmtDate, relDay, useApi, type Home, type Me, type NewsDismissed, type NewsEvent } from "@/lib/api";

function Hero({ data }: { data: Home }) {
  const h = data.hero;
  const pick = data.for_you[0];
  if (h) {
    return (
      <section className="hero">
        <div className="hero-art" style={h.backdrop_path ? { backgroundImage: `url(${backdrop(h.backdrop_path)})` } : undefined} />
        <div className="glass hero-panel">
          <div className="hero-eyebrow">
            <CalendarClock size={14} /> Up next for you · {relDay(h.date)}
          </div>
          <h1 className="hero-title">{h.show}</h1>
          <div className="hero-meta">
            {h.kind === "season_premiere" ? (
              <Chip tone="accent">Season {h.season} premieres {fmtDate(h.date)}</Chip>
            ) : (
              <Chip tone="accent">{epLabel(h.season, h.episode)}</Chip>
            )}
            {h.networks?.[0] && <span className="hero-network">{h.networks[0]}</span>}
          </div>
          {h.kind === "episode" && h.title && <p className="hero-reason hero-ep-title">&ldquo;{h.title}&rdquo;</p>}
          <div className="hero-actions">
            <Link href={`/show/${h.tmdb_id}`} className="btn btn-primary">
              Details <ArrowRight size={16} />
            </Link>
            <Link href="/timeline" className="btn">
              Your timeline
            </Link>
          </div>
        </div>
      </section>
    );
  }
  if (pick) {
    return (
      <section className="hero">
        <div className="hero-art" style={pick.backdrop_path ? { backgroundImage: `url(${backdrop(pick.backdrop_path)})` } : undefined} />
        <div className="glass hero-panel">
          <div className="hero-eyebrow">Picked for you</div>
          <h1 className="hero-title">{pick.title}</h1>
          <p className="hero-reason">{pick.reason}</p>
          <div className="hero-actions">
            <Link href="/for-you" className="btn btn-primary">
              More picks <ArrowRight size={16} />
            </Link>
          </div>
        </div>
      </section>
    );
  }
  return null;
}

// A new member has nothing for Telly to go on. Ask, right here, instead of leaving them to find
// Your taste. Their first picks are built as soon as they answer (recs.warm on the API).
function TasteStarter({ ready, waiting, onTold, onClose }: { ready: boolean; waiting: boolean; onTold: (liked: boolean) => void; onClose: () => void }) {
  return (
    <section className="section">
      <div className="glass starter-card">
        <div className="starter-head">
          <div>
            <h2 className="section-title">What do you love watching?</h2>
            <p className="muted small starter-sub">
              Name a few shows or movies you liked, watched anywhere. Telly builds your picks from them in about a minute.
            </p>
          </div>
          <button className="icon-btn ghost" aria-label="Not now" onClick={onClose}>
            <X size={16} />
          </button>
        </div>
        <Tell onTold={onTold} />
        {ready ? (
          <p className="small accent-text">
            Your picks are in, below. Add more any time on <Link href="/taste">Your taste</Link>.
          </p>
        ) : waiting ? (
          <p className="small accent-text" role="status">Building your picks…</p>
        ) : (
          <p className="small muted">
            Rate things on IMDb? <Link href="/taste">Bring your ratings in</Link> instead.
          </p>
        )}
      </div>
    </section>
  );
}

type Undo = { ev: NewsEvent; at: number; text: string; undo: boolean };

/** Dismiss / Mark watched on What's new: gone at once, then the API, with Undo (restore) for a
 *  few seconds. A failed write puts the item back. */
function useNewsActions(data: Home | null, setData: (fn: (d: Home | null) => Home | null) => void) {
  const [toast, setToast] = useState<Undo | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const show = useCallback((t: Undo | null, ms = 6000) => {
    if (timer.current) clearTimeout(timer.current);
    setToast(t);
    if (t) timer.current = setTimeout(() => setToast(null), ms);
  }, []);
  useEffect(() => () => void (timer.current && clearTimeout(timer.current)), []);
  const putBack = useCallback(
    (ev: NewsEvent, at: number) =>
      setData((d) => {
        if (!d || d.whats_new.some((x) => x.event_id === ev.event_id)) return d;
        const list = [...d.whats_new];
        list.splice(Math.min(at, list.length), 0, ev);
        return { ...d, whats_new: list };
      }),
    [setData],
  );
  const act = useCallback(
    (ev: NewsEvent, action: NewsAction) => {
      const at = Math.max(0, data?.whats_new.findIndex((x) => x.event_id === ev.event_id) ?? 0);
      setData((d) => (d ? { ...d, whats_new: d.whats_new.filter((x) => x.event_id !== ev.event_id) } : d));
      show({ ev, at, text: action === "watched" ? `Marked watched · ${ev.show}` : "Dismissed", undo: true });
      api<NewsDismissed>(`/whats-new/${ev.event_id}/${action === "watched" ? "watched" : "dismiss"}`, { method: "POST" }).catch(() => {
        putBack(ev, at);
        show({ ev, at, text: "Couldn't save that. Try again.", undo: false }, 4000);
      });
    },
    [data, setData, show, putBack],
  );
  const undo = useCallback(() => {
    if (!toast) return;
    const { ev, at } = toast;
    putBack(ev, at);
    show(null);
    api(`/whats-new/${ev.event_id}/restore`, { method: "POST" }).catch(() => {
      setData((d) => (d ? { ...d, whats_new: d.whats_new.filter((x) => x.event_id !== ev.event_id) } : d));
      show({ ev, at, text: "Couldn't undo that.", undo: false }, 4000);
    });
  }, [toast, putBack, show, setData]);
  return { toast, act, undo };
}

export default function HomePage() {
  const { data, loading, reload, setData } = useApi<Home>("/home");
  const { data: me } = useApi<Me>("/me");
  const news = useNewsActions(data, setData);
  // Once they start answering, the card stays until they leave, even though needs_taste flips.
  const [started, setStarted] = useState(false);
  const [told, setTold] = useState(false);
  const noPicks = !!data && data.for_you.length === 0;
  const waiting = noPicks && (told || !!data?.building);

  // their first picks take ~10–60 s: check back every 3 s, for up to two minutes
  useEffect(() => {
    if (!waiting) return;
    let tries = 0;
    const t = setInterval(() => (++tries > 40 ? clearInterval(t) : reload()), 3000);
    return () => clearInterval(t);
  }, [waiting, reload]);

  const onTold = (liked: boolean) => {
    setStarted(true);
    if (liked) setTold(true);
  };
  const closeStarter = () => {
    setStarted(false);
    setData((d) => (d ? { ...d, needs_taste: false } : d));
    api("/home/taste-prompt/dismiss", { method: "POST" }).catch(() => {});
  };
  useBackdrop(backdrop(data?.hero?.backdrop_path ?? data?.for_you[0]?.backdrop_path, "w780"));

  const dropNewPick = (tmdb_id: number, media_type: "tv" | "movie") =>
    setData((d) =>
      d
        ? { ...d, whats_new_picks: (d.whats_new_picks ?? []).filter((x) => !(x.tmdb_id === tmdb_id && x.media_type === media_type)) }
        : d,
    );
  const newPicks = data?.whats_new_picks ?? [];

  if (loading || !data) return <Skeleton rows={4} />;
  return (
    <div className="page">
      <Hero data={data} />

      {(data.needs_taste || started) && <TasteStarter ready={started && !noPicks} waiting={waiting} onTold={onTold} onClose={closeStarter} />}

      {data.airing_soon.length > 0 && (
        <Section title="Airing soon" action={<Link href="/timeline" className="section-link">Timeline</Link>}>
          <Rail label="Airing soon">
            {data.airing_soon.map((it) => (
              <EpisodeCard key={`${it.tmdb_id}-${it.date}-${it.episode}`} item={it} />
            ))}
          </Rail>
        </Section>
      )}

      <Section title="What's new">
        {data.whats_new.length > 0 && (
          <div className="glass event-list" role="list">
            {data.whats_new.map((ev) => (
              <EventRow key={ev.event_id} ev={ev} onAct={news.act} />
            ))}
          </div>
        )}
        {newPicks.length > 0 && (
          <div className={data.whats_new.length > 0 ? "news-fresh-wrap" : undefined}>
            {data.whats_new.length > 0 ? (
              <h3 className="news-subhead">New releases for you</h3>
            ) : (
              <p className="muted small news-caption">Fresh releases matched to your taste, saved preferences, and recent watches.</p>
            )}
            <Rail label="New releases for you">
              {newPicks.map((p) => (
                <PickCard
                  key={`${p.media_type}-${p.tmdb_id}`}
                  pick={p}
                  canRequest={me?.can_request}
                  compact
                  badgeLabel={p.kind_label}
                  trendingBadge={false}
                  quickActions
                  onRated={(v) => v === -1 && dropNewPick(p.tmdb_id, p.media_type)}
                />
              ))}
            </Rail>
          </div>
        )}
        {data.whats_new.length === 0 && newPicks.length === 0 && (
          <Empty>Nothing new in the last month. Renewals, premieres and new episodes for your shows land here.</Empty>
        )}
      </Section>

      {news.toast && (
        <Toast key={`${news.toast.ev.event_id}-${news.toast.text}`} onUndo={news.toast.undo ? news.undo : undefined}>
          {news.toast.text}
        </Toast>
      )}

      {data.trending.length > 0 && (
        <Section title="Trending">
          <Rail label="Trending">
            {data.trending.map((p) => (
              <PickCard key={`${p.media_type}-${p.tmdb_id}`} pick={p} canRequest={me?.can_request} compact trendingBadge={false} />
            ))}
          </Rail>
        </Section>
      )}

      {data.recent_picks?.length > 0 && (
        <Section title="Based on your recent watches">
          <p className="muted small">Inspired by what you watched on Plex in the last 30 days. Syncs hourly.</p>
          <Rail label="Based on your recent watches">
            {data.recent_picks.map((p) => (
              <PickCard key={`${p.media_type}-${p.tmdb_id}`} pick={p} canRequest={me?.can_request} compact />
            ))}
          </Rail>
        </Section>
      )}

      {data.for_you.length > 0 && (
        <Section title="For you" action={<Link href="/for-you" className="section-link">See all</Link>}>
          <Rail label="For you">
            {data.for_you.slice(0, 10).map((p) => (
              <PickCard key={`${p.media_type}-${p.tmdb_id}`} pick={p} canRequest={me?.can_request} compact />
            ))}
          </Rail>
        </Section>
      )}
    </div>
  );
}
