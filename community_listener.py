"""Community Listener — reactive replies in dev communities.

Runs every 2 hours. AgentFetch-only.

Pipeline per run:
1. Walk monitored subreddits (from products.py:AgentFetch.social.reddit_subreddits).
2. Pull recent posts (≤24h old, not yet seen — dedup via community_threads).
3. Per-post relevance scan (Claude classifier): is the OP genuinely asking
   something AgentFetch could answer?
4. If yes: draft a value-first reply (helpful first, mentions AgentFetch only
   when it's the actual answer).
5. Self-critique pass on the draft (Claude rates 1-10 for "would this annoy
   the community?"). Posts only at score ≥ MIN_QUALITY_TO_POST (default 9).
6. Rate limits enforced before posting:
   - settings.MAX_REDDIT_POSTS_PER_DAY_PER_SUB per subreddit
   - GLOBAL_DAILY_REPLY_CAP across all subs
7. Log every decision (post / skip) to community_threads with reasoning.

By design, ZERO Discord-approval gating. Quality threshold + rate limits are
the safety mechanism. If it's borderline, skip.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from salesbot.config import settings
from salesbot.products import get_product
from salesbot.shared import db, discord_client, reddit_client
from salesbot.shared.anthropic_loop import generate_json

logger = logging.getLogger(__name__)


MIN_QUALITY_TO_POST = 9        # 0-10 self-critique scale (steady-state)
MIN_RELEVANCE_TO_DRAFT = 7     # 0-10 relevance to AgentFetch (steady-state)
GLOBAL_DAILY_REPLY_CAP = 5     # across all subreddits combined (steady-state)

# Warmup-mode overrides — used when settings.LISTENER_WARMUP_MODE is True.
# Built for fresh Reddit accounts that would get shadow-banned for posting any
# product link. Replies are pure helpful-only (no AgentFetch mention), and
# volumes are much lower so the account looks like a real human catching up.
WARMUP_MIN_QUALITY = 9.5
WARMUP_MIN_RELEVANCE = 6        # cast a wider net for "questions where I can be useful"
WARMUP_GLOBAL_CAP = 2
WARMUP_PER_SUB_CAP = 1


# =============================================================================
# Prompts
# =============================================================================

RELEVANCE_SYSTEM = """You are a strict triage classifier for a developer-tools company. Given a Reddit post, you decide whether AgentFetch (an MCP-native web fetch layer for AI agents) is GENUINELY the right answer to what the OP is asking.

You return JSON. You score on a 0–10 scale where:
- 0–3: not asking about web fetching / token costs / MCP / agent infra at all
- 4–6: tangentially related but AgentFetch isn't the best answer
- 7–8: asking exactly the kind of question AgentFetch solves
- 9–10: ICP-perfect; the OP is essentially describing the problem AgentFetch solves

You are conservative. When in doubt, score low. The cost of a false positive (an irrelevant reply) is much higher than the cost of a false negative (a missed opportunity).
"""

REPLY_SYSTEM = """You write helpful, concrete Reddit replies as Brett — a builder who shipped AgentFetch (an open-source MCP server for AI agent web fetching). Your replies sound like a useful peer, not a salesperson.

Hard rules:
- 80–180 words. No essays, no bullet-spam.
- Open with a SPECIFIC, concrete answer to the OP's question. Don't restate their problem.
- Mention AgentFetch ONLY if it's actually the answer. If a more direct solution exists, give that and skip the product mention.
- If you do mention AgentFetch, link the GitHub repo (github.com/bch1212/agentfetch-mcp) once, never the hosted product page on first contact. Open source first, commercial second.
- Disclose ownership: include "(disclosure: I built it)" right after the first AgentFetch mention.
- No emoji-decorated openings. No "Great question!". No "Hope this helps!".
- Plain Reddit Markdown only.
- Sign off with nothing.
"""

# Warmup-mode reply prompt — strict no-product-mention. The point is to build
# karma + presence, not to drive signups. AgentFetch is invisible.
WARMUP_REPLY_SYSTEM = """You write short, helpful Reddit replies as someone building AI agent infrastructure. You are warming up a new Reddit account by being purely useful — no product mentions, no links, no self-promotion of any kind.

Hard rules:
- 60–140 words. Tight. Specific.
- Open with a direct, concrete answer to the OP's question.
- DO NOT mention AgentFetch, agentfetch.dev, or github.com/bch1212/anything. Not once. Not in passing. Not as "I've worked on something similar." Nothing.
- DO NOT include any URLs to your own projects or domains.
- General-purpose links to public docs (Anthropic, LangChain, Wikipedia, official package READMEs) are fine and encouraged.
- No "Great question!" / "Hope this helps!". No emoji-decorated openings.
- Plain Reddit Markdown only.
- Sign off with nothing.

If the only useful answer would require mentioning your own work, return an empty reply — better to skip than to violate this rule.
"""

CRITIQUE_SYSTEM = """You are a brutal community-quality critic. You read a Reddit reply that's about to be posted and rate it 0–10 on whether it would ANNOY the community.

10 = genuinely helpful, would get upvoted; 0 = obvious spam.

You penalize HARD for:
- Generic "let me solve your problem" framing
- Insufficient specificity (no concrete example, no code, no command)
- Missing ownership disclosure when promoting one's own tool
- Reading like an LLM
- Restating the question before answering
- Linking the hosted commercial site instead of the open-source repo on first contact
- Sales-y language ("solution", "leverage", "robust", "in today's fast-paced world")
- More than one product link
- Tone mismatched to the subreddit

You return JSON. You explain in one sentence (≤30 words) what drove the score.
"""


# =============================================================================
# Public API
# =============================================================================

async def run() -> dict[str, Any]:
    run_id = await db.record_agent_run_start("community_listener")
    summary: dict[str, Any] = {
        "subs_scanned": 0,
        "posts_seen": 0,
        "drafted": 0,
        "posted": 0,
        "skipped_low_relevance": 0,
        "skipped_low_quality": 0,
        "skipped_rate_limit": 0,
        "errors": [],
    }

    try:
        if settings.required_for("reddit"):
            summary["errors"].append("Reddit creds not configured")
            await db.record_agent_run_end(
                run_id, status="error", error="reddit unconfigured", summary=summary
            )
            return summary
        if not settings.ANTHROPIC_API_KEY:
            summary["errors"].append("ANTHROPIC_API_KEY not set")
            await db.record_agent_run_end(
                run_id, status="error", error="anthropic unconfigured", summary=summary
            )
            return summary

        product = get_product("agentfetch")
        warmup = settings.LISTENER_WARMUP_MODE
        # Pick mode-appropriate thresholds and caps
        min_quality = WARMUP_MIN_QUALITY if warmup else MIN_QUALITY_TO_POST
        min_relevance = WARMUP_MIN_RELEVANCE if warmup else MIN_RELEVANCE_TO_DRAFT
        global_cap = WARMUP_GLOBAL_CAP if warmup else GLOBAL_DAILY_REPLY_CAP
        per_sub_cap = WARMUP_PER_SUB_CAP if warmup else settings.MAX_REDDIT_POSTS_PER_DAY_PER_SUB
        summary["mode"] = "warmup" if warmup else "steady"

        replied_today_global = await _replies_today_total()
        replies_per_sub = await _replies_today_per_sub()

        for subreddit in product.social.reddit_subreddits:
            summary["subs_scanned"] += 1
            posts = await reddit_client.fetch_recent_posts(subreddit, limit=30)
            summary["posts_seen"] += len(posts)

            for post in posts:
                # Dedupe — already considered?
                if await _seen(post.id):
                    continue

                # Rate limits
                if replied_today_global >= global_cap:
                    summary["skipped_rate_limit"] += 1
                    await _log_decision(
                        post,
                        decision="skipped_rate_limit",
                        reasoning=f"global daily cap hit ({global_cap})",
                    )
                    continue
                sub_count = replies_per_sub.get(subreddit, 0)
                if sub_count >= per_sub_cap:
                    summary["skipped_rate_limit"] += 1
                    await _log_decision(
                        post,
                        decision="skipped_rate_limit",
                        reasoning=f"per-sub cap hit for r/{subreddit} ({per_sub_cap})",
                    )
                    continue

                relevance = await _score_relevance(post)
                if relevance["score"] < min_relevance:
                    summary["skipped_low_relevance"] += 1
                    await _log_decision(
                        post,
                        decision="skipped_irrelevant",
                        reasoning=f"relevance={relevance['score']}: {relevance['why']}",
                    )
                    continue

                draft = await _draft_reply(post, relevance, warmup=warmup)
                if not draft:
                    # Warmup mode returned empty — model decided product mention was needed
                    summary["skipped_low_relevance"] += 1
                    await _log_decision(
                        post,
                        decision="skipped_irrelevant",
                        reasoning="warmup-mode model declined to answer without product mention",
                    )
                    continue
                summary["drafted"] += 1
                quality = await _score_quality(post, draft)

                if quality["score"] < min_quality:
                    summary["skipped_low_quality"] += 1
                    await _log_decision(
                        post,
                        decision="skipped_low_quality",
                        reasoning=f"quality={quality['score']}: {quality['why']}",
                        reply_text=draft,
                        quality_score=quality["score"],
                    )
                    continue

                # Auto-append AI disclosure (Responsible Builder Policy compliance)
                final_reply = _with_disclosure(draft)

                # Compliance gate — only post if the Reddit account is registered
                # as a bot per developers.reddit.com/app-registration.
                if not settings.REDDIT_BOT_REGISTERED:
                    summary["skipped_low_quality"] += 0  # don't double-count
                    await _log_decision(
                        post,
                        decision="skipped_unregistered",
                        reasoning=(
                            "REDDIT_BOT_REGISTERED=False — drafting for observation only. "
                            "Register the bot at developers.reddit.com/app-registration before enabling."
                        ),
                        reply_text=final_reply,
                        quality_score=quality["score"],
                    )
                    continue

                # Post it
                result = await reddit_client.reply_to_post(post.id, final_reply)
                if not result.posted:
                    await _log_decision(
                        post,
                        decision="error",
                        reasoning=f"reddit reply failed: {result.error}",
                        reply_text=final_reply,
                        quality_score=quality["score"],
                    )
                    continue

                summary["posted"] += 1
                replied_today_global += 1
                replies_per_sub[subreddit] = sub_count + 1
                await _log_decision(
                    post,
                    decision="replied",
                    reasoning=f"posted; quality={quality['score']}",
                    reply_text=final_reply,
                    quality_score=quality["score"],
                    reply_url=result.permalink,
                )

        await discord_client.post_agent_summary(
            "community_listener",
            title=f"Community sweep ({summary.get('mode', 'steady')})",
            fields={
                "Mode": summary.get("mode", "steady"),
                "Subs scanned": summary["subs_scanned"],
                "Posts seen": summary["posts_seen"],
                "Drafted": summary["drafted"],
                "Posted": summary["posted"],
                "Skipped (low relevance)": summary["skipped_low_relevance"],
                "Skipped (low quality)": summary["skipped_low_quality"],
                "Skipped (rate limit)": summary["skipped_rate_limit"],
            },
            success=not summary["errors"],
            error="\n".join(summary["errors"]) if summary["errors"] else None,
        )
        await db.record_agent_run_end(run_id, status="success", summary=summary)
        return summary
    except Exception as e:
        logger.exception("community_listener run failed")
        await db.record_agent_run_end(run_id, status="error", error=str(e), summary=summary)
        raise


# =============================================================================
# Scoring + drafting
# =============================================================================

async def _score_relevance(post: reddit_client.RedditPost) -> dict:
    schema_hint = '{"score": int (0-10), "why": str (≤30 words)}'
    user = f"""Subreddit: r/{post.subreddit}
Title: {post.title}
Body:
\"\"\"
{post.body[:3000]}
\"\"\"

How likely is AgentFetch (MCP-native web fetch for AI agents — token estimation,
smart caching, auto-routing across Trafilatura/Jina/FireCrawl/pypdf) the right
answer here? Return JSON only.
"""
    out = await generate_json(
        system_prompt=RELEVANCE_SYSTEM,
        user_prompt=user,
        schema_hint=schema_hint,
        max_tokens=200,
        temperature=0.0,
    )
    out = out if isinstance(out, dict) else {}
    return {
        "score": int(out.get("score", 0)),
        "why": str(out.get("why", ""))[:300],
    }


async def _draft_reply(
    post: reddit_client.RedditPost, relevance: dict, *, warmup: bool = False
) -> str:
    schema_hint = '{"reply": str (plain Reddit Markdown; "" if warmup-mode would require product mention)}'
    user = f"""Subreddit: r/{post.subreddit}
Title: {post.title}
Body:
\"\"\"
{post.body[:3000]}
\"\"\"

Relevance reasoning (from triage): {relevance['why']}

Write a reply per the rules in the system prompt. Be specific. Lead with the
direct answer. Return JSON only.
"""
    out = await generate_json(
        system_prompt=WARMUP_REPLY_SYSTEM if warmup else REPLY_SYSTEM,
        user_prompt=user,
        schema_hint=schema_hint,
        max_tokens=1000,
        temperature=0.5,
    )
    out = out if isinstance(out, dict) else {}
    reply = (out.get("reply") or "").strip()
    # Defensive: in warmup mode, strip any reply that slipped a product mention through
    if warmup and reply and _mentions_product(reply):
        logger.warning("warmup-mode draft contained product mention — discarding")
        return ""
    return reply


_FORBIDDEN_IN_WARMUP = (
    "agentfetch",
    "agentfetch.dev",
    "github.com/bch1212",
)


def _mentions_product(text: str) -> bool:
    lower = text.lower()
    return any(t in lower for t in _FORBIDDEN_IN_WARMUP)


def _with_disclosure(reply: str) -> str:
    """Append the AI-disclosure footer if it's not already present.

    Per the Responsible Builder Policy's "Be transparent" mandate. The footer
    is non-optional — if Claude omits it, this guard adds it before posting.
    """
    disclosure = settings.LISTENER_AI_DISCLOSURE.strip()
    if not disclosure:
        return reply
    # Already disclosed in some form?
    lower = reply.lower()
    if any(token in lower for token in ("autonomous ai", "ai-generated", "ai generated", "ai-drafted", "ai assistant", "/u/" )):
        return reply
    return reply.rstrip() + "\n\n" + disclosure


async def _score_quality(post: reddit_client.RedditPost, draft: str) -> dict:
    schema_hint = '{"score": int (0-10), "why": str (≤30 words)}'
    user = f"""Subreddit: r/{post.subreddit}
Original post:
\"\"\"
{post.title}

{post.body[:1500]}
\"\"\"

Reply about to be posted:
\"\"\"
{draft}
\"\"\"

Rate this reply 0–10. Be brutal. Return JSON only.
"""
    out = await generate_json(
        system_prompt=CRITIQUE_SYSTEM,
        user_prompt=user,
        schema_hint=schema_hint,
        max_tokens=200,
        temperature=0.1,
    )
    out = out if isinstance(out, dict) else {}
    return {
        "score": int(out.get("score", 0)),
        "why": str(out.get("why", ""))[:300],
    }


# =============================================================================
# DB helpers
# =============================================================================

async def _seen(thread_id: str) -> bool:
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM community_threads WHERE platform='reddit' AND thread_id=$1 LIMIT 1",
            thread_id,
        )
    return bool(row)


async def _log_decision(
    post: reddit_client.RedditPost,
    *,
    decision: str,
    reasoning: str,
    reply_text: Optional[str] = None,
    quality_score: Optional[float] = None,
    reply_url: Optional[str] = None,
) -> None:
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO community_threads
                (platform, thread_id, sub_or_account, title, url, decision,
                 quality_score, reply_text, reply_url, reasoning)
            VALUES ('reddit', $1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (platform, thread_id, parent_id) DO NOTHING
            """,
            post.id,
            post.subreddit,
            post.title,
            post.permalink,
            decision,
            quality_score,
            reply_text,
            reply_url,
            reasoning,
        )


async def _replies_today_total() -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    async with db.acquire() as conn:
        return (
            await conn.fetchval(
                """
                SELECT count(*) FROM community_threads
                WHERE platform='reddit' AND decision='replied' AND seen_at >= $1
                """,
                cutoff,
            )
            or 0
        )


async def _replies_today_per_sub() -> dict[str, int]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT sub_or_account, count(*) AS n
            FROM community_threads
            WHERE platform='reddit' AND decision='replied' AND seen_at >= $1
            GROUP BY sub_or_account
            """,
            cutoff,
        )
    return {r["sub_or_account"]: int(r["n"]) for r in rows}


# =============================================================================
# CLI / cron entry
# =============================================================================

async def _main() -> None:
    logging.basicConfig(level=settings.LOG_LEVEL)
    await db.init_db()
    summary = await run()
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(_main())
