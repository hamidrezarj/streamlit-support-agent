#!/usr/bin/env bash
# Chunk -> BM25 index -> embeddings (cached; only new/changed chunks are paid) -> sanity eval
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src
python -m ssa.index.chunk
python -m ssa.index.store
python -m ssa.index.embed --dry-run
python -m ssa.index.embed
python -m ssa.eval.retrieval_auto
