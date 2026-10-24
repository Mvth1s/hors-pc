"""Relevé automatique des prix : découverte des fiches (cache data/discovered.json) puis relevé.

Découverte, sans URL saisie, uniquement par des chemins autorisés par robots.txt :
1. EAN de l'édition standard : pages catégories Boulanger (nom, éditeur et EAN par produit),
   correspondance stricte titre + plateforme + édition standard, puis EAN confirmé par le
   `gtin13` de la fiche ;
2. E.Leclerc : fiche ouverte directement par l'EAN (`/fp/x-<EAN>`), EAN vérifié ;
3. Pixmania : sitemaps produits (slugs), correspondance stricte, puis EAN ou nom vérifié.
Les URL trouvées sont mises en cache ; une recherche infructueuse n'est relancée qu'après
RETRY_DAYS jours.
"""

import json
import logging
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import ValidationError

from horspc.models import GameEntry, Platform, ScrapedObservation
from horspc.prices import sources as src
from horspc.prices.fetch import Disallowed, Fetcher, SourceBlocked
from horspc.prices.matching import title_matches
from horspc.prices.trust import rejection

log = logging.getLogger(__name__)

RETRY_DAYS = 14
MAX_CANDIDATES = 5
SOURCE_ORDER = ("leclerc", "boulanger", "pixmania")


def load_config(path: Path) -> dict[str, bool]:
    """Sources activées d'après data/sources.config.yaml (absente = désactivée)."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {name: bool((cfg or {}).get("enabled")) for name, cfg in raw.items()}


def load_discovered(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": 1, "games": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def write_discovered(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def load_history(path: Path) -> list[ScrapedObservation]:
    if not path.exists():
        return []
    return [
        ScrapedObservation.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_history(path: Path, history: Iterable[ScrapedObservation]) -> None:
    """Une ligne par relevé ; un relevé du même jeu, marchand et jour remplace le précédent."""
    latest: dict[tuple[str, str, date], ScrapedObservation] = {}
    for obs in history:
        latest[(obs.slug, obs.merchant, obs.observed_at)] = obs
    lines = [
        o.model_dump_json()
        for o in sorted(latest.values(), key=lambda o: (o.observed_at, o.slug, o.merchant))
    ]
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


@dataclass
class Report:
    """Résumé d'un run, pour les logs et la notification Discord."""

    ok: int = 0
    blocked: dict[str, str] = field(default_factory=dict)
    broken: list[str] = field(default_factory=list)
    events: list[dict[str, str]] = field(default_factory=list)

    @property
    def needs_attention(self) -> bool:
        return bool(self.blocked or self.broken)

    def to_json(self) -> str:
        kinds = Counter(e["kind"] for e in self.events)
        return json.dumps(
            {
                "ok": self.ok,
                "blocked": self.blocked,
                "broken": self.broken,
                "counts": dict(kinds),
                "events": self.events,
            },
            ensure_ascii=False,
            indent=2,
        )


class Scraper:
    def __init__(self, enabled: dict[str, bool], fetcher: Fetcher) -> None:
        self.enabled = [s for s in SOURCE_ORDER if enabled.get(s)]
        self.fetcher = fetcher
        self._listings: dict[str, list[src.ListingItem]] = {}
        self._pixmania_locs: list[str] | None = None
        self._gone: set[str] = set()
        self._pages: dict[str, src.ProductPage | None] = {}

    # --- Run complet -----------------------------------------------------------------

    def run(
        self,
        games: list[GameEntry],
        names: dict[str, list[str]],
        discovered: dict[str, Any],
        today: date,
    ) -> tuple[list[ScrapedObservation], Report]:
        observations: list[ScrapedObservation] = []
        for game in games:
            entry = discovered["games"].setdefault(
                game.slug, {"eans": [], "sources": {}, "checked": {}}
            )
            titles = [game.title, *names.get(game.slug, [])]
            self._discover(game, titles, entry, today)
            for source in self.enabled:
                url = entry["sources"].get(source)
                if not url:
                    continue
                obs = self._scrape(game, titles, source, url, entry, today)
                if obs is not None:
                    observations.append(obs)
                elif url in self._gone:
                    del entry["sources"][source]
                    entry["checked"].pop(source, None)
        events = [e.__dict__ for e in self.fetcher.events]
        broken = sorted(
            {f"{e.source} : {e.url}" for e in self.fetcher.events if e.kind == "broken"}
        )
        report = Report(len(observations), dict(self.fetcher.blocked), broken, events)
        return observations, report

    # --- Relevé d'une fiche ------------------------------------------------------------

    def _scrape(
        self,
        game: GameEntry,
        titles: list[str],
        source: str,
        url: str,
        entry: dict[str, Any],
        today: date,
    ) -> ScrapedObservation | None:
        page = self._product(source, url)
        if page is None:
            return None
        if not self._same_game(page, titles, game.platforms[0], entry):
            self.fetcher.record(source, "rejected", url, f"autre produit : {page.name}")
            return None
        if page.currency not in (None, "EUR") or page.price is None:
            self.fetcher.record(source, "rejected", url, "prix absent ou hors euros")
            return None
        if page.available is False:
            self.fetcher.record(source, "rejected", url, "indisponible")
            return None
        reason = rejection(source, page.seller, page.condition or "?", "physical")
        if reason:
            self.fetcher.record(source, "rejected", url, reason)
            return None
        try:
            obs = ScrapedObservation(
                slug=game.slug,
                merchant=source,
                seller=page.seller,
                price=page.price,
                shipping=page.shipping,
                format="physical",
                condition="new",
                url=url,
                ean=next(iter(sorted(page.eans & set(entry["eans"]))), None),
                observed_at=today,
            )
        except ValidationError as exc:
            self.fetcher.record(source, "rejected", url, str(exc.errors()[0]["msg"]))
            return None
        self.fetcher.record(source, "ok", url, f"{page.price} € ({page.seller})")
        return obs

    def _product(self, source: str, url: str) -> src.ProductPage | None:
        """Fiche lue une seule fois par run (la découverte et le relevé la partagent)."""
        if url not in self._pages:
            self._pages[url] = self._read_product(source, url)
        return self._pages[url]

    def _read_product(self, source: str, url: str) -> src.ProductPage | None:
        try:
            response = self.fetcher.get(source, url)
        except (SourceBlocked, Disallowed):
            return None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404, 410):
                self._gone.add(url)  # fiche retirée : elle sera recherchée à nouveau
                self.fetcher.record(source, "gone", url, f"HTTP {exc.response.status_code}")
            else:
                self.fetcher.record(source, "error", url, f"HTTP {exc.response.status_code}")
            return None
        except httpx.HTTPError as exc:
            self.fetcher.record(source, "error", url, str(exc)[:120])
            return None
        try:
            return src.PARSERS[source](response.text)
        except src.MissingData as exc:
            self.fetcher.record(source, "broken", url, str(exc))
            return None

    @staticmethod
    def _same_game(
        page: src.ProductPage, titles: list[str], platform: Platform, entry: dict[str, Any]
    ) -> bool:
        """EAN identique, ou titre + plateforme + édition standard."""
        if page.eans & set(entry["eans"]):
            return True
        return any(title_matches(page.name, t, platform) for t in titles)

    # --- Découverte ----------------------------------------------------------------------

    def _due(self, entry: dict[str, Any], key: str, today: date) -> bool:
        checked = entry["checked"].get(key)
        return checked is None or date.fromisoformat(checked) <= today - timedelta(days=RETRY_DAYS)

    def _discover(
        self, game: GameEntry, titles: list[str], entry: dict[str, Any], today: date
    ) -> None:
        platform = game.platforms[0]
        # Boulanger et Pixmania apportent les EAN dont Leclerc a besoin : ils passent d'abord.
        steps = {
            "boulanger": lambda: self._discover_boulanger(game, titles, platform, entry),
            "pixmania": lambda: self._discover_pixmania(game, titles, platform, entry),
            "leclerc": lambda: self._discover_leclerc(game, entry),
        }
        for source, step in steps.items():
            if source not in self.enabled or source in entry["sources"]:
                continue
            if source == "leclerc" and not entry["eans"]:
                continue
            if self._due(entry, source, today):
                step()
                entry["checked"][source] = today.isoformat()

    def _add_ean(self, entry: dict[str, Any], ean: str) -> None:
        if ean not in entry["eans"]:
            entry["eans"] = sorted({*entry["eans"], ean})

    def _discover_boulanger(
        self, game: GameEntry, titles: list[str], platform: Platform, entry: dict[str, Any]
    ) -> None:
        candidates = [
            item
            for item in self._boulanger_listing(platform)
            if item.ean and any(title_matches(item.name, t, platform, item.extra) for t in titles)
        ]
        for item in candidates[:MAX_CANDIDATES]:
            page = self._product("boulanger", item.url)
            if page is not None and item.ean in page.eans:
                self._add_ean(entry, item.ean)
                if "boulanger" not in entry["sources"]:
                    entry["sources"]["boulanger"] = item.url
                    log.info(
                        "Découverte : %s → Boulanger %s (EAN %s)", game.slug, item.url, item.ean
                    )

    def _boulanger_listing(self, platform: Platform) -> list[src.ListingItem]:
        category = src.BOULANGER_CATEGORIES[platform]
        if category not in self._listings:
            items: dict[str, src.ListingItem] = {}
            for number in range(1, src.BOULANGER_MAX_PAGES + 1):
                url = src.BOULANGER + category + (f"?numPage={number}" if number > 1 else "")
                try:
                    page = self.fetcher.get("boulanger", url).text
                except (SourceBlocked, Disallowed, httpx.HTTPError):
                    break
                new = [i for i in src.boulanger_listing(page) if i.url not in items]
                if not new:
                    break
                items.update((i.url, i) for i in new)
            self._listings[category] = list(items.values())
        return self._listings[category]

    def _discover_leclerc(self, game: GameEntry, entry: dict[str, Any]) -> None:
        for ean in entry["eans"]:
            url = src.leclerc_url(ean)
            try:
                response = self.fetcher.get("leclerc", url)
                page = src.parse_leclerc(response.text)
            except (SourceBlocked, Disallowed, httpx.HTTPError, src.MissingData):
                continue
            if ean in page.eans:
                entry["sources"]["leclerc"] = str(response.url)
                log.info("Découverte : %s → Leclerc %s", game.slug, response.url)
                return

    def _discover_pixmania(
        self, game: GameEntry, titles: list[str], platform: Platform, entry: dict[str, Any]
    ) -> None:
        candidates = [
            loc
            for loc in self._pixmania_sitemap()
            if any(title_matches(src.pixmania_slug_name(loc), t, platform) for t in titles)
        ]
        for url in candidates[:MAX_CANDIDATES]:
            page = self._product("pixmania", url)
            if page is not None and self._same_game(page, titles, platform, entry):
                for ean in page.eans:
                    self._add_ean(entry, ean)
                entry["sources"]["pixmania"] = url
                log.info("Découverte : %s → Pixmania %s", game.slug, url)
                return

    def _pixmania_sitemap(self) -> list[str]:
        if self._pixmania_locs is None:
            locs: list[str] = []
            try:
                index = self.fetcher.get("pixmania", src.PIXMANIA_SITEMAP).content
                for child in src.sitemap_locs(index):
                    if "products" in child:
                        locs += src.sitemap_locs(self.fetcher.get("pixmania", child).content)
            except (SourceBlocked, Disallowed, httpx.HTTPError):
                pass
            self._pixmania_locs = locs
        return self._pixmania_locs
