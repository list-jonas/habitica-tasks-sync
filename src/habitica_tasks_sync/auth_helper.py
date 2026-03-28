"""One-shot CLI to mint a Google OAuth token for a given pair.

Run on a machine with a browser. Produces a token.json that is then mounted
into the container. Refresh tokens are long-lived; re-run only when revoked.

    python -m habitica_tasks_sync.auth_helper \
        --client ./credentials.json \
        --token  ./tokens/alice.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from .google_tasks import SCOPES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="habitica-tasks-sync-auth",
        description="Mint a Google Tasks OAuth token (run once per Google account on a machine with a browser).",
    )
    parser.add_argument("--client", required=True, type=Path,
                        help="OAuth client JSON downloaded from Google Cloud Console (Desktop app type).")
    parser.add_argument("--token", required=True, type=Path,
                        help="Where to write the resulting token JSON.")
    parser.add_argument("--port", type=int, default=0,
                        help="Local port for the OAuth callback (0 = pick a free one).")
    args = parser.parse_args(argv)

    if not args.client.exists():
        print(f"error: client secret file not found: {args.client}", file=sys.stderr)
        return 2

    args.token.parent.mkdir(parents=True, exist_ok=True)

    flow = InstalledAppFlow.from_client_secrets_file(str(args.client), SCOPES)
    creds = flow.run_local_server(
        port=args.port,
        prompt="consent",
        access_type="offline",
        open_browser=True,
        success_message="Authorized. You can close this tab.",
    )
    args.token.write_text(creds.to_json(), encoding="utf-8")
    try:
        args.token.chmod(0o600)
    except OSError:
        pass
    print(f"Wrote {args.token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
