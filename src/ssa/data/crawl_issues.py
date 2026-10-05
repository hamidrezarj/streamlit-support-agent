"""Crawl Streamlit GitHub issues (+ comments, + timeline) for the chosen domain.

Run:   python -m ssa.data.crawl_issues            (from repo root, PYTHONPATH=src)
       python -m ssa.data.crawl_issues --max-issues 20 --no-timeline   # smoke test

Phases (each one is resumable - files already on disk are skipped):
  1. candidates : label listing + keyword search  -> data/raw/github/candidates.json
  2. select     : drop PRs, date filter, priority order, cap -> selection in crawl_log
  3. comments   : /issues/{n}/comments               -> data/raw/github/comments/{n}.json
  4. timeline   : /issues/{n}/timeline (compact)     -> data/raw/github/timeline/{n}.json

Raw issue objects come straight from the listing/search responses, so no extra
request per issue is needed. Each raw file is wrapped as {"fetched_at", "data"}.
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import config as C
from .github_client import GitHubClient, NotFound, RateLimitExhausted
from .io_utils import read_json, utcnow, write_json

log = logging.getLogger("crawl_issues")

ISSUES_DIR = C.GITHUB_RAW / "issues"
COMMENTS_DIR = C.GITHUB_RAW / "comments"
TIMELINE_DIR = C.GITHUB_RAW / "timeline"
CANDIDATES = C.GITHUB_RAW / "candidates.json"
CRAWL_LOG = C.GITHUB_RAW / "crawl_log.json"

# Timeline events worth keeping (others are noise like 'subscribed', 'mentioned')
KEEP_EVENTS = {"closed", "reopened", "labeled", "unlabeled", "cross-referenced",
               "referenced", "marked_as_duplicate", "unmarked_as_duplicate",
               "connected", "renamed"}


def _save_issue(it: dict) -> None:
    p = ISSUES_DIR / f"{it['number']}.json"
    write_json(p, {"fetched_at": utcnow(), "data": it})


# ------------------------------------------------------------- phase 1
def collect_candidates(gh: GitHubClient, labels, keywords, per_kw: int) -> dict:
    cands: dict[str, dict] = {}

    def add(it: dict, source: str) -> None:
        key = str(it["number"])
        c = cands.setdefault(key, {"sources": [], "is_pr": "pull_request" in it,
                                   "created_at": it["created_at"],
                                   "comments": it.get("comments", 0)})
        if source not in c["sources"]:
            c["sources"].append(source)
        if not c["is_pr"]:
            _save_issue(it)

    for lab in labels:
        n0 = len(cands)
        for it in gh.paginate(f"/repos/{C.REPO}/issues", {"labels": lab, "state": "all"}):
            add(it, f"label:{lab}")
        log.info("label %-26s -> +%d new (total %d)", lab, len(cands) - n0, len(cands))

    for kw in keywords:
        q = f'repo:{C.REPO} is:issue in:title,body "{kw}"'
        n0 = len(cands)
        for it in gh.search_issues(q, max_items=per_kw):
            add(it, f"kw:{kw}")
        log.info("keyword %-24r -> +%d new (total %d)", kw, len(cands) - n0, len(cands))
    return cands


# ------------------------------------------------------------- phase 2
def select(cands: dict, since: str, max_issues: int) -> tuple[list[int], dict]:
    stats = {"candidates": len(cands), "dropped_pull_requests": 0, "dropped_before_since": 0}
    pool = []
    for num, c in cands.items():
        if c["is_pr"]:
            stats["dropped_pull_requests"] += 1
            continue
        if c["created_at"] < since:
            stats["dropped_before_since"] += 1
            continue
        has_label = any(s.startswith("label:") for s in c["sources"])
        n_kw = sum(s.startswith("kw:") for s in c["sources"])
        # deterministic priority: labelled first, then #keyword hits, then discussion size
        pool.append(((not has_label, -n_kw, -c["comments"], int(num)), int(num)))
    pool.sort()
    chosen = [n for _, n in pool[:max_issues]]
    stats["eligible"] = len(pool)
    stats["selected"] = len(chosen)
    return sorted(chosen), stats


# ------------------------------------------------------------- phase 3/4
def fetch_comments(gh: GitHubClient, numbers: list[int]) -> int:
    done = 0
    for i, n in enumerate(numbers, 1):
        p = COMMENTS_DIR / f"{n}.json"
        if p.exists():
            continue
        issue = read_json(ISSUES_DIR / f"{n}.json")["data"]
        if issue.get("comments", 0) == 0:
            write_json(p, {"fetched_at": utcnow(), "data": []})
            continue
        items = list(gh.paginate(f"/repos/{C.REPO}/issues/{n}/comments"))
        write_json(p, {"fetched_at": utcnow(), "data": items})
        done += 1
        if done % 25 == 0:
            log.info("comments: %d/%d issues (requests so far %d)", i, len(numbers), gh.request_count)
    return done


def _compact_event(e: dict) -> dict:
    out = {"event": e.get("event"), "created_at": e.get("created_at"),
           "actor_type": (e.get("actor") or {}).get("type")}
    if e.get("label"):
        out["label"] = e["label"].get("name")
    if e.get("commit_id"):
        out["commit_id"] = e["commit_id"]
    if e.get("state_reason"):
        out["state_reason"] = e["state_reason"]
    if e.get("rename"):
        out["rename"] = e["rename"]
    src = (e.get("source") or {}).get("issue")
    if src:
        out["source"] = {"number": src.get("number"), "is_pr": "pull_request" in src,
                         "state": src.get("state"), "title": src.get("title"),
                         "repo": (src.get("repository") or {}).get("full_name")}
    return out


def fetch_timelines(gh: GitHubClient, numbers: list[int]) -> int:
    done = 0
    for n in numbers:
        p = TIMELINE_DIR / f"{n}.json"
        if p.exists():
            continue
        try:
            ev = [_compact_event(e) for e in gh.paginate(f"/repos/{C.REPO}/issues/{n}/timeline")
                  if e.get("event") in KEEP_EVENTS]
        except NotFound:
            ev = []
        write_json(p, {"fetched_at": utcnow(), "data": ev})
        done += 1
        if done % 25 == 0:
            log.info("timeline: %d fetched (requests so far %d)", done, gh.request_count)
    return done


# ------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-issues", type=int, default=C.MAX_ISSUES)
    ap.add_argument("--since", default=C.CREATED_SINCE)
    ap.add_argument("--per-keyword", type=int, default=C.MAX_SEARCH_RESULTS_PER_KEYWORD)
    ap.add_argument("--labels", nargs="*", default=C.DOMAIN_LABELS)
    ap.add_argument("--keywords", nargs="*", default=C.DOMAIN_KEYWORDS)
    ap.add_argument("--no-timeline", action="store_true")
    ap.add_argument("--refresh-candidates", action="store_true",
                    help="re-run phase 1 even if candidates.json exists")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    C.load_dotenv()
    gh = GitHubClient()
    if not gh.authenticated:
        log.warning("GITHUB_TOKEN not set: 60 requests/hour. Fine for a smoke test only.")

    started = utcnow()
    prev = read_json(CRAWL_LOG) if CRAWL_LOG.exists() else {}
    try:
        cand_params = {"labels": args.labels, "keywords": args.keywords, "per_keyword": args.per_keyword}
        cached = read_json(CANDIDATES) if CANDIDATES.exists() else None
        if cached and cached.get("params") == cand_params and not args.refresh_candidates:
            cands, cand_time = cached["data"], cached["fetched_at"]
            log.info("reusing %d candidates from %s", len(cands), CANDIDATES.name)
        else:
            cands = collect_candidates(gh, args.labels, args.keywords, args.per_keyword)
            cand_time = utcnow()
            write_json(CANDIDATES, {"fetched_at": cand_time, "params": cand_params, "data": cands})

        chosen, stats = select(cands, args.since, args.max_issues)
        log.info("selection: %s", stats)
        fetch_comments(gh, chosen)
        if not args.no_timeline:
            fetch_timelines(gh, chosen)
        status = "complete"
    except RateLimitExhausted as e:
        log.error("%s", e)
        status, chosen, stats = "partial", locals().get("chosen", []), locals().get("stats", {})

    write_json(CRAWL_LOG, {
        "status": status,
        "repo": C.REPO,
        "run_started_at": started,
        "run_finished_at": utcnow(),
        "candidates_fetched_at": locals().get("cand_time"),
        "authenticated": gh.authenticated,
        "requests_this_run": gh.request_count,
        "requests_previous_runs": prev.get("requests_total", 0),
        "requests_total": prev.get("requests_total", 0) + gh.request_count,
        "params": {"labels": args.labels, "keywords": args.keywords, "since": args.since,
                   "per_keyword": args.per_keyword, "max_issues": args.max_issues,
                   "timeline": not args.no_timeline},
        "selection_stats": stats,
        "selected_issue_numbers": chosen,
        "sampling_method": ("union of label listing and keyword search (state=all); "
                            "drop PRs and issues created before `since`; order by "
                            "(has domain label, #keyword hits, #comments) desc; keep top max_issues"),
    })
    log.info("done (%s). requests this run: %d", status, gh.request_count)
    return 0 if status == "complete" else 2


if __name__ == "__main__":
    sys.exit(main())
