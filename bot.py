"""
Hive CommentRewarder bot.

Scans the newest posts, picks the ones (<= 24h old) that list
`commentrewarder` as a beneficiary, upvotes them and leaves a short,
post-specific comment. The same author is skipped for 3 days.

DRY_RUN=true (default) simulates everything: nothing is voted or posted.
"""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

API = "https://api.hive.blog"
BENEFICIARY = "commentrewarder"
MAX_POST_AGE = timedelta(hours=24)
AUTHOR_COOLDOWN = timedelta(days=3)
HISTORY_FILE = Path(os.getenv("HISTORY_FILE", "history.json"))

DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"
ACCOUNT = os.getenv("HIVE_ACCOUNT", "")
POSTING_KEY = os.getenv("HIVE_POSTING_KEY", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
VOTE_WEIGHT = int(os.getenv("VOTE_WEIGHT", "20"))      # percent
MAX_PER_RUN = int(os.getenv("MAX_PER_RUN", "3"))
SCAN_LIMIT = int(os.getenv("SCAN_LIMIT", "50"))

SYSTEM_PROMPT = """You write short comments on Hive blog posts.
Rules:
- Write in English, in a warm and friendly tone.
- 2 to 3 short sentences at most.
- Address the author directly and mention something specific from the post.
- Never use em dashes or en dashes.
- Never use the Oxford comma.
- No hashtags, no links, no emojis, no generic praise like "great post".
Return only the comment text."""


# ---------- helpers ----------
def rpc(method, params):
    r = requests.post(
        API,
        json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data["result"]


def parse_time(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def load_history():
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return {}


def save_history(h):
    HISTORY_FILE.write_text(json.dumps(h, indent=2))


def clean_comment(text):
    text = text.replace("\u2014", ", ").replace("\u2013", ", ").replace(" - ", ", ")
    text = re.sub(r"\s+", " ", text).strip().strip('"')
    return text


# ---------- scanning ----------
def fetch_recent_posts():
    posts = rpc("bridge.get_ranked_posts",
                {"sort": "created", "tag": "", "limit": SCAN_LIMIT, "observer": ""})
    return posts


def has_beneficiary(post):
    return any(b.get("account") == BENEFICIARY for b in post.get("beneficiaries", []))


def eligible(post, history, now):
    created = parse_time(post["created"])
    if now - created > MAX_POST_AGE:
        return False, "older than 24h"
    if not has_beneficiary(post):
        return False, "no commentrewarder beneficiary"
    author = post["author"]
    last = history.get(author)
    if last and now - parse_time(last) < AUTHOR_COOLDOWN:
        return False, "author on 3 day cooldown"
    if ACCOUNT and any(v.get("voter") == ACCOUNT for v in post.get("active_votes", [])):
        return False, "already voted"
    return True, "ok"


# ---------- comment generation ----------
def generate_comment(post):
    title = post.get("title", "")
    body = post.get("body", "")[:4000]
    author = post["author"]

    if not ANTHROPIC_KEY:
        return f"[placeholder] @{author}, set ANTHROPIC_API_KEY to generate a real comment about \"{title}\"."

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 200,
            "system": SYSTEM_PROMPT,
            "messages": [{
                "role": "user",
                "content": f"Author: @{author}\nTitle: {title}\n\nPost:\n{body}",
            }],
        },
        timeout=60,
    )
    resp.raise_for_status()
    text = "".join(b.get("text", "") for b in resp.json()["content"])
    return clean_comment(text)


# ---------- live actions ----------
def vote_and_comment(post, comment):
    from beem import Hive
    from beem.comment import Comment

    hive = Hive(keys=[POSTING_KEY], node=[API])
    ident = f"@{post['author']}/{post['permlink']}"
    Comment(ident, blockchain_instance=hive).upvote(VOTE_WEIGHT, voter=ACCOUNT)
    permlink = "re-" + post["author"].replace(".", "") + "-" + datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz").lower()
    hive.post(title="", body=comment, author=ACCOUNT,
              reply_identifier=ident, permlink=permlink)


# ---------- main ----------
def main():
    now = datetime.now(timezone.utc)
    history = load_history()
    print(f"mode: {'DRY RUN (simulation)' if DRY_RUN else 'LIVE'}")

    if not DRY_RUN and not (ACCOUNT and POSTING_KEY):
        raise SystemExit("HIVE_ACCOUNT and HIVE_POSTING_KEY are required in live mode")

    posts = fetch_recent_posts()
    print(f"scanned {len(posts)} new posts")

    done = 0
    for post in posts:
        ok, reason = eligible(post, history, now)
        tag = f"@{post['author']}/{post['permlink']}"
        if not ok:
            print(f"  skip {tag}: {reason}")
            continue

        comment = generate_comment(post)
        print(f"\n  MATCH {tag}")
        print(f"  title: {post.get('title')}")
        print(f"  would vote: {VOTE_WEIGHT}%")
        print(f"  would comment: {comment}\n")

        if not DRY_RUN:
            vote_and_comment(post, comment)
            history[post["author"]] = now.strftime("%Y-%m-%dT%H:%M:%S")
            save_history(history)
            time.sleep(20)  # Hive allows one comment every 3 seconds; stay well above it

        done += 1
        if done >= MAX_PER_RUN:
            break

    print(f"\nfinished: {done} action(s) {'simulated' if DRY_RUN else 'executed'}")


if __name__ == "__main__":
    main()
