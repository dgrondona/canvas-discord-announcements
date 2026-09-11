#!/usr/bin/env python3
"""Open a Discord forum thread for each Canvas lab assignment.

Designed to run on a schedule (see .github/workflows/labs.yml). Every lab gets
one thread, recorded in ``lab_threads.json``, so reruns don't duplicate it. When
the assignment changes the thread's posts are edited in place, and a reply calls
out the change when it is something worth noticing, like a due date moving.

Shared Canvas/Discord plumbing lives in common.py.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from common import (
    DISCORD_THREAD_NAME_LIMIT,
    DISCORD_TITLE_LIMIT,
    CanvasConfig,
    ConfigError,
    Webhook,
    build_allowed_mentions,
    canvas_session,
    chunk_markdown,
    delete_discord_message,
    discord_timestamp,
    edit_discord_message,
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
    write_json_state,
)

STATE_FILE = "lab_threads.json"

# A lab's thread should stay deduped for the whole course, so entries live far
# longer than the announcements file's 90 days.
STATE_RETENTION_DAYS = 365

# Discord caps message content at 2000; leave headroom for a mention prefix.
CHUNK_LIMIT = 1900
EMBED_FIELD_VALUE_LIMIT = 1024
EMBED_COLOR = 0x4A90D9  # fixed: a colour derived from "due soon" would churn

# Fields whose change is worth a reply in the thread, rather than a silent edit.
NOTABLE_FIELDS = ("name", "due_at", "points_possible", "unlock_at", "lock_at")


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    canvas: CanvasConfig
    webhook: Webhook
    mention: str
    pattern: re.Pattern
    forum_tags: list[str]
    preview: str
    backfill: bool
    dry_run: bool
    verbose: bool


def load_config() -> Config:
    preview = (env("PREVIEW", "") or "").lower()
    if preview in {"off", "none", "false"}:
        preview = ""
    if preview and preview != "sample":
        raise ConfigError(f"PREVIEW must be 'sample', got {preview!r}.")

    # A sample preview never calls Canvas, so only the webhook has to be real.
    needs_canvas = preview != "sample"

    raw_pattern = env("LAB_PATTERN", r"\bLab\b")
    try:
        pattern = re.compile(raw_pattern, re.IGNORECASE)
    except re.error as exc:
        raise ConfigError(f"LAB_PATTERN is not a valid regular expression ({exc}).") from None

    tags = [tag.strip() for tag in (env("LAB_FORUM_TAGS", "") or "").split(",") if tag.strip()]
    for tag in tags:
        if not tag.isdigit():
            raise ConfigError(f"LAB_FORUM_TAGS must be numeric tag ids, got {tag!r}.")

    return Config(
        canvas=load_canvas_config(required=needs_canvas),
        webhook=env_webhook("DISCORD_FORUM_WEBHOOK_URL"),
        mention=env_mention("DISCORD_LAB_MENTION"),
        pattern=pattern,
        forum_tags=tags,
        preview=preview,
        backfill=env_flag("BACKFILL"),
        dry_run=env_flag("DRY_RUN"),
        verbose=env_flag("VERBOSE"),
    )


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------


def load_state() -> dict[str, dict] | None:
    """Return assignment id -> entry, or None when no state file exists yet."""
    raw = read_json_state(STATE_FILE)
    if raw is None:
        return None
    if isinstance(raw, dict):
        labs = raw.get("labs", {})
        if isinstance(labs, dict):
            return {str(key): value for key, value in labs.items() if isinstance(value, dict)}
    return {}


def save_state(labs: dict[str, dict]) -> None:
    cutoff = now() - timedelta(days=STATE_RETENTION_DAYS)
    kept = {}
    for lab_id, entry in labs.items():
        stamp = parse_time(entry.get("created_at") or entry.get("seen_at"))
        if stamp is None or stamp >= cutoff:
            kept[lab_id] = entry

    write_json_state(
        STATE_FILE,
        {
            "updated_at": iso(now()),
            # Sorted numerically so the commit diff stays readable.
            "labs": dict(sorted(kept.items(), key=lambda item: int(item[0]))),
        },
    )


# --------------------------------------------------------------------------
# canvas
# --------------------------------------------------------------------------


def fetch_labs(session: requests.Session, config: Config) -> list[dict]:
    params = {"per_page": 100, "order_by": "due_at"}
    url = f"{config.canvas.url}/api/v1/courses/{config.canvas.course_id}/assignments"
    assignments = paginate(session, config.canvas, url, params)

    labs = [
        assignment
        for assignment in assignments
        if assignment.get("id") is not None
        and assignment.get("published", True)
        and config.pattern.search(assignment.get("name") or "")
    ]
    labs.sort(key=lambda item: (due_time(item) or datetime.max.replace(tzinfo=timezone.utc), item["id"]))
    return labs


def due_time(assignment: dict) -> datetime | None:
    return parse_time(assignment.get("due_at"))


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def notable_of(assignment: dict) -> dict:
    return {field: assignment.get(field) for field in NOTABLE_FIELDS}


def _pretty_points(value) -> str:
    # Canvas returns floats; 10.0 should read as 10.
    if value is None:
        return "none"
    try:
        return str(int(value)) if float(value).is_integer() else str(value)
    except (TypeError, ValueError):
        return str(value)


def _format_time_field(value) -> str:
    parsed = parse_time(value)
    if not parsed:
        return "—"
    return f"{discord_timestamp(parsed, 'F')}\n{discord_timestamp(parsed, 'R')}"


def build_embed(config: Config, assignment: dict, course_name: str) -> dict:
    """The metadata card. Deliberately holds no description text.

    The description goes in message content instead, which Discord caps at 2000
    characters, so a long one spans the starter post plus replies.
    """
    name = (assignment.get("name") or "Untitled assignment")[:DISCORD_TITLE_LIMIT]
    fields = [{"name": "Due", "value": _format_time_field(assignment.get("due_at")), "inline": True}]

    points = assignment.get("points_possible")
    if points is not None:
        fields.append({"name": "Points", "value": _pretty_points(points), "inline": True})

    submission_types = assignment.get("submission_types") or []
    if submission_types:
        pretty_types = ", ".join(t.replace("_", " ") for t in submission_types)
        fields.append(
            {"name": "Submit via", "value": pretty_types[:EMBED_FIELD_VALUE_LIMIT], "inline": True}
        )

    for label, key in (("Opens", "unlock_at"), ("Closes", "lock_at")):
        if assignment.get(key):
            fields.append(
                {"name": label, "value": _format_time_field(assignment.get(key)), "inline": True}
            )

    embed: dict = {"title": name, "color": EMBED_COLOR, "fields": fields}
    if assignment.get("html_url"):
        embed["url"] = assignment["html_url"]
    if course_name:
        embed["footer"] = {"text": course_name}
    return embed


def render(config: Config, assignment: dict, course_name: str) -> tuple[dict, list[str]]:
    """Build the starter payload plus the continuation messages."""
    description = html_to_markdown(assignment.get("description"))
    chunks = chunk_markdown(description, CHUNK_LIMIT)

    prefix = f"{config.mention} " if config.mention else ""
    first = chunks[0] if chunks else "_(no description on this assignment)_"

    starter = {
        "content": f"{prefix}{first}"[:2000],
        "embeds": [build_embed(config, assignment, course_name)],
        "allowed_mentions": build_allowed_mentions(config.mention),
    }
    return starter, chunks[1:]


def fingerprint(config: Config, assignment: dict) -> str:
    """Hash what the thread should say, to detect an assignment changing.

    Rendered with an empty course name on purpose: the hash has to be computed
    the same way whether or not the course name has been fetched yet, and the
    course name is not assignment content anyway.
    """
    starter, continuations = render(config, assignment, "")
    payload = json.dumps([starter, continuations], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def describe_changes(before: dict, after: dict) -> list[str]:
    labels = {
        "name": "Title",
        "due_at": "Due date",
        "points_possible": "Points",
        "unlock_at": "Opens",
        "lock_at": "Closes",
    }
    lines = []
    for field in NOTABLE_FIELDS:
        old, new = before.get(field), after.get(field)
        if old == new:
            continue
        if field.endswith("_at"):
            old_text = discord_timestamp(parse_time(old), "f") if parse_time(old) else "none"
            new_text = discord_timestamp(parse_time(new), "f") if parse_time(new) else "none"
        elif field == "points_possible":
            old_text, new_text = _pretty_points(old), _pretty_points(new)
        else:
            old_text = "none" if old is None else str(old)
            new_text = "none" if new is None else str(new)
        lines.append(f"• {labels[field]}: {old_text} → {new_text}")
    return lines


# --------------------------------------------------------------------------
# discord
# --------------------------------------------------------------------------


def create_thread(
    session: requests.Session, config: Config, assignment: dict, course_name: str
) -> dict:
    """Open the forum thread and return its state entry."""
    starter, continuations = render(config, assignment, course_name)

    payload = dict(starter)
    payload["thread_name"] = (assignment.get("name") or "Untitled assignment")[
        :DISCORD_THREAD_NAME_LIMIT
    ]
    if config.forum_tags:
        payload["applied_tags"] = config.forum_tags

    created = post_to_discord(session, config.webhook, payload, wait=True)
    if not created or not created.get("id"):
        raise RuntimeError(
            "Discord did not return the created thread. The webhook must point at a forum "
            "channel, and the channel must not require tags unless LAB_FORUM_TAGS is set."
        )

    # A forum thread's id is the id of its starter message, so this one value is
    # both the thread to reply into and the message to edit later.
    thread_id = str(created["id"])
    message_ids = [thread_id]

    for chunk in continuations:
        time.sleep(1)  # stay well inside the webhook rate limit
        reply = post_to_discord(
            session,
            config.webhook,
            {"content": chunk, "allowed_mentions": {"parse": []}},
            wait=True,
            thread_id=thread_id,
        )
        if reply and reply.get("id"):
            message_ids.append(str(reply["id"]))

    return {
        "state": "threaded",
        "thread_id": thread_id,
        "message_ids": message_ids,
        "created_at": iso(now()),
        "fingerprint": fingerprint(config, assignment),
        "notable": notable_of(assignment),
    }


def update_thread(
    session: requests.Session,
    config: Config,
    entry: dict,
    assignment: dict,
    course_name: str,
) -> dict:
    """Bring an existing thread back in line with the assignment."""
    starter, continuations = render(config, assignment, course_name)
    thread_id = entry["thread_id"]
    stored_ids = [str(i) for i in entry.get("message_ids") or [thread_id]]
    wanted = [starter] + [
        {"content": chunk, "allowed_mentions": {"parse": []}} for chunk in continuations
    ]

    message_ids = list(stored_ids)
    for index, payload in enumerate(wanted):
        if index < len(stored_ids):
            edit_discord_message(
                session, config.webhook, stored_ids[index], payload, thread_id=thread_id
            )
        else:
            # The description grew past what the existing messages can hold.
            reply = post_to_discord(
                session, config.webhook, payload, wait=True, thread_id=thread_id
            )
            if reply and reply.get("id"):
                message_ids.append(str(reply["id"]))

    # The description shrank: drop the messages that are now surplus.
    for stale in stored_ids[len(wanted) :]:
        delete_discord_message(session, config.webhook, stale, thread_id=thread_id)
        if stale in message_ids:
            message_ids.remove(stale)

    return {
        **entry,
        "message_ids": message_ids,
        "fingerprint": fingerprint(config, assignment),
        "notable": notable_of(assignment),
        "updated_at": iso(now()),
    }


def announce_changes(
    session: requests.Session, config: Config, thread_id: str, lines: list[str]
) -> None:
    prefix = f"{config.mention} " if config.mention else ""
    body = f"{prefix}📝 **This assignment changed**\n" + "\n".join(lines)
    post_to_discord(
        session,
        config.webhook,
        {"content": body[:2000], "allowed_mentions": build_allowed_mentions(config.mention)},
        thread_id=thread_id,
    )


# --------------------------------------------------------------------------
# preview
# --------------------------------------------------------------------------


SAMPLE_DESCRIPTION = """
<p>This is a formatting preview, not a real lab. Delete this thread when you're
done looking at it.</p>
<h2>Objectives</h2>
<ul>
  <li>Check that <strong>bold</strong>, <em>italic</em> and lists render</li>
  <li>Confirm the due date below shows in <em>your</em> timezone
    <ul><li>Discord renders it per-viewer, so it is never wrong</li></ul>
  </li>
</ul>
<ol><li>Numbered steps</li><li>survive too</li></ol>
<blockquote><p>Block quotes look like this.</p></blockquote>
<p>Smart punctuation survives &mdash; &ldquo;like this&rdquo; &hellip; 50&ndash;60%.</p>
"""


def sample_lab(config: Config) -> dict:
    due = now() + timedelta(days=7)
    return {
        "id": 0,
        "name": "Lab 0 — formatting preview",
        "description": SAMPLE_DESCRIPTION,
        "due_at": iso(due),
        "points_possible": 20.0,
        "submission_types": ["online_upload"],
        "published": True,
        "html_url": f"{config.canvas.url}/courses/{config.canvas.course_id}",
    }


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def describe(config: Config, assignment: dict) -> str:
    """What to call an assignment in the run log.

    Names are omitted by default because Actions logs are world-readable on a
    public repo, and an assignment name is course content.
    """
    if config.verbose:
        return repr(assignment.get("name") or "(unnamed)")
    return f"assignment {assignment.get('id')}"


def run(config: Config) -> None:
    if config.preview:
        discord = requests.Session()
        lab = sample_lab(config)
        create_thread(discord, config, lab, "Formatting preview")
        print("Preview thread created. The state file was not touched - delete the thread when done.")
        return

    canvas = canvas_session(config.canvas)

    previous = load_state()
    first_run = previous is None
    labs_state: dict[str, dict] = {} if first_run else previous

    labs = fetch_labs(canvas, config)
    print(f"{len(labs)} assignment(s) matched the lab pattern.")

    to_create: list[dict] = []
    to_update: list[tuple[dict, dict]] = []
    for lab in labs:
        entry = labs_state.get(str(lab["id"]))
        if entry is None:
            # Seeding on the very first run keeps the forum from filling with
            # threads for labs that are already over.
            if first_run and not config.backfill:
                continue
            to_create.append(lab)
        elif entry.get("state") == "seeded":
            if config.backfill:
                to_create.append(lab)
        elif entry.get("fingerprint") != fingerprint(config, lab):
            to_update.append((entry, lab))

    # Nothing above this point may touch state: a dry run leaves the file alone.
    if config.dry_run:
        for lab in to_create:
            print(f"  would create a thread for: {describe(config, lab)}")
        for entry, lab in to_update:
            print(f"  would update the thread for: {describe(config, lab)}")
        if first_run and not config.backfill:
            print(f"  would record {len(labs)} existing lab(s) without threading them")
        print("DRY_RUN is set - nothing was posted and the state file was left alone.")
        return

    if first_run and not config.backfill:
        for lab in labs:
            labs_state[str(lab["id"])] = {"state": "seeded", "seen_at": iso(now())}
            print(f"  recorded without threading: {describe(config, lab)}")
        save_state(labs_state)
        print(
            f"First run: recorded {len(labs)} existing lab(s) without creating threads. Labs added "
            "from here on get one automatically. (Re-run with backfill to thread these too.)"
        )
        return

    if not to_create and not to_update:
        if first_run:
            save_state(labs_state)  # so the next run isn't a "first run" too
        return

    discord = requests.Session()
    course_name = fetch_course_name(canvas, config.canvas)
    created = updated = 0
    try:
        for lab in to_create:
            labs_state[str(lab["id"])] = create_thread(discord, config, lab, course_name)
            created += 1
            print(f"  thread created: {describe(config, lab)}")
            time.sleep(1)

        for entry, lab in to_update:
            changes = describe_changes(entry.get("notable") or {}, notable_of(lab))
            labs_state[str(lab["id"])] = update_thread(discord, config, entry, lab, course_name)
            if changes:
                announce_changes(discord, config, entry["thread_id"], changes)
            updated += 1
            noun = "updated and announced" if changes else "quietly updated"
            print(f"  thread {noun}: {describe(config, lab)}")
            time.sleep(1)
    finally:
        # Persist whatever made it out, so a mid-run failure can't double-post.
        save_state(labs_state)
        print(f"Created {created} thread(s), updated {updated}.")


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
