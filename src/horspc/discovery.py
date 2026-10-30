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
from horspc.prices.matching import (
    EDITION_WORDS,
    FILLERS,
    platform_words,
    title_matches,
    tokens,
    url_platforms,
)
from horspc.prices.scraper import Scraper

log = logging.getLogger(__name__)

INDEX_FIELDS = (
    "name,alternative_names.name,alternative_names.comment,game_type.type,platforms,"
    "involved_companies.company.name"
)
# Commentaire IGDB d'un nom alternatif français (« French title », « French Title »...).
FRENCH_COMMENT = re.compile(r"\bfrench\b|\bfran[cç]ais", re.I)
# Produit d'occasion ou reconditionné : écarté dès la récolte (l'état neuf est revérifié au relevé).
USED_WORDS = frozenset({"occasion", "reconditionne", "reconditionnee", "used", "refurbished"})
# Région de localisation du titre affiché (identifiant IGDB /regions ; il n'y a pas de France).
TITLE_REGION = "EU"
# Source du titre affiché, par priorité : localisation Europe, nom alternatif qui a servi au
# rapprochement avec un produit Leclerc (enseigne française), nom alternatif commenté comme
# titre français, nom IGDB.
TITLE_SOURCES = ("localisation_eu", "nom_leclerc", "titre_francais_igdb", "nom_igdb")
# Ventilation des produits sans correspondance (mots des noms marchands, sans accents).
PREPAID_WORDS = frozenset({"psn", "eshop", "abonnement", "prepayee", "gamepass", "membership"})
ACCESSORY_WORDS = frozenset(
    {
        "manette", "manettes", "controller", "dualsense", "dualshock", "volant", "pedalier",
        "casque", "headset", "chargeur", "charge", "cable", "housse", "etui", "sacoche", "sac",
        "malette", "mallette", "coque", "support", "station", "dock", "stand", "protection",
        "protege", "film", "verre", "stockage", "ssd", "microsd", "memoire", "adaptateur",
        "grip", "batterie", "clavier", "souris", "camera", "telecommande", "ventilateur",
        "skin", "sticker", "tapis", "siege", "fauteuil", "figurine", "amiibo", "peluche",
        "funko", "accessoire", "accessoires", "kit", "joy", "con",
    }
)  # fmt: skip
PACK_WORDS = frozenset({"pack", "bundle", "coffret", "lot", "console"})
FRENCH_WORDS = frozenset(
    {"le", "la", "les", "des", "du", "de", "et", "l", "d", "un", "une", "au", "aux", "pour",
     "sur", "dans", "avec", "monde", "aventure", "aventures", "secret", "ile"}
)  # fmt: skip
EXAMPLES = 20


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
    french_names: tuple[str, ...] = ()  # noms alternatifs commentés comme titres français


@dataclass(frozen=True)
class Identification:
    status: str  # matched | ambiguous | unmatched
    igdb_id: int | None = None
    candidates: tuple[int, ...] = ()  # jeux en concurrence (ambiguous, ou battus en couverture)
    name: str | None = None  # nom IGDB (principal ou alternatif) qui a servi au rapprochement


@dataclass
class Discovered:
    """Jeu retenu : fiche IGDB, éditions vendues et produits qui les attestent."""

    igdb_id: int
    name: str
    title: str
    game_type: str
    products: dict[Platform, list[Product]]
    names: tuple[str, ...] = ()  # nom IGDB et noms alternatifs (correspondance au relevé)
    companies: tuple[str, ...] = ()  # éditeurs et studios, tolérés dans les noms marchands
    title_source: str = "nom_igdb"
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
    pc_listed: dict[int, str]  # PC dans platforms sans date PC : écarté (jeux découverts)
    kept: list[Discovered]
    unmatched: dict[str, list[tuple[Product, str]]] = field(default_factory=dict)
    curated_missing: list[dict[str, Any]] = field(default_factory=list)
    curated_pc_listed: dict[str, str] = field(default_factory=dict)  # slug → où (WARNING)


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
            french_names=tuple(
                a["name"]
                for a in row.get("alternative_names") or []
                if a.get("name") and FRENCH_COMMENT.search(a.get("comment") or "")
            ),
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
        # Tous les mots (pour expliquer un échec) : un mot porté par plus de 200 noms (« the »,
        # « of ») n'est pas indexé, il n'apporte rien.
        self._all_words: dict[str, list[tuple[int, str, frozenset[str]]]] = defaultdict(list)
        for entry in named:
            for word in entry[2]:
                if frequency[word] <= 200:
                    self._all_words[word].append(entry)

    def identify(self, product: Product) -> Identification:
        words = set(tokens(product.name))
        coverage: dict[int, int] = {}
        matched_name: dict[int, str] = {}
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
                    matched_name[igdb_id] = name
        if not coverage:
            return Identification("unmatched")
        best = max(coverage.values())
        top = sorted(i for i, n in coverage.items() if n == best)
        others = tuple(sorted(i for i in coverage if i not in top))
        if len(top) > 1:
            return Identification("ambiguous", candidates=tuple(top))
        return Identification("matched", top[0], others, matched_name[top[0]])

    def explain(self, product: Product) -> tuple[str, str]:
        """Motif (et détail) d'un produit sans correspondance, pour le rapport du dry-run."""
        words = set(tokens(product.name))
        if PREPAID_WORDS & words or {"carte", "store"} <= words or {"game", "pass"} <= words:
            return "carte prépayée ou abonnement", ""
        if ACCESSORY_WORDS & words:
            return "accessoire", ", ".join(sorted(ACCESSORY_WORDS & words))
        if PACK_WORDS & words:
            return "pack ou console", ", ".join(sorted(PACK_WORDS & words))
        ordered = tokens(product.name)
        platform_idx = platform_words(ordered)
        rest = {w for i, w in enumerate(ordered) if i not in platform_idx}
        best: tuple[int, str, str] | None = None  # (mots du nom, motif, détail)
        for igdb_id, name, name_words in self._by_word(words):
            if not name_words <= words or (best and len(name_words) <= best[0]):
                continue
            game = self.index[igdb_id]
            if product.platform not in game.platforms:
                best = (len(name_words), "jeu IGDB sans cette plateforme", name)
                continue
            allowed = name_words | FILLERS | {t for c in game.companies for t in tokens(c)}
            extra = sorted(w for w in rest - allowed if not (w.isdigit() and len(w) >= 5))
            reason = "édition spéciale" if set(extra) & EDITION_WORDS else "mots en trop"
            best = (len(name_words), reason, f"{name} + {' '.join(extra) or '(plateforme)'}")
        if best:
            return best[1], best[2]
        if EDITION_WORDS & words:
            return "édition spéciale (titre inconnu)", ", ".join(sorted(EDITION_WORDS & words))
        close = self._closest(rest - FILLERS)
        if close:
            return "variante d'écriture d'un titre IGDB", close
        if FRENCH_WORDS & words:
            return "titre FR probable non rattaché", ""
        return "titre absent d'IGDB", ""

    def _by_word(self, words: Iterable[str]) -> Iterator[tuple[int, str, frozenset[str]]]:
        for word in words:
            yield from self._all_words.get(word, ())

    def _closest(self, words: set[str]) -> str | None:
        """Nom IGDB partageant au moins 60 % de ses mots (et au moins deux) avec le produit."""
        best: tuple[float, str] | None = None
        for _, name, name_words in self._by_word(words):
            shared = len(name_words & words)
            ratio = shared / len(name_words)
            if shared >= 2 and ratio >= 0.6 and (best is None or ratio > best[0]):
                best = (ratio, name)
        return None if best is None else best[1]


# --- Filtre ----------------------------------------------------------------------------------


def load_exclusions(path: Path, today: date) -> list[DiscoveryExclusion]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return DiscoveryExclusions.model_validate(
        {"exclusions": raw}, context={"today": today}
    ).exclusions


@dataclass(frozen=True)
class CuratedGame:
    """Jeu de games.yaml, pour la priorité et le diagnostic des jeux curés introuvables."""

    igdb_id: int
    slug: str
    title: str
    platforms: tuple[Platform, ...]


def discover(
    igdb: IgdbClient,
    products: list[Product],
    harvest_rejected: Counter[str],
    curated: list[CuratedGame],
    excluded_ids: set[int],
    now: datetime,
) -> Result:
    index = fetch_index(igdb)
    log.info("Index IGDB : %d fiches", len(index))
    matcher = Matcher(index)
    identifications = {p: matcher.identify(p) for p in products}
    unmatched: dict[str, list[tuple[Product, str]]] = defaultdict(list)
    for product, ident in identifications.items():
        if ident.status == "ambiguous":
            log.info(
                "Ambigu : %s %s → %s",
                product.source,
                product.url,
                ", ".join(f"{i} {index[i].name}" for i in ident.candidates),
            )
        elif ident.status == "unmatched":
            reason, detail = matcher.explain(product)
            unmatched[reason].append((product, detail))
    groups: dict[int, dict[Platform, list[Product]]] = defaultdict(lambda: defaultdict(list))
    for product, ident in identifications.items():
        if ident.igdb_id is not None:
            groups[ident.igdb_id][product.platform].append(product)

    curated_ids = {c.igdb_id for c in curated}
    found_curated = set(groups) & curated_ids
    excluded = set(groups) & excluded_ids - found_curated
    wrong_type: Counter[str] = Counter()
    pending = []
    for igdb_id in sorted(set(groups) - found_curated - excluded):
        game_type = index[igdb_id].game_type
        if game_type in candidates.GAME_TYPES:
            pending.append(igdb_id)
        else:
            wrong_type[game_type] += 1

    ctx = candidates.context(igdb, now)
    kept: list[Discovered] = []
    pc_released: list[int] = []
    pc_listed: dict[int, str] = {}
    for rows, editions in _details(igdb, pending):
        for row in rows:
            evaluated = candidates.evaluate(row, editions.get(row["id"], []), ctx, True)
            if evaluated is None:
                pc_released.append(row["id"])
                continue
            # Règle stricte des jeux découverts : le PC dans platforms suffit à écarter.
            where = candidates.pc_listed(row, ctx)
            if where is not None:
                pc_listed[row["id"]] = where
                continue
            game = index[row["id"]]
            kept.append(
                Discovered(
                    igdb_id=row["id"],
                    name=row["name"],
                    title=row["name"],
                    game_type=evaluated.game_type,
                    products={p: list(v) for p, v in sorted(groups[row["id"]].items())},
                    names=game.names,
                    companies=game.companies,
                    notes=evaluated.notes,
                )
            )
    # Jeux de games.yaml : même détection, simple avertissement (le build l'émettra en WARNING).
    curated_pc_listed = {}
    by_id = {c.igdb_id: c for c in curated}
    for rows, _ in _details(igdb, sorted(curated_ids), with_editions=False):
        for row in rows:
            where = candidates.pc_listed(row, ctx)
            if where is not None:
                curated_pc_listed[by_id[row["id"]].slug] = where
    localized = localized_titles(igdb, [g.igdb_id for g in kept])
    for game in kept:
        game.title, game.title_source = display_title(
            game, localized.get(game.igdb_id), index[game.igdb_id], identifications
        )
    return Result(
        products,
        harvest_rejected,
        identifications,
        found_curated,
        excluded,
        wrong_type,
        pc_released,
        pc_listed,
        sorted(kept, key=lambda g: _sort_key(g.title)),
        dict(unmatched),
        _curated_missing(curated, found_curated, index, identifications),
        curated_pc_listed,
    )


def _details(
    igdb: IgdbClient, ids: list[int], with_editions: bool = True
) -> Iterator[tuple[list[dict[str, Any]], dict[int, list[dict[str, Any]]]]]:
    """Fiches complètes (champs de candidates) et leurs éditions, par lots de PAGE_LIMIT."""
    for start in range(0, len(ids), PAGE_LIMIT):
        chunk = ids[start : start + PAGE_LIMIT]
        rows = igdb.query(
            "games",
            f"fields {candidates.FIELDS}; where id = ({','.join(map(str, chunk))});"
            f" limit {PAGE_LIMIT};",
        )
        yield rows, candidates.editions_by_parent(igdb, chunk) if with_editions else {}


def display_title(
    game: Discovered,
    localized: str | None,
    indexed: IndexedGame,
    identifications: dict[Product, Identification],
) -> tuple[str, str]:
    """Titre affiché et sa source, selon l'ordre de TITLE_SOURCES (jamais le nom marchand)."""
    if localized:
        return localized, "localisation_eu"
    main = frozenset(tokens(game.name))
    leclerc = Counter(
        ident.name
        for product, ident in identifications.items()
        if ident.igdb_id == game.igdb_id
        and product.source == "leclerc"
        and ident.name
        and frozenset(tokens(ident.name)) != main
    )
    if leclerc:
        # Le nom le plus souvent rapproché, puis l'ordre alphabétique (résultat stable).
        return min(leclerc, key=lambda n: (-leclerc[n], n)), "nom_leclerc"
    if indexed.french_names:
        return indexed.french_names[0], "titre_francais_igdb"
    return game.name, "nom_igdb"


def _curated_missing(
    curated: list[CuratedGame],
    found: set[int],
    index: dict[int, IndexedGame],
    identifications: dict[Product, Identification],
) -> list[dict[str, Any]]:
    """Diagnostic des jeux de games.yaml qu'aucun produit n'a rattachés à leur igdb_id."""
    out = []
    for game in curated:
        if game.igdb_id in found:
            continue
        indexed = index.get(game.igdb_id)
        # Produits qui correspondent au titre de games.yaml (souvent français).
        similar = [
            {
                "source": p.source,
                "platform": p.platform,
                "name": p.name,
                "identification": i.status,
                "igdb_id": i.igdb_id,
                "candidates": list(i.candidates),
            }
            for p, i in identifications.items()
            if p.platform in game.platforms and title_matches(p.name, game.title, p.platform)
        ]
        out.append(
            {
                "slug": game.slug,
                "title": game.title,
                "igdb_id": game.igdb_id,
                "in_index": indexed is not None,
                "igdb_name": None if indexed is None else indexed.name,
                "igdb_platforms": [] if indexed is None else sorted(indexed.platforms),
                "products_matching_title": similar[:EXAMPLES],
            }
        )
    return out


def localized_titles(igdb: IgdbClient, ids: list[int]) -> dict[int, str]:
    """Titre localisé région Europe par igdb_id, quand IGDB en connaît un."""
    regions = igdb.query(
        "regions", f'fields id,identifier; where identifier = "{TITLE_REGION}"; limit 10;'
    )
    if not regions:
        return {}
    region = regions[0]["id"]
    titles: dict[int, str] = {}
    for start in range(0, len(ids), PAGE_LIMIT):
        chunk = ",".join(map(str, ids[start : start + PAGE_LIMIT]))
        rows = igdb.query(
            "game_localizations",
            f"fields game,name,region; where game = ({chunk}) & region = {region};"
            f" limit {PAGE_LIMIT};",
        )
        for row in sorted(rows, key=lambda r: r.get("id", 0)):
            if row.get("name"):
                titles.setdefault(row["game"], row["name"])
    return titles


def _sort_key(title: str) -> str:
    plain = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", plain).strip()


def sample_offers(
    scraper: Scraper, kept: list[Discovered], size: int, today: date, seed: int = 0
) -> dict[str, Any]:
    """Ouvre les fiches d'un échantillon de jeux retenus et compte ceux qui ont une offre
    acceptée par le relevé (correspondance, neuf, règle de confiance). Comme au relevé, les
    éditeurs et studios IGDB sont tolérés dans les noms marchands. Rien n'est écrit."""
    chosen = random.Random(seed).sample(kept, min(size, len(kept)))
    with_offer = 0
    fiches = 0
    first_event = len(scraper.fetcher.events)
    for game in chosen:
        entry_game = GameEntry(
            slug=f"igdb-{game.igdb_id}",
            title=game.name,
            editions=[{"platform": p} for p in game.products],
        )
        scraper.companies[entry_game.slug] = list(game.companies)
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
    rejected = Counter(
        e.detail.split(" : ")[0]
        for e in scraper.fetcher.events[first_event:]
        if e.kind == "rejected"
    )
    return {
        "games": len(chosen),
        "with_offer": with_offer,
        "fiches_opened": fiches,
        "rejections": dict(rejected.most_common()),
    }


# --- Rapport du dry-run ---------------------------------------------------------------------

# Moyennes mesurées sur site/ le 30/10/2026 (50 jeux) : jaquette, capture, page de jeu, carte
# du catalogue (index.html) et entrée de data.json, en octets.
COVER_BYTES = 26_656
SCREEN_BYTES = 59_772
PAGE_BYTES = 16_032
CARD_BYTES = 1_887
DATA_BYTES = 2_766
DISCOVERED_SCREENS = 1


def summary(
    result: Result, sample: dict[str, Any] | None = None, max_new: int | None = None
) -> dict[str, Any]:
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
        "pc_listed_without_date": len(result.pc_listed),
        "kept": len(kept),
        "kept_pc_announced": sum(1 for g in kept if any("portage PC" in n for n in g.notes)),
        "kept_by_type": dict(Counter(g.game_type for g in kept)),
        "kept_editions": editions,
        "kept_by_platform": dict(Counter(p for g in kept for p in g.products)),
        "title_sources": {s: sum(g.title_source == s for g in kept) for s in TITLE_SOURCES},
        "weekly_fiches": fiches,
        "first_run_images": images,
        "site_bytes_added": len(kept) * per_game,
        "index_html_bytes_added": len(kept) * CARD_BYTES,
        "data_json_bytes_added": len(kept) * DATA_BYTES,
    }
    if max_new is not None:
        out["max_new"] = max_new
        out["over_max_new"] = max(0, len(kept) - max_new)
    if sample:
        out["offer_sample"] = sample
    out["unmatched"] = {
        reason: {
            "count": len(items),
            "examples": [
                f"[{p.source}/{p.platform}] {p.name}" + (f" — {d}" if d else "")
                for p, d in items[:EXAMPLES]
            ],
        }
        for reason, items in sorted(result.unmatched.items(), key=lambda kv: -len(kv[1]))
    }
    out["curated_missing"] = result.curated_missing
    out["curated_pc_listed"] = result.curated_pc_listed
    out["pc_listed_examples"] = [
        f"{i} {where}" for i, where in sorted(result.pc_listed.items())[:EXAMPLES]
    ]
    return out


def kept_rows(result: Result) -> Iterable[str]:
    """Une ligne par jeu retenu, pour relecture (TSV)."""
    yield "igdb_id\ttitle\ttitle_source\tigdb_name\ttype\tplatforms\tproducts\tnotes"
    for g in result.kept:
        n = sum(len(v) for v in g.products.values())
        yield "\t".join(
            [
                str(g.igdb_id),
                g.title,
                g.title_source,
                g.name,
                g.game_type,
                ",".join(g.products),
                str(n),
                "; ".join(g.notes),
            ]
        )
