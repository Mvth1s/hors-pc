"""Rendu statique : catalogue lisible sans JS, pages de jeux, JSON-LD, sitemap."""

import html
import json
import re
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from horspc.models import DataFile
from horspc.render import GAMES_DIR, SITE_URL, external_links, render_site

SITE = Path(__file__).parents[1] / "site"
VERIFICATION = (
    '<meta name="google-site-verification" content="Ab9vPVa0ebMxdxThZr2DukWFs8mVU9BNoJDm-ucitxA">'
)


def jsonld(text: str) -> list[dict[str, Any]]:
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', text, re.S)
    return [json.loads(b) for b in blocks]


def canonical(text: str) -> str:
    return re.search(r'<link rel="canonical" href="([^"]+)">', text).group(1)


def footer(text: str) -> str:
    return re.search(r"<footer\b.*?</footer>", text, re.S).group(0)


def root_pages() -> list[Path]:
    return sorted(SITE.glob("*.html"))


def game_pages() -> list[Path]:
    return sorted(SITE.glob(f"{GAMES_DIR}/*/index.html"))


def visible_games() -> list[dict[str, Any]]:
    data = json.loads((SITE / "data.json").read_text(encoding="utf-8"))
    return [g for g in data["games"] if g["pc_status"] != "released"]


# --- Site versionné (rendu par le dernier build) ------------------------------------


def test_catalogue_lisible_sans_js() -> None:
    text = (SITE / "index.html").read_text(encoding="utf-8")
    titles = re.findall(r'<h2 class="title"><a href="jeux/[^"]+/">([^<]+)</a></h2>', text)
    expected = [g["title"] for g in visible_games()]
    assert len(titles) == len(expected) == 8
    assert sorted(t.replace("&#x27;", "'") for t in titles) == sorted(expected)
    assert re.search(r'<strong id="count">8 jeux</strong>', text)
    assert "fetch(" not in (SITE / "app.js").read_text(encoding="utf-8")


def test_verification_search_console_dans_index() -> None:
    head = (SITE / "index.html").read_text(encoding="utf-8").split("</head>")[0]
    assert VERIFICATION in head
    for page in root_pages():
        if page.name != "index.html":
            assert "google-site-verification" not in page.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "page", root_pages() + game_pages(), ids=lambda p: str(p.relative_to(SITE))
)
def test_seo_commun(page: Path) -> None:
    text = page.read_text(encoding="utf-8")
    assert text.startswith('<!doctype html>\n<html lang="fr">')
    url = canonical(text)
    assert url.startswith(SITE_URL)
    assert re.search(r'<meta name="description" content="[^"]{30,}">', text)
    for prop in ("og:title", "og:description", "og:url", "og:image", "og:type"):
        assert f'property="{prop}"' in text, prop
    assert re.search(rf'<meta property="og:url" content="{re.escape(url)}">', text)
    assert re.search(r'<meta property="og:image" content="https://[^"]+">', text)
    assert re.search(r'<meta name="twitter:card" content="summary(_large_image)?">', text)
    assert len(re.findall(r"<h1\b", text)) == 1
    for tag in ("<header", "<main", "<footer"):
        assert text.count(tag) == 1, tag
    for data in jsonld(text):
        assert data["@context"] == "https://schema.org"


def test_jsonld_accueil() -> None:
    (graph,) = jsonld((SITE / "index.html").read_text(encoding="utf-8"))
    types = {node["@type"]: node for node in graph["@graph"]}
    assert set(types) == {"WebSite", "Person", "ItemList"}
    assert types["WebSite"]["url"] == SITE_URL
    assert (types["Person"]["name"], types["Person"]["alternateName"]) == (
        "Mvth1s",
        "Mathis Aguado",
    )
    items = types["ItemList"]["itemListElement"]
    assert [i["position"] for i in items] == list(range(1, 9))
    assert all(i["url"].startswith(f"{SITE_URL}{GAMES_DIR}/") for i in items)


@pytest.mark.parametrize("page", game_pages(), ids=lambda p: p.parent.name)
def test_page_jeu(page: Path) -> None:
    text = page.read_text(encoding="utf-8")
    slug = page.parent.name
    assert canonical(text) == f"{SITE_URL}{GAMES_DIR}/{slug}/"
    (game,) = jsonld(text)
    assert game["@type"] == "VideoGame"
    assert game["url"] == canonical(text)
    assert game["image"].startswith(SITE_URL)
    assert set(game["gamePlatform"]) <= {"PlayStation 5", "Nintendo Switch", "Nintendo Switch 2"}
    assert 'href="../../"' in text  # lien vers le catalogue


def test_footer_des_pages_de_jeux() -> None:
    reference = footer((SITE / "index.html").read_text(encoding="utf-8"))
    expected = re.sub(
        r'\b(href|src)="(?!https?:|mailto:|#|&#)([^"]*)"', r'\1="../../\2"', reference
    )
    for page in game_pages():
        assert footer(page.read_text(encoding="utf-8")) == expected, page.parent.name


def test_sitemap_liste_toutes_les_pages() -> None:
    text = (SITE / "sitemap.xml").read_text(encoding="utf-8")
    locs = re.findall(r"<loc>(.*?)</loc>", text)
    expected = {SITE_URL} | {SITE_URL + p.name for p in root_pages() if p.name != "index.html"}
    expected |= {f"{SITE_URL}{GAMES_DIR}/{p.parent.name}/" for p in game_pages()}
    assert set(locs) == expected
    assert len(re.findall(r"<lastmod>\d{4}-\d{2}-\d{2}</lastmod>", text)) == len(locs)
    assert not (SITE / "robots.txt").exists()  # ne serait pas lu en sous-chemin (cf. README)


# --- Rendus synthétiques ----------------------------------------------------------------

NOW = datetime(2026, 10, 26, 6, 0, tzinfo=UTC)


def game(slug: str, **kw: Any) -> dict[str, Any]:
    return {
        "slug": slug,
        "title": slug.replace("-", " ").title(),
        "platforms": ["ps5"],
        "release_date": "2024-09-06",
        "release_date_source": "igdb",
        "launch_price_cents": 6999,
        "igdb_id": 1,
        "igdb_url": "https://www.igdb.com/games/x",
        "igdb_fetched_at": "2026-10-26T06:00:00Z",
        "cover_url": None,
        "cover_image_id": None,
        "pc_status": "none",
        "pc_status_source": "igdb",
        "developers": [],
        "publishers": [],
        "summary": None,
        "summary_source": None,
        "screenshots": [],
        "trailer_youtube_id": None,
        "offer": None,
    } | kw


def offer(observed: date, stale: bool, price: int = 5330) -> dict[str, Any]:
    return {
        "price_cents": price,
        "shipping_cents": None,
        "discount_pct": 24,
        "format": "physical",
        "condition": "new",
        "merchant": "Amazon",
        "seller": None,
        "url": "https://www.amazon.fr/dp/X",
        "observed_at": observed.isoformat(),
        "source": "manual",
        "stale": stale,
    }


@pytest.fixture
def site(tmp_path: Path) -> Path:
    for page in root_pages():
        shutil.copy(page, tmp_path / page.name)
    return tmp_path


def render(site: Path, games: list[dict[str, Any]], now: datetime = NOW) -> None:
    data = DataFile.model_validate({"generated_at": now, "games": games})
    render_site(data, site)


def test_offre_valide_et_offre_perimee(site: Path) -> None:
    fresh_day = date(2026, 10, 20)
    render(
        site,
        [
            game("frais", offer=offer(fresh_day, stale=False)),
            game("ancien", offer=offer(date(2026, 7, 1), stale=True)),
            game("sans-prix"),
        ],
    )
    (fresh,) = jsonld((site / GAMES_DIR / "frais" / "index.html").read_text(encoding="utf-8"))
    assert fresh["offers"] == {
        "@type": "Offer",
        "price": "53.30",
        "priceCurrency": "EUR",
        "priceValidUntil": (fresh_day + timedelta(days=30)).isoformat(),
        "availability": "https://schema.org/InStock",
        "itemCondition": "https://schema.org/NewCondition",
        "url": "https://www.amazon.fr/dp/X",
        "seller": {"@type": "Organization", "name": "Amazon"},
    }
    for slug in ("ancien", "sans-prix"):
        (data,) = jsonld((site / GAMES_DIR / slug / "index.html").read_text(encoding="utf-8"))
        assert "offers" not in data
    stale_page = (site / GAMES_DIR / "ancien" / "index.html").read_text(encoding="utf-8")
    assert "Prix à vérifier" in stale_page


def test_jeu_sorti_sur_pc_ni_liste_ni_page(site: Path) -> None:
    render(site, [game("reste"), game("parti")])
    render(site, [game("reste"), game("parti", pc_status="released")])
    index = (site / "index.html").read_text(encoding="utf-8")
    assert "jeux/parti/" not in index
    assert not (site / GAMES_DIR / "parti").exists()
    assert "parti" not in (site / "sitemap.xml").read_text(encoding="utf-8")


def lastmods(site: Path) -> dict[str, str]:
    text = (site / "sitemap.xml").read_text(encoding="utf-8")
    return dict(re.findall(r"<loc>(.*?)</loc>\s*<lastmod>(.*?)</lastmod>", text))


def test_lastmod_reel(site: Path) -> None:
    render(site, [game("stable"), game("modifie")])
    later = NOW + timedelta(days=7)
    render(site, [game("stable"), game("modifie", launch_price_cents=5999)], later)
    dates = lastmods(site)
    assert dates[f"{SITE_URL}{GAMES_DIR}/stable/"] == "2026-10-26"
    assert dates[f"{SITE_URL}{GAMES_DIR}/modifie/"] == "2026-11-02"
    assert dates[SITE_URL] == "2026-11-02"
    # Pages statiques : la date « Dernière mise à jour » affichée dans la page.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", dates[SITE_URL + "faq.html"])


def test_rendu_idempotent(site: Path) -> None:
    games = [game("a", offer=offer(date(2026, 10, 20), stale=False)), game("b")]
    render(site, games)
    first = {p: p.read_text(encoding="utf-8") for p in site.rglob("*.html")}
    render(site, games)
    assert {p: p.read_text(encoding="utf-8") for p in site.rglob("*.html")} == first


# --- Liens externes -----------------------------------------------------------------------

LINK = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S)


def all_pages() -> list[Path]:
    return root_pages() + game_pages()


@pytest.mark.parametrize("page", all_pages(), ids=lambda p: str(p.relative_to(SITE)))
def test_liens_externes_nouvel_onglet(page: Path) -> None:
    text = page.read_text(encoding="utf-8")
    checked = 0
    for attrs, inner in LINK.findall(text):
        href = re.search(r'href="([^"]*)"', attrs).group(1)
        if href.startswith("mailto:") or html_entity_mailto(href):
            assert "target=" not in attrs, href
            continue
        if not re.match(r"https?://", href):
            continue
        checked += 1
        assert 'target="_blank"' in attrs, href
        rel = re.search(r'rel="([^"]*)"', attrs).group(1).split()
        assert {"noopener", "noreferrer"} <= set(rel), href
        label = re.search(r'aria-label="([^"]*)"', attrs)
        if label:
            assert label.group(1).endswith("(nouvel onglet)"), href
        else:
            assert '<span class="visually-hidden"> (nouvel onglet)</span>' in inner, href
            assert "↗" in inner or "<svg" in inner, href
    assert checked > 0


def html_entity_mailto(href: str) -> bool:
    return html.unescape(href).startswith("mailto:")


def test_external_links_unitaire() -> None:
    source = (
        '<a href="https://x.fr/a" rel="noopener">Code source ↗</a>'
        '<a class="cta" href="https://shop.fr" rel="noopener nofollow" target="_blank">Voir'
        '<svg aria-hidden="true"></svg></a>'
        '<a href="mailto:a@b.fr">a@b.fr</a>'
        '<a href="faq.html">FAQ</a>'
        '<a href="https://gh.com" aria-label="GitHub"><svg></svg></a>'
    )
    out = external_links(source)
    assert external_links(out) == out  # idempotent
    assert (
        '<a href="https://x.fr/a" target="_blank" rel="noopener noreferrer">Code source'
        '<span class="ext-icon" aria-hidden="true">↗</span>'
        '<span class="visually-hidden"> (nouvel onglet)</span></a>'
    ) in out
    assert 'rel="nofollow noopener noreferrer"' in out
    assert out.count('class="ext-icon"') == 1  # le CTA garde sa propre icône SVG
    assert '<a href="mailto:a@b.fr">a@b.fr</a>' in out
    assert '<a href="faq.html">FAQ</a>' in out
    assert 'aria-label="GitHub (nouvel onglet)"' in out


# --- Page de jeu enrichie -------------------------------------------------------------------


def rich(**kw: Any) -> dict[str, Any]:
    return game(
        "riche",
        developers=["Team Asobi"],
        publishers=["Sony Interactive Entertainment"],
        summary="First line.\nSecond line.",
        summary_source="igdb",
        screenshots=[
            {"image_id": "scaaa1", "url": "screens/riche/scaaa1.webp"},
            {"image_id": "scaaa2", "url": "screens/riche/scaaa2.webp"},
        ],
        trailer_youtube_id="wHMNQzLG_Jg",
        **kw,
    )


def test_page_jeu_enrichie(site: Path) -> None:
    render(site, [rich()])
    text = (site / GAMES_DIR / "riche" / "index.html").read_text(encoding="utf-8")
    assert "<dt>Studio</dt><dd>Team Asobi</dd>" in text
    assert "<dt>Éditeur</dt><dd>Sony Interactive Entertainment</dd>" in text
    assert '<div lang="en"><p>First line.</p><p>Second line.</p></div>' in text
    assert "Résumé IGDB (en anglais)" in text
    assert text.count('loading="lazy" decoding="async" alt="Capture de') == 2
    assert 'src="../../screens/riche/scaaa1.webp" width="940" height="529"' in text
    assert 'data-youtube-id="wHMNQzLG_Jg"' in text
    assert "La lecture charge YouTube (Google), qui peut déposer des cookies." in text
    assert "<iframe" not in text
    (data,) = jsonld(text)
    assert data["author"] == [{"@type": "Organization", "name": "Team Asobi"}]
    assert data["publisher"][0]["name"] == "Sony Interactive Entertainment"
    assert data["description"] == "First line.\nSecond line."


def test_description_fr_sans_mention_igdb(site: Path) -> None:
    render(site, [rich() | {"summary": "Résumé en français.", "summary_source": "manual"}])
    text = (site / GAMES_DIR / "riche" / "index.html").read_text(encoding="utf-8")
    assert "<p>Résumé en français.</p>" in text
    assert "Résumé IGDB" not in text and 'lang="en"' not in text.split("<main", 1)[1]


def test_capture_hors_dossier_du_jeu_refusee() -> None:
    bad = rich() | {"screenshots": [{"image_id": "x", "url": "screens/autre/x.webp"}]}
    with pytest.raises(ValueError, match="capture"):
        DataFile.model_validate({"generated_at": NOW, "games": [bad]})
