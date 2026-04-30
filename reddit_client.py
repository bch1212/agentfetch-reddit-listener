"""Reddit client (PRAW wrapper).

Async-from-sync — PRAW is sync-only, so we run calls in a thread pool. Returns
structured dataclasses; never raises (Reddit rate limits + outages are common).

The Community Listener uses this to:
- Pull recent posts from monitored subreddits
- Reply to a post or comment

We deliberately do NOT use this for posting top-level content (the Publisher
will get its own Reddit-posting path in a future phase). Listener-only here.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from salesbot.config import settings

logger = logging.getLogger(__name__)


@dataclass
class RedditPost:
    id: str
    title: str
    body: str
    author: str
    subreddit: str
    url: str
    permalink: str
    created_utc: datetime
    score: int
    num_comments: int
    is_self: bool


@dataclass
class ReplyResult:
    posted: bool
    permalink: Optional[str] = None
    error: Optional[str] = None


def _client():
    import praw

    missing = settings.required_for("reddit")
    if missing:
        raise RuntimeError(f"Reddit creds missing: {', '.join(missing)}")
    return praw.Reddit(
        client_id=settings.REDDIT_CLIENT_ID,
        client_secret=settings.REDDIT_CLIENT_SECRET,
        username=settings.REDDIT_USERNAME,
        password=settings.REDDIT_PASSWORD,
        user_agent=settings.REDDIT_USER_AGENT,
        check_for_async=False,
    )


async def fetch_recent_posts(
    subreddit: str,
    *,
    limit: int = 30,
    max_age_hours: int = 24,
) -> list[RedditPost]:
    """Pull recent posts from /r/{subreddit}. Returns [] on error."""

    def _sync() -> list[RedditPost]:
        try:
            r = _client()
            sub = r.subreddit(subreddit)
            now = datetime.now(timezone.utc)
            out: list[RedditPost] = []
            for post in sub.new(limit=limit):
                created = datetime.fromtimestamp(post.created_utc, tz=timezone.utc)
                if (now - created).total_seconds() > max_age_hours * 3600:
                    continue
                out.append(
                    RedditPost(
                        id=post.id,
                        title=str(post.title or ""),
                        body=str(post.selftext or ""),
                        author=str(post.author) if post.author else "[deleted]",
                        subreddit=subreddit,
                        url=str(post.url),
                        permalink=f"https://reddit.com{post.permalink}",
                        created_utc=created,
                        score=int(post.score or 0),
                        num_comments=int(post.num_comments or 0),
                        is_self=bool(post.is_self),
                    )
                )
            return out
        except Exception:  # noqa: BLE001
            logger.exception("Reddit fetch failed for r/%s", subreddit)
            return []

    return await asyncio.to_thread(_sync)


async def reply_to_post(post_id: str, body: str) -> ReplyResult:
    """Post a reply on a Reddit submission. Returns structured result."""

    def _sync() -> ReplyResult:
        try:
            r = _client()
            submission = r.submission(id=post_id)
            comment = submission.reply(body=body)
            permalink = (
                f"https://reddit.com{comment.permalink}"
                if comment is not None
                else None
            )
            return ReplyResult(posted=True, permalink=permalink)
        except Exception as e:  # noqa: BLE001
            logger.exception("Reddit reply failed for %s", post_id)
            return ReplyResult(posted=False, error=str(e))

    return await asyncio.to_thread(_sync)
