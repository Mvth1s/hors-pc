"""Audit mensuel des sources de prix (`horspc sources-audit`), depuis les IP du runner.

Pour chaque enseigne de data/sources.audit.yaml : robots.txt (statut, règles de notre groupe,
verdicts sur nos chemins), TDMRep (fichier, en-tête, meta), accueil et fiche connue (statut,
défi anti-bot), empreinte des clauses pertinentes des CGU, et JSON-LD lisible pour les sources
actives. L'état est comparé à data/sources.audit.json : un changement est signalé pour être relu,
jamais interprété ni appliqué (sources.config.yaml n'est pas modifié).

Mêmes règles que le relevé : User-Agent identifiable, 1 requête par seconde par hôte, nouvel
essai sur 5xx seulement, aucun contournement. Une page interdite par robots.txt n'est pas
demandée ; une enseigne qui bloque (ou dont robots.txt n'est pas vérifiable) n'est plus sollicitée
pendant l'audit.
"""

import hashlib
import html
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml

from horspc.http import HttpClient
from horspc.prices import sources as src
from horspc.prices.fetch import BLOCK_STATUSES, CHALLENGE_MARKERS, SERVER_ERRORS
from horspc.robots import parse

# Clauses suivies dans les CGU : extraction, robots, aspiration, fouille, reproduction.
CLAUSE_WORDS = re.compile(
    r"extra(?:ction|ire|it)|robot|aspir|scrap|fouille|data mining|text and data|crawl|spider"
    r"|reprodu(?:ction|ire|it)|tdm",
    re.I,
)
TDM_META = re.compile(
    r"<meta\b[^>]*name=[\"']tdm-reservation[\"'][^>]*content=[\"']([^\"']*)[\"']", re.I
)


def load_registry(path: Path) -> dict[str, dict[str, Any]]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_state(path: Path, state: dict[str, Any]) -> None:
    text = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")


def clauses(page: str) -> list[str]:
    """Paragraphes du texte de la page qui contiennent un mot suivi (espaces normalisés)."""
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S | re.I)
    text = re.sub(r"<(?:br|/p|/li|/h\d|/div|/td|/tr)\b[^>]*>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    paragraphs = (re.sub(r"\s+", " ", p).strip() for p in text.split("\n"))
    return [p for p in paragraphs if p and CLAUSE_WORDS.search(p)]


def fingerprint(parts: list[str]) -> str | None:
    if not parts:
        return None
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


@dataclass
class Auditor:
    """Audit des enseignes avec un client HTTP qui ne réessaie que sur 5xx."""

    http: HttpClient
    _blocked_hosts: set[str] = field(default_factory=set)
    _robots: dict[str, Any] = field(default_factory=dict)  # origine -> Robots ou None

    @classmethod
    def create(cls) -> "Auditor":
        return cls(HttpClient(retry_statuses=SERVER_ERRORS, timeout=20.0))

    def get(self, url: str) -> tuple[int | str, httpx.Response | None]:
        """(statut, réponse) ; statut « erreur réseau » si la requête échoue."""
        try:
            response = self.http.request("GET", url)
        except httpx.HTTPStatusError as exc:
            return exc.response.status_code, exc.response
        except httpx.HTTPError:
            return "erreur réseau", None
        return response.status_code, response

    def audit(self, sid: str, cfg: dict[str, Any]) -> dict[str, Any]:
        home = cfg["home"]
        origin = "{0.scheme}://{0.netloc}".format(urlsplit(home))
        snap: dict[str, Any] = {"robots": self._robots_section(origin, cfg)}
        snap["tdm"] = self._tdmrep(origin)
        pages = {"home": home}
        if cfg.get("product"):
            pages["product"] = cfg["product"]
        for key, url in pages.items():
            snap[key], response = self._page(url)
            if key == "home":
                snap["tdm"].update(_tdm_headers(response))
            if key == "product" and cfg.get("active"):
                snap["jsonld"] = _jsonld(cfg["active"], snap[key], response)
        snap["terms"] = {url: self._terms(url) for url in cfg.get("terms") or []}
        return snap

    def _robots_for(self, origin: str) -> tuple[Any, int | str, str | None]:
        """robots.txt d'une origine, lu une fois par audit : (Robots ou None, statut, empreinte).

        Seuls 404 et 410 valent « aucune règle » ; tout autre échec rend l'hôte non vérifiable :
        il n'est plus sollicité (même règle que le relevé).
        """
        if origin not in self._robots:
            status, response = self.get(origin + "/robots.txt")
            if status == 200 and response is not None:
                robots = parse(response.text)
                rules = fingerprint(sorted(r.raw for r in robots.rules))
            elif status in (404, 410):
                robots, rules = parse(""), None
            else:
                robots, rules = None, None
                self._blocked_hosts.add(urlsplit(origin).netloc)
            self._robots[origin] = (robots, status, rules)
        return self._robots[origin]

    def _robots_section(self, origin: str, cfg: dict[str, Any]) -> dict[str, Any]:
        robots, status, rules = self._robots_for(origin)
        out: dict[str, Any] = {"status": status, "rules": rules}
        verdicts = {}
        for path in cfg.get("paths") or [urlsplit(cfg.get("product") or cfg["home"]).path]:
            if robots is None:
                verdicts[path] = "non vérifiable"
                continue
            allowed, rule = robots.verdict(origin + path)
            verdicts[path] = ("autorisé" if allowed else "interdit") + (
                f" ({rule.raw})" if rule else ""
            )
        out["verdicts"] = verdicts
        return out

    def _tdmrep(self, origin: str) -> dict[str, Any]:
        url = origin + "/.well-known/tdmrep.json"
        if not self._allowed(url):
            return {"tdmrep": "non demandé"}
        status, response = self.get(url)
        if status != 200 or response is None:
            return {"tdmrep": status}
        try:
            policy = json.loads(response.text)
        except ValueError:
            # Page de défi ou page HTML servie en 200 : pas de déclaration TDMRep.
            return {"tdmrep": "200, pas un fichier TDMRep"}
        canonical = json.dumps(policy, sort_keys=True, ensure_ascii=False)
        return {"tdmrep": f"200 ({fingerprint([canonical])})"}

    def _page(self, url: str) -> tuple[dict[str, Any], httpx.Response | None]:
        if not self._allowed(url):
            reason = "bloqué" if urlsplit(url).netloc in self._blocked_hosts else "interdit"
            return {"status": f"non demandé ({reason})", "blocked": None}, None
        status, response = self.get(url)
        blocked = status in BLOCK_STATUSES
        marker = None
        if response is not None and not blocked:
            marker = next((m for m in CHALLENGE_MARKERS if m in response.text), None)
            blocked = marker is not None
        if blocked:
            self._blocked_hosts.add(urlsplit(url).netloc)  # plus de requête vers cet hôte
        snap: dict[str, Any] = {"status": status, "blocked": blocked}
        if marker:
            snap["challenge"] = marker
        return snap, response

    def _terms(self, url: str) -> dict[str, Any]:
        page, response = self._page(url)
        if response is None or page["blocked"] or page["status"] != 200:
            return {"status": page["status"], "clauses": None, "hash": None}
        found = clauses(response.text)
        return {"status": 200, "clauses": len(found), "hash": fingerprint(found)}

    def _allowed(self, url: str) -> bool:
        """URL demandable : hôte non bloqué, robots.txt vérifiable et qui l'autorise."""
        parts = urlsplit(url)
        if parts.netloc in self._blocked_hosts:
            return False
        robots, _, _ = self._robots_for(f"{parts.scheme}://{parts.netloc}")
        return robots is not None and robots.allowed(url)

    def close(self) -> None:
        self.http.close()


def _tdm_headers(response: httpx.Response | None) -> dict[str, Any]:
    if response is None:
        return {"header": None, "meta": None}
    meta = TDM_META.search(response.text)
    return {
        "header": response.headers.get("tdm-reservation"),
        "meta": meta.group(1) if meta else None,
    }


def _jsonld(parser: str, page: dict[str, Any], response: httpx.Response | None) -> str:
    if response is None or page["blocked"] or page["status"] != 200:
        return f"non lu ({page['status']})"
    try:
        product = src.PARSERS[parser](response.text)
    except src.MissingData as exc:
        return f"cassé : {exc}"
    return "lisible" if product.eans else "lisible, sans gtin"


# --- Comparaison ------------------------------------------------------------------------------


def flatten(snap: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(snap, dict):
        out: dict[str, Any] = {}
        for key, value in snap.items():
            out |= flatten(value, f"{prefix}.{key}" if prefix else key)
        return out
    return {prefix: snap}


LABELS = {
    "robots.status": "robots.txt : statut HTTP",
    "robots.rules": "robots.txt : règles qui s'appliquent à HorsPC (empreinte)",
    "tdm.tdmrep": "TDMRep (/.well-known/tdmrep.json)",
    "tdm.header": "TDMRep : en-tête tdm-reservation",
    "tdm.meta": "TDMRep : meta tdm-reservation",
    "home.status": "accueil : statut HTTP",
    "home.blocked": "accueil : blocage",
    "home.challenge": "accueil : page de défi",
    "product.status": "fiche : statut HTTP",
    "product.blocked": "fiche : blocage",
    "product.challenge": "fiche : page de défi",
    "jsonld": "fiche : JSON-LD",
}


def describe(key: str, before: Any, after: Any) -> str:
    if key.startswith("robots.verdicts."):
        label = f"robots.txt : verdict pour `{key.removeprefix('robots.verdicts.')}`"
    elif key.startswith("terms."):
        url, _, part = key.removeprefix("terms.").rpartition(".")
        if part == "hash":
            return f"CGU <{url}> : clauses modifiées (empreinte {before} → {after}), à relire"
        label = f"CGU <{url}> : {'statut HTTP' if part == 'status' else 'nombre de clauses'}"
    else:
        label = LABELS.get(key, key)
    if key.endswith(".blocked") and isinstance(before, bool) and isinstance(after, bool):
        return f"{label} : {'blocage levé' if before else 'blocage apparu'}"
    return f"{label} : {before} → {after}"


def carry_terms(previous: dict[str, Any] | None, current: dict[str, Any]) -> None:
    """Une CGU illisible ce mois-ci (bloquée, 403) garde sa dernière empreinte lue : seule une
    lecture réussie peut signaler un changement de clauses, pas un aléa d'accès."""
    if previous is None:
        return
    for sid, snap in current["sources"].items():
        old_terms = previous.get("sources", {}).get(sid, {}).get("terms", {})
        for url, term in snap.get("terms", {}).items():
            old = old_terms.get(url)
            if term["status"] != 200 and old and old.get("hash") is not None:
                term.update(clauses=old["clauses"], hash=old["hash"], status=old["status"])
                term["unread"] = current["checked_at"]


def compare(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, list[str]]:
    """Changements par enseigne (vide si aucun) ; une enseigne nouvelle n'est pas un changement.

    Les CGU sont comparées seulement quand elles ont été lues (voir carry_terms)."""
    if previous is None:
        return {}
    changes: dict[str, list[str]] = {}
    for sid, snap in current["sources"].items():
        old = previous.get("sources", {}).get(sid)
        if old is None:
            continue
        a, b = flatten(old), flatten(snap)
        for flat in (a, b):  # date de dernière lecture manquée : information, pas changement
            for key in [k for k in flat if k.endswith(".unread")]:
                del flat[key]
        diff = [
            describe(k, a.get(k), b.get(k))
            for k in sorted(a.keys() | b.keys())
            if a.get(k) != b.get(k)
        ]
        if diff:
            changes[sid] = diff
    return changes


def run(registry: dict[str, dict[str, Any]], auditor: Auditor, today: date) -> dict[str, Any]:
    sources = {}
    for sid, cfg in registry.items():
        sources[sid] = auditor.audit(sid, cfg)
    return {"schema": 1, "checked_at": today.isoformat(), "sources": sources}


def audit_and_compare(
    registry: dict[str, dict[str, Any]],
    auditor: Auditor,
    previous: dict[str, Any] | None,
    today: date,
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Nouvel état (CGU illisibles reprises de l'état précédent) et changements à relire."""
    current = run(registry, auditor, today)
    carry_terms(previous, current)
    return current, compare(previous, current)


# --- Rapports ---------------------------------------------------------------------------------


def changes_markdown(
    changes: dict[str, list[str]], registry: dict[str, dict[str, Any]], today: date
) -> str:
    lines = [
        f"Audit du {today.strftime('%d/%m/%Y')} depuis les IP de GitHub Actions. "
        "Chaque point est à relire à la main (docs/sources.md) ; aucune source n'a été activée "
        "ni désactivée automatiquement.",
        "",
    ]
    for sid, diff in changes.items():
        cfg = registry.get(sid, {})
        lines.append(f"### {cfg.get('name', sid)} ({cfg.get('status', '?')})")
        lines += [f"- {d}" for d in diff]
        lines.append("")
    return "\n".join(lines)


def summary_markdown(
    current: dict[str, Any],
    changes: dict[str, list[str]],
    registry: dict[str, dict[str, Any]],
    initial: bool,
) -> str:
    lines = ["## Audit des sources de prix", ""]
    if initial:
        lines += ["État initial enregistré (pas d'état précédent à comparer).", ""]
    elif changes:
        lines += [
            f"**{len(changes)} enseigne(s) avec des changements à relire** (issue ouverte).",
            "",
        ]
    else:
        lines += ["Aucun changement depuis le dernier audit.", ""]
    lines += [
        "| Enseigne | Statut | robots.txt | Accueil | Fiche | JSON-LD |",
        "|---|---|---|---|---|---|",
    ]
    for sid, snap in current["sources"].items():
        cfg = registry[sid]
        home = snap["home"]
        product = snap.get("product", {})
        cells = [
            cfg["name"],
            cfg["status"],
            str(snap["robots"]["status"]),
            f"{home['status']}{' (bloqué)' if home.get('blocked') else ''}",
            f"{product.get('status', '-')}{' (bloqué)' if product.get('blocked') else ''}",
            snap.get("jsonld", "-"),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
