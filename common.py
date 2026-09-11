#!/usr/bin/env python3
"""Shared plumbing for the Canvas → Discord bots.

Both entry points - ``canvas_to_discord.py`` (announcements) and
``canvas_labs.py`` (lab forum threads) - talk to the same two APIs in the same
way, so the Canvas session, HTML conversion, webhook posting and state-file
handling live here rather than being duplicated.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import html2text
except ImportError:  # pragma: no cover
    sys.exit("html2text is not installed - run: pip install -r requirements.txt")

DISCORD_TITLE_LIMIT = 256
DISCORD_DESCRIPTION_LIMIT = 4096
DISCORD_CONTENT_LIMIT = 2000
DISCORD_AUTHOR_LIMIT = 256
DISCORD_FOOTER_LIMIT = 2048
DISCORD_THREAD_NAME_LIMIT = 100


class ConfigError(Exception):
    """Something about the environment is wrong and no run can succeed."""


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


def env(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    # Unset secrets arrive as empty strings in Actions, not as missing keys.
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if required:
        raise ConfigError(f"{name} is not set (or is empty).")
    return default


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def env_mention(name: str) -> str:
    """Read a mention setting, accepting the bare id people paste out of Discord."""
    mention = env(name, "") or ""
    return f"<@{mention}>" if mention.isdigit() else mention


@dataclass(frozen=True)
class Webhook:
    """A Discord webhook URL plus the setting it came from, for error messages.

    A webhook is bound to one channel, so each destination channel needs its own.
    """

    url: str
    env_name: str


def env_webhook(name: str, *, required: bool = True) -> Webhook:
    url = env(name, "", required=required) or ""
    if url and "discord.com/api/webhooks/" not in url and "discordapp.com/api/webhooks/" not in url:
        raise ConfigError(
            f"{name} does not look like a Discord webhook URL "
            "(expected https://discord.com/api/webhooks/<id>/<token>)."
        )
    return Webhook(url=url, env_name=name)


@dataclass(frozen=True)
class CanvasConfig:
    url: str
    token: str
    course_id: str


def load_canvas_config(*, required: bool = True) -> CanvasConfig:
    url = (env("CANVAS_URL", "https://canvas.instructure.com", required=required) or "").rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    course_id = env("COURSE_ID", "0", required=required) or "0"
    if not course_id.isdigit():
        raise ConfigError(
            f"COURSE_ID must be the numeric course id, got {course_id!r}. "
            "It is the number in the course URL: https://<canvas>/courses/12345"
        )

    return CanvasConfig(
        url=url,
        token=env("CANVAS_TOKEN", "", required=required) or "",
        course_id=course_id,
    )


# --------------------------------------------------------------------------
# canvas
# --------------------------------------------------------------------------


def canvas_session(canvas: CanvasConfig) -> requests.Session:
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {canvas.token}"})
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


def check_canvas(response: requests.Response, canvas: CanvasConfig) -> None:
    if response.status_code == 401:
        raise ConfigError(
            "Canvas rejected CANVAS_TOKEN (401). Generate a fresh access token at "
            f"{canvas.url}/profile/settings and update the repository secret."
        )
    if response.status_code == 403:
        raise ConfigError(
            f"Canvas returned 403 - the token's account cannot read course {canvas.course_id}."
        )
    if response.status_code == 404:
        raise ConfigError(
            f"Canvas returned 404 - check CANVAS_URL ({canvas.url}) "
            f"and COURSE_ID ({canvas.course_id})."
        )
    response.raise_for_status()


def paginate(session: requests.Session, canvas: CanvasConfig, url: str, params: dict) -> list[dict]:
    """Follow Canvas's Link header until every page has been collected."""
    next_params: dict | None = params
    items: list[dict] = []
    next_url: str | None = url
    while next_url:
        response = session.get(next_url, params=next_params, timeout=30)
        check_canvas(response, canvas)
        page = response.json()
        if not isinstance(page, list):
            raise RuntimeError(f"Unexpected Canvas response: {str(page)[:300]}")
        items.extend(page)
        next_url = response.links.get("next", {}).get("url")
        next_params = None  # the Link header URL already carries the query string
    return items


def fetch_course_name(session: requests.Session, canvas: CanvasConfig) -> str:
    fallback = f"Course {canvas.course_id}"
    try:
        response = session.get(f"{canvas.url}/api/v1/courses/{canvas.course_id}", timeout=30)
        response.raise_for_status()
        return response.json().get("name") or fallback
    except (requests.RequestException, ValueError):
        return fallback


# --------------------------------------------------------------------------
# canvas html -> discord markdown
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
    # html2text leaves a space between closing emphasis and punctuation ("**bold** ,").
    # The lookbehind keeps a list bullet ("* ...text") from being treated as one.
    text = re.sub(r"(?<=\S)(\*\*|__|\*|_)[ \t]+(?=[,.;:!?)\]])", r"\1", text)
    text = re.sub(r"^\s*(\*\s?){3,}\s*$", "───────────────", text, flags=re.MULTILINE)
    return text.strip()


def truncate(text: str, limit: int, url: str, label: str = "read the full announcement") -> str:
    if len(text) <= limit:
        return text
    suffix = f"\n\n… [{label}]({url})" if url else "\n\n…"
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


def chunk_markdown(text: str, limit: int = 1900) -> list[str]:
    """Split rendered markdown into message-sized pieces.

    Discord caps a message's ``content`` at 2000 characters. Rather than
    truncating a long Canvas description, callers post the first piece and send
    the rest as follow-up replies, so nothing is lost. Breaks are preferred at a
    paragraph, then a line, then a space, so formatting survives the split.
    """
    text = (text or "").strip()
    if not text:
        return []

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = -1
        for separator in ("\n\n", "\n", " "):
            candidate = window.rfind(separator)
            # Ignore a break so early that it would waste most of the message.
            if candidate > limit // 2:
                cut = candidate
                break
        if cut <= 0:
            cut = limit  # one enormous unbroken run: split it hard
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    if remaining:
        chunks.append(remaining)
    return chunks


# --------------------------------------------------------------------------
# discord
# --------------------------------------------------------------------------


_MENTION_PATTERN = re.compile(r"<@!?(\d+)>|<@&(\d+)>|@(everyone|here)")


def build_allowed_mentions(mention: str) -> dict:
    """Whitelist exactly the mentions we put in ``content``.

    Anything else stays inert - including an @everyone inside Canvas content,
    since a content mention only pings if it is listed here and embeds never
    ping at all.
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


def _discord_request(
    session: requests.Session,
    method: str,
    url: str,
    webhook: Webhook,
    payload: dict,
    params: dict | None = None,
) -> dict | None:
    """Send one webhook call, handling 429s and mapping failures to clear errors."""
    for _ in range(5):
        response = session.request(method, url, json=payload, params=params, timeout=30)

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
                f"Discord rejected the webhook ({response.status_code}) - {webhook.env_name} "
                "is wrong or the webhook was deleted."
            )
        if response.status_code == 400:
            raise RuntimeError(f"Discord rejected the message: {response.text[:500]}")

        # No automatic retries on this session: a retried POST can double-post.
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    raise RuntimeError("Discord kept rate limiting the webhook; giving up for this run.")


def post_to_discord(
    session: requests.Session,
    webhook: Webhook,
    payload: dict,
    *,
    wait: bool = False,
    thread_id: str | None = None,
) -> dict | None:
    """Execute the webhook.

    ``wait`` returns the created message, which is how a forum post's thread id
    is recovered - a thread's id is the id of its starter message. ``thread_id``
    posts into an existing thread instead of creating one.
    """
    params: dict = {}
    if wait:
        params["wait"] = "true"
    if thread_id:
        params["thread_id"] = thread_id
    return _discord_request(session, "POST", webhook.url, webhook, payload, params or None)


def edit_discord_message(
    session: requests.Session,
    webhook: Webhook,
    message_id: str,
    payload: dict,
    *,
    thread_id: str | None = None,
) -> dict | None:
    """Edit a message this webhook previously sent."""
    params = {"thread_id": thread_id} if thread_id else None
    url = f"{webhook.url.rstrip('/')}/messages/{message_id}"
    return _discord_request(session, "PATCH", url, webhook, payload, params)


# --------------------------------------------------------------------------
# state files
# --------------------------------------------------------------------------


def read_json_state(path: str):
    """Return the parsed state file, or None when it does not exist yet.

    None means "first run" and is deliberately distinct from an empty file.
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"warning: could not read {path} ({exc}); treating it as empty", file=sys.stderr)
        return {}


def write_json_state(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat()


def parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def discord_timestamp(value: datetime, style: str = "F") -> str:
    """Render a time Discord shows in each viewer's own timezone."""
    return f"<t:{int(value.timestamp())}:{style}>"
