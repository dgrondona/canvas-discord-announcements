#!/usr/bin/env python3
"""Post new Canvas course announcements to a Discord channel via webhook.

Designed to run on a schedule (see .github/workflows/canvas.yml). Every
announcement that has been posted is recorded in ``sent_announcements.json``,
so the run window can safely overlap and missed runs get caught up later.

Shared Canvas/Discord plumbing lives in common.py.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from common import (
    DISCORD_AUTHOR_LIMIT,
    DISCORD_CONTENT_LIMIT,
    DISCORD_DESCRIPTION_LIMIT,
    DISCORD_FOOTER_LIMIT,
    DISCORD_TITLE_LIMIT,
    CanvasConfig,
    ConfigError,
    Webhook,
    build_allowed_mentions,
    canvas_session,
    env,
    env_flag,
    env_mention,
    env_webhook,
    fetch_course_name,
    html_to_markdown,
    iso,
    load_canvas_config,
    now,
    paginate,
    parse_time,
    post_to_discord,
    read_json_state,
    truncate,
    write_json_state,
)

STATE_FILE = "sent_announcements.json"

# Remembered IDs are pruned past this age. It must stay comfortably larger than
# the lookback window, or a pruned announcement would come back and repost.
STATE_RETENTION_DAYS = 90

# Sidebar colour, chosen by a keyword in the announcement title. First match
# wins, so CRITICAL outranks IMPORTANT outranks REMINDER. Word boundaries keep
# "[CRITICAL]" and "CRITICAL:" matching without also catching "critically".
PRIORITY_COLORS = (
    (re.compile(r"\bcritical\b", re.IGNORECASE), 0xE13223),   # red
    (re.compile(r"\bimportant\b", re.IGNORECASE), 0xE67E22),  # orange
    (re.compile(r"\breminder\b", re.IGNORECASE), 0x74C0FC),   # light blue
)
DEFAULT_COLOR = 0x99AAB5  # gray


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    canvas: CanvasConfig
    webhook: Webhook
    mention: str
    preview: str
    course_wide_only: bool
    verbose: bool
    lookback_days: int
    dry_run: bool
    post_on_first_run: bool


def load_config() -> Config:
    preview = (env("PREVIEW", "") or "").lower()
    if preview in {"off", "none", "false"}:
        preview = ""
    if preview and preview not in {"sample", "latest"}:
        raise ConfigError(f"PREVIEW must be 'sample' or 'latest', got {preview!r}.")

    # A sample preview never calls Canvas, so only the webhook has to be real.
    needs_canvas = preview != "sample"

    raw_lookback = env("LOOKBACK_DAYS", "7")
    try:
        lookback_days = int(raw_lookback)
    except ValueError:
        raise ConfigError(f"LOOKBACK_DAYS must be a whole number, got {raw_lookback!r}.") from None
    if not 1 <= lookback_days <= 60:
        raise ConfigError(f"LOOKBACK_DAYS must be between 1 and 60, got {lookback_days}.")

    return Config(
        canvas=load_canvas_config(required=needs_canvas),
        webhook=env_webhook("DISCORD_WEBHOOK_URL"),
        mention=env_mention("DISCORD_MENTION"),
        preview=preview,
        course_wide_only=env_flag("COURSE_WIDE_ONLY"),
        verbose=env_flag("VERBOSE"),
        lookback_days=lookback_days,
        dry_run=env_flag("DRY_RUN"),
        post_on_first_run=env_flag("POST_ON_FIRST_RUN"),
    )


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------


def load_state() -> dict[str, str] | None:
    """Return id -> ISO timestamp, or None when no state file exists yet."""
    raw = read_json_state(STATE_FILE)
    if raw is None:
        return None

    if isinstance(raw, list):  # the original format was a bare list of ids
        stamp = iso(now())
        return {str(item): stamp for item in raw}
    if isinstance(raw, dict):
        sent = raw.get("sent", raw)
        if isinstance(sent, dict):
            return {str(key): str(value) for key, value in sent.items()}
    return {}


def save_state(sent: dict[str, str]) -> None:
    cutoff = now() - timedelta(days=STATE_RETENTION_DAYS)
    kept = {}
    for announcement_id, stamp in sent.items():
        parsed = parse_time(stamp)
        if parsed is None or parsed >= cutoff:
            kept[announcement_id] = stamp

    write_json_state(
        STATE_FILE,
        {
            "updated_at": iso(now()),
            # Sorted by timestamp so new entries append and the commit diff stays small.
            "sent": dict(sorted(kept.items(), key=lambda item: (item[1], item[0]))),
        },
    )


# --------------------------------------------------------------------------
# canvas
# --------------------------------------------------------------------------


def fetch_announcements(session: requests.Session, config: Config) -> list[dict]:
    current = now()
    params = {
        "context_codes[]": f"course_{config.canvas.course_id}",
        "start_date": iso(current - timedelta(days=config.lookback_days)),
        # Canvas defaults end_date to start_date + 28d; a day of slack on the far
        # end covers clock skew and announcements dated slightly in the future.
        "end_date": iso(current + timedelta(days=1)),
        "active_only": "true",
        # Documented to come back only for section-scoped topics, so it doubles
        # as a check for is_section_specific, which Canvas does not document.
        "include[]": "sections",
        "per_page": 100,
    }
    return paginate(session, config.canvas, f"{config.canvas.url}/api/v1/announcements", params)


def is_section_specific(announcement: dict) -> bool:
    """Whether this went to particular sections rather than the whole course.

    Note this is the only "not everyone" case that can reach us at all: a Canvas
    message to one student is an Inbox conversation, a different API this script
    never touches, so those can't leak into the channel.
    """
    return bool(announcement.get("is_section_specific") or announcement.get("sections"))


# --------------------------------------------------------------------------
# discord
# --------------------------------------------------------------------------


SAMPLE_HTML = """
<p>This is a formatting preview, not a real announcement. If everything below
renders properly, the bot is set up correctly.</p>
<h2>Headings</h2>
<p>Text can be <strong>bold</strong>, <em>italic</em>, or
<a href="https://github.com/dgrondona/canvas-discord-announcements">a link</a>.</p>
<h4>Headings deeper than Discord supports get clamped</h4>
<ul>
  <li>Bulleted lists</li>
  <li>...with <strong>formatting</strong> inside
    <ul><li>and nesting</li></ul>
  </li>
</ul>
<ol><li>Numbered lists</li><li>work too</li></ol>
<blockquote><p>Block quotes look like this.</p></blockquote>
<hr />
<p>Smart punctuation survives &mdash; &ldquo;like this&rdquo; &hellip; 50&ndash;60%.</p>
<p>An @everyone typed inside an announcement stays inert. Only the mention
configured in DISCORD_MENTION actually pings.</p>
<p><img src="https://example.edu/seating.png" alt="images become their alt text" /></p>
"""


def sample_announcement(config: Config) -> dict:
    return {
        "id": 0,
        "title": "Formatting preview",
        "user_name": "Canvas → Discord bot",
        "message": SAMPLE_HTML,
        "posted_at": iso(now()),
        "html_url": f"{config.canvas.url}/courses/{config.canvas.course_id}",
    }


def embed_color(title: str) -> int:
    for pattern, color in PRIORITY_COLORS:
        if pattern.search(title or ""):
            return color
    return DEFAULT_COLOR


def build_payload(config: Config, announcement: dict, course_name: str) -> dict:
    url = announcement.get("html_url") or ""
    title = (announcement.get("title") or "(untitled announcement)")[:DISCORD_TITLE_LIMIT]
    body = html_to_markdown(announcement.get("message")) or "_(no message body)_"

    embed: dict = {
        "author": {
            "name": course_name[:DISCORD_AUTHOR_LIMIT],
            "url": f"{config.canvas.url}/courses/{config.canvas.course_id}",
        },
        "title": title,
        "description": truncate(body, DISCORD_DESCRIPTION_LIMIT, url),
        "color": embed_color(title),
    }
    if url:
        embed["url"] = url

    posted_at = announcement_time(announcement)
    if posted_at:
        embed["timestamp"] = iso(posted_at)

    author = announcement.get("user_name")
    if author:
        embed["footer"] = {"text": f"Posted by {author}"[:DISCORD_FOOTER_LIMIT]}

    prefix = f"{config.mention} " if config.mention else ""
    return {
        "content": f"{prefix}📢 New announcement in **{course_name}**"[:DISCORD_CONTENT_LIMIT],
        "embeds": [embed],
        "allowed_mentions": build_allowed_mentions(config.mention),
    }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def describe(config: Config, announcement: dict) -> str:
    """What to call an announcement in the run log.

    Titles are omitted by default because Actions logs are world-readable on a
    public repo, and an announcement title is course content. Set VERBOSE=true
    on a manual run when you actually need to read them back.
    """
    if config.verbose:
        return repr(announcement.get("title") or "(untitled)")
    return f"announcement {announcement.get('id')}"


def announcement_time(announcement: dict) -> datetime | None:
    for key in ("posted_at", "delayed_post_at", "created_at"):
        parsed = parse_time(announcement.get(key))
        if parsed:
            return parsed
    return None


def _stamp_for(announcement: dict) -> str:
    return iso(announcement_time(announcement) or now())


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def run(config: Config) -> None:
    if config.preview:
        run_preview(config)
        return

    canvas = canvas_session(config.canvas)

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

    scoped: list[dict] = []
    if config.course_wide_only:
        scoped = [item for item in new if is_section_specific(item)]
        new = [item for item in new if not is_section_specific(item)]

    # Nothing above this point may touch `sent`: a dry run has to leave the
    # state file exactly as it found it.
    if config.dry_run:
        for announcement in scoped:
            print(f"  would skip, went to specific sections: {describe(config, announcement)}")
        for announcement in new:
            print(f"  would post: {describe(config, announcement)}")
        print("DRY_RUN is set - nothing was posted and the state file was left alone.")
        return

    for announcement in scoped:
        # Recorded as handled so each is reported once, rather than on every run
        # until it ages out of the lookback window.
        sent[str(announcement["id"])] = _stamp_for(announcement)
        print(f"  skipped, went to specific sections: {describe(config, announcement)}")

    if first_run and not config.post_on_first_run:
        for announcement in new:
            sent[str(announcement["id"])] = _stamp_for(announcement)
            print(f"  recorded without posting: {describe(config, announcement)}")
        save_state(sent)
        print(
            f"First run: recorded {len(new)} existing announcement(s) without posting so the "
            "channel doesn't get a backlog dump. Anything new from here on will be posted. "
            "(Re-run with post_on_first_run to post the backlog instead.)"
        )
        return

    if not new:
        # first_run creates the file so the next run isn't a "first run" too;
        # scoped means there are skip records to persist.
        if first_run or scoped:
            save_state(sent)
        return

    discord = requests.Session()
    course_name = fetch_course_name(canvas, config.canvas)
    posted = 0
    try:
        for index, announcement in enumerate(new):
            post_to_discord(
                discord, config.webhook, build_payload(config, announcement, course_name)
            )
            sent[str(announcement["id"])] = _stamp_for(announcement)
            posted += 1
            print(f"  posted: {describe(config, announcement)}")
            if index < len(new) - 1:
                time.sleep(1)  # stay well inside the webhook rate limit
    finally:
        # Persist whatever made it out, so a mid-run failure can't cause a repost.
        save_state(sent)
        print(f"Posted {posted} of {len(new)} announcement(s).")


def run_preview(config: Config) -> None:
    """Post one announcement to Discord to eyeball the formatting.

    Deliberately reads no state and writes none, so a preview can be run any
    number of times without affecting what the scheduled runs consider posted.
    """
    if config.preview == "sample":
        announcement = sample_announcement(config)
        course_name = "Formatting preview"
    else:
        canvas = canvas_session(config.canvas)
        announcements = fetch_announcements(canvas, config)
        if not announcements:
            print(f"No announcements in the last {config.lookback_days} day(s) to preview.")
            return
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        announcement = max(announcements, key=lambda item: announcement_time(item) or epoch)
        course_name = fetch_course_name(canvas, config.canvas)

    post_to_discord(
        requests.Session(), config.webhook, build_payload(config, announcement, course_name)
    )
    print(f"Preview posted: {describe(config, announcement)}. The state file was not touched.")


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
