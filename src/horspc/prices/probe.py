"""Diagnostic `scrape_dry_run` : ce que voit le runner (IP GitHub Actions) pour chaque source.

Mêmes règles que le relevé (robots.txt, 1 requête/s par hôte, User-Agent identifiable, aucun
contournement) ; rien n'est écrit dans data/ et rien n'est publié.
"""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from horspc.prices import sources as src
from horspc.prices.fetch import Disallowed, Fetcher, SourceBlocked

# Fiche témoin si discovered.json n'en donne pas (Mario Kart World, EAN vérifié le 25/10/2026).
FALLBACK_EAN = "0045496312336"


@dataclass
class ProbeRow:
    source: str
    kind: str  # robots.txt | catégorie | sitemap | fiche
    url: str
    result: str
    detail: str


def probe_urls(source: str, discovered: dict[str, Any]) -> list[tuple[str, str]]:
    """(type, URL) à tester : point d'entrée de la découverte, puis une fiche connue."""
    games = discovered.get("games", {}).values()
    known = next((g["sources"][source] for g in games if source in g["sources"]), None)
    if source == "leclerc":
        return [("fiche", known or src.leclerc_url(FALLBACK_EAN))]
    if source == "boulanger":
        entry = [("catégorie", src.BOULANGER + src.BOULANGER_CATEGORIES["ps5"])]
        return entry + ([("fiche", known)] if known else [])
    if source == "pixmania":
        return [("sitemap", src.PIXMANIA_SITEMAP)] + ([("fiche", known)] if known else [])
    return []


def _describe(source: str, kind: str, response: httpx.Response) -> str:
    if kind == "catégorie":
        return f"{len(src.boulanger_listing(response.text))} produits lus"
    if kind == "sitemap":
        return f"{len(src.sitemap_locs(response.content))} sitemaps listés"
    try:
        page = src.PARSERS[source](response.text)
    except src.MissingData as exc:
        return f"JSON-LD : {exc}"
    price = "sans prix" if page.price is None else f"{page.price} €"
    return f"JSON-LD OK : {price}, vendeur {page.seller or 'non identifié'}"


def probe(fetcher: Fetcher, enabled: list[str], discovered: dict[str, Any]) -> list[ProbeRow]:
    rows: list[ProbeRow] = []
    for source in enabled:
        seen: set[str] = set()
        for kind, url in probe_urls(source, discovered):
            parts = urlsplit(url)
            origin = f"{parts.scheme}://{parts.netloc}"
            if origin not in seen:
                seen.add(origin)
                robots = fetcher.robots.get(url)
                if robots.unverifiable:
                    state = f"{robots.unverifiable} : source non sollicitée"
                elif robots.absent:
                    state = "absent (404 ou 410) : aucune règle, tout autorisé"
                elif not robots.rules:
                    state = "lu, aucune règle pour HorsPC : tout autorisé"
                else:
                    state = f"lu ({len(robots.rules)} règles pour HorsPC)"
                rows.append(ProbeRow(source, "robots.txt", f"{origin}/robots.txt", state, ""))
            rows.append(_probe_one(fetcher, source, kind, url))
    return rows


def _probe_one(fetcher: Fetcher, source: str, kind: str, url: str) -> ProbeRow:
    try:
        response = fetcher.get(source, url)
    except SourceBlocked as exc:
        return ProbeRow(source, kind, url, "bloqué", str(exc))
    except Disallowed as exc:
        return ProbeRow(source, kind, url, "interdit par robots.txt", str(exc))
    except httpx.HTTPStatusError as exc:
        return ProbeRow(source, kind, url, f"HTTP {exc.response.status_code}", "")
    except httpx.HTTPError as exc:
        return ProbeRow(source, kind, url, "erreur réseau", str(exc)[:120])
    detail = _describe(source, kind, response)
    if str(response.url) != url:
        detail += f" (redirigé vers {response.url})"
    return ProbeRow(source, kind, url, f"HTTP {response.status_code}", detail)


def markdown(rows: list[ProbeRow], user_agent: str) -> str:
    """Tableau pour $GITHUB_STEP_SUMMARY."""
    cell = lambda text: text.replace("|", "\\|").replace("\n", " ")  # noqa: E731
    lines = [
        "## Diagnostic des sources (scrape_dry_run)",
        "",
        f"User-Agent : `{user_agent}`. Rien n'est écrit ni publié.",
        "",
        "| Source | Type | URL | Résultat | Détail |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {r.source} | {r.kind} | {cell(r.url)} | {cell(r.result)} | {cell(r.detail)} |"
        for r in rows
    ]
    return "\n".join(lines) + "\n"
