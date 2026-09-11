# Canvas → Discord announcements

A GitHub Action that checks a Canvas course for new announcements every 30 minutes and
posts them to a Discord channel through a webhook.

Announcements arrive as Discord embeds: the Canvas HTML is converted to markdown, so bold
and italic text, headings, lists, blockquotes and links survive instead of showing up as raw
`<p>` tags. The embed title links back to Canvas.

## Setup

Everything is configured through repository secrets — no file in this repo needs editing.

### 1. Canvas access token

Canvas → **Account → Settings → Approved Integrations → + New Access Token**. Copy the
token; Canvas only shows it once. It inherits your own permissions, so it can read any
course you're enrolled in.

### 2. Course ID

The number in the course URL: `https://canvas.ucmerced.edu/courses/12345` → `12345`.

### 3. Discord webhook

In the target channel: **Edit Channel → Integrations → Webhooks → New Webhook**, then
**Copy Webhook URL**. Anyone holding that URL can post to the channel, so keep it secret.

### 4. Your Discord user ID (for the ping)

Discord → **User Settings → Advanced → Developer Mode** on, then right-click your name →
**Copy User ID**.

### 5. Add the secrets

Repository **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Required | Value |
| --- | --- | --- |
| `CANVAS_TOKEN` | yes | The token from step 1 |
| `COURSE_ID` | yes | The number from step 2 |
| `DISCORD_WEBHOOK_URL` | yes | The URL from step 3 |
| `DISCORD_MENTION` | no | Who to ping — see below |
| `CANVAS_URL` | no | Defaults to `https://canvas.ucmerced.edu` |

`DISCORD_MENTION` accepts a bare user ID, `<@user-id>`, a role as `<@&role-id>`, or
`@everyone`. Leave it unset for no ping at all.

**To go from pinging just yourself to pinging the channel, change `DISCORD_MENTION` to
`@everyone`.** Nothing else changes.

Only the mention configured there can ping. An `@everyone` written inside an announcement by
an instructor stays inert, because the ping list is built from `DISCORD_MENTION` alone.

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
- On a **private** repo a 30-minute schedule is roughly 1,400+ Actions minutes a month,
  which exceeds the free tier. Either keep the repo public or widen the cron in
  [.github/workflows/canvas.yml](.github/workflows/canvas.yml).

## Running locally

```bash
pip install -r requirements.txt

export CANVAS_URL="https://canvas.ucmerced.edu"
export CANVAS_TOKEN="..."
export COURSE_ID="12345"
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
export DISCORD_MENTION="<@your-user-id>"

DRY_RUN=true python canvas_to_discord.py
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
| Push fails in "Commit announcement state" | **Settings → Actions → General → Workflow permissions** → "Read and write permissions" |

Secrets appear as `***` in logs, including inside these messages. If you need to see the
course ID or Canvas URL while debugging, set them as repository *variables* instead of
secrets — the workflow reads either.
