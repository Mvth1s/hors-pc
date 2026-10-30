"""Client IGDB v4 : date de sortie console, fiche IGDB et statut PC de chaque jeu."""

import logging
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import httpx

from horspc.http import HttpClient
from horspc.models import GameEntry, PcStatus, Platform

log = logging.getLogger(__name__)

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API_URL = "https://api.igdb.com/v4"

PC_SLUG = "win"
PLATFORM_SLUGS: dict[Platform, str] = {
    "ps5": "ps5",
    "switch": "switch",
    "switch2": "switch-2",
    "xbox_series": "series-x-s",
}

GAME_FIELDS = (
    "name,url,platforms,release_dates.platform,release_dates.date,"
    "release_dates.release_region,release_dates.status,cover.image_id,summary,"
    "involved_companies.developer,involved_companies.publisher,involved_companies.company.name,"
    "screenshots.image_id,videos.video_id,videos.name,alternative_names.name,"
    "external_games.external_game_source.name,external_games.game_release_format.format,"
    "external_games.platform.slug"
)
MAX_SCREENSHOTS = 4
PAGE_LIMIT = 500  # maximum de résultats par requête IGDB

# release_date console : seules ces régions et ces statuts (ou un statut absent) comptent.
RELEASE_REGIONS = ("europe", "worldwide")
FULL_RELEASE = "Full Release"
# pc_status : une ligne PC à l'un de ces statuts (ou sans statut) et datée du passé = released.
PC_RELEASED_STATUSES = (FULL_RELEASE, "Early Access", "Advanced Access", "Offline")
CANCELLED = "Cancelled"


YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}")

# Preuves de format (external_games, lus par nom). Stores officiels : vente numérique ; sans
# plateforme sur l'entrée, celle du store. « Microsoft » (aussi un store PC) ne compte qu'avec une
# plateforme Xbox explicite.
OFFICIAL_STORES: dict[str, str | None] = {
    "Playstation Store US": "ps5",
    "Xbox Marketplace": "series-x-s",
    "Microsoft": None,
}
AMAZON = "Amazon"


class IgdbError(Exception):
    """IGDB injoignable ou réponse inexploitable."""


@dataclass(frozen=True)
class IgdbGame:
    igdb_id: int
    url: str | None
    release_date: date | None
    pc_status: PcStatus
    cover_image_id: str | None = None
    summary: str | None = None
    developers: tuple[str, ...] = ()
    publishers: tuple[str, ...] = ()
    screenshot_ids: tuple[str, ...] = ()
    trailer_id: str | None = None
    alternative_names: tuple[str, ...] = ()  # pour reconnaître les fiches marchandes
    # Par édition : sold_digital (store officiel ou Amazon Digital) et listed_physical (entrée
    # Physical) d'après external_games.
    format_proofs: dict[str, dict[str, bool]] = field(default_factory=dict)


@dataclass(frozen=True)
class ReleaseRefs:
    """Identifiants IGDB des régions et statuts de sortie, lus par leur nom."""

    regions: frozenset[int]
    statuses: dict[str, int]


class IgdbClient:
    """Accès à l'API IGDB avec un token Twitch client_credentials mis en cache."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        http: HttpClient,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._clock = clock
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._platform_ids: dict[str, int] | None = None
        self._release_refs: ReleaseRefs | None = None

    def fetch_games(self, games: Sequence[GameEntry], now: datetime) -> dict[str, IgdbGame | None]:
        """Renvoie, pour chaque slug, la fiche IGDB trouvée ou None."""
        try:
            return self._fetch_games(games, now)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise IgdbError(f"IGDB indisponible : {exc}") from exc

    def _fetch_games(self, games: Sequence[GameEntry], now: datetime) -> dict[str, IgdbGame | None]:
        platform_ids = self.platform_ids()
        refs = self.release_refs()
        by_id: dict[int, dict[str, Any]] = {}
        ids = sorted({g.igdb_id for g in games if g.igdb_id is not None})
        # IGDB renvoie au plus PAGE_LIMIT fiches par requête : identifiants envoyés par lots.
        for start in range(0, len(ids), PAGE_LIMIT):
            chunk = ",".join(map(str, ids[start : start + PAGE_LIMIT]))
            rows = self.query(
                "games", f"fields {GAME_FIELDS}; where id = ({chunk}); limit {PAGE_LIMIT};"
            )
            by_id.update((row["id"], row) for row in rows)

        result: dict[str, IgdbGame | None] = {}
        for game in games:
            console_ids = {platform_ids[PLATFORM_SLUGS[p]] for p in game.platforms}
            if game.igdb_id is not None:
                row = by_id.get(game.igdb_id)
            else:
                row = self._search(game, console_ids)
            if row is None:
                log.warning("IGDB : aucun résultat pour %s", game.slug)
                result[game.slug] = None
                continue
            release, rejected = release_date(row, console_ids, refs)
            if release is None and game.release_date_override is None:
                _warn_no_release_date(game.slug, rejected)
            result[game.slug] = IgdbGame(
                igdb_id=row["id"],
                url=row.get("url"),
                release_date=release,
                pc_status=pc_status(row, platform_ids[PC_SLUG], refs, now),
                cover_image_id=(row.get("cover") or {}).get("image_id"),
                summary=(row.get("summary") or "").strip() or None,
                developers=_companies(row, "developer"),
                publishers=_companies(row, "publisher"),
                screenshot_ids=tuple(
                    s["image_id"] for s in (row.get("screenshots") or [])[:MAX_SCREENSHOTS]
                ),
                trailer_id=_trailer(row),
                alternative_names=tuple(
                    a["name"] for a in (row.get("alternative_names") or []) if a.get("name")
                ),
                format_proofs={p: format_proofs(row, [p]) for p in game.platforms},
            )
            if game.igdb_id is None:
                log.info("IGDB : %s → %d (%s)", game.slug, row["id"], row.get("name"))
        return result

    def platform_ids(self) -> dict[str, int]:
        """Identifiants IGDB des plateformes utiles, lus via l'endpoint platforms."""
        if self._platform_ids is None:
            slugs = [PC_SLUG, *PLATFORM_SLUGS.values()]
            quoted = ",".join(f'"{s}"' for s in slugs)
            rows = self.query("platforms", f"fields id,slug; where slug = ({quoted}); limit 50;")
            found = {row["slug"]: row["id"] for row in rows}
            missing = sorted(set(slugs) - found.keys())
            if missing:
                raise KeyError(f"plateformes IGDB introuvables : {', '.join(missing)}")
            self._platform_ids = found
        return self._platform_ids

    def release_refs(self) -> ReleaseRefs:
        """Régions et statuts de sortie, via /release_date_regions et /release_date_statuses."""
        if self._release_refs is None:
            regions = self.ids_by_name("release_date_regions", "region", RELEASE_REGIONS)
            statuses = self.ids_by_name(
                "release_date_statuses", "name", (*PC_RELEASED_STATUSES, CANCELLED)
            )
            self._release_refs = ReleaseRefs(frozenset(regions.values()), statuses)
        return self._release_refs

    def ids_by_name(self, endpoint: str, field: str, names: Sequence[str]) -> dict[str, int]:
        """Identifiants des entrées de `endpoint` dont `field` vaut l'un des `names`."""
        quoted = ",".join(f'"{n}"' for n in names)
        rows = self.query(endpoint, f"fields id,{field}; where {field} = ({quoted}); limit 50;")
        found = {row[field]: row["id"] for row in rows}
        missing = sorted(set(names) - found.keys())
        if missing:
            raise KeyError(f"/{endpoint} : introuvable(s) : {', '.join(missing)}")
        return {name: found[name] for name in names}

    def query(self, endpoint: str, body: str) -> list[dict[str, Any]]:
        """POST d'une requête Apicalypse ; renouvelle le token une fois si IGDB renvoie 401."""
        for attempt in range(2):
            try:
                response = self._http.post(
                    f"{API_URL}/{endpoint}",
                    content=body,
                    headers={
                        "Client-ID": self._client_id,
                        "Authorization": f"Bearer {self._access_token()}",
                        "Accept": "application/json",
                    },
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401 and attempt == 0:
                    self._token = None
                    continue
                raise
            data = response.json()
            if not isinstance(data, list):
                raise ValueError(f"réponse inattendue de /{endpoint}")
            return data
        raise AssertionError("unreachable")

    def _access_token(self) -> str:
        if self._token is None or self._clock() >= self._token_expires_at:
            response = self._http.post(
                TOKEN_URL,
                # Dans le corps et non dans l'URL : le secret ne doit pas fuiter dans les logs.
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "client_credentials",
                },
            )
            payload = response.json()
            self._token = payload["access_token"]
            # Marge d'une minute avant l'expiration annoncée.
            self._token_expires_at = self._clock() + float(payload["expires_in"]) - 60
        return self._token

    def _search(self, game: GameEntry, console_ids: set[int]) -> dict[str, Any] | None:
        title = game.title.replace('"', "")
        ids = ",".join(map(str, sorted(console_ids)))
        rows = self.query(
            "games",
            f'search "{title}"; fields {GAME_FIELDS}; where platforms = ({ids}); limit 10;',
        )
        wanted = _normalize(game.title)
        exact = {r["id"]: r for r in rows if _normalize(r.get("name", "")) == wanted}
        if len(exact) == 1:
            return next(iter(exact.values()))
        # Jamais deviné : sans correspondance exacte unique, le jeu reste sans fiche IGDB.
        if exact:
            log.warning(
                "IGDB : %s ambigu (%s), à épingler via igdb_id",
                game.slug,
                ", ".join(f"{r['id']} {r.get('name')}" for r in exact.values()),
            )
        elif rows:
            log.warning(
                "IGDB : aucune correspondance exacte pour %s (premier résultat : %s %s), "
                "à épingler via igdb_id",
                game.slug,
                rows[0]["id"],
                rows[0].get("name"),
            )
        return None


def _companies(row: dict[str, Any], role: str) -> tuple[str, ...]:
    """Noms des sociétés ayant le rôle donné (developer ou publisher), sans doublon."""
    names = [
        ic["company"]["name"]
        for ic in row.get("involved_companies") or []
        if ic.get(role) and (ic.get("company") or {}).get("name")
    ]
    return tuple(dict.fromkeys(names))


def _trailer(row: dict[str, Any]) -> str | None:
    """Identifiant YouTube de la première vidéo nommée « trailer », sinon de la première vidéo."""
    videos = [v for v in row.get("videos") or [] if YOUTUBE_ID.fullmatch(v.get("video_id", ""))]
    trailers = [v for v in videos if "trailer" in (v.get("name") or "").lower()]
    chosen = (trailers or videos or [None])[0]
    return None if chosen is None else chosen["video_id"]


def _normalize(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", ascii_text.lower())


def release_date(
    row: dict[str, Any], console_ids: set[int], refs: ReleaseRefs
) -> tuple[date | None, Counter[str]]:
    """Plus ancienne sortie console en Europe ou Monde, en version complète (ou sans statut).

    Renvoie aussi le décompte des lignes écartées, par motif : statut, région, sans date.
    """
    kept: list[int] = []
    rejected: Counter[str] = Counter()
    for rd in row.get("release_dates") or []:
        if rd.get("platform") not in console_ids:
            continue
        if "date" not in rd:
            rejected["sans date"] += 1
        elif "status" in rd and rd["status"] != refs.statuses[FULL_RELEASE]:
            rejected["statut"] += 1
        elif rd.get("release_region") not in refs.regions:
            rejected["région"] += 1
        else:
            kept.append(rd["date"])
    if not kept:
        return None, rejected
    return datetime.fromtimestamp(min(kept), UTC).date(), rejected


def _warn_no_release_date(slug: str, rejected: Counter[str]) -> None:
    if rejected:
        detail = ", ".join(f"{n} {reason}" for reason, n in sorted(rejected.items()))
        why = f"{sum(rejected.values())} date(s) écartée(s) : {detail}"
    else:
        why = "aucune date pour ces plateformes"
    log.warning(
        "IGDB : %s, aucune date de sortie retenue (%s) ; ajouter un release_date_override",
        slug,
        why,
    )


def pc_status(row: dict[str, Any], pc_id: int, refs: ReleaseRefs, now: datetime) -> PcStatus:
    """Statut PC déduit des release_dates PC, les lignes Cancelled étant ignorées.

    released : une ligne Full Release, Early Access, Advanced Access, Offline ou sans statut,
    datée du passé. announced : une autre ligne retenue (date future ou absente, ou statut
    Alpha, Beta...). none : aucune ligne PC retenue, même si le PC figure dans `platforms`.
    """
    cancelled = refs.statuses[CANCELLED]
    released = {refs.statuses[name] for name in PC_RELEASED_STATUSES}
    lines = [
        rd
        for rd in row.get("release_dates") or []
        if rd.get("platform") == pc_id and rd.get("status") != cancelled
    ]
    if not lines:
        return "none"
    now_ts = now.timestamp()
    for rd in lines:
        status_ok = "status" not in rd or rd["status"] in released
        if "date" in rd and rd["date"] <= now_ts and status_ok:
            return "released"
    return "announced"


def format_proofs(row: dict[str, Any], platforms: Sequence[Platform]) -> dict[str, bool]:
    """Ventes numérique et physique attestées par external_games pour ces plateformes."""
    slugs = {PLATFORM_SLUGS[p] for p in platforms}
    digital = physical = False
    for ext in row.get("external_games") or []:
        source = (ext.get("external_game_source") or {}).get("name")
        fmt = (ext.get("game_release_format") or {}).get("format")
        slug = (ext.get("platform") or {}).get("slug") or OFFICIAL_STORES.get(source or "")
        if slug not in slugs:
            continue
        if source in OFFICIAL_STORES or (source == AMAZON and fmt == "Digital"):
            digital = True
        if fmt == "Physical":
            physical = True
    return {"sold_digital": digital, "listed_physical": physical}
