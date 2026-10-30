"""`horspc discover` : catalogue découvert dans les rayons des sources de prix actives.

1. Récolte : pages catégories E.Leclerc (nom, EAN, fiche) et sitemaps produits de Pixmania
   (slug lisible citant exactement une plateforme cible), lus par le Scraper du relevé : mêmes
   règles (robots.txt, 1 req/s par hôte, arrêt de la source si blocage). Aucune fiche ouverte.
2. Identification : index IGDB des fiches présentes sur PS5, Switch, Switch 2 ou Xbox Series
   (tous types, hors éditions), téléchargé une fois ; un produit est rattaché à un jeu par
   `title_matches` (titre ou nom alternatif + plateforme + édition standard). Plusieurs jeux :
   celui dont le titre couvre strictement le plus de mots l'emporte, sinon produit écarté.
3. Filtre : types Main Game, Remake, Remaster, puis `candidates.evaluate` (fiche, fiches liées,
   éditions, jeu d'origine d'un Remaster ou d'un Port), sans la règle « legacy » : seul un
   jeu sorti sur PC est écarté.
"""

import logging
import random
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from horspc import candidates
from horspc.igdb import PAGE_LIMIT, PLATFORM_SLUGS, IgdbClient
from horspc.models import DiscoveryExclusion, DiscoveryExclusions, GameEntry, Platform
from horspc.prices import sources as src
from horspc.prices.matching import title_matches, tokens, url_platforms
from horspc.prices.scraper import Scraper

log = logging.getLogger(__name__)

INDEX_FIELDS = (
    "name,alternative_names.name,game_type.type,platforms,involved_companies.company.name"
)
# Produit d'occasion ou reconditionné : écarté dès la récolte (l'état neuf est revérifié au relevé).
USED_WORDS = frozenset({"occasion", "reconditionne", "reconditionnee", "used", "refurbished"})
# Région de localisation du titre affiché, par ordre de préférence (identifiants IGDB /regions).
TITLE_REGIONS = ("fr-FR", "EU")


@dataclass(frozen=True)
class Product:
    """Une carte d'un rayon : produit vendu pour une console."""

    source: str
    platform: Platform
    name: str  # nom marchand (Pixmania : slug lisible)
    url: str
    ean: str | None


@dataclass
class IndexedGame:
    igdb_id: int
    name: str
    names: tuple[str, ...]  # nom IGDB et noms alternatifs
    game_type: str
    platforms: set[Platform]  # plateformes cibles de la fiche et de ses éditions
    companies: tuple[str, ...]


@dataclass(frozen=True)
class Identification:
    status: str  # matched | ambiguous | unmatched
    igdb_id: int | None = None
    candidates: tuple[int, ...] = ()  # jeux en concurrence (ambiguous, ou battus en couverture)


@dataclass
class Discovered:
    """Jeu retenu : fiche IGDB, éditions vendues et produits qui les attestent."""

    igdb_id: int
    name: str
    title: str
    game_type: str
    products: dict[Platform, list[Product]]
    names: tuple[str, ...] = ()  # nom IGDB et noms alternatifs (correspondance au relevé)
    notes: list[str] = field(default_factory=list)


@dataclass
class Result:
    products: list[Product]
    harvest_rejected: Counter[str]
    identifications: dict[Product, Identification]
    curated: set[int]  # igdb_id déjà dans games.yaml
    excluded: set[int]  # igdb_id de discovery.exclude.yaml
    wrong_type: Counter[str]
    pc_released: list[int]
    kept: list[Discovered]


# --- Récolte -----------------------------------------------------------------------------


def harvest(scraper: Scraper) -> tuple[list[Product], Counter[str]]:
    """Produits des rayons des sources activées, sans ouvrir de fiche."""
    products: list[Product] = []
    rejected: Counter[str] = Counter()
    if "leclerc" in scraper.enabled:
        for platform in src.LECLERC_CATEGORIES:
            for item in scraper.leclerc_listing(platform):
                products.append(Product("leclerc", platform, item.name, item.url, item.ean))
    if "pixmania" in scraper.enabled:
        for loc in scraper.pixmania_sitemap():
            named = url_platforms(loc)
            if len(named) != 1:
                rejected["pixmania : slug sans plateforme cible unique"] += 1
                continue
            products.append(
                Product("pixmania", next(iter(named)), src.pixmania_slug_name(loc), loc, None)
            )
    kept = []
    for product in products:
        if USED_WORDS & set(tokens(product.name)):
            rejected["occasion ou reconditionné"] += 1
        else:
            kept.append(product)
    return kept, rejected


# --- Index IGDB et identification ----------------------------------------------------------


def fetch_index(igdb: IgdbClient) -> dict[int, IndexedGame]:
    """Fiches IGDB (hors éditions) présentes sur une plateforme cible, avec leurs noms."""
    platform_ids = igdb.platform_ids()
    console = {platform_ids[slug]: p for p, slug in PLATFORM_SLUGS.items()}
    ids = ",".join(map(str, sorted(console)))
    games: dict[int, IndexedGame] = {}
    for row in _cursor(igdb, f"platforms = ({ids}) & version_parent = null", INDEX_FIELDS):
        names = [row.get("name") or ""]
        names += [a["name"] for a in row.get("alternative_names") or [] if a.get("name")]
        games[row["id"]] = IndexedGame(
            igdb_id=row["id"],
            name=row.get("name") or "",
            names=tuple(dict.fromkeys(n for n in names if n)),
            game_type=(row.get("game_type") or {}).get("type", "?"),
            platforms={console[p] for p in row.get("platforms") or [] if p in console},
            companies=tuple(
                dict.fromkeys(
                    ic["company"]["name"]
                    for ic in row.get("involved_companies") or []
                    if (ic.get("company") or {}).get("name")
                )
            ),
        )
    # Une édition (« Nintendo Switch 2 Edition ») ajoute sa plateforme au jeu parent.
    for row in _cursor(
        igdb, f"platforms = ({ids}) & version_parent != null", "version_parent,platforms"
    ):
        parent = games.get(row.get("version_parent"))
        if parent is not None:
            parent.platforms |= {console[p] for p in row.get("platforms") or [] if p in console}
    return games


def _cursor(igdb: IgdbClient, where: str, fields: str) -> Iterator[dict[str, Any]]:
    """Toutes les fiches de `where`, par pages de PAGE_LIMIT triées par id (curseur sur l'id)."""
    last = 0
    while True:
        rows = igdb.query(
            "games",
            f"fields {fields}; where {where} & id > {last}; sort id asc; limit {PAGE_LIMIT};",
        )
        yield from rows
        if len(rows) < PAGE_LIMIT:
            return
        last = rows[-1]["id"]


class Matcher:
    """Rattache un produit à un jeu de l'index, avec la règle stricte du relevé des prix."""

    def __init__(self, index: dict[int, IndexedGame]) -> None:
        self.index = index
        named = [(g.igdb_id, n, frozenset(tokens(n))) for g in index.values() for n in g.names]
        named = [entry for entry in named if entry[2]]
        frequency = Counter(t for _, _, words in named for t in words)
        # Chaque nom est rangé sous son mot le plus rare : un produit ne teste que les noms
        # rangés sous l'un de ses mots.
        self._buckets: dict[str, list[tuple[int, str, frozenset[str]]]] = defaultdict(list)
        for entry in named:
            self._buckets[min(entry[2], key=lambda t: (frequency[t], t))].append(entry)

    def identify(self, product: Product) -> Identification:
        words = set(tokens(product.name))
        coverage: dict[int, int] = {}
        for word in words:
            for igdb_id, name, name_words in self._buckets.get(word, ()):
                game = self.index[igdb_id]
                if (
                    name_words <= words
                    and product.platform in game.platforms
                    and len(name_words) > coverage.get(igdb_id, 0)
                    and title_matches(
                        product.name, name, product.platform, game.companies, {product.platform}
                    )
                ):
                    coverage[igdb_id] = len(name_words)
        if not coverage:
            return Identification("unmatched")
        best = max(coverage.values())
        top = sorted(i for i, n in coverage.items() if n == best)
        others = tuple(sorted(i for i in coverage if i not in top))
        if len(top) > 1:
            return Identification("ambiguous", candidates=tuple(top))
        return Identification("matched", top[0], others)


# --- Filtre ----------------------------------------------------------------------------------


def load_exclusions(path: Path, today: date) -> list[DiscoveryExclusion]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return DiscoveryExclusions.model_validate(
        {"exclusions": raw}, context={"today": today}
    ).exclusions


def discover(
    igdb: IgdbClient,
    products: list[Product],
    harvest_rejected: Counter[str],
    curated_ids: set[int],
    excluded_ids: set[int],
    now: datetime,
) -> Result:
    index = fetch_index(igdb)
    log.info("Index IGDB : %d fiches", len(index))
    matcher = Matcher(index)
    identifications = {p: matcher.identify(p) for p in products}
    for product, ident in identifications.items():
        if ident.status == "ambiguous":
            log.info(
                "Ambigu : %s %s → %s",
                product.source,
                product.url,
                ", ".join(f"{i} {index[i].name}" for i in ident.candidates),
            )
    groups: dict[int, dict[Platform, list[Product]]] = defaultdict(lambda: defaultdict(list))
    for product, ident in identifications.items():
        if ident.igdb_id is not None:
            groups[ident.igdb_id][product.platform].append(product)

    curated = set(groups) & curated_ids
    excluded = set(groups) & excluded_ids - curated
    wrong_type: Counter[str] = Counter()
    pending = []
    for igdb_id in sorted(set(groups) - curated - excluded):
        game_type = index[igdb_id].game_type
        if game_type in candidates.GAME_TYPES:
            pending.append(igdb_id)
        else:
            wrong_type[game_type] += 1

    ctx = candidates.context(igdb, now)
    kept: list[Discovered] = []
    pc_released: list[int] = []
    for start in range(0, len(pending), PAGE_LIMIT):
        chunk = pending[start : start + PAGE_LIMIT]
        rows = igdb.query(
            "games",
            f"fields {candidates.FIELDS}; where id = ({','.join(map(str, chunk))});"
            f" limit {PAGE_LIMIT};",
        )
        editions = candidates.editions_by_parent(igdb, chunk)
        for row in rows:
            evaluated = candidates.evaluate(row, editions.get(row["id"], []), ctx, True)
            if evaluated is None:
                pc_released.append(row["id"])
                continue
            kept.append(
                Discovered(
                    igdb_id=row["id"],
                    name=row["name"],
                    title=row["name"],
                    game_type=evaluated.game_type,
                    products={p: list(v) for p, v in sorted(groups[row["id"]].items())},
                    names=index[row["id"]].names,
                    notes=evaluated.notes,
                )
            )
    titles = localized_titles(igdb, [g.igdb_id for g in kept])
    for game in kept:
        game.title = titles.get(game.igdb_id, game.name)
    return Result(
        products,
        harvest_rejected,
        identifications,
        curated,
        excluded,
        wrong_type,
        pc_released,
        sorted(kept, key=lambda g: _sort_key(g.title)),
    )


def localized_titles(igdb: IgdbClient, ids: list[int]) -> dict[int, str]:
    """Titre localisé (France, sinon Europe) par igdb_id, quand IGDB en connaît un."""
    quoted = ",".join(f'"{r}"' for r in TITLE_REGIONS)
    regions = igdb.query(
        "regions", f"fields id,identifier; where identifier = ({quoted}); limit 10;"
    )
    rank = {r["id"]: TITLE_REGIONS.index(r["identifier"]) for r in regions}
    if not rank:
        return {}
    best: dict[int, tuple[int, str]] = {}
    for start in range(0, len(ids), PAGE_LIMIT):
        chunk = ",".join(map(str, ids[start : start + PAGE_LIMIT]))
        rows = igdb.query(
            "game_localizations",
            f"fields game,name,region; where game = ({chunk})"
            f" & region = ({','.join(map(str, rank))}); limit {PAGE_LIMIT};",
        )
        for row in rows:
            order = rank[row["region"]]
            if row.get("name") and (row["game"] not in best or order < best[row["game"]][0]):
                best[row["game"]] = (order, row["name"])
    return {game: name for game, (_, name) in best.items()}


def _sort_key(title: str) -> str:
    plain = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", plain).strip()


def sample_offers(
    scraper: Scraper, kept: list[Discovered], size: int, today: date, seed: int = 0
) -> dict[str, Any]:
    """Ouvre les fiches d'un échantillon de jeux retenus et compte ceux qui ont une offre
    acceptée par le relevé (correspondance, neuf, règle de confiance). Rien n'est écrit."""
    chosen = random.Random(seed).sample(kept, min(size, len(kept)))
    with_offer = 0
    fiches = 0
    for game in chosen:
        entry_game = GameEntry(
            slug=f"igdb-{game.igdb_id}",
            title=game.name,
            editions=[{"platform": p} for p in game.products],
        )
        found = False
        for platform, products in game.products.items():
            # Au plus une fiche par source et par édition, Leclerc d'abord.
            for product in sorted({p.source: p for p in products}.values(), key=lambda p: p.source):
                entry = {"eans": [product.ean] if product.ean else [], "sources": {}, "checked": {}}
                fiches += 1
                titles = list(dict.fromkeys([game.name, game.title, *game.names]))
                if scraper.scrape(
                    entry_game, platform, titles, product.source, product.url, entry, today
                ):
                    found = True
                    break
            if found:
                break
        with_offer += found
    return {"games": len(chosen), "with_offer": with_offer, "fiches_opened": fiches}


# --- Rapport du dry-run ---------------------------------------------------------------------

# Moyennes mesurées sur site/ le 30/10/2026 (50 jeux) : jaquette, capture, page de jeu, carte
# du catalogue (index.html) et entrée de data.json, en octets.
COVER_BYTES = 26_656
SCREEN_BYTES = 59_772
PAGE_BYTES = 16_032
CARD_BYTES = 1_887
DATA_BYTES = 2_766
DISCOVERED_SCREENS = 1


def summary(result: Result, sample: dict[str, int] | None = None) -> dict[str, Any]:
    """Chiffres du dry-run : volumes, motifs d'écart et estimations de coût."""
    status = Counter(i.status for i in result.identifications.values())
    by_source = Counter(f"{p.source}/{p.platform}" for p in result.products)
    matched_by_source = Counter(
        f"{p.source}/{p.platform}"
        for p, i in result.identifications.items()
        if i.status == "matched"
    )
    kept = result.kept
    editions = sum(len(g.products) for g in kept)
    # Relevé hebdomadaire : une fiche par édition et par source où le jeu est vendu.
    fiches = sum(len({p.source for p in ps}) for g in kept for ps in g.products.values())
    images = len(kept) * (1 + DISCOVERED_SCREENS)
    per_game = COVER_BYTES + DISCOVERED_SCREENS * SCREEN_BYTES + PAGE_BYTES
    out: dict[str, Any] = {
        "products": len(result.products),
        "products_by_source": dict(sorted(by_source.items())),
        "harvest_rejected": dict(result.harvest_rejected),
        "identification": dict(status),
        "matched_by_source": dict(sorted(matched_by_source.items())),
        "games_identified": len({i.igdb_id for i in result.identifications.values() if i.igdb_id}),
        "already_curated": len(result.curated),
        "excluded": len(result.excluded),
        "wrong_type": dict(result.wrong_type),
        "pc_released": len(result.pc_released),
        "kept": len(kept),
        "kept_pc_announced": sum(1 for g in kept if any("portage PC" in n for n in g.notes)),
        "kept_by_type": dict(Counter(g.game_type for g in kept)),
        "kept_editions": editions,
        "kept_by_platform": dict(Counter(p for g in kept for p in g.products)),
        "localized_titles": sum(1 for g in kept if g.title != g.name),
        "weekly_fiches": fiches,
        "first_run_images": images,
        "site_bytes_added": len(kept) * per_game,
        "index_html_bytes_added": len(kept) * CARD_BYTES,
        "data_json_bytes_added": len(kept) * DATA_BYTES,
    }
    if sample:
        out["offer_sample"] = sample
    return out


def kept_rows(result: Result) -> Iterable[str]:
    """Une ligne par jeu retenu, pour relecture (TSV)."""
    yield "igdb_id\ttitle\tigdb_name\ttype\tplatforms\tproducts\tnotes"
    for g in result.kept:
        n = sum(len(v) for v in g.products.values())
        yield "\t".join(
            [
                str(g.igdb_id),
                g.title,
                g.name,
                g.game_type,
                ",".join(g.products),
                str(n),
                "; ".join(g.notes),
            ]
        )
