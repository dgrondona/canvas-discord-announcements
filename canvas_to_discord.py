#!/usr/bin/env python3
"""Post new Canvas course announcements to a Discord channel via webhook.

Designed to run on a schedule (see .github/workflows/canvas.yml). Every
announcement that has been posted is recorded in ``sent_announcements.json``,
so the run window can safely overlap and missed runs get caught up later.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import html2text
except ImportError:  # pragma: no cover
    sys.exit("html2text is not installed - run: pip install -r requirements.txt")

STATE_FILE = "sent_announcements.json"

# Remembered IDs are pruned past this age. It must stay comfortably larger than
# the lookback window, or a pruned announcement would come back and repost.
STATE_RETENTION_DAYS = 90

DISCORD_TITLE_LIMIT = 256
DISCORD_DESCRIPTION_LIMIT = 4096
DISCORD_CONTENT_LIMIT = 2000
DISCORD_AUTHOR_LIMIT = 256
DISCORD_FOOTER_LIMIT = 2048
EMBED_COLOR = 0xE13223  # Canvas red


class ConfigError(Exception):
    """Something about the environment is wrong and no run can succeed."""


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    canvas_url: str
    canvas_token: str
    course_id: str
    webhook_url: str
    mention: str
    lookback_days: int
    dry_run: bool
    post_on_first_run: bool


def _env(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    # Unset secrets arrive as empty strings in Actions, not as missing keys.
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if required:
        raise ConfigError(f"{name} is not set (or is empty).")
    return default


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def load_config() -> Config:
    canvas_url = _env("CANVAS_URL", required=True).rstrip("/")
    if not canvas_url.startswith(("http://", "https://")):
        canvas_url = f"https://{canvas_url}"

    course_id = _env("COURSE_ID", required=True)
    if not course_id.isdigit():
        raise ConfigError(
            f"COURSE_ID must be the numeric course id, got {course_id!r}. "
            "It is the number in the course URL: https://<canvas>/courses/12345"
        )

    webhook_url = _env("DISCORD_WEBHOOK_URL", required=True)
    if "discord.com/api/webhooks/" not in webhook_url and "discordapp.com/api/webhooks/" not in webhook_url:
        raise ConfigError(
            "DISCORD_WEBHOOK_URL does not look like a Discord webhook URL "
            "(expected https://discord.com/api/webhooks/<id>/<token>)."
        )

    raw_lookback = _env("LOOKBACK_DAYS", "7")
    try:
        lookback_days = int(raw_lookback)
    except ValueError:
        raise ConfigError(f"LOOKBACK_DAYS must be a whole number, got {raw_lookback!r}.") from None
    if not 1 <= lookback_days <= 60:
        raise ConfigError(f"LOOKBACK_DAYS must be between 1 and 60, got {lookback_days}.")

    # A bare id is the thing people paste out of Discord, so accept it.
    mention = _env("DISCORD_MENTION", "") or ""
    if mention.isdigit():
        mention = f"<@{mention}>"

    return Config(
        canvas_url=canvas_url,
        canvas_token=_env("CANVAS_TOKEN", required=True),
        course_id=course_id,
        webhook_url=webhook_url,
        mention=mention,
        lookback_days=lookback_days,
        dry_run=_env_flag("DRY_RUN"),
        post_on_first_run=_env_flag("POST_ON_FIRST_RUN"),
    )


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------


def load_state() -> dict[str, str] | None:
    """Return id -> ISO timestamp, or None when no state file exists yet."""
    if not os.path.exists(STATE_FILE):
        return None

    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"warning: could not read {STATE_FILE} ({exc}); treating it as empty", file=sys.stderr)
        return {}

    if isinstance(raw, list):  # the original format was a bare list of ids
        stamp = _iso(_now())
        return {str(item): stamp for item in raw}
    if isinstance(raw, dict):
        sent = raw.get("sent", raw)
        if isinstance(sent, dict):
            return {str(key): str(value) for key, value in sent.items()}
    return {}


def save_state(sent: dict[str, str]) -> None:
    cutoff = _now() - timedelta(days=STATE_RETENTION_DAYS)
    kept = {}
    for announcement_id, stamp in sent.items():
        parsed = _parse_time(stamp)
        if parsed is None or parsed >= cutoff:
            kept[announcement_id] = stamp

    payload = {
        "updated_at": _iso(_now()),
        # Sorted by timestamp so new entries append and the commit diff stays small.
        "sent": dict(sorted(kept.items(), key=lambda item: (item[1], item[0]))),
    }
    with open(STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


# --------------------------------------------------------------------------
# canvas
# --------------------------------------------------------------------------


def canvas_session(config: Config) -> requests.Session:
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {config.canvas_token}"})
    retry = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


def _check_canvas(response: requests.Response, config: Config) -> None:
    if response.status_code == 401:
        raise ConfigError(
            "Canvas rejected CANVAS_TOKEN (401). Generate a fresh access token at "
            f"{config.canvas_url}/profile/settings and update the repository secret."
        )
    if response.status_code == 403:
        raise ConfigError(
            f"Canvas returned 403 - the token's account cannot read course {config.course_id}."
        )
    if response.status_code == 404:
        raise ConfigError(
            f"Canvas returned 404 - check CANVAS_URL ({config.canvas_url}) "
            f"and COURSE_ID ({config.course_id})."
        )
    response.raise_for_status()


def fetch_announcements(session: requests.Session, config: Config) -> list[dict]:
    now = _now()
    params: dict | None = {
        "context_codes[]": f"course_{config.course_id}",
        "start_date": _iso(now - timedelta(days=config.lookback_days)),
        # Canvas defaults end_date to start_date + 28d; a day of slack on the far
        # end covers clock skew and announcements dated slightly in the future.
        "end_date": _iso(now + timedelta(days=1)),
        "active_only": "true",
        "per_page": 100,
    }

    url: str | None = f"{config.canvas_url}/api/v1/announcements"
    announcements: list[dict] = []
    while url:
        response = session.get(url, params=params, timeout=30)
        _check_canvas(response, config)
        page = response.json()
        if not isinstance(page, list):
            raise RuntimeError(f"Unexpected Canvas response: {str(page)[:300]}")
        announcements.extend(page)
        url = response.links.get("next", {}).get("url")
        params = None  # the Link header URL already carries the query string
    return announcements


def fetch_course_name(session: requests.Session, config: Config) -> str:
    fallback = f"Course {config.course_id}"
    try:
        response = session.get(f"{config.canvas_url}/api/v1/courses/{config.course_id}", timeout=30)
        response.raise_for_status()
        return response.json().get("name") or fallback
    except (requests.RequestException, ValueError):
        return fallback


# --------------------------------------------------------------------------
# discord
# --------------------------------------------------------------------------


def _markdown_converter() -> html2text.HTML2Text:
    converter = html2text.HTML2Text()
    converter.body_width = 0  # Discord does its own wrapping
    converter.images_to_alt = True  # Canvas-hosted images need auth; Discord can't load them
    converter.unicode_snob = True  # keep em-dashes and smart quotes instead of ASCII-ising them
    converter.ignore_tables = False
    return converter


_CONVERTER = _markdown_converter()


def html_to_markdown(html: str | None) -> str:
    """Turn Canvas's editor HTML into markdown Discord actually renders."""
    if not html:
        return ""

    text = _CONVERTER.handle(html)
    text = text.replace("\xa0", " ")
    # Canvas pads with empty <p>/&nbsp; blocks; strip them back to blank lines.
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Discord only renders heading levels 1-3, and has no horizontal rule.
    text = re.sub(r"^#{4,}\s*", "### ", text, flags=re.MULTILINE)
    # html2text indents top-level bullets by 2 spaces; Discord reads that as a
    # nested list, so shift every list level up by one.
    text = re.sub(r"^  (?=(?:[*+-]|\d+\.)\s)", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*(\*\s?){3,}\s*$", "───────────────", text, flags=re.MULTILINE)
    return text.strip()


_MENTION_PATTERN = re.compile(r"<@!?(\d+)>|<@&(\d+)>|@(everyone|here)")


def build_allowed_mentions(mention: str) -> dict:
    """Whitelist exactly the mentions we put in ``content``.

    Anything else stays inert - including an @everyone inside the announcement
    body, since a content mention only pings if it is listed here and embeds
    never ping at all. Switching to @everyone is a DISCORD_MENTION change.
    """
    users: list[str] = []
    roles: list[str] = []
    parse: list[str] = []

    for user_id, role_id, keyword in _MENTION_PATTERN.findall(mention or ""):
        if user_id:
            users.append(user_id)
        elif role_id:
            roles.append(role_id)
        elif keyword:
            parse.append("everyone")  # the same flag covers @everyone and @here

    allowed: dict = {"parse": sorted(set(parse))}
    if users:
        allowed["users"] = sorted(set(users))
    if roles:
        allowed["roles"] = sorted(set(roles))
    return allowed


def _truncate(text: str, limit: int, url: str) -> str:
    if len(text) <= limit:
        return text
    suffix = f"\n\n… [read the full announcement]({url})" if url else "\n\n…"
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


def build_payload(config: Config, announcement: dict, course_name: str) -> dict:
    url = announcement.get("html_url") or ""
    title = (announcement.get("title") or "(untitled announcement)")[:DISCORD_TITLE_LIMIT]
    body = html_to_markdown(announcement.get("message")) or "_(no message body)_"

    embed: dict = {
        "author": {
            "name": course_name[:DISCORD_AUTHOR_LIMIT],
            "url": f"{config.canvas_url}/courses/{config.course_id}",
        },
        "title": title,
        "description": _truncate(body, DISCORD_DESCRIPTION_LIMIT, url),
        "color": EMBED_COLOR,
    }
    if url:
        embed["url"] = url

    posted_at = announcement_time(announcement)
    if posted_at:
        embed["timestamp"] = _iso(posted_at)

    author = announcement.get("user_name")
    if author:
        embed["footer"] = {"text": f"Posted by {author}"[:DISCORD_FOOTER_LIMIT]}

    prefix = f"{config.mention} " if config.mention else ""
    return {
        "content": f"{prefix}📢 New announcement in **{course_name}**"[:DISCORD_CONTENT_LIMIT],
        "embeds": [embed],
        "allowed_mentions": build_allowed_mentions(config.mention),
    }


def post_to_discord(session: requests.Session, config: Config, payload: dict) -> None:
    for attempt in range(1, 6):
        response = session.post(config.webhook_url, json=payload, timeout=30)

        if response.status_code == 429:
            try:
                retry_after = float(response.json().get("retry_after", 1.0))
            except (ValueError, TypeError, AttributeError):
                retry_after = 1.0
            wait = min(max(retry_after, 0.5), 30.0)
            print(f"    rate limited by Discord, retrying in {wait:.1f}s")
            time.sleep(wait)
            continue

        if response.status_code in (401, 403, 404):
            raise ConfigError(
                f"Discord rejected the webhook ({response.status_code}) - DISCORD_WEBHOOK_URL "
                "is wrong or the webhook was deleted."
            )
        if response.status_code == 400:
            raise RuntimeError(f"Discord rejected the message: {response.text[:500]}")

        # No automatic retries on this session: a retried POST can double-post.
        response.raise_for_status()
        return

    raise RuntimeError("Discord kept rate limiting the webhook; giving up for this run.")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def announcement_time(announcement: dict) -> datetime | None:
    for key in ("posted_at", "delayed_post_at", "created_at"):
        parsed = _parse_time(announcement.get(key))
        if parsed:
            return parsed
    return None


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def run(config: Config) -> None:
    canvas = canvas_session(config)

    previous = load_state()
    first_run = previous is None
    sent: dict[str, str] = {} if first_run else previous

    announcements = fetch_announcements(canvas, config)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    announcements.sort(key=lambda item: announcement_time(item) or epoch)

    new = [
        announcement
        for announcement in announcements
        if announcement.get("id") is not None and str(announcement["id"]) not in sent
    ]

    print(
        f"{len(announcements)} announcement(s) in the last {config.lookback_days} day(s); "
        f"{len(new)} not yet posted."
    )

    if config.dry_run:
        for announcement in new:
            print(f"  would post: {announcement.get('title')!r}")
        print("DRY_RUN is set - nothing was posted and the state file was left alone.")
        return

    if first_run and not config.post_on_first_run:
        for announcement in new:
            sent[str(announcement["id"])] = _stamp_for(announcement)
            print(f"  recorded without posting: {announcement.get('title')!r}")
        save_state(sent)
        print(
            f"First run: recorded {len(new)} existing announcement(s) without posting so the "
            "channel doesn't get a backlog dump. Anything new from here on will be posted. "
            "(Re-run with post_on_first_run to post the backlog instead.)"
        )
        return

    if not new:
        if first_run:
            save_state(sent)  # create the file so the next run isn't a "first run" too
        return

    discord = requests.Session()
    course_name = fetch_course_name(canvas, config)
    posted = 0
    try:
        for index, announcement in enumerate(new):
            post_to_discord(discord, config, build_payload(config, announcement, course_name))
            sent[str(announcement["id"])] = _stamp_for(announcement)
            posted += 1
            print(f"  posted: {announcement.get('title')!r}")
            if index < len(new) - 1:
                time.sleep(1)  # stay well inside the webhook rate limit
    finally:
        # Persist whatever made it out, so a mid-run failure can't cause a repost.
        save_state(sent)
        print(f"Posted {posted} of {len(new)} announcement(s).")


def _stamp_for(announcement: dict) -> str:
    return _iso(announcement_time(announcement) or _now())


def main() -> None:
    try:
        config = load_config()
        run(config)
    except ConfigError as exc:
        sys.exit(f"Configuration error: {exc}")
    except requests.RequestException as exc:
        sys.exit(f"Network error talking to Canvas or Discord: {exc}")


if __name__ == "__main__":
    main()
