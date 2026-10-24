"""CLI : `uv run python -m horspc build [--offline]`."""

import argparse
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from horspc import build
from horspc.http import HttpClient
from horspc.igdb import IgdbClient


def load_dotenv(path: Path) -> None:
    """Charge un fichier .env simple (CLE=valeur) sans écraser l'environnement existant."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="horspc", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("build", help="génère site/data.json")
    cmd.add_argument("--offline", action="store_true", help="sans IGDB : reprend data.json")
    cmd.add_argument("--games", type=Path, default=build.GAMES_PATH)
    cmd.add_argument("--prices", type=Path, default=build.PRICES_PATH)
    cmd.add_argument("--output", type=Path, default=build.OUTPUT_PATH)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    load_dotenv(build.ROOT / ".env")
    now = datetime.now(UTC).replace(microsecond=0)

    with HttpClient() as http:
        igdb = None
        if not args.offline:
            client_id = os.environ.get("IGDB_CLIENT_ID", "")
            client_secret = os.environ.get("IGDB_CLIENT_SECRET", "")
            if not client_id or not client_secret:
                print(
                    "IGDB_CLIENT_ID et IGDB_CLIENT_SECRET sont requis (ou utiliser --offline).",
                    file=sys.stderr,
                )
                return 2
            igdb = IgdbClient(client_id, client_secret, http)
        try:
            build.run(
                now=now,
                igdb=igdb,
                games_path=args.games,
                prices_path=args.prices,
                output_path=args.output,
            )
        except ValidationError as exc:
            print(f"Validation refusée, data.json non écrit :\n{exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
