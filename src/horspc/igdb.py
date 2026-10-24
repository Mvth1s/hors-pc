"""Client IGDB v4 : date de sortie console, fiche IGDB et statut PC de chaque jeu."""

import logging
import re
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx

from horspc.http import HttpClient
from horspc.models import GameEntry, PcStatus, Platform

log = logging.getLogger(__name__)

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API_URL = "https://api.igdb.com/v4"

PC_SLUG = "win"
PLATFORM_SLUGS: dict[Platform, str] = {"ps5": "ps5", "switch": "switch", "switch2": "switch-2"}

GAME_FIELDS = "name,url,platforms,release_dates.platform,release_dates.date"


class IgdbError(Exception):
    """IGDB injoignable ou réponse inexploitable."""


@dataclass(frozen=True)
class IgdbGame:
    igdb_id: int
    url: str | None
    release_date: date | None
    pc_status: PcStatus


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

    def fetch_games(self, games: Sequence[GameEntry], now: datetime) -> dict[str, IgdbGame | None]:
        """Renvoie, pour chaque slug, la fiche IGDB trouvée ou None."""
        try:
            return self._fetch_games(games, now)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise IgdbError(f"IGDB indisponible : {exc}") from exc

    def _fetch_games(self, games: Sequence[GameEntry], now: datetime) -> dict[str, IgdbGame | None]:
        platform_ids = self.platform_ids()
        by_id: dict[int, dict[str, Any]] = {}
        ids = [g.igdb_id for g in games if g.igdb_id is not None]
        if ids:
            rows = self.query(
                "games",
                f"fields {GAME_FIELDS}; where id = ({','.join(map(str, ids))}); limit 500;",
            )
            by_id = {row["id"]: row for row in rows}

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
            result[game.slug] = _to_game(row, console_ids, platform_ids[PC_SLUG], now)
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
                params={
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
        if not rows:
            return None
        wanted = _normalize(game.title)
        exact = [r for r in rows if _normalize(r.get("name", "")) == wanted]
        if not exact:
            log.warning(
                "IGDB : correspondance approximative pour %s → %s (%s), à épingler via igdb_id",
                game.slug,
                rows[0]["id"],
                rows[0].get("name"),
            )
        return (exact or rows)[0]


def _normalize(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", ascii_text.lower())


def _to_game(row: dict[str, Any], console_ids: set[int], pc_id: int, now: datetime) -> IgdbGame:
    release_dates = row.get("release_dates") or []
    console_dates = [
        rd["date"] for rd in release_dates if rd.get("platform") in console_ids and "date" in rd
    ]
    release = min(console_dates, default=None)
    return IgdbGame(
        igdb_id=row["id"],
        url=row.get("url"),
        release_date=None if release is None else datetime.fromtimestamp(release, UTC).date(),
        pc_status=pc_status(row, pc_id, now),
    )


def pc_status(row: dict[str, Any], pc_id: int, now: datetime) -> PcStatus:
    """released si une sortie PC est passée, announced si le PC est listé, none sinon."""
    pc_dates = [rd for rd in row.get("release_dates") or [] if rd.get("platform") == pc_id]
    now_ts = now.timestamp()
    if any("date" in rd and rd["date"] <= now_ts for rd in pc_dates):
        return "released"
    if pc_dates or pc_id in (row.get("platforms") or []):
        return "announced"
    return "none"
