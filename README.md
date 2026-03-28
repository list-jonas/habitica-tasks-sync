# habitica-tasks-sync

Bidirectional sync between [Habitica](https://habitica.com/) todos and
[Google Tasks](https://tasks.google.com/), runnable as a single Docker
container. Designed to support more than one person — each `pair` in the
config couples one Habitica account with one Google Tasks list.

## What it syncs

| Field | Habitica | Google Tasks |
|---|---|---|
| Title | `text` | `title` |
| Notes | `notes` | `notes` (with checklist appended as `[x] item` lines) |
| Due date (date only) | `date` | `due` |
| Completion | scored via `/score/up` or `/score/down` | `status` |
| Checklist | `checklist[]` | flattened into `notes` |
| Deletes | propagated both ways | propagated both ways |

Habits, dailies, rewards, tags, priority, attribute and reminders are
intentionally not synced — they have no equivalent on the Google side.

## How it works

1. Every `sync_interval_seconds`, for each pair the engine pulls all
   Habitica todos and the delta of Google tasks since the last successful
   sync (`updatedMin` cursor with a 5-minute overlap to absorb clock skew).
2. Tasks are paired in a SQLite mapping table by their native IDs. New
   tasks on either side are created on the other; deleted tasks are
   tombstoned so they aren't recreated next cycle.
3. When both sides changed since the last sync, the side with the later
   `updatedAt` wins.

## Setup

### 1. Habitica credentials

Each user logs into Habitica and copies their **User ID** and **API
Token** from *Settings → Site Data*. Both are UUIDs. The container also
needs a developer "x-client" UUID — create a free Habitica account just
for that purpose and use *its* User ID in `user_agent.uuid`.

### 2. Google OAuth client

Create an OAuth 2.0 Client ID in
[Google Cloud Console](https://console.cloud.google.com/apis/credentials):

- Application type: **Desktop app**
- Enable the **Google Tasks API** for the project.
- Add your Google account as a test user under the OAuth consent screen
  (or publish the app).
- Download the JSON; save it as `tokens/credentials.json`.

### 3. Mint per-user OAuth tokens (once, on a machine with a browser)

```bash
pip install -r requirements.txt
python -m habitica_tasks_sync.auth_helper \
    --client tokens/credentials.json \
    --token  tokens/alice.json
# Repeat for bob.json, charlie.json, etc.
```

A browser window opens for each Google account. The resulting `token.json`
contains a refresh token good until the user revokes access.

### 4. Configure

```bash
cp config.example.yaml config.yaml
$EDITOR config.yaml
```

Set the `user_agent.uuid`, the per-pair Habitica credentials (or export
the env vars they reference), and the path to each pair's token file.

### 5. Run

```bash
docker compose up -d
docker compose logs -f
```

Or directly:

```bash
HABITICA_SYNC_CONFIG=./config.yaml python -m habitica_tasks_sync
```

## File layout once running

```
.
├── config.yaml                 # your config (gitignored)
├── data/
│   └── sync.sqlite3            # mappings + tombstones
└── tokens/
    ├── credentials.json        # shared OAuth client secret
    ├── alice.json              # alice's refresh token
    └── bob.json                # bob's refresh token
```

## Operational notes

- **Rate limits.** Habitica allows 30 req/min per user+IP. Each pair makes
  ~3 calls per cycle plus one per task mutation. The 300-second default
  interval keeps headroom.
- **Cold start.** The first cycle full-fetches both sides and creates
  every missing task on the other. Expect it to take longer.
- **Conflict resolution.** Last-writer-wins by API `updatedAt`. The losing
  side's content is overwritten.
- **Deletions are permanent.** Habitica has no soft-delete. If you delete
  on one side, it's gone on both.
- **Checklist round-tripping.** Items are pushed to Google as bullet
  lines in notes (read-only there). Habitica remains the source of truth
  for checklist structure.

## Adding a third (or fourth) user

Append another entry under `pairs:` and mint another token JSON. No code
changes needed.
