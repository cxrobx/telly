# Watch history and ratings

Built 2026-09-28 from Chris's ask (a History page like Following, fed by Plex, with manual adds and ratings that shape picks). Code: `backend/src/telly/history.py`, `recs.py` (seeds, negatives, the model's prompt), `web/src/app/history/page.tsx`.

## What it is

The History page is laid out like Following. It lists everything someone watched on Plex, plus anything they add from elsewhere. Each title has a **status** and, once they give one, a **rating**. The ratings are the strongest taste signal Telly has.

- **Rating:** Not for me / Liked it / Loved it (stored as −1 / 1 / 2). "Not rated" is the default and never counts as a dislike. Ratings are per show, not per season or episode.
- **Status from Plex:**
  - *caught_up*: they've seen the latest aired episode of a show that's still running.
  - *finished*: the same for an ended or canceled show, and any movie they played.
  - *watching*: played in the last 60 days, with aired episodes left.
  - *stalled*: nothing for 60 days, with aired episodes left.
- **Status set by hand:** watching / finished / dropped. What someone sets always wins over Plex. "From Plex" puts it back to Plex's status.
- **Stalled is never taken as dropped.** On a shared server people often finish a show somewhere else, so the page asks "Still watching?" instead.
- **Storage:** `watch_entries` only holds what the person said. Plex plays are never copied there. A Plex title with no rating and no status has no row.
- **Migration:** the old `told` taste signal ("they said they liked it") moved here as Liked it. A `told` title that had been switched off came over unrated.

## How ratings shape picks (`recs.py`)

The rating decides whether a title counts for or against. Plex history only changes how much it counts.

| What they said | Effect |
|---|---|
| Loved it | Seed at 1.3× their most-watched show: the strongest seed there is |
| Liked it | Seed at 0.9× their most-watched show, or its Plex weight if that's higher |
| Not rated, watched on Plex | Plex weight, as before (episodes × recency) |
| Not rated, added by hand | Seed at 0.5× |
| Not for me (or 4 and under on IMDb) | Not a seed. It pushes down what TMDB recommends off it (weight 0.8) |
| Dropped early | Not a seed. It pushes down lookalikes (weight 0.4) |
| Dropped later | Not a seed, and nothing else happens |

- **Dropped early** means 2 episodes or fewer, or up to 5 episodes if that's under 15% of what has aired. Past 5 episodes a drop is never early: 26 episodes of a 1,000-episode anime is under 3% of the show and still a real try.
- **The push is capped at half.** A dislike can sink a lookalike, but it never hides a whole genre.
- At most 6 negatives are used per person, to limit TMDB calls.
- **The model gets categories, not numbers:** "Loved: …", "Liked: …", "Watched a lot: …", "Didn't like: …", "Gave up on after an episode or two: …". Each candidate that resembles something they disliked is labelled as such in the prompt.
- Anything with a History row is never recommended back to them.

## Why three levels (research, 2026-09-28)

- **More ratings beat finer ones.** Netflix got 200% more ratings when it moved from 5 stars to thumbs in 2017 [1]. With 10–30 people on the server, how many ratings people give matters more than how fine the scale is.
- **Finer scales barely help.** Peska & Balcar found only a small accuracy gain from finer scales, often wiped out by even a small drop in how many ratings people gave [2].
- **The middle of a scale is noise.** When people re-rated the same movies, their own ratings disagreed by an RMSE of about 0.55–0.63. The extremes were the most consistent, 2↔3 and 3↔4 were the most common changes, and over 90% of changes were ±1 [3].
- **Round numbers dominate.** IMDb and Letterboxd ratings pile up on round numbers, so most of a 10-point scale goes unused [4].
- **Loving isn't liking.** Netflix added "Love this" in 2022 for that reason [5]. For Telly the split matters: loved shows are the best seeds.
- **Plex supplies the amount.** Hu, Koren & Volinsky split implicit data into a preference and a confidence [6]. Here the rating is the preference and Plex viewing is the confidence.
- **Showing a predicted rating biases people toward it** [7]. So nothing like "you'll like this 87%" appears before someone rates.

The weights and thresholds above are Telly's own starting values, not published figures; tune them against real use.

## Not done yet

- **A prompt at the moment someone finishes** (a Discord DM or ntfy when they finish a season: "rate it?"). The research says this is when people are most willing to rate. Parked until Chris asks for it.
- **Per-person normalisation** (someone who loves everything means less by "Loved"). There is too little data to matter yet.
- **Importing Plex's own star ratings.** Telly already stores signed-in members' Plex tokens, so it could read their `userRating`s.

## Sources

1. https://about.netflix.com/en/news/goodbye-stars-hello-thumbs
2. Peska & Balcar, RecSys 2022 — https://dl.acm.org/doi/10.1145/3523227.3551479
3. Amatriain, Pujol & Oliver, UMAP 2009 — https://amatria.in/pubs/umap09.pdf
4. https://www.datawrapper.de/blog/movie-reviews-rating-scales
5. https://variety.com/2022/digital/news/netflix-two-thumbs-up-ratings-1235228641/
6. Hu, Koren & Volinsky, ICDM 2008 — http://yifanhu.net/PUB/cf.pdf
7. Cosley et al., CHI 2003 — https://experts.umn.edu/en/publications/is-seeing-believing-how-recommender-interfaces-affect-users-opini
