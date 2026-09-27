#!/usr/bin/env bash
# Deploy Telly to the NAS: ship the committed tree (git archive HEAD, never the working
# copy), build next to the running stack, then swap. No git checkout on the NAS, nothing
# pushed anywhere. A failed build leaves the running containers and the previous src alone.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -n "$(git status --porcelain)" ]; then
  echo "Working tree is dirty. Commit first: deploys ship HEAD only." >&2
  exit 1
fi
REV=$(git rev-parse --short HEAD)
R=/volume2/docker/telly
D=/usr/local/bin/docker

echo "→ shipping $REV"
ssh nas "mkdir -p $R/data && rm -rf $R/src.new && mkdir -p $R/src.new"
git archive --format=tar HEAD | ssh nas "tar -x -C $R/src.new"
ssh nas "test -f $R/.env" || { echo "Missing $R/.env on the NAS (see deploy/README.md)." >&2; exit 1; }

echo "→ building (the live stack keeps running)"
if ! ssh nas "cd $R && cp src.new/deploy/docker-compose.yml docker-compose.new.yml && \
    sed -e 's#\./src/#./src.new/#' docker-compose.new.yml > docker-compose.build.yml && \
    $D compose -p telly -f docker-compose.build.yml build"; then
  echo "Build failed; nothing was changed on the running stack." >&2
  ssh nas "rm -rf $R/src.new $R/docker-compose.build.yml $R/docker-compose.new.yml"
  exit 1
fi

echo "→ swapping in $REV"
ssh nas "cd $R && rm -rf src.prev && { [ -d src ] && mv src src.prev || true; } && mv src.new src && \
  mv docker-compose.new.yml docker-compose.yml && rm -f docker-compose.build.yml && echo $REV > REVISION && \
  $D compose -p telly up -d"

echo "→ health"
for i in $(seq 1 30); do
  if curl -sf http://192.168.4.22:18940/health >/dev/null && curl -sf -o /dev/null http://192.168.4.22:18941/signin; then
    curl -s http://192.168.4.22:18940/health; echo
    echo "✓ Telly $REV is up"
    exit 0
  fi
  sleep 2
done
echo "✗ health check failed; logs: ssh nas '$D logs telly-api --tail 50'" >&2
exit 1
