"""robots.txt selon la RFC 9309 (jokers `*` et `$`, règle la plus longue, Allow en cas d'égalité).

`urllib.robotparser` n'est pas utilisé : il ignore les jokers (il autorisait à tort `/recherche*`).
"""

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

import httpx

from horspc.http import HttpClient

PRODUCT_TOKEN = "horspc"


@dataclass(frozen=True)
class Rule:
    allow: bool
    path: str
    line: int
    raw: str


@dataclass
class Robots:
    """Règles applicables à notre robot pour un hôte."""

    rules: list[Rule] = field(default_factory=list)
    disallow_all: bool = False  # robots.txt injoignable (5xx, erreur réseau) : tout interdit

    def verdict(self, url: str) -> tuple[bool, Rule | None]:
        """(autorisé, règle appliquée) pour une URL de cet hôte."""
        if self.disallow_all:
            return False, None
        parts = urlsplit(url)
        path = _normalize(parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        best: Rule | None = None
        for rule in self.rules:
            if _matches(rule.path, path) and (
                best is None
                or len(rule.path) > len(best.path)
                or (len(rule.path) == len(best.path) and rule.allow and not best.allow)
            ):
                best = rule
        return (best is None or best.allow), best

    def allowed(self, url: str) -> bool:
        return self.verdict(url)[0]


def parse(text: str, token: str = PRODUCT_TOKEN) -> Robots:
    """Groupes du robots.txt : celui qui nomme notre robot, sinon `*` (RFC 9309, § 2.2.1)."""
    groups: list[tuple[list[str], list[Rule]]] = []
    agents: list[str] = []
    rules: list[Rule] = []
    in_agents = False
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (x.strip() for x in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if not in_agents and (agents or rules):
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
            in_agents = True
        elif key in ("allow", "disallow"):
            in_agents = False
            if agents and value:
                rules.append(Rule(key == "allow", _normalize(value), number, raw.strip()))
        else:
            in_agents = False
    if agents:
        groups.append((agents, rules))
    specific = [r for a, r in groups if any(x != "*" and x in token for x in a)]
    chosen = specific or [r for a, r in groups if "*" in a]
    return Robots([rule for group in chosen for rule in group])


def _normalize(path: str) -> str:
    """Décodage des %XX non réservés, comme la RFC 9309 le demande pour comparer les chemins."""
    return re.sub(r"%(?!2F|2f)[0-9A-Fa-f]{2}", lambda m: unquote(m.group(0)), path)


def _matches(pattern: str, path: str) -> bool:
    regex = "".join(".*" if c == "*" else re.escape(c) for c in pattern.rstrip("$"))
    return re.match(regex + ("$" if pattern.endswith("$") else ""), path) is not None


class RobotsCache:
    """robots.txt relus à chaque run (une fois par hôte et par run)."""

    def __init__(self, http: HttpClient) -> None:
        self._http = http
        self._cache: dict[str, Robots] = {}

    def get(self, url: str) -> Robots:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._cache:
            self._cache[origin] = self._fetch(origin)
        return self._cache[origin]

    def _fetch(self, origin: str) -> Robots:
        try:
            response = self._http.request("GET", f"{origin}/robots.txt")
        except httpx.HTTPStatusError as exc:
            # RFC 9309 § 2.3.1.3 : 4xx = pas de restriction ; 5xx = tout interdire.
            return Robots(disallow_all=exc.response.status_code >= 500)
        except httpx.HTTPError:
            return Robots(disallow_all=True)
        return parse(response.text)
