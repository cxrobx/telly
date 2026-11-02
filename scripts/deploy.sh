#!/usr/bin/env bash
# Deploy Telly to the NAS: build the committed tree (git archive HEAD, never the working
# copy) on this Mac, load both images onto the NAS, then swap. Nothing pushed anywhere, and
# the NAS builds nothing, so it keeps no build cache. A failed build leaves the running
# containers alone; a failed health check rolls back.
# scripts/deploy.sh --rollback goes back to the images the last deploy replaced.
# Off the home LAN: NAS=nas-ts NAS_HOST=100.88.68.22 scripts/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

export NAS=${NAS:-nas}  # nas-ship.sh reads this too
NAS_HOST=${NAS_HOST:-192.168.4.22}
R=/volume2/docker/telly
D=/usr/local/bin/docker
API=telly-telly-api
WEB=telly-telly-web
SHIP=${SHIP:-$HOME/Projects/nas-ops/scripts/nas-ship.sh}

health() {
  for i in $(seq 1 30); do
    if curl -sf http://$NAS_HOST:18940/health >/dev/null && curl -sf -o /dev/null http://$NAS_HOST:18941/signin; then
      curl -s http://$NAS_HOST:18940/health; echo
      return 0
    fi
    sleep 2
  done
  return 1
}

rollback() {
  echo "→ rolling back to the previous images"
  ssh "$NAS" "cd $R && $D tag $API:previous $API:latest && $D tag $WEB:previous $WEB:latest && \
    { [ ! -f REVISION.prev ] || mv REVISION.prev REVISION; } && $D compose -p telly up -d"
  if health; then
    echo "✓ Telly is back on the previous images"
  else
    echo "✗ health check failed after rollback; logs: ssh nas '$D logs telly-api --tail 50'" >&2
    return 1
  fi
}

if [ "${1:-}" = "--rollback" ]; then
  rollback
  exit
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "Working tree is dirty. Commit first: deploys ship HEAD only." >&2
  exit 1
fi
REV=$(git rev-parse --short HEAD)

ssh "$NAS" "mkdir -p $R/data"
ssh "$NAS" "test -f $R/.env" || { echo "Missing $R/.env on the NAS (see deploy/README.md)." >&2; exit 1; }

echo "→ building $REV on this Mac (the live stack keeps running)"
CTX=$(mktemp -d)
trap 'rm -rf "$CTX"' EXIT
git archive --format=tar HEAD | tar -x -C "$CTX"
"$SHIP" "$API:$REV" "$CTX/backend"
"$SHIP" "$WEB:$REV" "$CTX/web" --build-arg TELLY_API_URL=http://telly-api:8940

echo "→ swapping in $REV"
git show HEAD:deploy/docker-compose.yml | ssh "$NAS" "cat > $R/docker-compose.yml"
ssh "$NAS" "cd $R && cp REVISION REVISION.prev && \
  $D tag \$($D inspect --format '{{.Image}}' telly-api) $API:previous && \
  $D tag \$($D inspect --format '{{.Image}}' telly-web) $WEB:previous && \
  $D tag $API:$REV $API:latest && $D tag $WEB:$REV $WEB:latest && echo $REV > REVISION && \
  $D compose -p telly up -d"

echo "→ health"
if health; then
  echo "✓ Telly $REV is up"
  exit 0
fi
echo "✗ health check failed; logs: ssh nas '$D logs telly-api --tail 50'" >&2
rollback
exit 1
