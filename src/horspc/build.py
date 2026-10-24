"""Fusionne games.yaml, les relevés de prix et IGDB, puis écrit site/data.json validé."""

import json
import logging
import os
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from horspc.igdb import IgdbClient, IgdbError, IgdbGame
from horspc.models import Catalog, DataFile, GameOut, to_cents
from horspc.prices import ManualPriceProvider, Offer, PriceProvider

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
GAMES_PATH = ROOT / "data" / "games.yaml"
PRICES_PATH = ROOT / "data" / "prices.manual.yaml"
OUTPUT_PATH = ROOT / "site" / "data.json"


def load_catalog(games_path: Path, prices_path: Path, now: datetime) -> Catalog:
    """Lit et valide les deux fichiers YAML ; lève ValidationError si une entrée est refusée."""
    return Catalog.model_validate(
        {"games": _read_yaml(games_path), "observations": _read_yaml(prices_path)},
        context={"today": now.date()},
    )


def load_previous(path: Path) -> dict[str, GameOut]:
    """Jeux du data.json précédent, par slug ; vide s'il est absent ou illisible."""
    if not path.exists():
        return {}
    try:
        previous = DataFile.model_validate_json(path.read_bytes())
    except ValidationError as exc:
        log.warning("data.json précédent illisible, ignoré : %s", exc)
        return {}
    return {game.slug: game for game in previous.games}


def build_data(
    catalog: Catalog,
    provider: PriceProvider,
    igdb_results: dict[str, IgdbGame | None] | None,
    previous: dict[str, GameOut],
    now: datetime,
) -> DataFile:
    """Assemble et valide le contenu de data.json.

    `igdb_results` vaut None quand IGDB est injoignable : les champs IGDB sont alors repris
    du data.json précédent.
    """
    games = []
    for game in catalog.games:
        info = None if igdb_results is None else igdb_results.get(game.slug)
        entry = {
            "slug": game.slug,
            "title": game.title,
            "platforms": game.platforms,
            "launch_price_cents": _cents(game.launch_price),
            "cover_url": None,
            **_igdb_fields(info, previous.get(game.slug), now),
        }
        if game.pc_override is not None:
            entry["pc_status"] = game.pc_override.status
            entry["pc_status_source"] = "manual"
        offer = provider.get_best_offer(game, now.date())
        entry["offer"] = (
            None if offer is None else _offer_fields(offer, entry["launch_price_cents"])
        )
        games.append(entry)
    return DataFile.model_validate({"generated_at": now, "games": games})


def write_data(data: DataFile, path: Path) -> None:
    """Écrit data.json de façon atomique (fichier temporaire puis renommage)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    payload = json.dumps(data.model_dump(mode="json"), ensure_ascii=False, indent=2)
    tmp.write_text(payload + "\n", encoding="utf-8")
    os.replace(tmp, path)


def run(
    *,
    now: datetime,
    igdb: IgdbClient | None,
    games_path: Path = GAMES_PATH,
    prices_path: Path = PRICES_PATH,
    output_path: Path = OUTPUT_PATH,
) -> DataFile:
    """Build complet. Lève ValidationError si les entrées ou la sortie sont invalides."""
    catalog = load_catalog(games_path, prices_path, now)
    previous = load_previous(output_path)
    igdb_results = None
    if igdb is None:
        log.warning("Mode hors ligne : champs IGDB repris du data.json précédent")
    else:
        try:
            igdb_results = igdb.fetch_games(catalog.games, now)
        except IgdbError as exc:
            log.warning("%s ; champs IGDB repris du data.json précédent", exc)
    data = build_data(
        catalog, ManualPriceProvider(catalog.observations), igdb_results, previous, now
    )
    write_data(data, output_path)
    log.info("%s écrit : %d jeux", output_path, len(data.games))
    return data


def discount_pct(price_cents: int, launch_price_cents: int | None) -> int | None:
    """Réduction en % par rapport au prix de lancement, arrondie à l'entier (peut être négative)."""
    if launch_price_cents is None:
        return None
    ratio = Decimal(100) * (1 - Decimal(price_cents) / Decimal(launch_price_cents))
    return int(ratio.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _igdb_fields(info: IgdbGame | None, prev: GameOut | None, now: datetime) -> dict[str, Any]:
    if info is not None:
        return {
            "release_date": info.release_date,
            "igdb_id": info.igdb_id,
            "igdb_url": info.url,
            "igdb_fetched_at": now,
            "pc_status": info.pc_status,
            "pc_status_source": "igdb",
        }
    if prev is not None:
        return {
            "release_date": prev.release_date,
            "igdb_id": prev.igdb_id,
            "igdb_url": prev.igdb_url,
            "igdb_fetched_at": prev.igdb_fetched_at,
            # Un statut manuel retiré de games.yaml ne doit pas survivre au build suivant.
            "pc_status": prev.pc_status if prev.pc_status_source == "igdb" else "none",
            "pc_status_source": "igdb",
        }
    return {
        "release_date": None,
        "igdb_id": None,
        "igdb_url": None,
        "igdb_fetched_at": None,
        "pc_status": "none",
        "pc_status_source": "igdb",
    }


def _offer_fields(offer: Offer, launch_price_cents: int | None) -> dict[str, Any]:
    return {
        "price_cents": offer.price_cents,
        "shipping_cents": offer.shipping_cents,
        "discount_pct": discount_pct(offer.price_cents, launch_price_cents),
        "format": offer.format,
        "condition": offer.condition,
        "merchant": offer.merchant,
        "url": offer.url,
        "observed_at": offer.observed_at,
        "source": offer.source,
        "stale": offer.stale,
    }


def _cents(amount: Decimal | None) -> int | None:
    return None if amount is None else to_cents(amount)


def _read_yaml(path: Path) -> list[Any]:
    content = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [] if content is None else content
