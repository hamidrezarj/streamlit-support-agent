"""Turn raw GitHub JSON into clean, linked issue records.

Run:  python -m ssa.data.clean_issues      (after crawl_issues and crawl_docs)

Output
  data/processed/issues.jsonl          one record per selected issue, comments embedded
  data/processed/issue_families.json   duplicate families (needed for the eval split)
  data/processed/cleaning_report.json  what was removed and why (counts)

Principles (from the project brief)
* Missing environment info stays null - never guessed.
* Labels / state are kept with their history; they are hints, not ground truth.
* `fixed_in` comes from release notes and is FUTURE information relative to the
  issue - carry `release_date` so the eval can filter it out (leakage control).
* Removed: bot comments, "+1"-style noise, issue-template boilerplate, images,
  @mentions, logins, e-mail addresses, home-directory user names.
"""
from __future__ import annotations

import logging
import re
import sys
from collections import Counter, defaultdict

from . import config as C
from .io_utils import read_json, read_jsonl, utcnow, write_json, write_jsonl

log = logging.getLogger("clean_issues")

MAINTAINER_ASSOC = {"OWNER", "MEMBER", "COLLABORATOR"}

# ------------------------------------------------------------- text cleaning
BOILERPLATE = [
    re.compile(r"^- \[[ xX]\] I (have searched|added a|have provided).*$", re.M),
    re.compile(r"^_No response_\s*$", re.M),
    re.compile(r"^If needed, add any other context about the problem here.*$", re.M),
    re.compile(r"^\**If (you'd like|this issue affects you).*?(initial post|reaction).*$", re.M | re.I),
    re.compile(r"^Your feedback helps us prioritize.*$", re.M),
    re.compile(r"!\[Views\]\(https://api\.views-badge\.org[^)]*\)"),
    re.compile(r"<!--.*?-->", re.S),
]
IMG_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")
IMG_HTML = re.compile(r"<img\b[^>]*>", re.I)
MENTION = re.compile(r"(?<![\w/])@[A-Za-z0-9-]{1,39}\b(?![./]\w)")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
HOME = re.compile(r"(/Users/|/home/|C:\\\\Users\\\\|C:\\Users\\)([^/\\\s]+)")
NOISE = re.compile(r"^\W*(\+1|same( here| issue| problem)?|me too|any updates?|bump|thanks?( you)?)\W*$", re.I)
FENCE = re.compile(r"(```[^\n]*\n)(.*?)(\n```)", re.S)


def _trim_code(m: re.Match) -> str:
    lines = m.group(2).split("\n")
    if len(lines) <= C.MAX_CODE_BLOCK_LINES:
        return m.group(0)
    head, tail = lines[:25], lines[-(C.MAX_CODE_BLOCK_LINES - 25):]
    return (m.group(1) + "\n".join(head) +
            f"\n... [{len(lines) - len(head) - len(tail)} lines trimmed] ...\n" +
            "\n".join(tail) + m.group(3))


def _trim_len(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    h = int(n * 0.4)
    return s[:h] + f"\n... [{len(s) - n} chars trimmed] ...\n" + s[-(n - h):]


def clean_text(s: str | None, max_chars: int) -> str:
    s = (s or "").replace("\r\n", "\n")
    for rx in BOILERPLATE:
        s = rx.sub("", s)
    s = IMG_MD.sub("[image]", s)
    s = IMG_HTML.sub("[image]", s)
    s = EMAIL.sub("<email>", s)
    s = HOME.sub(lambda m: m.group(1) + "<user>", s)
    s = MENTION.sub("@user", s)
    s = FENCE.sub(_trim_code, s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return _trim_len(s, max_chars)


# ------------------------------------------------------------- structure
SECTION_H = re.compile(r"^#{1,3}\s+(.+?)\s*$", re.M)


def split_template(body: str) -> dict[str, str]:
    """Bug/feature templates use '### Heading' sections -> {heading_lower: text}."""
    parts = SECTION_H.split(body)
    out = {}
    if parts[0].strip():
        out["_preamble"] = parts[0].strip()
    for i in range(1, len(parts) - 1, 2):
        txt = parts[i + 1].strip()
        if txt:
            out[parts[i].strip().lower()] = txt
    return out


EMPTY = re.compile(r"^(n/?a|none|-|unknown|not sure|\?|latest)?$", re.I)
VERSION_NUM = re.compile(r"\b(\d+\.\d+(?:\.\d+)?)\b")
ENV_FIELDS = {
    "streamlit_version": [r"streamlit version", r"streamlit=="],
    "python_version": [r"python version"],
    "os": [r"operating system", r"os version"],
    "browser": [r"browser(?: version)?"],
}


def extract_env(body: str) -> dict:
    """Pull env fields from the 'Debug info' block. Absent/placeholder -> None."""
    env: dict = {}
    for field, keys in ENV_FIELDS.items():
        raw = None
        for k in keys:
            if k.endswith("=="):
                m = re.search(r"(?im)^\s*" + re.escape(k) + r"\s*([\w.]+)", body)
            else:
                m = re.search(r"(?im)^\s*[-*]?\s*" + k + r"\s*:[ \t]*(.*)$", body)
            if m:
                raw = m.group(1).strip()
                break
        if raw is not None and EMPTY.match(raw):
            raw = None
        env[field] = raw
        if field in ("streamlit_version", "python_version"):
            num = VERSION_NUM.search(raw) if raw else None
            env[field + "_parsed"] = num.group(1) if num else None
    return env


CONTEXT_KEYWORDS = {
    "docker": r"\bdocker", "kubernetes": r"\bkubernetes|\bk8s\b|\bhelm\b",
    "nginx": r"\bnginx", "apache": r"\bapache\b|httpd", "traefik": r"\btraefik",
    "community_cloud": r"community cloud|share\.streamlit|streamlit\.app\b",
    "aws": r"\baws\b|\bec2\b|\becs\b|elastic beanstalk|sagemaker", "azure": r"\bazure",
    "gcp": r"\bgcp\b|cloud run|app engine", "heroku": r"\bheroku",
    "huggingface": r"hugging ?face|hf spaces", "snowflake": r"\bsnowflake|\bsis\b",
    "https_ssl": r"\bssl\b|\btls\b|https", "websocket": r"websocket|\bwss?://",
    "reverse_proxy": r"reverse proxy|\bproxy_pass\b|load balancer|\bingress",
    "windows": r"\bwindows\b", "wsl": r"\bwsl\b",
}


URL = re.compile(r"\b(?:https?|wss?)://\S+")


def detect_context(text: str) -> list[str]:
    """Keyword hints about the deployment context (hints, not verified facts)."""
    t = URL.sub(" ", text.lower())   # don't let github/docs links trigger matches
    return sorted(k for k, rx in CONTEXT_KEYWORDS.items() if re.search(rx, t))


ERR_LINE = re.compile(r"^\s*([A-Za-z_][\w.]*(?:Error|Exception|Warning))\s*:\s*(.{0,200})$", re.M)


def error_signatures(text: str) -> list[str]:
    sigs = []
    for exc, msg in ERR_LINE.findall(text):
        msg = re.sub(r"0x[0-9a-f]+|\d+", "N", msg)        # normalise numbers/addresses
        msg = re.sub(r"(['\"]).*?\1", "'…'", msg)          # normalise quoted values
        s = f"{exc}: {msg.strip()}"[:160]
        if s not in sigs:
            sigs.append(s)
    return sigs[:5]


ISSUE_REF = re.compile(r"(?:streamlit/streamlit/issues/|(?<![\w/&])#)(\d{2,6})\b")
DUP_OF = re.compile(r"(?i)duplicate of\s+(?:https://github\.com/streamlit/streamlit/issues/|#)(\d+)")


def _regression(sections: dict) -> bool | None:
    txt = sections.get("is this a regression?")
    if not txt:
        return None
    if re.search(r"- \[[xX]\]\s*Yes", txt):
        return True
    return False if re.search(r"- \[ \]\s*Yes", txt) else None


def kind_from_labels(labels: list[str]) -> str:
    for k in ("type:bug", "type:regression", "type:enhancement", "type:docs", "type:question"):
        if k in labels:
            return k.split(":")[1]
    if "bug" in labels:
        return "bug"
    if "enhancement" in labels or "feature request" in labels:
        return "enhancement"
    return "unknown"


# ------------------------------------------------------------- families
class DSU:
    def __init__(self):
        self.p: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


# ------------------------------------------------------------- main
def build() -> dict:
    crawl = read_json(C.GITHUB_RAW / "crawl_log.json")
    numbers = crawl["selected_issue_numbers"]
    cands = read_json(C.GITHUB_RAW / "candidates.json")["data"]

    # release-note links: issue -> versions that mention it
    fixed_in = defaultdict(list)
    rn_path = C.PROCESSED / "release_notes.jsonl"
    if rn_path.exists():
        for r in read_jsonl(rn_path):
            for n in r["linked_issues"]:
                fixed_in[n].append({"version": r["version"], "release_date": r["release_date"],
                                    "category": r["category"], "release_note_id": r["id"]})
    else:
        log.warning("release_notes.jsonl missing - run crawl_docs first for fixed_in links")

    rep = Counter()
    dsu = DSU()
    records = []
    for n in numbers:
        iw = read_json(C.GITHUB_RAW / "issues" / f"{n}.json")
        it = iw["data"]
        if "pull_request" in it:  # defensive; selection already drops PRs
            rep["skipped_pr"] += 1
            continue
        cpath = C.GITHUB_RAW / "comments" / f"{n}.json"
        tpath = C.GITHUB_RAW / "timeline" / f"{n}.json"
        raw_comments = read_json(cpath)["data"] if cpath.exists() else None
        timeline = read_json(tpath)["data"] if tpath.exists() else []
        if raw_comments is None:
            rep["issues_missing_comment_file"] += 1
            raw_comments = []

        author = (it.get("user") or {}).get("login")
        body = clean_text(it.get("body"), C.MAX_BODY_CHARS)
        sections = split_template(body)

        comments = []
        for c in raw_comments:
            u = c.get("user") or {}
            if u.get("type") == "Bot" or (u.get("login") or "").endswith("[bot]"):
                rep["comments_removed_bot"] += 1
                continue
            txt = clean_text(c.get("body"), C.MAX_COMMENT_CHARS)
            if not txt or NOISE.match(txt) or len(txt) < 8:
                rep["comments_removed_noise"] += 1
                continue
            comments.append({
                "id": c["id"], "url": c["html_url"],
                "created_at": c["created_at"], "updated_at": c["updated_at"],
                "author_association": c.get("author_association"),
                "is_maintainer": c.get("author_association") in MAINTAINER_ASSOC,
                "is_issue_author": u.get("login") == author,
                "text": txt,
            })
            rep["comments_kept"] += 1
        rep["comments_raw"] += len(raw_comments)

        all_text = it.get("title", "") + "\n" + body + "\n" + "\n".join(c["text"] for c in comments)

        # duplicates & relations
        dup_targets = sorted({int(x) for c in comments for x in DUP_OF.findall(c["text"])} |
                             {int(x) for x in DUP_OF.findall(body)})
        for d in dup_targets:
            dsu.union(n, d)
        dsu.find(n)
        refs = {int(x) for x in ISSUE_REF.findall(all_text)} - {n}
        xref_issues = {e["source"]["number"] for e in timeline
                       if e.get("source") and not e["source"]["is_pr"]
                       and e["source"].get("repo") == C.REPO}
        xref_prs = sorted({e["source"]["number"] for e in timeline
                           if e.get("source") and e["source"]["is_pr"]
                           and e["source"].get("repo") == C.REPO})

        labels = [l["name"] for l in it.get("labels", [])]
        records.append({
            "id": f"issue:{n}",
            "type": "issue",
            "number": n,
            "url": it["html_url"],
            "title": it["title"],
            "kind": kind_from_labels(labels),
            "state": it["state"],
            "state_reason": it.get("state_reason"),
            "labels": labels,
            "label_history": [{"event": e["event"], "label": e.get("label"), "at": e["created_at"]}
                              for e in timeline if e["event"] in ("labeled", "unlabeled")],
            "created_at": it["created_at"],
            "updated_at": it["updated_at"],
            "closed_at": it.get("closed_at"),
            "closed_by_commit": any(e["event"] == "closed" and e.get("commit_id") for e in timeline),
            "author_association": it.get("author_association"),
            "reactions_total": (it.get("reactions") or {}).get("total_count", 0),
            "body": body,
            "sections": sections,
            "env": extract_env(body),
            "regression": _regression(sections),
            "context_keywords": detect_context(all_text),
            "error_signatures": error_signatures(body + "\n" + "\n".join(c["text"] for c in comments)),
            "comments": comments,
            "n_comments_raw": len(raw_comments),
            "duplicate_of": dup_targets,
            "related_issues": sorted(refs | xref_issues),
            "linked_prs": xref_prs,
            "fixed_in": sorted(fixed_in.get(n, []), key=lambda x: x["release_date"] or ""),
            "selected_by": cands.get(str(n), {}).get("sources", []),
            "fetched_at": iw["fetched_at"],
        })
        rep["issues"] += 1
        rep["issues_with_streamlit_version"] += records[-1]["env"]["streamlit_version"] is not None
        rep["issues_closed_as_duplicate"] += it.get("state_reason") == "duplicate"

    # families (only over issues we have + their duplicate targets)
    fams = defaultdict(list)
    for r in records:
        root = dsu.find(r["number"])
        r["family_id"] = f"fam:{root}"
        fams[r["family_id"]].append(r["number"])
    has_dup = {r["family_id"] for r in records if r["duplicate_of"]}
    multi = {k: v for k, v in fams.items() if len(v) > 1 or k in has_dup}

    write_jsonl(C.PROCESSED / "issues.jsonl", records)
    write_json(C.PROCESSED / "issue_families.json", {"families_with_duplicates": multi,
                                                     "n_families": len(fams)})
    report = {"built_at": utcnow(), **dict(rep), "families": len(fams),
              "families_with_duplicates": len(multi),
              "issues_with_fixed_in": sum(1 for r in records if r["fixed_in"])}
    write_json(C.PROCESSED / "cleaning_report.json", report)
    return report


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    rep = build()
    log.info("cleaning report: %s", rep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
