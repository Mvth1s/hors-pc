"""CLI : `uv run python -m horspc build [--offline] | stale | price add | candidates`."""

import argparse
import logging
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from horspc import build, candidates, price_add, stale
from horspc.http import USER_AGENT, HttpClient
from horspc.igdb import IgdbClient
from horspc.prices import probe, scraper
from horspc.prices.fetch import Fetcher


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
        # Relevé automatique des prix : build complet seulement, jamais hors ligne.
        fetcher = None if args.offline or args.no_scrape else Fetcher.create()
        try:
            build.run(
                now=now,
                igdb=igdb,
                games_path=args.games,
                prices_path=args.prices,
                output_path=args.output,
                http=None if igdb is None else http,
                fetcher=fetcher,
                report_path=args.scrape_report,
            )
        except ValidationError as exc:
            print(f"Validation refusée, data.json non écrit :\n{exc}", file=sys.stderr)
            return 1
        except build.PreviousDataError as exc:
            print(f"data.json précédent refusé, rien n'est écrit : {exc}", file=sys.stderr)
            return 1
        finally:
            if fetcher is not None:
                fetcher.close()
    return 0


def cmd_scrape_report(args: argparse.Namespace) -> int:
    """Résumé d'un bilan de relevé : Markdown (Step Summary) ou message Discord (vide si RAS)."""
    if not args.report.exists():
        print("Pas de bilan de relevé (relevé non lancé ou build interrompu).")
        return 0
    report = scraper.Report.from_json(args.report.read_text(encoding="utf-8"))
    if args.format == "markdown":
        print(report.markdown(), end="")
    else:
        print(report.discord(args.run_url) or "", end="")
    return 0


def cmd_scrape_probe(args: argparse.Namespace) -> int:
    """Teste chaque source activée depuis la machine courante, sans rien écrire dans data/."""
    enabled = [s for s, on in scraper.load_config(build.SOURCES_PATH).items() if on]
    discovered = scraper.load_discovered(build.DISCOVERED_PATH)
    fetcher = Fetcher.create()
    try:
        rows = probe.probe(fetcher, enabled, discovered)
    finally:
        fetcher.close()
    print(probe.markdown(rows, USER_AGENT), end="")
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


def cmd_candidates(args: argparse.Namespace) -> int:
    if args.output.resolve() == args.games.resolve():
        print("--output ne doit pas être games.yaml : les candidats sont à curer.", file=sys.stderr)
        return 2
    credentials = igdb_credentials()
    if credentials is None:
        print("IGDB_CLIENT_ID et IGDB_CLIENT_SECRET sont requis.", file=sys.stderr)
        return 2
    games = yaml.safe_load(args.games.read_text(encoding="utf-8")) or []
    known = {g["igdb_id"] for g in games if g.get("igdb_id")}
    now = datetime.now(UTC).replace(microsecond=0)
    with HttpClient() as http:
        popular, recent = candidates.find_candidates(
            IgdbClient(*credentials, http),
            known,
            now,
            limit=args.limit,
            min_ratings=args.min_ratings,
            recent_limit=args.recent_limit,
            include_legacy=args.include_legacy,
        )
    text = candidates.render_yaml(popular, recent, now.date(), args.min_ratings)
    try:
        candidates.validate_entries(yaml.safe_load(text) or [])
    except ValidationError as exc:
        print(f"Candidats invalides, fichier non écrit :\n{exc}", file=sys.stderr)
        return 1
    args.output.write_text(text, encoding="utf-8")
    print(f"{len(popular)} candidats et {len(recent)} sorties récentes écrits dans {args.output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="horspc", description="Outils du catalogue HorsPC.")
    sub = parser.add_subparsers(dest="command", required=True)

    cmd = sub.add_parser("build", help="génère site/data.json")
    cmd.add_argument("--offline", action="store_true", help="sans IGDB : reprend data.json")
    cmd.add_argument("--no-scrape", action="store_true", help="sans relevé automatique des prix")
    cmd.add_argument("--scrape-report", type=Path, help="écrit le bilan du relevé (JSON)")
    cmd.add_argument("--output", type=Path, default=build.OUTPUT_PATH)
    cmd.set_defaults(func=cmd_build)

    cmd = sub.add_parser("scrape-report", help="résume un bilan de relevé (--scrape-report)")
    cmd.add_argument("report", type=Path)
    cmd.add_argument("--format", choices=["markdown", "discord"], default="markdown")
    cmd.add_argument("--run-url", default="", help="lien du run, ajouté au message Discord")
    cmd.set_defaults(func=cmd_scrape_report)

    cmd = sub.add_parser("scrape-probe", help="diagnostic des sources, sans rien écrire")
    cmd.set_defaults(func=cmd_scrape_probe)

    cmd = sub.add_parser("stale", help="liste les jeux sans prix ou au relevé trop ancien")
    cmd.set_defaults(func=cmd_stale)

    price = sub.add_parser("price", help="relevés de prix manuels").add_subparsers(
        dest="price_command", required=True
    )
    price_cmd = price.add_parser("add", help="ajoute un relevé de façon interactive")
    price_cmd.set_defaults(func=cmd_price_add)

    cmd = sub.add_parser("candidates", help="propose des jeux IGDB à ajouter au catalogue")
    cmd.add_argument("--limit", type=int, default=50, help="nombre de candidats les plus notés")
    cmd.add_argument("--min-ratings", type=int, default=20, help="total_rating_count minimal")
    cmd.add_argument("--recent-limit", type=int, default=20, help="sorties récentes (hypes)")
    cmd.add_argument(
        "--include-legacy",
        action="store_true",
        help="garde les jeux sortis d'abord sur une autre console",
    )
    cmd.add_argument("--output", type=Path, default=build.ROOT / "data" / "candidates.yaml")
    cmd.set_defaults(func=cmd_candidates)

    for cmd in (sub.choices["build"], sub.choices["stale"], price_cmd, sub.choices["candidates"]):
        cmd.add_argument("--games", type=Path, default=build.GAMES_PATH)
        cmd.add_argument("--prices", type=Path, default=build.PRICES_PATH)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
