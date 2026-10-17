"""Fetch and parse TLDR newsletter issues.

Everything comes from the public, server-rendered HTML on tldr.tech: the archive page lists issue
URLs (JSON-LD ItemList) and each issue page holds the date, headline, sections and stories.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

BASE = "https://tldr.tech"
CATEGORY_RE = re.compile(r"^[a-z0-9-]{1,30}$")
SECTION_RE = re.compile(r"^[a-z0-9-]{1,50}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
READ_TIME_RE = re.compile(r"\s*\((\d+)\s+minute read\)\s*$", re.I)
SPONSOR_RE = re.compile(r"\s*\((?:sponsor|sponsored)\)\s*$", re.I)
CACHE_TTL = 900  # seconds
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; KnowlyBot/1.0)", "Accept": "text/html"}


class TldrError(Exception):
    pass


class IssueNotFound(TldrError):
    pass


@dataclass
class Story:
    id: str  # "s1", "s2"... in page order (sponsored stories count), stable for a given issue
    title: str
    url: str
    summary: str
    read_minutes: int | None = None
    sponsored: bool = False


@dataclass
class Section:
    name: str
    emoji: str
    stories: list[Story] = field(default_factory=list)
    id: str = ""  # stable slug of the name, unique within the issue ("big-tech-startups")

    @property
    def key(self) -> str:
        return self.id or slugify(self.name)


def slugify(name: str) -> str:
    """"Programming, Design & Data Science" -> "programming-design-data-science"; the browser uses the same rule."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50].strip("-") or "section"


@dataclass
class Issue:
    category: str
    date: str
    url: str
    headline: str
    sections: list[Section] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _clean_url(url: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not (k == "utm_source" and v == "tldrnewsletter")]
    return urlunsplit(parts._replace(query=urlencode(query)))


def parse_archive(html: str, category: str) -> list[tuple[str, str]]:
    """Return (date, url) for every issue on an archive page, newest first."""
    pattern = re.compile(rf"^{re.escape(BASE)}/{re.escape(category)}/(\d{{4}}-\d{{2}}-\d{{2}})$")
    found: dict[str, str] = {}
    for tag in BeautifulSoup(html, "html.parser").find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except ValueError:
            continue
        # The ItemList sits under "mainEntity" on the archive's CollectionPage.
        node = data.get("mainEntity", data) if isinstance(data, dict) else {}
        for el in node.get("itemListElement", []) if isinstance(node, dict) else []:
            m = pattern.match(el.get("url", ""))
            if m:
                found[m.group(1)] = el["url"]
    return sorted(found.items(), reverse=True)


def parse_issue(html: str, category: str, date: str, url: str) -> Issue:
    soup = BeautifulSoup(html, "html.parser")
    h2 = soup.find("h2")
    issue = Issue(category=category, date=date, url=url, headline=h2.get_text(" ", strip=True) if h2 else "")
    count = 0
    for sec in soup.find_all("section"):
        header = sec.find("header")
        name_el = header.find("h3") if header else None
        emoji_el = header.find("div") if header else None
        section = Section(
            name=name_el.get_text(" ", strip=True) if name_el else "",
            emoji=emoji_el.get_text(strip=True) if emoji_el else "",
        )
        for art in sec.find_all("article"):
            link = art.find("a", href=True)
            h3 = art.find("h3")
            if not link or not h3:
                continue
            title = h3.get_text(" ", strip=True)
            sponsored = bool(SPONSOR_RE.search(title))
            title = SPONSOR_RE.sub("", title)
            m = READ_TIME_RE.search(title)
            body = art.find("div", class_="newsletter-html")
            count += 1
            section.stories.append(Story(
                id=f"s{count}",
                title=READ_TIME_RE.sub("", title),
                url=_clean_url(link["href"]),
                summary=body.get_text(" ", strip=True) if body else "",
                read_minutes=int(m.group(1)) if m else None,
                sponsored=sponsored,
            ))
        if section.stories:
            base = slugify(section.name)
            used = {x.id for x in issue.sections}
            section.id = base if base not in used else next(f"{base}-{n}" for n in range(2, 99) if f"{base}-{n}" not in used)
            issue.sections.append(section)
    if not issue.sections:
        raise TldrError(f"No stories found on {url}; the page layout may have changed")
    return issue


class TldrScraper:
    def __init__(self, client: httpx.Client | None = None, ttl: int = CACHE_TTL):
        self._client = client or httpx.Client(headers=HEADERS, timeout=15, follow_redirects=True)
        self._ttl = ttl
        self._cache: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def _get(self, path: str) -> str:
        with self._lock:
            hit = self._cache.get(path)
            if hit and time.monotonic() - hit[0] < self._ttl:
                return hit[1]
        try:
            r = self._client.get(BASE + path)
        except httpx.HTTPError as exc:
            raise TldrError(f"Could not reach tldr.tech: {type(exc).__name__}") from exc
        if r.status_code == 404:
            raise IssueNotFound(path)
        if r.status_code != 200:
            raise TldrError(f"tldr.tech returned {r.status_code} for {path}")
        with self._lock:
            self._cache[path] = (time.monotonic(), r.text)
        return r.text

    @staticmethod
    def _check(category: str) -> None:
        if not CATEGORY_RE.match(category):
            raise TldrError("Invalid newsletter category")

    def list_issues(self, category: str = "tech") -> list[dict]:
        self._check(category)
        return [{"date": d, "url": u} for d, u in parse_archive(self._get(f"/{category}/archives"), category)]

    def get_issue(self, date: str, category: str = "tech") -> Issue:
        self._check(category)
        if not DATE_RE.match(date):
            raise TldrError("Date must be YYYY-MM-DD")
        return parse_issue(self._get(f"/{category}/{date}"), category, date, f"{BASE}/{category}/{date}")

    def latest(self, category: str = "tech") -> Issue:
        """Newest published issue; skips dates that are listed but not yet readable."""
        for item in self.list_issues(category):
            try:
                return self.get_issue(item["date"], category)
            except IssueNotFound:
                continue
        raise IssueNotFound(f"No issues found for {category}")


scraper = TldrScraper()


if __name__ == "__main__":
    import sys

    print(json.dumps(scraper.latest(sys.argv[1] if len(sys.argv) > 1 else "tech").to_dict(), indent=2, ensure_ascii=False))
