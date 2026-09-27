import type { Ratings } from "@/lib/api";

// IMDb and Rotten Tomatoes at a glance. The tomato turns into a green splat below 60%, as
// Rotten Tomatoes itself does; the audience score shows only when a source has one.
const votes = (n: number | null) => (n == null ? "" : n >= 1000 ? `${Math.round(n / 1000)}k votes` : `${n} votes`);

function Tomato({ fresh }: { fresh: boolean }) {
  return fresh ? (
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden>
      <circle cx="8" cy="9.2" r="6" fill="#fa320a" />
      <path d="M8 3.4c-.6-1.3-1.9-2-3.1-1.7.9.5 1.4 1.3 1.5 2.1-1.2-.3-2.3.1-3 .9 1.3-.2 2.6.2 3.2 1 .4-.7 1-1.2 1.4-1.3.4.1 1 .6 1.4 1.3.6-.8 1.9-1.2 3.2-1-.7-.8-1.8-1.2-3-.9.1-.8.6-1.6 1.5-2.1C10 1.4 8.6 2.1 8 3.4Z" fill="#00912d" />
    </svg>
  ) : (
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden>
      <path d="M8 1.5l1.4 3.4 3.6-1.2-1.6 3.4 3.2 1.9-3.6.8.6 3.7L8 11.2l-3.6 2.3.6-3.7-3.6-.8 3.2-1.9L3 3.7l3.6 1.2L8 1.5Z" fill="#0ac855" />
    </svg>
  );
}

function Popcorn() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden>
      <path d="M3.5 6.5h9l-1.3 8h-6.4l-1.3-8Z" fill="#fa320a" />
      <path d="M5.4 6.5l.6 8M8 6.5v8M10.6 6.5l-.6 8" stroke="#fff" strokeWidth=".9" />
      <circle cx="5" cy="5.2" r="1.8" fill="#fff4d6" /><circle cx="8" cy="4.3" r="2" fill="#fff4d6" /><circle cx="11" cy="5.2" r="1.8" fill="#fff4d6" />
    </svg>
  );
}

export function Scores({ ratings, compact = false }: { ratings?: Ratings; compact?: boolean }) {
  if (!ratings || (ratings.imdb == null && ratings.rt_critic == null && ratings.rt_audience == null)) return null;
  const { imdb, imdb_votes, imdb_id, rt_critic, rt_audience } = ratings;
  return (
    <div className="scores" aria-label="Ratings">
      {imdb != null && (
        <a
          className="score"
          href={imdb_id ? `https://www.imdb.com/title/${imdb_id}/` : undefined}
          target="_blank"
          rel="noreferrer"
          title={`IMDb ${imdb}/10${imdb_votes ? ` · ${votes(imdb_votes)}` : ""}`}
          onClick={(e) => e.stopPropagation()}
        >
          <span className="imdb-badge">IMDb</span>
          <span>{imdb.toFixed(1)}</span>
          {!compact && imdb_votes != null && <span className="score-sub">{votes(imdb_votes)}</span>}
        </a>
      )}
      {rt_critic != null && (
        <span className="score" title={`Rotten Tomatoes critics: ${rt_critic}%`}>
          <Tomato fresh={rt_critic >= 60} />
          <span>{rt_critic}%</span>
        </span>
      )}
      {rt_audience != null && (
        <span className="score" title={`Rotten Tomatoes audience: ${rt_audience}%`}>
          <Popcorn />
          <span>{rt_audience}%</span>
        </span>
      )}
    </div>
  );
}
