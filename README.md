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

Habits, dailies, rewards, priority, attribute and reminders are
intentionally not synced — they have no equivalent on the Google side.
Tags *are* used (but not synced as content): in multi-list mode each
configured Google Tasks list is paired with a Habitica tag, and tasks
are routed between sides based on which list-tag they carry.

## How it works

1. Every `sync_interval_seconds`, for each pair the engine pulls all
   Habitica todos and the delta of Google tasks since the last successful
   sync (`updatedMin` cursor with a 5-minute overlap to absorb clock skew).
2. Tasks are paired in a SQLite mapping table by their native IDs. New
   tasks on either side are created on the other; deleted tasks are
   tombstoned so they aren't recreated next cycle.
3. When both sides changed since the last sync, the side with the later
   `updatedAt` wins.
4. On the very first sync (empty mapping table) tasks with matching
   titles on both sides are *adopted* into a single mapping rather than
   duplicated, so existing users with content on both sides don't end up
   with two copies of everything.

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

## Syncing multiple Google Tasks lists per Habitica account

To sync more than one Google list into the same Habitica account, list
them under `google.tasklists` instead of the single-list
`tasklist_id` / `tasklist_title`:

```yaml
google:
  credentials_file: /tokens/credentials.json
  token_file: /tokens/alice.json
  tasklists:
    - title: "Personal"   # default list (first entry); tag defaults to "Personal"
    - title: "Work"
    - title: "Shopping"
      tag: "errand"        # tag may differ from the list title
```

Routing rules:

- Each list pairs with one Habitica tag. Tags (and lists) are created on
  first sync if they don't exist yet. Existing Habitica tags are matched
  case-insensitively.
- A new Google task is created in Habitica with the source list's tag
  attached.
- A new Habitica task ends up in the Google list whose tag is on the
  task. If none of the configured tags are present, the task lands in
  the first configured list and that list's tag is added back to the
  Habitica task so the next cycle is deterministic.
- Changing the tag on an existing Habitica task migrates the matching
  Google task to the new list (delete on the old, recreate on the new —
  Google Tasks has no cross-list move).
- Adoption-by-title on first sync only collapses tasks within the same
  configured list, so a "Personal" Habitica todo won't be merged into a
  same-named "Work" Google task by mistake.

## Environment variables in config

The config file supports `${VAR}` and `${VAR:-default}` interpolation.
A bare `${VAR}` reference for an unset variable raises an error at
startup — use the `:-` form to opt into "may be empty". This keeps a
forgotten `export` from silently producing blank credentials.

```yaml
habitica:
  user_id: "${ALICE_HABITICA_USER_ID}"           # required
  api_token: "${ALICE_HABITICA_API_TOKEN}"       # required
  app_name: "${SYNC_APP_NAME:-habitica-tasks-sync}"  # optional
```

## Development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .[dev]
.venv/bin/pytest
```

The test suite uses in-memory stub clients to exercise the full sync
algorithm — no network calls.
