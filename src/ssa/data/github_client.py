"""Minimal GitHub REST client: auth, pagination, rate limits, retries.

* Token is read from GITHUB_TOKEN and is never printed or written anywhere.
* Primary rate limit: we watch X-RateLimit-Remaining / -Reset and sleep before
  running dry. If the wait would exceed `max_wait` we raise RateLimitExhausted,
  so the crawler stops cleanly and a re-run resumes from the cache.
* Secondary rate limit (403/429 + Retry-After): honoured with backoff.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Iterator

import requests

API = "https://api.github.com"
log = logging.getLogger(__name__)


class GitHubError(RuntimeError):
    pass


class NotFound(GitHubError):
    pass


class RateLimitExhausted(GitHubError):
    pass


class GitHubClient:
    def __init__(self, token: str | None = None, *, min_remaining: int = 3,
                 max_retries: int = 6, timeout: int = 30, max_wait: int = 900):
        token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self.authenticated = bool(token)
        self.min_remaining = min_remaining
        self.max_retries = max_retries
        self.timeout = timeout
        self.max_wait = max_wait
        self.request_count = 0
        self.last_rate: dict[str, str] = {}
        self._s = requests.Session()
        self._s.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "qbc13-streamlit-support-agent-crawler",
        })
        if token:
            self._s.headers["Authorization"] = f"Bearer {token}"

    # ------------------------------------------------------------------ core
    def _wait_until(self, reset_epoch: float, why: str) -> None:
        wait = max(0.0, reset_epoch - time.time()) + 2
        if wait > self.max_wait:
            raise RateLimitExhausted(
                f"{why}; reset in {wait/60:.0f} min (> max_wait). "
                "Progress is cached - re-run later to resume."
            )
        log.warning("%s - sleeping %.0fs", why, wait)
        time.sleep(wait)

    def get(self, url: str, params: dict | None = None) -> requests.Response:
        if not url.startswith("http"):
            url = API + url
        for attempt in range(self.max_retries):
            try:
                r = self._s.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as e:  # network blip
                log.warning("network error %s (attempt %d)", type(e).__name__, attempt + 1)
                time.sleep(2 ** attempt)
                continue
            self.request_count += 1
            rem = r.headers.get("X-RateLimit-Remaining")
            reset = r.headers.get("X-RateLimit-Reset")
            res = r.headers.get("X-RateLimit-Resource", "core")
            if rem is not None:
                self.last_rate[res] = rem

            if r.status_code == 200:
                if rem is not None and reset and int(rem) < self.min_remaining:
                    self._wait_until(int(reset), f"{res} rate limit low ({rem} left)")
                return r
            if r.status_code == 404:
                raise NotFound(url)
            if r.status_code in (403, 429):
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    time.sleep(int(retry_after) + 1)
                    continue
                if rem == "0" and reset:
                    self._wait_until(int(reset), f"{res} rate limit hit")
                    continue
                # secondary limit without headers -> exponential backoff
                time.sleep(min(60 * (attempt + 1), 300))
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            raise GitHubError(f"GET {url} -> HTTP {r.status_code}: {r.text[:200]}")
        raise GitHubError(f"GET {url}: retries exhausted")

    # ------------------------------------------------------------- helpers
    def paginate(self, url: str, params: dict | None = None,
                 max_items: int | None = None) -> Iterator[dict]:
        """Follow `Link: rel=next` headers. Yields items one by one."""
        params = {"per_page": 100, **(params or {})}
        n = 0
        while url:
            r = self.get(url, params=params)
            data = r.json()
            items = data["items"] if isinstance(data, dict) and "items" in data else data
            for it in items:
                yield it
                n += 1
                if max_items and n >= max_items:
                    return
            url = r.links.get("next", {}).get("url")
            params = None  # the next-URL already carries the query string

    def search_issues(self, query: str, max_items: int = 100) -> Iterator[dict]:
        """Search API (max 1000 results/query; 30 req/min with a token)."""
        for item in self.paginate("/search/issues", {"q": query}, max_items=max_items):
            yield item
        time.sleep(2.1 if self.authenticated else 6.1)  # stay under search limit
