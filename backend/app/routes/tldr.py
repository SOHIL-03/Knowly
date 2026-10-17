"""Public read-only TLDR newsletter endpoints, scraped from tldr.tech on demand (cached 15 min),
and the per-section interview podcast (generated once per section, then served from storage)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.deps import CurrentUser, optional_user, services
from app.errors import ApiError
from app.services import Services
from app.tldr import podcast
from app.tldr.scraper import CATEGORY_RE, DATE_RE, SECTION_RE, IssueNotFound, TldrError, scraper

router = APIRouter(prefix="/tldr")


def _call(fn, *args):
    try:
        return fn(*args)
    except IssueNotFound:
        raise ApiError(404, "not_found", "Issue not found")
    except TldrError as exc:
        raise ApiError(502, "tldr_unavailable", str(exc))


def _check(category: str, date: str, section: str) -> None:
    if not CATEGORY_RE.match(category) or not DATE_RE.match(date) or not SECTION_RE.match(section):
        raise ApiError(422, "invalid", "Invalid category, date or section")


@router.get("/latest")
def latest(category: str = Query(default="tech", max_length=30)):
    return _call(scraper.latest, category).to_dict()


@router.get("/issues")
def issues(category: str = Query(default="tech", max_length=30)):
    return {"category": category, "issues": _call(scraper.list_issues, category)}


@router.get("/issues/{date}")
def issue(date: str, category: str = Query(default="tech", max_length=30)):
    return _call(scraper.get_issue, date, category).to_dict()


class PodcastIn(BaseModel):
    category: str = Field(default="tech", max_length=30)
    date: str = Field(max_length=10)
    section: str = Field(max_length=50, description="Section id from the issue, e.g. big-tech-startups")
    user_preferences: str | None = Field(default=None, max_length=podcast.MAX_PREFERENCE_CHARS,
                                         description="Optional free text that personalizes the questions and answers")


@router.post("/podcast", status_code=202)
def start_podcast(body: PodcastIn, svc: Services = Depends(services), user: CurrentUser | None = Depends(optional_user)):
    """Starts the question-and-answer podcast for one section of an issue (all its stories). Poll GET /tldr/podcast.
    With user_preferences the episode is personalized: the response carries a `variant` to pass when polling. A signed-in
    caller (bearer token) with a verified learning profile also gets an episode shaped by it, private to them.
    The response always carries `podcastId`, the persistent id used by /podcast/{id}."""
    _check(body.category, body.date, body.section)
    if body.user_preferences is not None and not body.user_preferences.strip():
        raise ApiError(422, "invalid", "Describe your preference, or choose No for the standard podcast")
    return _call(podcast.request, svc, body.category, body.date, body.section, body.user_preferences, user.uid if user else None)


@router.get("/podcast")
def get_podcast(category: str = Query(default="tech", max_length=30), date: str = Query(max_length=10),
                section: str = Query(max_length=50), variant: str | None = Query(default=None, pattern=r"^[pu][0-9a-f]{10}$"),
                svc: Services = Depends(services), user: CurrentUser | None = Depends(optional_user)):
    _check(category, date, section)
    return podcast.view(svc, category, date, section, variant, user.uid if user else None)
