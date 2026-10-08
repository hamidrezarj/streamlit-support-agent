"""Snapshot the Streamlit docs repo at a pinned commit and parse it.

Run:   python -m ssa.data.crawl_docs              # uses data/raw/docs.lock if present
       python -m ssa.data.crawl_docs --sha <commit>
       python -m ssa.data.crawl_docs --update     # move the pin to current main

Outputs (data/processed/):
  docs_sections.jsonl   one record per heading section of every docs page
  config_options.jsonl  one record per config.toml option (server.baseUrlPath, ...)
  release_notes.jsonl   one record per release-note bullet, with linked issues/PRs
  versions.json         {version: release_date}

Notes
* The repo is cloned with a blob filter + sparse checkout (only *.md and the
  API JSON), ~50 MB instead of the full repo with images.
* Pages that use <Autofunction function="streamlit.x" /> have no prose in the
  markdown; we render it from python/streamlit.json (the docstrings that the
  site itself uses), for the newest Streamlit version present in that file.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import re
import subprocess
import sys
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

from . import config as C
from .io_utils import utcnow, write_json, write_jsonl

log = logging.getLogger("crawl_docs")

SPARSE = ["content/**/*.md", "/python/streamlit.json"]
RELEASE_DIR = "content/develop/quick-references/release-notes"
CONFIG_PAGE = "content/develop/api-reference/configuration/config-toml.md"


# ======================================================== git snapshot
def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def snapshot(sha: str | None, update: bool) -> str:
    repo = C.DOCS_REPO_DIR
    if not (repo / ".git").exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        log.info("cloning %s (sparse, blob-filtered)...", C.DOCS_REPO_URL)
        _git("clone", "--filter=blob:none", "--no-checkout", C.DOCS_REPO_URL, str(repo))
        _git("sparse-checkout", "set", "--no-cone", *SPARSE, cwd=repo)
    if sha is None and C.DOCS_LOCK.exists() and not update:
        sha = C.DOCS_LOCK.read_text().strip()
    if sha is None:
        _git("fetch", "origin", "main", cwd=repo)
        sha = _git("rev-parse", "origin/main", cwd=repo)
    else:
        try:
            _git("cat-file", "-e", f"{sha}^{{commit}}", cwd=repo)
        except subprocess.CalledProcessError:
            _git("fetch", "origin", sha, cwd=repo)
    _git("checkout", "--detach", sha, cwd=repo)
    C.DOCS_LOCK.write_text(sha + "\n")
    log.info("docs pinned at %s", sha)
    return sha


# ======================================================== text helpers
class _HTMLText(HTMLParser):
    """HTML -> plain text, keeping <pre> blocks as fenced code."""
    def __init__(self):
        super().__init__()
        self.out: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "div", "br", "ul", "ol"):
            self.out.append("\n")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "pre":
            self.out.append("\n```\n")
        elif tag in ("tt", "code"):
            self.out.append("`")

    def handle_endtag(self, tag):
        if tag == "pre":
            self.out.append("\n```\n")
        elif tag in ("tt", "code"):
            self.out.append("`")
        elif tag == "p":
            self.out.append("\n")

    def handle_data(self, data):
        self.out.append(data)


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    p = _HTMLText()
    p.feed(s)
    txt = html.unescape("".join(p.out))
    txt = re.sub(r"<[A-Z]\w*[^>]*/>", "", txt)          # leftover MDX like <Cloud .../>
    return re.sub(r"\n{3,}", "\n\n", txt).strip()


def slugify(h: str) -> str:
    h = re.sub(r"[`*_]", "", h.lower())
    h = re.sub(r"[^\w\s-]", "", h)
    return re.sub(r"\s+", "-", h.strip())


LINK = re.compile(r"!?\[([^\]]*)\]\(([^)]+)\)")


def md_inline_to_text(line: str) -> str:
    # [text](url) -> text ; images dropped
    return LINK.sub(lambda m: "" if m.group(0).startswith("!") else m.group(1), line)


def split_frontmatter(raw: str) -> tuple[dict, str]:
    meta: dict[str, str] = {}
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            for line in raw[3:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip('"')
            raw = raw[end + 4:]
    return meta, raw


# ======================================================== Autofunction
class ApiDocs:
    def __init__(self, path: Path):
        data = json.loads(path.read_text())
        self.version = max(data, key=lambda v: tuple(int(x) for x in re.findall(r"\d+", v)))
        self.entries = data[self.version]

    def render(self, name: str) -> str | None:
        e = self.entries.get(name) or self.entries.get(f"streamlit.{name}")
        if not e:
            return None
        parts = [f"`{e.get('signature', name)}`", html_to_text(e.get("description"))]
        args = e.get("args") or []
        if args:
            parts.append("Parameters:")
            for a in args:
                t = a.get("type_name") or ""
                d = html_to_text(a.get("description")).replace("\n", " ")
                parts.append(f"- {a.get('name')} ({t}): {d}")
        for r in e.get("returns") or []:
            parts.append(f"Returns ({r.get('type_name', '')}): "
                         f"{html_to_text(r.get('description')).replace(chr(10), ' ')}")
        return "\n".join(p for p in parts if p)


AUTOFN = re.compile(r'<Autofunction\s+function="([^"]+)"[^>]*/>')
MDX_OPEN = re.compile(r"^\s*<(Tip|Note|Warning|Important|Info)\b[^>]*>\s*$")
MDX_TAG = re.compile(r"</?[A-Z][\w.]*(\s[^<>]*)?/?>")   # any remaining JSX-ish tag


def clean_mdx(body: str, api: ApiDocs, stats: dict) -> str:
    """Strip MDX components outside code fences; inline Autofunction docs."""
    out, in_code = [], False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
            out.append(line)
            continue
        if in_code:
            out.append(line)
            continue
        m = AUTOFN.search(line)
        if m:
            rendered = api.render(m.group(1))
            stats["autofunction_total"] += 1
            if rendered:
                out.append(rendered)
            else:
                stats["autofunction_missing"].append(m.group(1))
            continue
        m = MDX_OPEN.match(line)
        if m:
            out.append(f"{m.group(1)}:")
            continue
        line = MDX_TAG.sub("", line)
        out.append(md_inline_to_text(line))
    return "\n".join(out)


# ======================================================== sections
HEADING = re.compile(r"^(#{1,4})\s+(.*)$")


def split_sections(text: str) -> list[tuple[list[str], str, str]]:
    """Return [(heading_path, anchor, body)] split on #..#### outside code."""
    sections, path, anchor, buf, in_code = [], [], "", [], False

    def flush():
        body = "\n".join(buf).strip()
        if len(body) >= 40:
            sections.append((list(path), anchor, re.sub(r"\n{3,}", "\n\n", body)))

    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
        m = None if in_code else HEADING.match(line)
        if m:
            flush()
            buf = []
            level, title = len(m.group(1)), m.group(2).replace("**", "").strip()
            path = path[: level - 1] + [title]
            anchor = slugify(title)
        else:
            buf.append(line)
    flush()
    return sections


def parse_docs(repo: Path, sha: str, api: ApiDocs) -> tuple[list[dict], dict]:
    rows, stats = [], {"pages": 0, "autofunction_total": 0, "autofunction_missing": []}
    for md in sorted((repo / "content").rglob("*.md")):
        rel = md.relative_to(repo).as_posix()
        if rel.startswith(RELEASE_DIR) or rel == CONFIG_PAGE:
            continue  # parsed by dedicated functions
        meta, body = split_frontmatter(md.read_text(encoding="utf-8"))
        if not meta.get("slug"):
            continue
        stats["pages"] += 1
        body = clean_mdx(body, api, stats)
        page_url = C.DOCS_SITE + meta["slug"]
        for i, (hpath, anchor, text) in enumerate(split_sections(body)):
            rows.append({
                "id": f"doc:{meta['slug']}#{anchor or i}",
                "type": "doc",
                "title": meta.get("title", ""),
                "heading_path": hpath,
                "url": page_url + (f"#{anchor}" if anchor else ""),
                "source_path": rel,
                "area": rel.split("/")[1] if rel.count("/") > 1 else "",
                "text": text,
                "docs_commit": sha,
                "api_version": api.version,
            })
    return rows, stats


# ======================================================== config.toml
def parse_config_options(repo: Path, sha: str) -> list[dict]:
    meta, body = split_frontmatter((repo / CONFIG_PAGE).read_text(encoding="utf-8"))
    url = C.DOCS_SITE + meta.get("slug", "/develop/api-reference/configuration/config.toml")
    rows, section, comment, in_toml = [], None, [], False
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("```"):
            in_toml = s.startswith("```toml") and not in_toml
            comment = []
            continue
        if not in_toml:
            continue
        if re.match(r"^\[[\w.]+\]$", s):
            section, comment = s[1:-1], []
        elif s.startswith("#"):
            comment.append(s.lstrip("#").strip())
        elif "=" in s and section:
            key, default = (x.strip() for x in s.split("=", 1))
            desc = "\n".join(comment).strip()
            env_var = f"STREAMLIT_{section.upper()}_" + re.sub(r"([A-Z])", r"_\1", key).upper()
            rows.append({
                "id": f"config:{section}.{key}",
                "type": "config_option",
                "title": f"{section}.{key}",
                "heading_path": ["config.toml", section, key],
                "url": url + f"#{section}",
                "source_path": CONFIG_PAGE,
                "text": f"[{section}] {key} = {default}\n{desc}\n"
                        f"Env var: {env_var}\n"
                        f"CLI flag: --{section}.{key}",
                "default": default,
                "docs_commit": sha,
            })
            comment = []
        elif not s:
            pass
    return rows


# ======================================================== release notes
VERSION_H = re.compile(r"^##\s+\**\s*Version\s+([\d.]+[\w.]*)", re.I)
DATE_L = re.compile(r"^_?Release date:\s*([^_]+)_?\s*$", re.I)
CAT_L = re.compile(r"^\*\*([^*]+?):?\*\*:?\s*$")
ISSUE_RE = re.compile(r"github\.com/streamlit/streamlit/issues/(\d+)")
PR_RE = re.compile(r"github\.com/streamlit/streamlit/pull/(\d+)")


def _parse_date(s: str) -> str | None:
    s = re.sub(r"(\d)(st|nd|rd|th)", r"\1", s.strip())
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def parse_release_notes(repo: Path, sha: str) -> tuple[list[dict], dict]:
    rows, versions, seen = [], {}, set()
    files = sorted((repo / RELEASE_DIR).glob("*.md"))
    for f in files:
        meta, body = split_frontmatter(f.read_text(encoding="utf-8"))
        page_url = C.DOCS_SITE + meta.get("slug", "")
        version = date = None
        category = "Changes"
        bullets: list[list[str]] = []

        def flush_bullets():
            for b in bullets:
                raw = " ".join(x.strip() for x in b)
                text = md_inline_to_text(raw).lstrip("- ").strip()
                key = (version, text)
                if not version or key in seen:
                    continue
                seen.add(key)
                is_fix = "fix" in category.lower() or bool(re.match(r"^\W*\s*Bug fix", text, re.I))
                rows.append({
                    # content-based id: stable if bullets are added/reordered later
                    "id": f"rn:{version}:{hashlib.sha1(text.encode()).hexdigest()[:8]}",
                    "type": "release_note",
                    "version": version,
                    "release_date": date,
                    "category": "Bug fix" if is_fix else category,
                    "title": f"Streamlit {version} - {category}",
                    "url": f"{page_url}#{slugify('Version ' + version)}",
                    "source_path": f.relative_to(repo).as_posix(),
                    "text": text,
                    "linked_issues": sorted({int(x) for x in ISSUE_RE.findall(raw)}),
                    "linked_prs": sorted({int(x) for x in PR_RE.findall(raw)}),
                    "docs_commit": sha,
                })
            bullets.clear()

        for line in body.splitlines():
            if m := VERSION_H.match(line):
                flush_bullets()
                version, date, category = m.group(1).rstrip("."), None, "Changes"
                continue
            if version is None:
                continue
            if line.startswith("## "):          # e.g. "## Upgrade Streamlit"
                flush_bullets()
                version = None
                continue
            if (m := DATE_L.match(line.strip())) and not date:
                date = _parse_date(m.group(1))
                versions[version] = date
                continue
            if m := CAT_L.match(line.strip()):
                flush_bullets()
                category = m.group(1).strip()
                continue
            if re.match(r"^[-*]\s+", line):        # top-level bullet
                bullets.append([line])
            elif re.match(r"^\s+[-*]\s+", line) and bullets:   # nested -> attach to parent
                bullets[-1].append(line)
            elif line.strip() and bullets and not line.startswith(("#", "<")):
                bullets[-1].append(line)          # wrapped continuation
        flush_bullets()
    return rows, versions


# ======================================================== main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sha", help="docs commit to pin (default: docs.lock or origin/main)")
    ap.add_argument("--update", action="store_true", help="ignore docs.lock, pin current main")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    sha = snapshot(args.sha, args.update)
    repo = C.DOCS_REPO_DIR
    api = ApiDocs(repo / "python/streamlit.json")

    docs, stats = parse_docs(repo, sha, api)
    cfg = parse_config_options(repo, sha)
    rn, versions = parse_release_notes(repo, sha)

    n_docs = write_jsonl(C.PROCESSED / "docs_sections.jsonl", docs)
    n_cfg = write_jsonl(C.PROCESSED / "config_options.jsonl", cfg)
    n_rn = write_jsonl(C.PROCESSED / "release_notes.jsonl", rn)
    write_json(C.PROCESSED / "versions.json", dict(sorted(
        versions.items(), key=lambda kv: [int(x) for x in re.findall(r"\d+", kv[0])])))
    write_json(C.RAW / "docs_crawl_log.json", {
        "docs_repo": C.DOCS_REPO_URL, "docs_commit": sha, "parsed_at": utcnow(),
        "api_json_version": api.version, "pages": stats["pages"],
        "sections": n_docs, "config_options": n_cfg, "release_note_bullets": n_rn,
        "release_versions": len(versions),
        "autofunction_rendered": stats["autofunction_total"] - len(stats["autofunction_missing"]),
        "autofunction_missing": sorted(set(stats["autofunction_missing"])),
    })
    log.info("docs: %d pages -> %d sections | config options: %d | release notes: %d bullets / %d versions",
             stats["pages"], n_docs, n_cfg, n_rn, len(versions))
    if stats["autofunction_missing"]:
        log.warning("unresolved Autofunction refs: %d (see docs_crawl_log.json)",
                    len(set(stats["autofunction_missing"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
