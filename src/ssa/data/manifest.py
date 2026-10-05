"""Write data/manifest.json: everything needed to reproduce / report the snapshot."""
from __future__ import annotations

import json
import platform
import sys

from . import config as C
from .io_utils import read_json, sha256_file, utcnow, write_json

FILES = ["issues.jsonl", "issue_families.json", "docs_sections.jsonl",
         "config_options.jsonl", "release_notes.jsonl", "versions.json"]


def main() -> int:
    gh = read_json(C.GITHUB_RAW / "crawl_log.json")
    docs = read_json(C.RAW / "docs_crawl_log.json")
    clean = read_json(C.PROCESSED / "cleaning_report.json")
    files = {}
    for f in FILES:
        p = C.PROCESSED / f
        if p.exists():
            n = sum(1 for _ in p.open()) if f.endswith(".jsonl") else None
            files[f] = {"sha256": sha256_file(p), "records": n, "bytes": p.stat().st_size}
    gh_public = {k: v for k, v in gh.items() if k != "selected_issue_numbers"}
    manifest = {
        "built_at": utcnow(),
        "python": platform.python_version(),
        "github": {**gh_public, "n_selected": len(gh.get("selected_issue_numbers", []))},
        "docs": docs,
        "cleaning": clean,
        "files": files,
        "reproduce": [
            "cp .env.example .env  # add GITHUB_TOKEN",
            "PYTHONPATH=src python -m ssa.data.crawl_docs        # pinned by data/raw/docs.lock",
            "PYTHONPATH=src python -m ssa.data.crawl_issues",
            "PYTHONPATH=src python -m ssa.data.clean_issues",
            "PYTHONPATH=src python -m ssa.data.manifest",
        ],
    }
    write_json(C.MANIFEST, manifest)
    # the selected issue list is long -> separate file, still versioned
    write_json(C.DATA / "selected_issues.json", gh.get("selected_issue_numbers", []))
    print(json.dumps({k: manifest["files"][k]["records"] for k in manifest["files"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
