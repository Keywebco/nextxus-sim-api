# Catalyst Memory

The Catalyst (and Pontus) in this service used to forget everything whenever
Render restarted or put the free service to sleep. Now the conversation is
backed up to a free GitHub repo all day long, and restored automatically
every time the service wakes up.

It follows the same idea as Roger's EchoCore / Echo 64 work: capture the
conversation, save a dated "echo" of each day, and reload it on startup. The
difference is where the echoes live. echocore.js wrote to a local folder.
Render's free disk is erased on every restart, so here the echoes go to a
GitHub repo instead.

## What it does

When the memory gets saved:

1. **Every new message.** Each exchange is saved right after the reply goes out.
2. **On a timer.** While the service is awake it checks every 30 minutes
   (`MEMORY_CHECKPOINT_MINUTES`). If anything is new, it writes the day's
   digest and the echo files.
3. **After every 12 new messages** (`MEMORY_DIGEST_EVERY`). Today's digest is
   refreshed so the Catalyst's summary stays current.
4. **On shutdown.** When Render stops the service for a deploy or restart,
   anything unsaved is flushed first.
5. **Five times a day from GitHub Actions** (7 AM, 11 AM, 3 PM, 7 PM and 11 PM
   Central Daylight Time). The workflow wakes the service if it's asleep. On
   wake, the service restores its memory, then the workflow asks it to back up
   the memory of the day. The first run after midnight closes out the previous
   day and folds it into long-term memory.

What gets restored on startup: the last 300 messages, the braid, the
long-term memory and the daily digests. Startup waits until all of this has
loaded before it answers the first message.

What the Catalyst sees on each message (it used to see only the last 6 messages):

- **LONG-TERM MEMORY**: a rolling summary of every earlier day, with sections
  for standing context, dated decisions, open items and next actions.
- **TODAY SO FAR**: a digest of today's conversation.
- **RECENT CONVERSATION**: the last 20 messages (`MEMORY_CONTEXT_MESSAGES`).

Where it is stored (a private repo is recommended, for example
`Keywebco/catalyst-memory`):

```
catalyst/state.json              machine state (recent messages, braid, summaries)
catalyst/echoes/2026-10-02.json  that day's full transcript + digest + EC-64 block
catalyst/echoes/2026-10-02.md    the same day, easy to read with a screen reader
```

Each daily file carries an `[EC64:v1;kind=summary;mode=…;transform=condensed]`
block, made with the Keywebco/echo-64 encoding (tested against its 12 golden
vectors). Any other Federation mind can decode that block byte for byte.
`transform=condensed` is set because the digest is a summary, not the
original text.

Safety rules built in:

- If the store can't be read at startup (GitHub outage, bad token), the
  service still answers, but it will **not** overwrite the saved memory. It
  retries on the timer and merges once it can read again.
- If two copies save at once, the GitHub file version (`sha`) catches it, and
  the two copies are merged instead of one overwriting the other.
- Backend error lines, such as a missing LLM key, are never fed back into
  memory.
- Tokens are only sent in request headers. They are never logged, returned
  by any endpoint, or written to the memory repo.

## New endpoints

| Endpoint | Auth | What it does |
| --- | --- | --- |
| `GET /memory/status` | none | Counts, timestamps, errors. No conversation text. |
| `POST /memory/checkpoint` | `Authorization: Bearer <MEMORY_CHECKPOINT_TOKEN>` | Back up the memory of the day now. Used by the cron. |
| `GET /health` | none | Now includes a small `memory` block. |

## Environment variables (names only)

| Name | Required? | Default | Meaning |
| --- | --- | --- | --- |
| `MEMORY_GITHUB_TOKEN` | **yes, for durable memory** | — | Fine-grained GitHub token with **Contents: Read and write** on the memory repo **only**. Secret. |
| `MEMORY_GITHUB_REPO` | recommended | `Keywebco/catalyst-memory` | Repo that holds the memory. |
| `MEMORY_CHECKPOINT_TOKEN` | **yes, for the cron** | — | Random secret. The same value goes in the GitHub Actions secret. |
| `MEMORY_GITHUB_BRANCH` | no | `main` | Branch in the memory repo. |
| `MEMORY_GITHUB_PATH` | no | `catalyst` | Folder inside the memory repo. |
| `MEMORY_STORE` | no | `auto` | `auto` uses GitHub if a token is set, otherwise a local file. Also `github`, `file`, `off`. |
| `MEMORY_FILE_DIR` | no | `/tmp/catalyst-memory` | Local-file store (development only; erased on Render). |
| `MEMORY_CHECKPOINT_MINUTES` | no | `30` | In-process timer interval. |
| `MEMORY_DIGEST_EVERY` | no | `12` | Refresh today's digest after this many new messages. |
| `MEMORY_CONTEXT_MESSAGES` | no | `20` | Recent messages shown to the persona. |
| `MEMORY_MAX_MESSAGES` | no | `300` | Messages kept in `state.json`. Every message stays in the daily echo files. |
| `MEMORY_SUMMARY_MODE` | no | `llm` | `llm` writes digests with the existing model. `extractive` makes no extra LLM calls. |
| `MEMORY_TIMEZONE` | no | `America/Chicago` | Sets where one day ends and the next begins. |
| `LLM_BASE_URL` | no | unused | Leftover from an earlier draft. Current replies and digests use MiMo, then DeepSeek. |
| `MEMORY_GITHUB_API` | no | `https://api.github.com` | Override for testing only. |

The GitHub Actions workflow needs the repo secret `MEMORY_CHECKPOINT_TOKEN`.
The repo variable `SIM_API_URL` is optional and defaults to
https://nextxus-sim-api.onrender.com.

## Deploy steps (plain English)

Steps marked **Roger** need Roger's own accounts. Nothing here costs money.

1. **Roger: create the memory repo.** On GitHub, create a new **private**
   repository named `catalyst-memory` under Keywebco. Tick "Add a README" so
   the `main` branch exists. Private repos are free.
   *Why a separate repo?* If memory were saved into `nextxus-sim-api` itself,
   every save would be a commit, and every commit would make Render redeploy
   the service.
2. **Roger: create a token for it.** GitHub, then Settings, Developer
   settings, Fine-grained personal access tokens, Generate new token.
   - Resource owner: Keywebco. Repository access: *Only select repositories*,
     then `catalyst-memory`.
   - Permissions: *Contents: Read and write*. Nothing else is needed (Metadata:
     Read is added automatically).
   - Pick an expiration and set a reminder. When the token expires, saving
     stops and `/memory/status` shows the error.
3. **Roger: make a checkpoint password.** Any long random string, for example
   from a password manager or `openssl rand -hex 32`.
4. **Roger: set the env vars on Render.** Dashboard, then `nextxus-sim-api`,
   then Environment. Add `MEMORY_GITHUB_TOKEN` (the token from step 2),
   `MEMORY_GITHUB_REPO` = `Keywebco/catalyst-memory`, and
   `MEMORY_CHECKPOINT_TOKEN` (the string from step 3). Save. The
   `render.yaml` in this patch lists the same names, but if the service
   wasn't created from a Blueprint, the dashboard is where they actually get
   set.
5. **Roger: add the Actions secret.** GitHub, then `Keywebco/nextxus-sim-api`,
   Settings, Secrets and variables, Actions, New repository secret. Name:
   `MEMORY_CHECKPOINT_TOKEN`. Value: the same string as step 3.
6. **Roger approves, then apply the patch** to `nextxus-sim-api` and push to
   `main`: `git apply catalyst-memory.patch`, then commit and push. Render
   redeploys automatically if auto-deploy is on. If not, click *Manual Deploy*.
7. **Check that it works.**
   - Open https://nextxus-sim-api.onrender.com/memory/status. You should see
     `"durable": true` and `"load_ok": true`.
   - Send the Catalyst a message. Within a few seconds,
     `catalyst/state.json` appears in the memory repo.
   - GitHub, then Actions, *Catalyst memory checkpoint*, *Run workflow*. It
     should finish green, and `catalyst/echoes/<today>.md` appears.
   - In Render, click *Restart service*. Then ask the Catalyst about
     something from before the restart.

## Things to know

- **Free-tier hours.** Each cron run wakes the service, and it stays up about
  15 minutes. Five runs a day is roughly 40 hours a month, out of the 750 free
  instance hours that all free services in the Render workspace share.
- **GitHub turns off scheduled workflows** in *public* repos after 60 days
  without a commit. If that happens, re-enable the workflow from the Actions
  tab, or move the workflow file into the private memory repo. Memory saves
  are commits, so that repo never goes quiet. The workflow then uses about
  150–300 of the free 2,000 private-repo Action minutes a month.
- **Digest cost.** In `llm` mode, digests and the long-term merge use the
  same MiMo or DeepSeek key and model as the replies. That's a few extra calls a day.
  Set `MEMORY_SUMMARY_MODE=extractive` to make no extra LLM calls.
- **No sender auth.** `/messages` still has no sender authentication, so
  anyone can post, and what they post is now remembered. Adding a shared
  secret to `/messages` (gap analysis item C3) is recommended as a follow-up.
- **Small crash window.** A crash in the second or so between a reply and its
  background save could lose that one exchange. The SIGTERM, timer and cron
  paths cover normal restarts and sleep.

## Tests

```
pip install -r requirements.txt pytest
python -m pytest tests -q          # unit tests, no network
python tests/e2e_restart.py        # real app + mock GitHub + mock LLM, restart/kill/outage
```
