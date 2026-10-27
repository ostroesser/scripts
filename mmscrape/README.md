# mmscrape — make Mattermost channels searchable with docrag

Downloads whole Mattermost channels through the API (read-only), keeps a local copy that
updates incrementally, and exports **one record per thread** for [docrag](../docrag/).
You then search docs and chat together and paste the result into Copilot.

## Setup (WSL)

Use the same venv as docrag (only `requests` is extra):

```bash
source ~/docrag-venv/bin/activate
pip install requests
```

### Authentication: the session cookie

With a SAML login and no personal access tokens, the scraper uses your browser session:

1. Open Mattermost in **Edge** (same server URL as the desktop app).
2. **F12** → **Application** → **Cookies** → the Mattermost domain → copy **`MMAUTHTOKEN`**.
3. In WSL:

```bash
export MM_URL='https://<your-mattermost-host>'
export MM_TOKEN='<MMAUTHTOKEN value>'
```

The cookie is your login: keep it out of files and the repo. When it expires, the scraper stops
with "HTTP 401 … copy a fresh MMAUTHTOKEN", and you repeat step 2.

**TLS / certificate error?** WSL doesn't trust the corporate CAs that Windows trusts. Run
`bash ../wsl-certs/import-windows-certs.sh` once, then open a new terminal. Don't disable verification.

## Use it

```bash
python mmscrape.py channels                                # what you can sync (team/channel, size)
python mmscrape.py sync team/team-support --since 2025-01-01   # first run: history since that date
python mmscrape.py sync team/team-support                      # later runs: only what changed
python mmscrape.py sync team/team-support --since 2023-01-01   # go further back: fetches the missing years
python mmscrape.py sync https://<host>/team/channels/team-support other-team/other-channel

python ../docrag/docrag.py ingest ../path/to/doc-export/*.txt mm_export/*.jsonl ../confluencescrape/confluence_export/*.txt
python ../docrag/docrag.py ask "deploy stuck on the deploy stage" --source "Team Support" --since 2026-01-01 --clip
```

- `--since` sets how far back the history goes. Busy support channels go back years, and old
  answers are often stale. Without it, the first sync gets everything. Passing an earlier date
  later fetches only the missing part; the same or a later date changes nothing.
- Each run reports what actually changed (`N new, N edited, N deleted`) and how far back the
  local copy goes. "Posts fetched" can be higher than "new": the API also returns posts that
  are already stored, and system messages (joins, leaves) are fetched but not kept.
- `--full` throws away the local copy and downloads again.
- `render` rebuilds the exports from the local copy without touching the network.
- Re-running `docrag ingest` is cheap: only new or changed threads are embedded again.

## What gets stored

| Where | What |
|---|---|
| `mm_data/<channel-id>.json` | raw posts (trimmed), the sync state |
| `mm_data/users.json` | user id → display name cache |
| `mm_export/<team>__<channel>.jsonl` | one record per thread, for docrag |

Each record:

```json
{"id": "…", "title": "Team Support · 2026-10-21 · Deploy stuck on the deploy stage…",
 "url": "https://<host>/team/pl/<post-id>", "source": "Team Support", "channel": "team/team-support",
 "date": "2026-10-22", "started": "2026-10-21", "replies": 3,
 "text": "Team Support · thread started 2026-10-21 by … · 3 replies\n\n**Name** · 2026-10-21 11:07\n…"}
```

- A thread is the root message plus all its replies, in order. A message without replies is its
  own record.
- `date` is the **last activity**, so `--since` keeps old threads that got a recent answer.
- Kept: edits (marked "(edited)"), bot attachments (text and fields), file names. Files
  themselves are not downloaded. System messages (joins, leaves) and deleted posts are dropped.
- Times are shown in your local time zone (override with `MM_TZ`, e.g. `Europe/Paris`).

## How syncing works

- First run: walks the channel from newest to oldest, 200 posts per call, with the `before`
  cursor. Rate limits (HTTP 429) are respected automatically.
- Later runs: fetch pages until reaching posts already stored, then one `since` call to catch
  edits and deletions of older posts.
- Going further back: continues from the oldest stored post down to the new `--since` date,
  without fetching recent history again. (A local copy made before this feature existed gets one
  walk from the newest post instead.)
- Server errors (HTTP 5xx, network drops) are retried 4 times with increasing waits.
- Replies whose thread started before `--since` get their whole thread fetched, so no thread is
  left without its question.

Only GET requests are made, plus `POST /users/ids` to look up names.

## Tests

```bash
python -m unittest test_mmscrape -v     # runs against a fake Mattermost API, no server needed
```

Keep in mind that channel exports contain colleagues' messages. Check your data-handling rules
before syncing large channels to your laptop.
