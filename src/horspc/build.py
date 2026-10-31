"""Fusionne games.yaml, les relevés de prix et IGDB, puis écrit site/data.json validé."""

import json
import logging
import os
from collections.abc import Iterable
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import ValidationError

from horspc import covers, discovery, render
from horspc.http import HttpClient
from horspc.igdb import PC_SLUG, IgdbClient, IgdbError, IgdbGame
from horspc.models import (
    Catalog,
    DataFile,
    DiscoveredGame,
    GameEntry,
    GameOut,
    ScrapedObservation,
    to_cents,
)
from horspc.prices import ManualPriceProvider, Offer, PriceProvider, scraper
from horspc.prices.fetch import Fetcher
from horspc.prices.selection import edition_offer_key
from horspc.prices.trust import PLATFORMS

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
GAMES_PATH = ROOT / "data" / "games.yaml"
PRICES_PATH = ROOT / "data" / "prices.manual.yaml"
OUTPUT_PATH = ROOT / "site" / "data.json"
SCRAPED_PATH = ROOT / "data" / "prices.scraped.jsonl"
DISCOVERED_PATH = ROOT / "data" / "discovered.json"
SOURCES_PATH = ROOT / "data" / "sources.config.yaml"
REDIRECTS_PATH = ROOT / "data" / "redirects.yaml"
DISCOVERED_GAMES_PATH = ROOT / "data" / "games.discovered.yaml"
EXCLUDE_PATH = ROOT / "data" / "discovery.exclude.yaml"
DISCOVERY_CACHE_PATH = ROOT / "data" / "discovery.cache.json"


def load_catalog(
    games_path: Path, prices_path: Path, now: datetime, discovered_path: Path | None = None
) -> Catalog:
    """Lit et valide games.yaml, prices.manual.yaml et, s'il est donné, games.discovered.yaml ;
    lève ValidationError si une entrée est refusée."""
    return merge_catalog(
        _read_yaml(games_path),
        discovery.load_discovered_games(discovered_path),
        _read_yaml(prices_path),
        now,
    )


def merge_catalog(
    curated: list[Any], discovered: list[DiscoveredGame], observations: list[Any], now: datetime
) -> Catalog:
    """games.yaml d'abord : un jeu découvert de même igdb_id ou de même slug est ignoré."""
    ids = {g.get("igdb_id") for g in curated if isinstance(g, dict)}
    slugs = {g.get("slug") for g in curated if isinstance(g, dict)}
    extra: list[GameEntry] = []
    for game in discovered:
        if game.igdb_id in ids or game.slug in slugs:
            log.info("Découverte : %s ignoré, déjà dans games.yaml", game.slug)
            continue
        extra.append(game.entry())
    return Catalog.model_validate(
        {
            "games": [*curated, *extra],
            "observations": observations,
            "discovered": frozenset(g.slug for g in extra),
        },
        context={"today": now.date()},
    )


# Seule migration admise : v6 → v7 ajoute `origin`, tous les jeux d'un data.json v6 étant curés.
MIGRATABLE_VERSION = 6


def _migrate_v6(raw: dict[str, Any]) -> dict[str, Any]:
    games = [{**g, "origin": "curated"} if isinstance(g, dict) else g for g in raw["games"]]
    return {**raw, "schema_version": 7, "games": games}


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
    if version == MIGRATABLE_VERSION and isinstance(raw.get("games"), list):
        raw = _migrate_v6(raw)
        version = expected
        log.info("%s en schema_version 6 : migré en %d (origin: curated)", path, expected)
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
    ruptures: dict[tuple[str, str], tuple[str, date]] | None = None,
) -> DataFile:
    """Assemble et valide le contenu de data.json.

    `igdb_results` vaut None quand IGDB est injoignable : les champs IGDB sont alors repris
    du data.json précédent. `generated_at` vaut `now` par défaut ; `now` sert toujours au
    calcul de `offer.stale`. `cover_ids` donne, par slug, l'image_id de la jaquette présente
    dans site/covers/ (absent ou None : pas de jaquette) ; `screen_ids`, les image_id des
    captures présentes dans site/screens/<slug>/. `ruptures` : par (slug, plateforme), la
    source et la date d'une fiche trouvée sans offre en ligne (affichée sans autre offre).
    """
    ruptures = ruptures or {}
    cover_ids = cover_ids or {}
    screen_ids = screen_ids or {}
    games = []
    for game in catalog.games:
        info = None if igdb_results is None else igdb_results.get(game.slug)
        entry = {
            "slug": game.slug,
            "title": game.title,
            "origin": "discovered" if game.slug in catalog.discovered else "curated",
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
        prev = previous.get(game.slug)
        editions = []
        for platform in game.platforms:
            offer = provider.get_offer(game, platform, now.date())
            editions.append(
                {
                    "platform": platform,
                    "offer": (
                        None if offer is None else _offer_fields(offer, entry["launch_price_cents"])
                    ),
                    **_proofs(info, prev, platform),
                    "out_of_stock": (
                        None
                        if offer is not None or (game.slug, platform) not in ruptures
                        else _rupture(*ruptures[(game.slug, platform)])
                    ),
                }
            )
        entry["editions"] = editions
        entry["best_offer"] = best_offer([e["offer"] for e in editions if e["offer"]])
        games.append(entry)
    return DataFile.model_validate({"generated_at": generated_at or now, "games": games})


def price_provider(
    catalog: Catalog, scraped: Iterable[ScrapedObservation] = ()
) -> ManualPriceProvider:
    """Relevés manuels et automatiques ; un relevé manuel sans platform vise l'unique édition."""
    single = {g.slug: g.platforms[0] for g in catalog.games if len(g.editions) == 1}
    return ManualPriceProvider(catalog.observations, scraped, single)


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
    discovered_games_path: Path | None = None,
    exclude_path: Path | None = None,
    max_new: int = discovery.MAX_NEW,
    discovery_cache_path: Path | None = None,
) -> DataFile:
    """Build complet.

    Les jaquettes sont téléchargées avec `http` (build complet uniquement) et écrites dans
    site/covers/ seulement après data.json. Avec `fetcher`, les prix sont relevés sur les
    sources activées ; sinon seul l'historique de prices.scraped.jsonl est lu. Avec `fetcher`,
    IGDB et `discovered_games_path`, le catalogue découvert est mis à jour (rayons des sources)
    puis fusionné à games.yaml ; un jeu découvert n'est affiché qu'avec une offre à jour
    (moins de 30 jours). Lève ValidationError si les entrées ou la sortie sont invalides,
    PreviousDataError si le data.json précédent est mal formé.
    """
    curated_raw = _read_yaml(games_path)
    observations_raw = _read_yaml(prices_path)
    # Validation de games.yaml seul d'abord : une entrée refusée arrête le build avant tout appel.
    curated = merge_catalog(curated_raw, [], [], now)
    discovered_games = discovery.load_discovered_games(discovered_games_path)
    bot = None if fetcher is None else scraper.Scraper(scraper.load_config(sources_path), fetcher)
    refreshed = None
    cache = discovery.load_cache(discovery_cache_path)
    if bot is not None and igdb is not None and discovered_games_path is not None:
        refreshed = _refresh_discovered(
            igdb, bot, curated, discovered_games, exclude_path, now, max_new, cache
        )
        if refreshed is not None:
            discovered_games = refreshed.games
    catalog = merge_catalog(curated_raw, discovered_games, observations_raw, now)
    previous_file = load_previous(output_path)
    previous = {} if previous_file is None else {g.slug: g for g in previous_file.games}
    igdb_results = None
    generated_at = now
    if igdb is None:
        log.info("Mode hors ligne : champs IGDB et generated_at repris du data.json précédent")
        # Un build hors ligne n'est pas une mise à jour : la fraîcheur affichée ne bouge pas.
        if previous_file is not None:
            generated_at = previous_file.generated_at
    else:
        try:
            igdb_results = igdb.fetch_games(catalog.games, now, catalog.discovered)
        except IgdbError as exc:
            log.warning("%s ; champs IGDB repris du data.json précédent", exc)
        else:
            _warn_pc_listed(igdb, curated)
    history = scraper.load_history(scraped_path)
    discovered = None
    report = None
    if fetcher is not None:
        names = {
            slug: list(info.alternative_names)
            for slug, info in (igdb_results or {}).items()
            if info is not None
        }
        companies = {
            slug: [*info.publishers, *info.developers]
            for slug, info in (igdb_results or {}).items()
            if info is not None
        }
        discovered = scraper.load_discovered(discovered_path)
        assert bot is not None
        new, report = bot.run(catalog.games, names, discovered, now.date(), companies)
        history += new
        log.info("Relevé automatique : %d prix, sources bloquées : %s", report.ok, report.blocked)
    provider = price_provider(catalog, history)
    shown = _visible(catalog, provider, now.date())
    site_dir = output_path.parent
    updates = []
    screen_updates = []
    for game in shown.games:
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
            screens = info.screenshot_ids
            if game.slug in catalog.discovered:
                screens = screens[: discovery.DISCOVERED_SCREENS]
            screen_updates.append(covers.fetch_screens(http, site_dir, game.slug, screens))
    data = build_data(
        shown,
        provider,
        igdb_results,
        previous,
        now,
        generated_at,
        {u.slug: u.image_id for u in updates},
        {u.slug: u.image_ids for u in screen_updates},
        scraper.out_of_stock(
            discovered if discovered is not None else scraper.load_discovered(discovered_path)
        ),
    )
    write_data(data, output_path)
    if discovered is not None:
        scraper.write_history(scraped_path, history)
        scraper.write_discovered(discovered_path, discovered)
    if refreshed is not None and discovered_games_path is not None:
        discovery.write_discovered_games(discovered_games_path, refreshed.games)
        if discovery_cache_path is not None:
            discovery.write_cache(discovery_cache_path, cache)
    if report is not None and report_path is not None:
        report_path.write_text(report.to_json() + "\n", encoding="utf-8")
    if igdb_results is not None and http is not None:
        # Hors ligne ou IGDB indisponible : les fichiers présents sont réutilisés tels quels.
        covers.apply_updates(site_dir, updates)
        covers.apply_screens(site_dir, screen_updates)
    if render_html and (site_dir / "index.html").exists():
        redirects = _read_yaml_map(REDIRECTS_PATH)
        render.render_site(data, site_dir, redirects)
    elif render_html:
        log.info("Pas de gabarit index.html dans %s : rendu HTML ignoré", site_dir)
    log.info("%s écrit : %d jeux", output_path, len(data.games))
    return data


def _refresh_discovered(
    igdb: IgdbClient,
    bot: scraper.Scraper,
    curated: Catalog,
    previous: list[DiscoveredGame],
    exclude_path: Path | None,
    now: datetime,
    max_new: int,
    cache: discovery.IdentifyCache,
) -> discovery.Refresh | None:
    """Catalogue découvert mis à jour, ou None (liste précédente gardée) si IGDB échoue."""
    excluded = {e.igdb_id for e in discovery.load_exclusions(exclude_path, now.date())}
    refs = [
        discovery.CuratedGame(g.igdb_id, g.slug, g.title, tuple(g.platforms))
        for g in curated.games
        if g.igdb_id is not None
    ]
    try:
        refreshed = discovery.refresh(igdb, bot, refs, previous, excluded, now, max_new, cache)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        log.warning("Découverte impossible (%s) : catalogue découvert inchangé", exc)
        return None
    titles = {g.igdb_id: g.slug for g in refreshed.games}
    for igdb_id, variants in refreshed.result.suspects.items():
        log.warning(
            "Découverte : %s a une variante sortie sur PC (%s) : à vérifier, "
            "data/discovery.exclude.yaml si c'est le même jeu",
            titles.get(igdb_id, f"igdb {igdb_id}"),
            ", ".join(variants),
        )
    return refreshed


def _warn_pc_listed(igdb: IgdbClient, curated: Catalog) -> None:
    """WARNING pour un jeu de games.yaml dont le PC figure dans les plateformes IGDB sans date
    de sortie PC (règle d'exclusion des jeux découverts, ici simple signalement)."""
    refs = [
        discovery.CuratedGame(g.igdb_id, g.slug, g.title, tuple(g.platforms))
        for g in curated.games
        if g.igdb_id is not None and g.pc_override is None
    ]
    try:
        found = discovery.curated_pc_warnings(igdb, refs, igdb.platform_ids()[PC_SLUG])
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        log.warning("Contrôle des plateformes PC impossible (%s)", exc)
        return
    for slug, where in sorted(found.items()):
        log.warning(
            "IGDB : %s a le PC dans ses plateformes (%s) sans sortie PC datée : à vérifier, "
            "pc_override si besoin",
            slug,
            where,
        )


def _visible(catalog: Catalog, provider: PriceProvider, today: date) -> Catalog:
    """Catalogue affiché : un jeu découvert n'y entre qu'avec une offre de moins de 30 jours
    (relevé automatique accepté par la règle de confiance) ; sinon il reste en attente."""
    games = [
        g
        for g in catalog.games
        if g.slug not in catalog.discovered
        or any(
            (offer := provider.get_offer(g, p, today)) is not None and not offer.stale
            for p in g.platforms
        )
    ]
    pending = len(catalog.games) - len(games)
    if pending:
        log.info("Découverte : %d jeux en attente d'une offre à jour", pending)
    return catalog.model_copy(update={"games": games})


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
    }


def _proofs(info: IgdbGame | None, prev: GameOut | None, platform: str) -> dict[str, bool]:
    """Preuves de format d'une édition : IGDB, sinon data.json précédent, sinon aucune."""
    if info is not None:
        return info.format_proofs.get(platform) or {"sold_digital": False, "listed_physical": False}
    old = next((e for e in prev.editions if e.platform == platform), None) if prev else None
    if old is None:
        return {"sold_digital": False, "listed_physical": False}
    return {"sold_digital": old.sold_digital, "listed_physical": old.listed_physical}


def _rupture(source: str, day: date) -> dict[str, Any]:
    return {"merchant": PLATFORMS[source].name, "observed_at": day}


def best_offer(offers: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Meilleure offre toutes éditions : à jour d'abord, puis physique, boîte avec code, démat,
    puis prix port compris (port inconnu : prix seul), relevé le plus récent, marchand."""
    if not offers:
        return None
    return min(
        offers,
        key=lambda o: edition_offer_key(
            o["stale"],
            o["format"],
            o["price_cents"],
            o["shipping_cents"],
            o["observed_at"],
            o["merchant"],
            o["platform"],
        ),
    )


def _cover_fields(slug: str, image_id: str | None) -> dict[str, Any]:
    return {
        "cover_url": None if image_id is None else covers.cover_url(slug),
        "cover_image_id": image_id,
    }


def _offer_fields(offer: Offer, launch_price_cents: int | None) -> dict[str, Any]:
    return {
        "platform": offer.platform,
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


def _read_yaml_map(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _read_yaml(path: Path) -> list[Any]:
    content = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [] if content is None else content
