# Canvas → Discord

Two GitHub Actions that mirror a Canvas course into Discord:

| Workflow | Does | Runs |
| --- | --- | --- |
| **Canvas Announcements** (`canvas.yml`) | posts new announcements to a text channel | every 15 min |
| **Lab Threads** (`labs.yml`) | opens a forum thread per lab assignment, to reply under with your work | every 30 min |

Both convert Canvas's HTML to markdown, so bold and italic text, headings, lists, block
quotes and links survive instead of arriving as raw `<p>` tags. Shared plumbing lives in
[common.py](common.py); each bot keeps its own state file so they never interfere.

## Setup

Everything is configured through repository secrets — no file in this repo needs editing.

### 1. Canvas access token

Canvas → **Account → Settings → Approved Integrations → + New Access Token**. Copy the
token; Canvas only shows it once. It inherits your own permissions, so it can read any
course you're enrolled in.

### 2. Course ID

The number in the course URL: `https://canvas.ucmerced.edu/courses/12345` → `12345`.

### 3. Discord webhooks (one per channel)

A Discord webhook can only post to the **one channel it was created on**, so each bot needs
its own. In each target channel — the announcements text channel, and the lab forum channel —
go to **Edit Channel → Integrations → Webhooks → New Webhook**, then **Copy Webhook URL**.
Anyone holding that URL can post to the channel, so keep both secret.

Skip the forum one if you only want announcements; `labs.yml` just fails cleanly until it's
set.

### 4. Your Discord user ID (for the ping)

Discord → **User Settings → Advanced → Developer Mode** on, then right-click your name →
**Copy User ID**.

### 5. Add the secrets

Repository **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Required | Value |
| --- | --- | --- |
| `CANVAS_TOKEN` | yes | The token from step 1 |
| `COURSE_ID` | yes | The number from step 2 |
| `DISCORD_WEBHOOK_URL` | yes | Announcements channel webhook, from step 3 |
| `DISCORD_FORUM_WEBHOOK_URL` | for labs | Lab forum channel webhook, from step 3 |
| `DISCORD_MENTION` | no | Who to ping for announcements — see below |
| `DISCORD_LAB_MENTION` | no | Who to ping for labs. Unset means no ping; a new forum thread already notifies channel followers. |
| `CANVAS_URL` | no | Defaults to `https://canvas.ucmerced.edu` |

Optional repository **variables** (same screen, "Variables" tab):

| Variable | Value |
| --- | --- |
| `COURSE_WIDE_ONLY` | `true` to post only course-wide announcements, skipping ones sent to specific sections. Defaults to off. |
| `LAB_PATTERN` | Which assignments count as labs. A regular expression, case-insensitive, matched against the assignment name. Defaults to `\bLab\b`. |
| `LAB_FORUM_TAGS` | Comma-separated forum tag ids to apply to new threads. Only needed if the forum requires tags. |

`DISCORD_MENTION` accepts a bare user ID, `<@user-id>`, a role as `<@&role-id>`, or
`@everyone`. Leave it unset for no ping at all.

**To go from pinging just yourself to pinging the channel, change `DISCORD_MENTION` to
`@everyone`.** Nothing else changes.

Only the mention configured there can ping. An `@everyone` written inside an announcement by
an instructor stays inert, because the ping list is built from `DISCORD_MENTION` alone.

## Lab threads

`labs.yml` watches the course's **assignments**, keeps the ones whose name matches
`LAB_PATTERN` (default: contains the word "Lab"), and gives each one a forum thread you can
reply under with your work. Unpublished assignments are ignored.

The thread's **first post is just the information card** — due date, points, submission types,
and a link back to Canvas. Due dates render as Discord timestamps, so everyone sees them in
their own timezone.

The **description follows as replies**, so the top of the thread stays scannable instead of
opening with a wall of text. Discord caps a message at 2000 characters, so a long description
is split across several replies at paragraph boundaries. A lab with no description just gets
the card.

**When an assignment changes**, the posts are edited in place so the thread is never stale.
If something worth noticing moved — the due date, points, title, or open/close dates — a reply
also says what changed:

> 📝 **This assignment changed**
> • Due date: Fri 3 Oct → Mon 6 Oct
> • Points: 20 → 30

An instructor fixing a typo in the description edits silently instead, so the thread doesn't
nag about nothing.

One limit: a webhook can't rename an existing forum thread, so if the assignment's title
changes, the thread keeps its original name. The card's title and the change reply both show
the new one.

**The first run creates nothing.** It records the labs that already exist so the forum doesn't
fill up with threads for labs that are already over. Anything added later gets a thread
automatically. To thread the existing ones too, run the workflow manually with **backfill**
ticked.

**Forum setup note:** if the forum has "Require people to select tags when posting" enabled,
webhook posts without tags are rejected. Either turn that off or set `LAB_FORUM_TAGS`.

## Colour coding

The embed's left sidebar is coloured by a keyword in the announcement title:

| Title contains | Colour |
| --- | --- |
| `CRITICAL` | red |
| `IMPORTANT` | orange |
| `REMINDER` | light blue |
| anything else | gray |

Matching ignores case and needs a whole word, so `[CRITICAL]` and `Critical:` both hit but
"critically acclaimed" doesn't. If a title has more than one, the most severe wins.

## Who an announcement went to

Canvas announcements go to a whole course or to particular sections of it. A message meant
for one student is an Inbox *conversation*, which lives behind a different API that this
script never calls — so a private message cannot end up in the channel.

That leaves section-scoped announcements. They are posted by default. Set the
`COURSE_WIDE_ONLY` repository variable to `true` to skip them and post only course-wide
ones; skipped announcements are named once in the run log and then not reconsidered.

## Seeing it in Discord before you rely on it

**Actions → Canvas Announcements → Run workflow**, set **preview**, and the run posts to
Discord immediately so you can look at the real formatting:

### Lab Threads

**Actions → Lab Threads → Run workflow**, set **preview**:

- **`sample`** — opens a thread for a made-up lab. Needs only `DISCORD_FORUM_WEBHOOK_URL`, so
  it works before the Canvas secrets are set.
- **`real`** — opens a thread for an actual assignment from your course. Put the lab in
  **preview_lab** (a name like `Lab 1`, or the numeric assignment id); leave it empty to take
  the earliest-due one. This is the way to confirm the bot handles your instructor's real
  formatting.

Add **dry_run** to either one to print what *would* be posted — thread name length, the due
date and points, and how many messages the description needs — without creating a thread. Add
**verbose** on top of that to print the text itself. Without **verbose** nothing that counts
as course content is logged, since Actions logs are public.

Previews create nothing in the state file, so previewing `Lab 1` does **not** stop it getting
its real thread later. Delete the throwaway thread when you're done.

### Canvas Announcements

- **`sample`** — posts a made-up announcement that exercises every supported construct:
  headings, bold/italic, nested and numbered lists, links, block quotes, smart punctuation,
  and an inert `@everyone`. Needs only `DISCORD_WEBHOOK_URL`, so it works before the Canvas
  secrets are set.
- **`latest`** — posts the most recent real announcement from your course, so you can see
  how your instructor's actual formatting comes through.

Neither one touches `sent_announcements.json`, so you can run a preview as many times as you
like and the scheduled runs still behave as if it never happened. (`dry_run`, by contrast,
posts nothing at all — it just prints what *would* go out.)

## First run

Run it manually once: **Actions → Canvas Announcements → Run workflow**.

The first run **records** the announcements already in the window instead of posting them,
so a week of backlog doesn't land in the channel at once. Everything that appears after that
gets posted. To post the backlog anyway, tick **post_on_first_run**.

The manual run also offers **dry_run** — print what would be posted and change nothing. It's
the fastest way to confirm the secrets are right.

## How duplicates are avoided

Every posted announcement ID is recorded in `sent_announcements.json`, which the workflow
commits back to the repo. Because of that:

- the 7-day lookback window can safely overlap between runs;
- a skipped or delayed run gets caught up by the next one;
- if a run dies halfway, the announcements that did go out are still recorded, so the retry
  doesn't repost them.

IDs older than 90 days are pruned so the file doesn't grow forever.

## Notes

- **Scheduled workflows only run on the default branch**, so this has to be on `main`.
- GitHub's cron is best-effort and often 5–20 minutes late. Harmless here — nothing is
  missed, it just arrives later.
- GitHub disables scheduled workflows after 60 days of repository inactivity. The state
  commits count as activity, so this keeps itself alive while announcements keep coming.
- **Nothing is missed if a run is skipped.** Each run looks back 7 days and skips whatever
  is already in the state file, so the schedule only controls *how late* an announcement
  arrives, never *whether* it arrives. Widening the cron costs latency, not coverage.
- **The cron is offset** (`3,18,33,48`) rather than `*/15`. GitHub queues every repo's
  `*/15` job at :00/:15/:30/:45, and runs caught in that stampede are delayed the most.
- **Actions minutes are free** because this repo is public. On a private repo GitHub bills
  a minimum of one minute per run, which at this cadence would be ~2,880 minutes/month
  against a 500 (Free) or 3,000 (Pro) allowance.
- **Run logs are public**, since Actions logs follow repository visibility. Announcement
  titles are therefore kept out of the logs — runs identify announcements by numeric ID.
  Tick **verbose** on a manual run to log titles when you need to read them back, and
  remember that output is public too.

## Privacy

Repository secrets stay secret in a public repo, and GitHub masks them in logs. Two things
are visible that wouldn't be otherwise:

- **Workflow run logs.** Hence the ID-only logging above. If you ever run with **verbose**,
  delete that run afterwards (Actions → the run → ⋯ → Delete workflow run).
- **`sent_announcements.json`** and **`lab_threads.json`.** They hold Canvas IDs, Discord
  message IDs and timestamps — numbers only, no titles or bodies, and not resolvable without
  access to the course.

`COURSE_ID` is a secret, so the repo doesn't reveal which course this watches. The Canvas
hostname does appear as the default for `CANVAS_URL` in the workflow; set `CANVAS_URL` as a
secret and drop that default if you'd rather it didn't.

## Running locally

```bash
pip install -r requirements.txt

export CANVAS_URL="https://canvas.ucmerced.edu"
export CANVAS_TOKEN="..."
export COURSE_ID="12345"
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
export DISCORD_MENTION="<@your-user-id>"

# announcements: print what would be posted, without posting
DRY_RUN=true python canvas_to_discord.py

# announcements: post a formatting sample (needs only the webhook)
PREVIEW=sample python canvas_to_discord.py
```

For the lab threads bot:

```bash
export DISCORD_FORUM_WEBHOOK_URL="https://discord.com/api/webhooks/..."

# show which assignments match, and what would happen to each
DRY_RUN=true VERBOSE=true python canvas_labs.py

# open a throwaway sample thread (needs only the forum webhook)
PREVIEW=sample python canvas_labs.py
```

## Troubleshooting

Failures print one clear line and fail the step.

| Message | Fix |
| --- | --- |
| `CANVAS_TOKEN is not set (or is empty)` | Secret missing or misspelled |
| `Canvas rejected CANVAS_TOKEN (401)` | Token expired or revoked — make a new one |
| `Canvas returned 403` | That account can't read the course |
| `Canvas returned 404` | Wrong `CANVAS_URL` or `COURSE_ID` |
| `COURSE_ID must be the numeric course id` | Use `12345`, not the course name |
| `Discord rejected the webhook (401/403/404)` | Webhook deleted or URL wrong |
| `DISCORD_FORUM_WEBHOOK_URL is not set` | The lab bot needs its own webhook, on the forum channel |
| `Discord did not return the created thread` | The forum webhook points at a normal text channel, or the forum requires tags and `LAB_FORUM_TAGS` isn't set |
| `LAB_PATTERN is not a valid regular expression` | Fix the pattern, or delete the variable to fall back to `\bLab\b` |
| Lab threads never appear | Expected until a *new* lab is posted — the first run only records existing ones. Run manually with **backfill** to thread them. |
| Push fails in "Commit announcement state" | **Settings → Actions → General → Workflow permissions** → "Read and write permissions" |

Secrets appear as `***` in logs, including inside these messages. If you need to see the
course ID or Canvas URL while debugging, set them as repository *variables* instead of
secrets — the workflow reads either.
