"""CLI : `uv run python -m horspc build [--offline] | stale | price add`."""

import argparse
import logging
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import ValidationError

from horspc import build, price_add, stale
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


def igdb_credentials() -> tuple[str, str] | None:
    """Identifiants IGDB lus dans l'environnement (ou .env), None s'ils manquent."""
    load_dotenv(build.ROOT / ".env")
    client_id = os.environ.get("IGDB_CLIENT_ID", "")
    client_secret = os.environ.get("IGDB_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None
    return client_id, client_secret


def cmd_build(args: argparse.Namespace) -> int:
    now = datetime.now(UTC).replace(microsecond=0)
    with HttpClient() as http:
        igdb = None
        if not args.offline:
            credentials = igdb_credentials()
            if credentials is None:
                print(
                    "IGDB_CLIENT_ID et IGDB_CLIENT_SECRET sont requis (ou utiliser --offline).",
                    file=sys.stderr,
                )
                return 2
            igdb = IgdbClient(*credentials, http)
        try:
            build.run(
                now=now,
                igdb=igdb,
                games_path=args.games,
                prices_path=args.prices,
                output_path=args.output,
                http=None if igdb is None else http,
            )
        except ValidationError as exc:
            print(f"Validation refusée, data.json non écrit :\n{exc}", file=sys.stderr)
            return 1
        except build.PreviousDataError as exc:
            print(f"data.json précédent refusé, rien n'est écrit : {exc}", file=sys.stderr)
            return 1
    return 0


def cmd_stale(args: argparse.Namespace) -> int:
    today = date.today()
    try:
        catalog = build.load_catalog(args.games, args.prices, datetime.now(UTC))
    except ValidationError as exc:
        print(f"Fichiers YAML invalides :\n{exc}", file=sys.stderr)
        return 1
    print(stale.format_report(stale.stale_games(catalog, today), today))
    return 0


def cmd_price_add(args: argparse.Namespace) -> int:
    return price_add.price_add(args.games, args.prices, date.today())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="horspc", description="Outils du catalogue HorsPC.")
    sub = parser.add_subparsers(dest="command", required=True)

    cmd = sub.add_parser("build", help="génère site/data.json")
    cmd.add_argument("--offline", action="store_true", help="sans IGDB : reprend data.json")
    cmd.add_argument("--output", type=Path, default=build.OUTPUT_PATH)
    cmd.set_defaults(func=cmd_build)

    cmd = sub.add_parser("stale", help="liste les jeux sans prix ou au relevé trop ancien")
    cmd.set_defaults(func=cmd_stale)

    price = sub.add_parser("price", help="relevés de prix manuels").add_subparsers(
        dest="price_command", required=True
    )
    price_cmd = price.add_parser("add", help="ajoute un relevé de façon interactive")
    price_cmd.set_defaults(func=cmd_price_add)

    for cmd in (sub.choices["build"], sub.choices["stale"], price_cmd):
        cmd.add_argument("--games", type=Path, default=build.GAMES_PATH)
        cmd.add_argument("--prices", type=Path, default=build.PRICES_PATH)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
