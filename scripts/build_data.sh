#!/usr/bin/env bash
# Build the full data snapshot. Safe to re-run: every step resumes from cache.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src
python -m ssa.data.crawl_docs "$@"
python -m ssa.data.crawl_issues
python -m ssa.data.clean_issues
python -m ssa.data.manifest
