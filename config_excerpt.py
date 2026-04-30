"""Configuration excerpt — Listener-relevant settings only.

Full config lives in the salesbot private repo. These are the knobs that
govern Listener behavior, included here for transparency to Reddit reviewers.
"""

# Standard PRAW credentials (loaded from env, never committed)
REDDIT_CLIENT_ID: str        # from reddit.com/prefs/apps script app
REDDIT_CLIENT_SECRET: str    # from reddit.com/prefs/apps script app
REDDIT_USERNAME: str         # the bot account
REDDIT_PASSWORD: str         # 2FA must be off (PRAW limitation)
REDDIT_USER_AGENT: str = "salesbot/1.0 by /u/<bot-username>"

# ----------------------------------------------------------------------------
# Compliance + safety knobs (Reddit Responsible Builder Policy)
# ----------------------------------------------------------------------------

# Hard kill switch — Listener fetches + drafts + logs but DOES NOT post
# replies until this is True. Default False so the system is compliant
# from the moment it boots, before Reddit's API access is approved.
REDDIT_BOT_REGISTERED: bool = False

# Auto-appended to every reply before posting (defensive — if the model
# omits it, the wrapper adds it). Per the "Be transparent" mandate.
LISTENER_AI_DISCLOSURE: str = "_(autonomous AI reply — feedback welcome)_"

# Warmup mode for fresh accounts. While True:
#   - replies contain ZERO product mentions and ZERO links
#   - daily caps drop to 2 total / 1 per subreddit
#   - quality threshold tightens to >= 9.5/10
LISTENER_WARMUP_MODE: bool = True

# Steady-state caps (used only when LISTENER_WARMUP_MODE is False)
MAX_REDDIT_POSTS_PER_DAY_PER_SUB: int = 2
