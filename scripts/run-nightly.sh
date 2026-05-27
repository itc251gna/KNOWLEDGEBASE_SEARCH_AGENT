#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
docker compose run --rm app python -m portal_search_agent.cli crawl --reset
