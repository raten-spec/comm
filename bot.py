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
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
# Models are tried in order; a 404 (retired/renamed model) moves on to the next one.
# Override the first choice with the GEMINI_MODEL env var.
MODELS = [m for m in [
    os.getenv("GEMINI_MODEL", ""),
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
] if m]
VOTE_WEIGHT = int(os.getenv("VOTE_WEIGHT", "20"))      # percent
MAX_PER_RUN = int(os.getenv("MAX_PER_RUN", "3"))
SCAN_LIMIT = int(os.getenv("SCAN_LIMIT", "50"))

SYSTEM_PROMPT = """You write short comments on Hive blog posts.
Rules:
- Write in English, in a warm and friendly tone.
- 2 to 3 short sentences at most.
- Talk to the author directly (you/your) and mention something specific from the post.
- Never use @mentions or usernames, the comment is already a direct reply to the author.
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
    text = re.sub(r"@[\w.-]+[,:]?\s*", "", text)  # safety net: no @mentions
    text = re.sub(r"\s+", " ", text).strip().strip('"')
    return text


# ---------- scanning ----------
def fetch_recent_posts():
    """bridge.get_ranked_posts allows max 20 per call, so paginate."""
    now = datetime.now(timezone.utc)
    posts = []
    start_author, start_permlink = "", ""
    while len(posts) < SCAN_LIMIT:
        params = {"sort": "created", "tag": "", "limit": 20, "observer": ""}
        if start_author:
            params["start_author"] = start_author
            params["start_permlink"] = start_permlink
        page = rpc("bridge.get_ranked_posts", params)
        if start_author and page:
            page = page[1:]  # first item repeats the previous page's last post
        if not page:
            break
        posts.extend(page)
        last = page[-1]
        start_author, start_permlink = last["author"], last["permlink"]
        if now - parse_time(last["created"]) > MAX_POST_AGE:
            break  # everything after this is older than 24h
        time.sleep(0.3)
    return posts[:SCAN_LIMIT]


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

    if not GEMINI_KEY:
        return f"[placeholder] set GEMINI_API_KEY to generate a real comment about \"{title}\"."

    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{
            "role": "user",
            "parts": [{"text": f"Author: {author}\nTitle: {title}\n\nPost:\n{body}"}],
        }],
        "generationConfig": {"maxOutputTokens": 200, "temperature": 0.9},
    }
    for model in MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(2):
            resp = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY},
                                 json=payload, timeout=60)
            if resp.status_code == 429:  # free tier rate limit, wait and retry once
                time.sleep(30)
                continue
            break
        if resp.status_code in (404, 400, 403):
            print(f"  model {model} unavailable ({resp.status_code}), trying next")
            continue
        if resp.status_code == 429:
            return None
        resp.raise_for_status()
        try:
            parts = resp.json()["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError):
            return None  # blocked or empty response
        text = clean_comment("".join(p.get("text", "") for p in parts))
        if text:
            print(f"  (model: {model})")
        return text
    return None


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

        try:
            comment = generate_comment(post)
        except requests.RequestException as e:
            print(f"  skip {tag}: comment generation failed ({e})")
            continue
        if not comment:
            print(f"  skip {tag}: no comment generated (rate limit or empty)")
            continue
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
