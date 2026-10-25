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

from horspc import covers, render
from horspc.http import HttpClient
from horspc.igdb import IgdbClient, IgdbError, IgdbGame
from horspc.models import Catalog, DataFile, GameOut, to_cents
from horspc.prices import ManualPriceProvider, Offer, PriceProvider, scraper
from horspc.prices.fetch import Fetcher

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
GAMES_PATH = ROOT / "data" / "games.yaml"
PRICES_PATH = ROOT / "data" / "prices.manual.yaml"
OUTPUT_PATH = ROOT / "site" / "data.json"
SCRAPED_PATH = ROOT / "data" / "prices.scraped.jsonl"
DISCOVERED_PATH = ROOT / "data" / "discovered.json"
SOURCES_PATH = ROOT / "data" / "sources.config.yaml"


def load_catalog(games_path: Path, prices_path: Path, now: datetime) -> Catalog:
    """Lit et valide les deux fichiers YAML ; lève ValidationError si une entrée est refusée."""
    return Catalog.model_validate(
        {"games": _read_yaml(games_path), "observations": _read_yaml(prices_path)},
        context={"today": now.date()},
    )


class PreviousDataError(Exception):
    """data.json précédent présent mais inutilisable (JSON invalide, schéma ou contenu refusé)."""


def load_previous(path: Path) -> DataFile | None:
    """data.json précédent, ou None s'il est absent.

    Lève PreviousDataError s'il est présent mais mal formé : JSON invalide, schema_version
    différent du schéma courant, ou contenu refusé par le modèle.
    """
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_bytes())
    except ValueError as exc:
        raise PreviousDataError(f"{path} n'est pas un JSON valide : {exc}") from exc
    expected = DataFile.model_fields["schema_version"].default
    version = raw.get("schema_version") if isinstance(raw, dict) else None
    if version != expected:
        raise PreviousDataError(
            f"{path} est en schema_version {version!r}, {expected} attendu : "
            "le régénérer avec un build complet (sans --offline) après l'avoir supprimé"
        )
    try:
        return DataFile.model_validate(raw)
    except ValidationError as exc:
        raise PreviousDataError(f"{path} est refusé par le modèle :\n{exc}") from exc


def build_data(
    catalog: Catalog,
    provider: PriceProvider,
    igdb_results: dict[str, IgdbGame | None] | None,
    previous: dict[str, GameOut],
    now: datetime,
    generated_at: datetime | None = None,
    cover_ids: dict[str, str | None] | None = None,
    screen_ids: dict[str, tuple[str, ...]] | None = None,
) -> DataFile:
    """Assemble et valide le contenu de data.json.

    `igdb_results` vaut None quand IGDB est injoignable : les champs IGDB sont alors repris
    du data.json précédent. `generated_at` vaut `now` par défaut ; `now` sert toujours au
    calcul de `offer.stale`. `cover_ids` donne, par slug, l'image_id de la jaquette présente
    dans site/covers/ (absent ou None : pas de jaquette) ; `screen_ids`, les image_id des
    captures présentes dans site/screens/<slug>/.
    """
    cover_ids = cover_ids or {}
    screen_ids = screen_ids or {}
    games = []
    for game in catalog.games:
        info = None if igdb_results is None else igdb_results.get(game.slug)
        entry = {
            "slug": game.slug,
            "title": game.title,
            "platforms": game.platforms,
            "launch_price_cents": _cents(game.launch_price),
            **_cover_fields(game.slug, cover_ids.get(game.slug)),
            **_igdb_fields(info, previous.get(game.slug), now),
            "screenshots": [
                {"image_id": i, "url": covers.screen_url(game.slug, i)}
                for i in screen_ids.get(game.slug, ())
            ],
        }
        if game.description_fr is not None:
            entry["summary"] = game.description_fr
            entry["summary_source"] = "manual"
        if game.pc_override is not None:
            entry["pc_status"] = game.pc_override.status
            entry["pc_status_source"] = "manual"
        if game.release_date_override is not None:
            entry["release_date"] = game.release_date_override.date
            entry["release_date_source"] = "manual"
        offer = provider.get_best_offer(game, now.date())
        entry["offer"] = (
            None if offer is None else _offer_fields(offer, entry["launch_price_cents"])
        )
        games.append(entry)
    return DataFile.model_validate({"generated_at": generated_at or now, "games": games})


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
    http: HttpClient | None = None,
    render_html: bool = True,
    fetcher: Fetcher | None = None,
    scraped_path: Path = SCRAPED_PATH,
    discovered_path: Path = DISCOVERED_PATH,
    sources_path: Path = SOURCES_PATH,
    report_path: Path | None = None,
) -> DataFile:
    """Build complet.

    Les jaquettes sont téléchargées avec `http` (build complet uniquement) et écrites dans
    site/covers/ seulement après data.json. Avec `fetcher`, les prix sont relevés sur les
    sources activées ; sinon seul l'historique de prices.scraped.jsonl est lu. Lève
    ValidationError si les entrées ou la sortie sont invalides, PreviousDataError si le
    data.json précédent est mal formé.
    """
    catalog = load_catalog(games_path, prices_path, now)
    previous_file = load_previous(output_path)
    previous = {} if previous_file is None else {g.slug: g for g in previous_file.games}
    igdb_results = None
    generated_at = now
    if igdb is None:
        log.warning("Mode hors ligne : champs IGDB et generated_at repris du data.json précédent")
        # Un build hors ligne n'est pas une mise à jour : la fraîcheur affichée ne bouge pas.
        if previous_file is not None:
            generated_at = previous_file.generated_at
    else:
        try:
            igdb_results = igdb.fetch_games(catalog.games, now)
        except IgdbError as exc:
            log.warning("%s ; champs IGDB repris du data.json précédent", exc)
    history = scraper.load_history(scraped_path)
    discovered = None
    report = None
    if fetcher is not None:
        names = {
            slug: list(info.alternative_names)
            for slug, info in (igdb_results or {}).items()
            if info is not None
        }
        discovered = scraper.load_discovered(discovered_path)
        bot = scraper.Scraper(scraper.load_config(sources_path), fetcher)
        new, report = bot.run(catalog.games, names, discovered, now.date())
        history += new
        log.info("Relevé automatique : %d prix, sources bloquées : %s", report.ok, report.blocked)
    site_dir = output_path.parent
    updates = []
    screen_updates = []
    for game in catalog.games:
        prev = previous.get(game.slug)
        prev_id = None if prev is None else prev.cover_image_id
        prev_screens = () if prev is None else tuple(s.image_id for s in prev.screenshots)
        info = None if igdb_results is None else igdb_results.get(game.slug)
        if info is None or http is None:
            updates.append(covers.keep_local(site_dir, game.slug, prev_id))
            screen_updates.append(covers.keep_local_screens(site_dir, game.slug, prev_screens))
        else:
            updates.append(
                covers.fetch_cover(http, site_dir, game.slug, info.cover_image_id, prev_id)
            )
            screen_updates.append(
                covers.fetch_screens(http, site_dir, game.slug, info.screenshot_ids)
            )
    data = build_data(
        catalog,
        ManualPriceProvider(catalog.observations, history),
        igdb_results,
        previous,
        now,
        generated_at,
        {u.slug: u.image_id for u in updates},
        {u.slug: u.image_ids for u in screen_updates},
    )
    write_data(data, output_path)
    if discovered is not None:
        scraper.write_history(scraped_path, history)
        scraper.write_discovered(discovered_path, discovered)
    if report is not None and report_path is not None:
        report_path.write_text(report.to_json() + "\n", encoding="utf-8")
    if igdb_results is not None and http is not None:
        # Hors ligne ou IGDB indisponible : les fichiers présents sont réutilisés tels quels.
        covers.apply_updates(site_dir, updates)
        covers.apply_screens(site_dir, screen_updates)
    if render_html and (site_dir / "index.html").exists():
        render.render_site(data, site_dir)
    elif render_html:
        log.warning("Pas de gabarit index.html dans %s : rendu HTML ignoré", site_dir)
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
            "release_date_source": None if info.release_date is None else "igdb",
            "igdb_id": info.igdb_id,
            "igdb_url": info.url,
            "igdb_fetched_at": now,
            "pc_status": info.pc_status,
            "pc_status_source": "igdb",
            "developers": list(info.developers),
            "publishers": list(info.publishers),
            "summary": info.summary,
            "summary_source": None if info.summary is None else "igdb",
            "trailer_youtube_id": info.trailer_id,
            "sold_digital": info.sold_digital,
            "listed_physical": info.listed_physical,
        }
    if prev is not None:
        # Une valeur manuelle retirée de games.yaml ne doit pas survivre au build suivant.
        keep_release = prev.release_date_source == "igdb"
        return {
            "release_date": prev.release_date if keep_release else None,
            "release_date_source": "igdb" if keep_release else None,
            "igdb_id": prev.igdb_id,
            "igdb_url": prev.igdb_url,
            "igdb_fetched_at": prev.igdb_fetched_at,
            "pc_status": prev.pc_status if prev.pc_status_source == "igdb" else "none",
            "pc_status_source": "igdb",
            "developers": prev.developers,
            "publishers": prev.publishers,
            "summary": prev.summary if prev.summary_source == "igdb" else None,
            "summary_source": "igdb" if prev.summary_source == "igdb" else None,
            "trailer_youtube_id": prev.trailer_youtube_id,
            "sold_digital": prev.sold_digital,
            "listed_physical": prev.listed_physical,
        }
    return {
        "release_date": None,
        "release_date_source": None,
        "igdb_id": None,
        "igdb_url": None,
        "igdb_fetched_at": None,
        "pc_status": "none",
        "pc_status_source": "igdb",
        "developers": [],
        "publishers": [],
        "summary": None,
        "summary_source": None,
        "trailer_youtube_id": None,
        "sold_digital": False,
        "listed_physical": False,
    }


def _cover_fields(slug: str, image_id: str | None) -> dict[str, Any]:
    return {
        "cover_url": None if image_id is None else covers.cover_url(slug),
        "cover_image_id": image_id,
    }


def _offer_fields(offer: Offer, launch_price_cents: int | None) -> dict[str, Any]:
    return {
        "price_cents": offer.price_cents,
        "shipping_cents": offer.shipping_cents,
        "discount_pct": discount_pct(offer.price_cents, launch_price_cents),
        "format": offer.format,
        "condition": offer.condition,
        "merchant": offer.merchant,
        "seller": offer.seller,
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
