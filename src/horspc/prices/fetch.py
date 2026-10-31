"""Récupération de pages pour le relevé automatique : robots.txt, blocages, aucun contournement.

- robots.txt (RFC 9309) relu à chaque run : une URL interdite n'est jamais demandée ;
- au plus une requête par seconde et par hôte, User-Agent identifiable, timeouts ;
- nouvel essai sur 5xx uniquement ;
- 400, 401, 403, 429, captcha ou défi anti-bot, ou robots.txt non vérifiable : la source est
  marquée bloquée pour le run et n'est plus sollicitée (pas de nouvel essai, pas de changement
  d'User-Agent, pas de proxy, pas de navigateur).
"""

import logging
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from horspc.http import HttpClient
from horspc.robots import RobotsCache

log = logging.getLogger(__name__)

SERVER_ERRORS = frozenset({500, 502, 503, 504})
# 400 : réponse de Boulanger aux IP de GitHub Actions (constaté le 25/10/2026).
BLOCK_STATUSES = frozenset({400, 401, 403, 429})
# Marqueurs de pages de défi : volontairement stricts (un simple mot « captcha » dans un script
# de page normale ne suffit pas).
CHALLENGE_MARKERS = (
    "geo.captcha-delivery.com",
    "_Incapsula_Resource",
    "Pardon Our Interruption",
    "/cdn-cgi/challenge-platform",
    "cf-chl-",
    "px-captcha",
    "Please enable JS and disable any ad blocker",
)
LEVELS = {"blocked": logging.WARNING, "rejected": logging.DEBUG}


class SourceBlocked(Exception):
    """La source refuse nos requêtes (code ou page de défi) : plus de requête pendant ce run."""


class Disallowed(Exception):
    """robots.txt interdit cette URL : elle n'est pas demandée."""


@dataclass
class Event:
    source: str
    kind: str  # blocked | disallowed | broken | rejected | error | ok
    url: str
    detail: str
    motif: str = ""  # détail sans la partie propre à la fiche, pour le récapitulatif

    def __post_init__(self) -> None:
        self.motif = self.motif or self.detail.split(" (")[0]


@dataclass
class Fetcher:
    http: HttpClient
    robots: RobotsCache
    blocked: dict[str, str] = field(default_factory=dict)  # source -> motif
    events: list[Event] = field(default_factory=list)

    @classmethod
    def create(cls) -> "Fetcher":
        http = HttpClient(retry_statuses=SERVER_ERRORS, timeout=20.0)
        return cls(http, RobotsCache(http))

    def get(self, source: str, url: str) -> httpx.Response:
        """Page autorisée par robots.txt ; lève Disallowed, SourceBlocked ou httpx.HTTPError."""
        if source in self.blocked:
            raise SourceBlocked(self.blocked[source])
        robots = self.robots.get(url)
        if robots.unverifiable:
            self._block(source, url, robots.unverifiable)
        allowed, rule = robots.verdict(url)
        if not allowed:
            detail = rule.raw if rule else "robots.txt non vérifiable"
            self.record(source, "disallowed", url, detail)
            raise Disallowed(detail)
        try:
            response = self.http.request("GET", url)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in BLOCK_STATUSES:
                self._block(source, url, f"HTTP {status}")
            raise
        final = str(response.url)
        if final != url and not self.robots.get(final).allowed(final):  # non vérifiable compris
            # Redirection vers un chemin interdit : la page n'est pas exploitée.
            self.record(source, "disallowed", final, "redirection vers un chemin interdit")
            raise Disallowed(final)
        marker = next((m for m in CHALLENGE_MARKERS if m in response.text), None)
        if marker:
            self._block(source, url, f"page de défi ({marker})")
        return response

    def record(self, source: str, kind: str, url: str, detail: str, motif: str = "") -> None:
        self.events.append(Event(source, kind, url, detail, motif))
        # WARNING réservé à ce qui demande une action (source bloquée) ; une offre refusée est
        # le fonctionnement normal : détail en DEBUG, récapitulatif par motif en fin de relevé.
        level = LEVELS.get(kind, logging.INFO)
        log.log(level, "%s [%s] %s : %s", source, kind, urlsplit(url).path, detail)

    def log_rejections(self) -> None:
        """Une ligne INFO : offres refusées par motif, du plus fréquent au plus rare."""
        rejected = Counter(e.motif for e in self.events if e.kind == "rejected")
        if rejected:
            detail = ", ".join(f"{n} {motif}" for motif, n in rejected.most_common())
            log.info("Offres refusées : %s", detail)

    def _block(self, source: str, url: str, reason: str) -> None:
        self.blocked[source] = reason
        self.record(source, "blocked", url, reason)
        raise SourceBlocked(reason)

    def close(self) -> None:
        self.http.close()
