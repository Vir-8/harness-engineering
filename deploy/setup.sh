#!/usr/bin/env bash
# Sets up the harness on a fresh Ubuntu server, from the copy of the repository on it. Safe to run again.
#   deploy/setup.sh
# Needs .env next to docker-compose.yml: OPENAI_API_KEY, GEMINI_API_KEY, HARNESS_API_KEY and API_HOST (see README).
set -euo pipefail
cd "$(dirname "$0")/.."
compose() { sudo docker compose -f docker-compose.yml -f docker-compose.deploy.yml --profile app "$@"; }

if ! command -v docker >/dev/null; then
  sudo apt-get update -q
  sudo apt-get install -y -q docker.io docker-compose-v2
fi

compose up -d --build
until curl -sf http://localhost:8000/health >/dev/null; do sleep 2; done  # the API is up once its databases are
compose exec -T api python scripts/ingest_policies.py  # embeddings for the policy text (a fraction of a cent)
compose exec -T api python scripts/seed_demo.py        # the demo day, including the graph of linked accounts

# Rebuild the demo every day at 09:00 UTC, so the console's "last 24 h" figures never go stale.
job="0 9 * * * cd $PWD && sudo docker compose -f docker-compose.yml -f docker-compose.deploy.yml --profile app exec -T api python scripts/seed_demo.py >> /tmp/seed_demo.log 2>&1"
{ crontab -l 2>/dev/null | grep -v seed_demo || true; echo "$job"; } | crontab -  # replaces any earlier copy
echo "Up. The API answers on https://$(grep '^API_HOST=' .env | cut -d= -f2)/health"
