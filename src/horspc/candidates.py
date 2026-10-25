"""`horspc candidates` : jeux IGDB susceptibles d'entrer au catalogue, à curer à la main."""

import json
import logging
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from horspc.igdb import (
    CANCELLED,
    PC_SLUG,
    PLATFORM_SLUGS,
    IgdbClient,
    ReleaseRefs,
    pc_status,
)
from horspc.models import GameEntry, Platform

log = logging.getLogger(__name__)

GAME_TYPES = ("Main Game", "Remake", "Remaster")
# Fiches liées qui peuvent porter la version PC d'un jeu (remaster, director's cut...).
RELATED = ("remasters", "remakes", "ports", "expanded_games", "standalone_expansions")
# Types qui restent le même jeu que leur parent_game : une sortie PC du parent compte.
# Un Remake est un autre jeu : son original n'est pas contrôlé.
SAME_GAME_TYPES = ("Remaster", "Port")
_RELEASE = "release_dates.platform,release_dates.date,release_dates.status"
FIELDS = ",".join(
    [
        "name,slug,platforms,first_release_date,total_rating_count,hypes,game_type.type",
        _RELEASE,
        *(
            f"{r}.name,{r}.platforms,{r}.{_RELEASE.replace(',', f',{r}.')}"
            for r in (*RELATED, "parent_game")
        ),
    ]
)
PAGE_SIZE = 500
MAX_PAGES = 20
RECENT_DAYS = 365


@dataclass
class Candidate:
    igdb_id: int
    name: str
    slug: str
    platforms: list[Platform]
    game_type: str
    first_release: date | None
    total_rating_count: int | None
    hypes: int | None
    notes: list[str] = field(default_factory=list)


@dataclass
class Context:
    """Identifiants IGDB résolus une fois par exécution."""

    pc_id: int
    console_ids: dict[int, Platform]
    refs: ReleaseRefs
    game_type_ids: list[int]
    now: datetime


def find_candidates(
    igdb: IgdbClient,
    known_ids: set[int],
    now: datetime,
    *,
    limit: int,
    min_ratings: int,
    recent_limit: int,
    include_legacy: bool = False,
) -> tuple[list[Candidate], list[Candidate]]:
    """Renvoie (candidats par total_rating_count, sorties récentes par hypes)."""
    platform_ids = igdb.platform_ids()
    ctx = Context(
        pc_id=platform_ids[PC_SLUG],
        console_ids={platform_ids[slug]: name for name, slug in PLATFORM_SLUGS.items()},
        refs=igdb.release_refs(),
        game_type_ids=list(igdb.ids_by_name("game_types", "type", GAME_TYPES).values()),
        now=now,
    )
    base = (
        f"platforms = ({_ids(ctx.console_ids)}) & game_type = ({_ids(ctx.game_type_ids)})"
        f" & version_parent = null & first_release_date < {int(now.timestamp())}"
    )
    popular = _collect(
        igdb,
        ctx,
        f"{base} & total_rating_count >= {min_ratings}",
        "total_rating_count",
        known_ids,
        limit,
        include_legacy,
    )
    since = int((now - timedelta(days=RECENT_DAYS)).timestamp())
    seen = known_ids | {c.igdb_id for c in popular}
    recent = _collect(
        igdb,
        ctx,
        f"{base} & first_release_date >= {since} & hypes != null",
        "hypes",
        seen,
        recent_limit,
        include_legacy,
    )
    return popular, recent


def _collect(
    igdb: IgdbClient,
    ctx: Context,
    where: str,
    sort: str,
    excluded_ids: set[int],
    limit: int,
    include_legacy: bool,
) -> list[Candidate]:
    kept: list[Candidate] = []
    for page in _pages(igdb, where, sort):
        rows = [r for r in page if r["id"] not in excluded_ids]
        editions = _editions_by_parent(igdb, [r["id"] for r in rows])
        for row in rows:
            candidate = _evaluate(row, editions.get(row["id"], []), ctx, include_legacy)
            if candidate is not None:
                kept.append(candidate)
                if len(kept) == limit:
                    return kept
    return kept


def _pages(igdb: IgdbClient, where: str, sort: str) -> Iterator[list[dict[str, Any]]]:
    for page in range(MAX_PAGES):
        rows = igdb.query(
            "games",
            f"fields {FIELDS}; where {where}; sort {sort} desc;"
            f" limit {PAGE_SIZE}; offset {page * PAGE_SIZE};",
        )
        if not rows:
            return
        yield rows
        if len(rows) < PAGE_SIZE:
            return


def _editions_by_parent(igdb: IgdbClient, ids: Sequence[int]) -> dict[int, list[dict[str, Any]]]:
    """Éditions (version_parent) des jeux donnés : une édition PC compte comme une sortie PC."""
    result: dict[int, list[dict[str, Any]]] = {}
    for start in range(0, len(ids), PAGE_SIZE):
        chunk = ids[start : start + PAGE_SIZE]
        rows = igdb.query(
            "games",
            f"fields name,version_parent,{_RELEASE}; where version_parent = ({_ids(chunk)});"
            f" limit {PAGE_SIZE};",
        )
        for row in rows:
            result.setdefault(row["version_parent"], []).append(row)
    return result


def _evaluate(
    row: dict[str, Any], editions: list[dict[str, Any]], ctx: Context, include_legacy: bool
) -> Candidate | None:
    notes = []
    game_type = (row.get("game_type") or {}).get("type", "?")
    parent = row.get("parent_game") if game_type in SAME_GAME_TYPES else None
    for entry, label in (
        [(row, None)]
        + [(rel, rel.get("name")) for r in RELATED for rel in row.get(r) or []]
        + [(ed, ed.get("name")) for ed in editions]
        + ([(parent, f"jeu d'origine {parent.get('name')}")] if parent else [])
    ):
        status = pc_status(entry, ctx.pc_id, ctx.refs, ctx.now)
        if status == "released":
            log.debug("Écarté, sorti sur PC : %s (%s)", row.get("name"), label or "fiche")
            return None
        if status == "announced":
            where = "sur la fiche" if label is None else f"via « {label} » (igdb {entry['id']})"
            notes.append(f"portage PC annoncé {where}")

    first, platforms = _first_release(row, ctx)
    if not platforms:
        if not include_legacy:
            return None
        platforms = sorted(
            {ctx.console_ids[p] for p in row.get("platforms", []) if p in ctx.console_ids}
        )
        notes.append("première sortie hors PS5/Switch/Switch 2/Xbox Series")
    return Candidate(
        igdb_id=row["id"],
        name=row["name"],
        slug=_slug(row["slug"]),
        platforms=platforms,
        game_type=game_type,
        first_release=first,
        total_rating_count=row.get("total_rating_count"),
        hypes=row.get("hypes"),
        notes=notes,
    )


def _first_release(row: dict[str, Any], ctx: Context) -> tuple[date | None, list[Platform]]:
    """Date de première sortie et plateformes cibles sorties ce jour-là ([] si aucune)."""
    cancelled = ctx.refs.statuses[CANCELLED]
    dated = [
        rd
        for rd in row.get("release_dates") or []
        if "date" in rd and rd.get("status") != cancelled
    ]
    if not dated:
        return None, []
    earliest = min(rd["date"] for rd in dated)
    platforms = sorted(
        {
            ctx.console_ids[rd["platform"]]
            for rd in dated
            if rd["date"] == earliest and rd.get("platform") in ctx.console_ids
        }
    )
    return datetime.fromtimestamp(earliest, UTC).date(), platforms


def _slug(igdb_slug: str) -> str:
    """Slug IGDB rendu conforme à games.yaml (les doublons IGDB finissent en --1, --2...)."""
    return re.sub(r"-{2,}", "-", re.sub(r"--\d+$", "", igdb_slug)).strip("-")


def _ids(ids: Any) -> str:
    return ",".join(str(i) for i in sorted(ids))


def render_yaml(
    popular: list[Candidate], recent: list[Candidate], today: date, min_ratings: int
) -> str:
    """YAML au format de games.yaml, contexte en commentaires, à curer à la main."""
    lines = [
        f"# Candidats générés le {today.isoformat()} par `horspc candidates` (données IGDB).",
        "# À curer à la main avant de copier dans data/games.yaml : titre français,",
        "# éditions (une entrée = un jeu, une édition par console), absence réelle de version PC,",
        "# prix de lancement.",
        "# Critères : sur PS5, Switch, Switch 2 ou Xbox Series ; Main Game, Remake ou Remaster ;",
        "# hors éditions (version_parent) ; pas sorti sur PC (fiche, fiches liées, éditions) ;",
        "# première sortie sur l'une de ces consoles ; absents de games.yaml.",
        "",
        f"# --- Les plus notés (total_rating_count >= {min_ratings}) ---",
        "",
    ]
    lines += _entries(popular, lambda c: f"{c.total_rating_count} notes")
    lines += [
        "",
        f"# --- Sorties récentes ({RECENT_DAYS} derniers jours, triées par hypes, "
        "sans seuil de notes) ---",
        "",
    ]
    lines += _entries(recent, lambda c: f"{c.hypes} hypes")
    return "\n".join(lines) + "\n"


def _entries(candidates: list[Candidate], score: Any) -> list[str]:
    lines = []
    for c in candidates:
        released = c.first_release.isoformat() if c.first_release else "date inconnue"
        lines.append(f"- slug: {c.slug}  # {score(c)} · {c.game_type} · sortie {released}")
        for note in c.notes:
            lines.append(f"  # {note}")
        lines.append(f"  title: {json.dumps(c.name, ensure_ascii=False)}")
        editions = ", ".join(f"{{platform: {p}}}" for p in c.platforms)
        lines.append(f"  editions: [{editions}]")
        lines.append(f"  igdb_id: {c.igdb_id}")
        lines.append("  launch_price: null")
        lines.append("")
    return lines


def validate_entries(entries: list[dict[str, Any]]) -> None:
    """Chaque entrée générée doit être une entrée valide de games.yaml."""
    for entry in entries:
        GameEntry.model_validate(entry)
