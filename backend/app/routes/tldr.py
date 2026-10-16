"""Public read-only TLDR newsletter endpoints, scraped from tldr.tech on demand (cached 15 min),
and the per-story interview podcast (generated once per story, then served from storage)."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.deps import services
from app.errors import ApiError
from app.services import Services
from app.tldr import podcast
from app.tldr.scraper import CATEGORY_RE, DATE_RE, IssueNotFound, TldrError, scraper

STORY_RE = re.compile(r"^s\d{1,3}$")

router = APIRouter(prefix="/tldr")


def _call(fn, *args):
    try:
        return fn(*args)
    except IssueNotFound:
        raise ApiError(404, "not_found", "Issue not found")
    except TldrError as exc:
        raise ApiError(502, "tldr_unavailable", str(exc))


def _check(category: str, date: str, story: str) -> None:
    if not CATEGORY_RE.match(category) or not DATE_RE.match(date) or not STORY_RE.match(story):
        raise ApiError(422, "invalid", "Invalid category, date or story")


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
    story: str = Field(max_length=4, description="Story id from the issue, e.g. s3")


@router.post("/podcast", status_code=202)
def start_podcast(body: PodcastIn, svc: Services = Depends(services)):
    """Starts the question-and-answer podcast for one story of an issue. Poll GET /tldr/podcast for the result."""
    _check(body.category, body.date, body.story)
    return _call(podcast.request, svc, body.category, body.date, body.story)


@router.get("/podcast")
def get_podcast(category: str = Query(default="tech", max_length=30), date: str = Query(max_length=10),
                story: str = Query(max_length=4), svc: Services = Depends(services)):
    _check(category, date, story)
    return podcast.view(svc, category, date, story)
