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

import json
import logging
import random
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, NamedTuple

import yaml

from horspc import candidates
from horspc.igdb import PAGE_LIMIT, PLATFORM_SLUGS, IgdbClient, pc_status
from horspc.models import (
    MAX_MISSED_RUNS,
    DiscoveredGame,
    DiscoveredGames,
    DiscoveryExclusion,
    DiscoveryExclusions,
    Edition,
    GameEntry,
    Platform,
)
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
    title_skipped: tuple[str, ...] = ()  # noms alternatifs écartés du titre pour leur casse
    notes: list[str] = field(default_factory=list)
    igdb_slug: str = ""  # slug IGDB normalisé (slug du site à la première découverte)
    first_release: date | None = None
    rating_count: int = 0


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
    wrong_type_ids: set[int] = field(default_factory=set)
    # Jeu retenu → fiches IGDB distinctes au nom prolongé d'un suffixe (« Plus », « HD »...)
    # sorties sur PC : signalées, jamais exclues automatiquement.
    suspects: dict[int, list[str]] = field(default_factory=dict)


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


def fetch_index(igdb: IgdbClient, only: Iterable[int] | None = None) -> dict[int, IndexedGame]:
    """Fiches IGDB (hors éditions) présentes sur une plateforme cible, avec leurs noms.

    `only` : seulement ces igdb_id (cache à jour, aucun produit à identifier) ; sinon tout
    l'index (environ 35 000 fiches au 30/10/2026).
    """
    platform_ids = igdb.platform_ids()
    console = {platform_ids[slug]: p for p, slug in PLATFORM_SLUGS.items()}
    ids = ",".join(map(str, sorted(console)))
    games: dict[int, IndexedGame] = {}
    if only is None:
        scopes = [""]
    else:
        wanted = sorted(set(only))
        scopes = [
            ",".join(map(str, wanted[i : i + PAGE_LIMIT]))
            for i in range(0, len(wanted), PAGE_LIMIT)
        ]
    rows = (
        row
        for scope in scopes
        for row in _cursor(
            igdb,
            f"platforms = ({ids}) & version_parent = null"
            + (f" & id = ({scope})" if scope else ""),
            INDEX_FIELDS,
        )
    )
    for row in rows:
        names = [row.get("name") or ""]
        # Noms alternatifs en écriture non latine ignorés : réduits à l'ASCII, il n'en resterait
        # que des mots isolés (« R », « 2026 ») qui fausseraient le rapprochement.
        names += [
            a["name"] for a in row.get("alternative_names") or [] if _latin(a.get("name") or "")
        ]
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
    parents = [""] if only is None else [s for s in scopes if s]
    editions = (
        row
        for scope in parents
        for row in _cursor(
            igdb,
            f"platforms = ({ids}) & version_parent " + (f"= ({scope})" if scope else "!= null"),
            "version_parent,platforms",
        )
    )
    for row in editions:
        parent = games.get(row.get("version_parent"))
        if parent is not None:
            parent.platforms |= {console[p] for p in row.get("platforms") or [] if p in console}
    return games


def _latin(name: str) -> bool:
    """Nom non vide dont toutes les lettres sont latines (accents compris)."""
    letters = [c for c in name if c.isalpha()]
    return bool(letters) and all("LATIN" in unicodedata.name(c, "") for c in letters)


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


# --- Cache d'identification (data/discovery.cache.json) ------------------------------------

CACHE_SCHEMA = 1
RETRY_DAYS = 14  # un produit sans correspondance ou ambigu est réidentifié après ce délai
# Verdicts définitifs : une fiche sortie sur PC ou d'un type non retenu n'est plus redemandée.
FINAL_VERDICTS = ("pc_released", "wrong_type")


@dataclass
class IdentifyCache:
    """Produit (URL) → rattachement, et igdb_id → verdict définitif."""

    products: dict[str, dict[str, Any]] = field(default_factory=dict)
    games: dict[int, dict[str, str]] = field(default_factory=dict)

    def lookup(self, product: Product, today: date) -> Identification | None:
        """Rattachement en cache : définitif s'il a abouti, sinon valable RETRY_DAYS jours."""
        entry = self.products.get(product.url)
        if entry is None or entry.get("platform") != product.platform:
            return None
        if entry["status"] == "matched":
            return Identification("matched", entry["igdb_id"], (), entry.get("name"))
        checked = date.fromisoformat(entry["checked"])
        if (today - checked).days < RETRY_DAYS:
            return Identification(entry["status"])
        return None

    def store(self, product: Product, ident: Identification, today: date) -> None:
        self.products[product.url] = {
            "platform": product.platform,
            "status": ident.status,
            "igdb_id": ident.igdb_id,
            "name": ident.name,
            "checked": today.isoformat(),
        }

    def keep_only(self, urls: set[str]) -> None:
        """Oublie les produits qui ne sont plus en rayon."""
        self.products = {u: e for u, e in self.products.items() if u in urls}


def load_cache(path: Path | None) -> IdentifyCache:
    if path is None or not path.exists():
        return IdentifyCache()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema") != CACHE_SCHEMA:
        log.warning("%s : schéma %r, cache ignoré", path, raw.get("schema"))
        return IdentifyCache()
    return IdentifyCache(
        products=raw.get("products", {}),
        games={int(k): v for k, v in raw.get("games", {}).items()},
    )


def write_cache(path: Path, cache: IdentifyCache) -> None:
    """Une ligne par entrée (diffs lisibles), clés triées."""

    def block(items: dict[str, Any]) -> str:
        lines = [
            f"    {json.dumps(k, ensure_ascii=False)}: "
            f"{json.dumps(v, ensure_ascii=False, sort_keys=True)}"
            for k, v in sorted(items.items())
        ]
        return "{\n" + ",\n".join(lines) + "\n  }" if lines else "{}"

    games = {str(k): v for k, v in sorted(cache.games.items())}
    path.write_text(
        f'{{\n  "schema": {CACHE_SCHEMA},\n  "games": {block(games)},\n'
        f'  "products": {block(cache.products)}\n}}\n',
        encoding="utf-8",
    )


# --- Filtre ----------------------------------------------------------------------------------


def load_exclusions(path: Path | None, today: date) -> list[DiscoveryExclusion]:
    if path is None or not path.exists():
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
    tracked: Iterable[int] = (),
    cache: IdentifyCache | None = None,
    explain: bool = True,
) -> Result:
    """`tracked` : igdb_id déjà découverts, évalués même sans produit en rayon ce run (le statut
    PC est revérifié à chaque build complet). `cache` : rattachements et verdicts connus, mis
    à jour ; sans produit à identifier, seules les fiches utiles sont lues (pas l'index
    complet). `explain` : motifs des produits sans correspondance (rapport du dry-run)."""
    today = now.date()
    tracked = set(tracked)
    cached: dict[Product, Identification] = {}
    if cache is not None:
        cached = {p: i for p in products if (i := cache.lookup(p, today)) is not None}
    todo = [p for p in products if p not in cached]
    final = (
        {}
        if cache is None
        else {
            i: v
            for i, v in cache.games.items()
            if v["verdict"] in FINAL_VERDICTS and i not in tracked
        }
    )
    if todo or explain:
        index = fetch_index(igdb)
    else:
        needed = {i.igdb_id for i in cached.values() if i.igdb_id} | tracked
        index = fetch_index(igdb, only=needed - set(final))
    log.info("Index IGDB : %d fiches, %d produits à identifier", len(index), len(todo))
    matcher = Matcher(index) if todo or explain else None
    identifications = {
        p: cached[p] if p in cached or matcher is None else matcher.identify(p) for p in products
    }
    unmatched: dict[str, list[tuple[Product, str]]] = defaultdict(list)
    for product in todo:
        ident = identifications[product]
        if cache is not None:
            cache.store(product, ident, today)
        if ident.status == "ambiguous":
            log.info(
                "Ambigu : %s %s → %s",
                product.source,
                product.url,
                ", ".join(f"{i} {index[i].name}" for i in ident.candidates),
            )
    if explain and matcher is not None:
        for product, ident in identifications.items():
            if ident.status == "unmatched":
                reason, detail = matcher.explain(product)
                unmatched[reason].append((product, detail))
    groups: dict[int, dict[Platform, list[Product]]] = defaultdict(lambda: defaultdict(list))
    for product, ident in identifications.items():
        if ident.igdb_id is not None:
            groups[ident.igdb_id][product.platform].append(product)

    curated_ids = {c.igdb_id for c in curated}
    considered = set(groups) | tracked
    found_curated = considered & curated_ids
    excluded = considered & excluded_ids - found_curated
    wrong_type: Counter[str] = Counter()
    wrong_type_ids: set[int] = set()
    pc_released: list[int] = []
    pending = []
    for igdb_id in sorted(considered - found_curated - excluded):
        if igdb_id in final:
            # Verdict définitif en cache : la fiche n'est pas redemandée.
            if final[igdb_id]["verdict"] == "pc_released":
                pc_released.append(igdb_id)
            else:
                wrong_type[final[igdb_id].get("type", "?")] += 1
                wrong_type_ids.add(igdb_id)
            continue
        indexed = index.get(igdb_id)
        game_type = (
            "hors index (plus sur une console cible)" if indexed is None else indexed.game_type
        )
        if game_type in candidates.GAME_TYPES:
            pending.append(igdb_id)
        else:
            wrong_type[game_type] += 1
            wrong_type_ids.add(igdb_id)
            if cache is not None and indexed is not None:
                cache.games[igdb_id] = {
                    "verdict": "wrong_type",
                    "type": game_type,
                    "checked": today.isoformat(),
                }

    ctx = candidates.context(igdb, now)
    kept: list[Discovered] = []
    pc_listed: dict[int, str] = {}
    for rows, editions in _details(igdb, pending):
        for row in rows:
            evaluated = candidates.evaluate(row, editions.get(row["id"], []), ctx, True)
            if evaluated is None:
                pc_released.append(row["id"])
                if cache is not None:
                    cache.games[row["id"]] = {
                        "verdict": "pc_released",
                        "checked": today.isoformat(),
                    }
                continue
            # Règle stricte des jeux découverts : le PC dans platforms suffit à écarter.
            where = candidates.pc_listed(row, ctx.pc_id)
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
                    igdb_slug=evaluated.slug,
                    first_release=evaluated.first_release,
                    rating_count=evaluated.total_rating_count or 0,
                )
            )
    # Jeux de games.yaml : même détection, simple avertissement (le build l'émettra en WARNING).
    curated_pc_listed = curated_pc_warnings(igdb, curated, ctx.pc_id)
    suspects = pc_variant_suspects(igdb, kept, ctx)
    localized = localized_titles(igdb, [g.igdb_id for g in kept])
    for game in kept:
        choice = display_title(
            game, localized.get(game.igdb_id), index[game.igdb_id], identifications
        )
        game.title, game.title_source, game.title_skipped = choice
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
        wrong_type_ids,
        suspects,
    )


# Suffixes d'une variante du même jeu (« Rain Code Plus », « X: Definitive Edition »...).
VARIANT_SUFFIXES = frozenset(
    {
        "plus", "definitive", "deluxe", "complete", "hd", "remastered", "remaster", "edition",
        "ultimate", "enhanced", "goty", "director", "directors", "redux", "anniversary",
        "special", "gold", "premium", "collection", "reloaded", "dx",
    }
)  # fmt: skip
NAMES_PER_QUERY = 10


def pc_variant_suspects(
    igdb: IgdbClient, kept: list[Discovered], ctx: candidates.Context
) -> dict[int, list[str]]:
    """Pour chaque jeu retenu, autres fiches IGDB sorties sur PC dont le nom est celui du jeu
    (ou d'un nom alternatif) suivi d'un suffixe de variante, ou d'un « + ».

    Fiches cherchées par préfixe de nom sur toute la base (une variante peut n'exister que
    sur PC, hors de l'index des consoles), NAMES_PER_QUERY noms par requête.
    """
    owners: dict[str, set[int]] = defaultdict(set)  # nom cherché → jeux retenus
    for game in kept:
        for name in game.names or (game.name,):
            if tokens(name):
                owners[name.replace('"', "")].add(game.igdb_id)
    names = sorted(owners)
    found: dict[int, set[tuple[str, int]]] = defaultdict(set)
    for start in range(0, len(names), NAMES_PER_QUERY):
        chunk = names[start : start + NAMES_PER_QUERY]
        where = " | ".join(f'name ~ "{n}"*' for n in chunk)
        # Paginé (curseur sur l'id) : un nom court peut à lui seul dépasser PAGE_LIMIT fiches.
        for row in _cursor(
            igdb,
            f"({where}) & platforms = ({ctx.pc_id})",
            "name,release_dates.platform,release_dates.date,release_dates.status",
        ):
            if pc_status(row, ctx.pc_id, ctx.refs, ctx.now) != "released":
                continue
            for name in chunk:
                for owner in owners[name]:
                    if row["id"] != owner and _is_variant(row.get("name") or "", name):
                        found[owner].add((row.get("name") or "", row["id"]))
    for owner, rows in found.items():
        log.info("Variante PC suspecte de %s : %s", owner, ", ".join(n for n, _ in sorted(rows)))
    return {
        owner: [f"{n} (igdb {i})" for n, i in sorted(rows)] for owner, rows in sorted(found.items())
    }


def _is_variant(candidate: str, name: str) -> bool:
    """`candidate` = `name` + suffixe de variante (premier mot ajouté) ou « + »."""
    base, words = tokens(name), tokens(candidate)
    if words[: len(base)] != base:
        return False
    extra = words[len(base) :]
    if extra:
        return extra[0] in VARIANT_SUFFIXES
    return "+" in candidate and "+" not in name


def curated_pc_warnings(igdb: IgdbClient, curated: list[CuratedGame], pc_id: int) -> dict[str, str]:
    """Jeux de games.yaml dont le PC figure dans les plateformes IGDB (fiche, fiches liées,
    jeu d'origine), même sans date PC : slug → où. Simple signalement, jamais une exclusion."""
    by_id = {c.igdb_id: c for c in curated}
    found = {}
    for rows, _ in _details(igdb, sorted(by_id), with_editions=False):
        for row in rows:
            where = candidates.pc_listed(row, pc_id)
            if where is not None and row.get("id") in by_id:
                found[by_id[row["id"]].slug] = where
    return found


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


class TitleChoice(NamedTuple):
    title: str
    source: str
    skipped: tuple[str, ...] = ()  # noms alternatifs écartés pour leur casse


def display_title(
    game: Discovered,
    localized: str | None,
    indexed: IndexedGame,
    identifications: dict[Product, Identification],
) -> TitleChoice:
    """Titre affiché et sa source, selon l'ordre de TITLE_SOURCES (jamais le nom marchand).

    Un nom alternatif à la casse douteuse (tout en majuscules, « LIBéREZ ») sert au
    rapprochement mais n'est jamais affiché : on passe à la source suivante.
    """
    if localized:
        return TitleChoice(localized, "localisation_eu")
    main = frozenset(tokens(game.name))
    leclerc = Counter(
        ident.name
        for product, ident in identifications.items()
        if ident.igdb_id == game.igdb_id
        and product.source == "leclerc"
        and ident.name
        and frozenset(tokens(ident.name)) != main
    )
    skipped = tuple(
        sorted({n for n in [*leclerc, *indexed.french_names] if not displayable_case(n)})
    )
    shown = {n: c for n, c in leclerc.items() if displayable_case(n)}
    if shown:
        # Le nom le plus souvent rapproché, puis l'ordre alphabétique (résultat stable).
        return TitleChoice(min(shown, key=lambda n: (-shown[n], n)), "nom_leclerc", skipped)
    french = [n for n in indexed.french_names if displayable_case(n)]
    if french:
        return TitleChoice(french[0], "titre_francais_igdb", skipped)
    return TitleChoice(game.name, "nom_igdb", skipped)


def displayable_case(name: str) -> bool:
    """Casse présentable : pas entièrement en majuscules (au moins 4 lettres) et aucun mot
    mêlant au moins deux majuscules, des minuscules puis une majuscule (« LIBéREZ »).
    « WarioWare », « NieR », « DX » ou « LEGO » dans un titre en minuscules restent admis."""
    letters = [c for c in name if c.isalpha()]
    if len(letters) >= 4 and all(c.isupper() for c in letters):
        return False
    for word in re.findall(r"[^\W\d_]+", name):
        cases = "".join("U" if c.isupper() else "l" for c in word)
        if re.search(r"^UU+l+U", cases):
            return False
    return True


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


# --- Mise à jour de games.discovered.yaml (build complet) -------------------------------------

MAX_NEW = 300  # nouveaux jeux au plus par run ; le reste attend le run suivant


@dataclass
class Refresh:
    games: list[DiscoveredGame]
    added: list[str]
    removed: dict[str, str]  # slug → motif
    capped: int  # jeux retenus mais reportés au run suivant (MAX_NEW)
    complete: bool  # toutes les sources activées ont été lues : une absence compte
    result: Result


def load_discovered_games(path: Path | None) -> list[DiscoveredGame]:
    if path is None or not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return DiscoveredGames.model_validate({"games": raw}).games


def write_discovered_games(path: Path, games: list[DiscoveredGame]) -> None:
    header = (
        "# Généré par la découverte automatique (horspc build), ne pas modifier à la main.\n"
        "# Prioritaire : data/games.yaml (même igdb_id ou même slug). Faux positifs :\n"
        "# data/discovery.exclude.yaml.\n"
    )
    rows = [g.model_dump(mode="json") for g in sorted(games, key=lambda g: g.slug)]
    body = yaml.safe_dump(rows, allow_unicode=True, sort_keys=False, width=100) if rows else "[]\n"
    path.write_text(header + body, encoding="utf-8")


def refresh(
    igdb: IgdbClient,
    scraper: Scraper,
    curated: list[CuratedGame],
    previous: list[DiscoveredGame],
    excluded_ids: set[int],
    now: datetime,
    max_new: int = MAX_NEW,
    cache: IdentifyCache | None = None,
) -> Refresh:
    """Nouvelle liste des jeux découverts : jeux en rayon retenus, jeux connus revérifiés.

    Un jeu connu sorti sur PC (ou PC dans platforms), exclu, passé dans games.yaml ou plus du
    bon type est retiré ; absent des rayons, il est retiré au MAX_MISSED_RUNS-ième run complet
    consécutif (un run où une source est bloquée ou vide ne compte pas).
    """
    today = now.date()
    products, rejected = harvest(scraper)
    sources = {p.source for p in products}
    complete = all(s in sources and s not in scraper.fetcher.blocked for s in _harvested(scraper))
    by_id = {g.igdb_id: g for g in previous}
    result = discover(
        igdb, products, rejected, curated, excluded_ids, now, by_id, cache, explain=False
    )
    if cache is not None and complete:
        cache.keep_only({p.url for p in products})
    kept = {g.igdb_id: g for g in result.kept}
    curated_slugs = {c.slug for c in curated}
    games: list[DiscoveredGame] = []
    removed: dict[str, str] = {}
    for old in previous:
        found = kept.get(old.igdb_id)
        reason = _removal_reason(old.igdb_id, result)
        if reason is None and old.slug in curated_slugs:
            reason = "slug repris par games.yaml"
        if reason is not None:
            removed[old.slug] = reason
            continue
        if found is not None and found.products:
            platforms = sorted({e.platform for e in old.editions} | set(found.products))
            games.append(
                old.model_copy(
                    update={
                        "title": found.title,
                        "title_source": found.title_source,
                        "editions": [Edition(platform=p) for p in platforms],
                        "last_seen": today,
                        "missed_runs": 0,
                    }
                )
            )
            continue
        missed = old.missed_runs + (1 if complete else 0)
        if missed >= MAX_MISSED_RUNS:
            removed[old.slug] = f"absent des rayons {missed} runs consécutifs"
        else:
            games.append(old.model_copy(update={"missed_runs": missed}))

    # Nouveaux jeux : sorties à venir et récentes d'abord, puis les plus notés.
    fresh = sorted(
        (g for g in result.kept if g.igdb_id not in by_id and g.products),
        key=lambda g: (-(g.first_release or date.max).toordinal(), -g.rating_count, g.igdb_id),
    )
    taken = {g.slug for g in games} | curated_slugs
    added = []
    for game in fresh[:max_new]:
        slug = game.igdb_slug or f"igdb-{game.igdb_id}"
        if slug in taken:
            slug = f"{slug}-{game.igdb_id}"
        taken.add(slug)
        added.append(slug)
        games.append(
            DiscoveredGame(
                slug=slug,
                title=game.title,
                title_source=game.title_source,
                igdb_id=game.igdb_id,
                editions=[Edition(platform=p) for p in game.products],
                first_seen=today,
                last_seen=today,
                missed_runs=0,
            )
        )
    for slug, reason in sorted(removed.items()):
        log.info("Découverte : %s retiré (%s)", slug, reason)
    log.info(
        "Découverte : %d jeux (%d ajoutés, %d retirés, %d reportés), rayons %s",
        len(games),
        len(added),
        len(removed),
        max(0, len(fresh) - max_new),
        "complets" if complete else "incomplets (absences non comptées)",
    )
    return Refresh(
        sorted(games, key=lambda g: g.slug),
        added,
        removed,
        max(0, len(fresh) - max_new),
        complete,
        result,
    )


def _harvested(scraper: Scraper) -> list[str]:
    """Sources activées qui ont un rayon (catégories Leclerc, sitemaps Pixmania)."""
    return [s for s in scraper.enabled if s in ("leclerc", "pixmania")]


def _removal_reason(igdb_id: int, result: Result) -> str | None:
    if igdb_id in result.pc_released:
        return "sorti sur PC"
    if igdb_id in result.pc_listed:
        return f"PC dans les plateformes IGDB ({result.pc_listed[igdb_id]})"
    if igdb_id in result.excluded:
        return "discovery.exclude.yaml"
    if igdb_id in result.curated:
        return "présent dans games.yaml"
    if igdb_id in result.wrong_type_ids:
        return "type IGDB non retenu ou hors index"
    return None


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
        "titles_case_skipped": sorted(
            f"{g.title} (écarté : {', '.join(g.title_skipped)})" for g in kept if g.title_skipped
        ),
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
    titles = {g.igdb_id: g.title for g in kept}
    out["suspects_pc_variant"] = [
        f"{titles.get(i, i)} (igdb {i}) → {', '.join(v)}" for i, v in result.suspects.items()
    ]
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
