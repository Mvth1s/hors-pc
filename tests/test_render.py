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
from horspc.render import GAMES_DIR, SCHEMA_PLATFORMS, SITE_URL, external_links, render_site

SITE = Path(__file__).parents[1] / "site"
VERIFICATION = (
    '<meta name="google-site-verification" content="Ab9vPVa0ebMxdxThZr2DukWFs8mVU9BNoJDm-ucitxA">'
)


def jsonld(text: str) -> list[dict[str, Any]]:
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', text, re.S)
    return [json.loads(b) for b in blocks]


XBOX_BADGE = (
    '<span class="console xb"><span class="c-long">Xbox Series</span>'
    '<span class="c-short" aria-hidden="true">Xbox</span></span>'
)


def game_jsonld(text: str) -> dict[str, Any]:
    """JSON-LD VideoGame d'une page de jeu (à côté de son BreadcrumbList)."""
    (game,) = [d for d in jsonld(text) if d["@type"] == "VideoGame"]
    return game


def canonical(text: str) -> str:
    return re.search(r'<link rel="canonical" href="([^"]+)">', text).group(1)


def footer(text: str) -> str:
    return re.search(r"<footer\b.*?</footer>", text, re.S).group(0)


def root_pages() -> list[Path]:
    return sorted(SITE.glob("*.html"))


def is_redirect(page: Path) -> bool:
    return 'http-equiv="refresh"' in page.read_text(encoding="utf-8")


def game_pages() -> list[Path]:
    """Pages de jeux, hors redirections des anciens slugs (jeux fusionnés)."""
    return [p for p in sorted(SITE.glob(f"{GAMES_DIR}/*/index.html")) if not is_redirect(p)]


def visible_games() -> list[dict[str, Any]]:
    data = json.loads((SITE / "data.json").read_text(encoding="utf-8"))
    return [g for g in data["games"] if g["pc_status"] != "released"]


# --- Site versionné (rendu par le dernier build) ------------------------------------


def test_catalogue_lisible_sans_js() -> None:
    text = (SITE / "index.html").read_text(encoding="utf-8")
    titles = re.findall(r'<h2 class="title"><a href="jeux/[^"]+/">([^<]+)</a></h2>', text)
    expected = [g["title"] for g in visible_games()]
    assert len(titles) == len(expected) >= 8
    assert sorted(t.replace("&#x27;", "'") for t in titles) == sorted(expected)
    assert f'<strong id="count">{len(expected)} jeux</strong>' in text
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
    assert [i["position"] for i in items] == list(range(1, len(visible_games()) + 1))
    assert all(i["url"].startswith(f"{SITE_URL}{GAMES_DIR}/") for i in items)


@pytest.mark.parametrize("page", game_pages(), ids=lambda p: p.parent.name)
def test_page_jeu(page: Path) -> None:
    text = page.read_text(encoding="utf-8")
    slug = page.parent.name
    assert canonical(text) == f"{SITE_URL}{GAMES_DIR}/{slug}/"
    game = game_jsonld(text)
    assert game["@type"] == "VideoGame"
    assert game["url"] == canonical(text)
    assert game["image"].startswith(SITE_URL)
    assert set(game["gamePlatform"]) <= set(SCHEMA_PLATFORMS.values())
    assert '<a class="back-btn" href="../../" data-back>← Retour au catalogue</a>' in text
    assert "Fil d'Ariane" not in text and 'class="back"' not in text
    (crumbs,) = [d for d in jsonld(text) if d["@type"] == "BreadcrumbList"]
    assert [i["item"] for i in crumbs["itemListElement"]] == [SITE_URL, canonical(text)]
    assert text.count('class="game-facts"') == 1


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


def game(
    slug: str,
    platforms: tuple[str, ...] = ("ps5",),
    offer: dict[str, Any] | None = None,
    sold_digital: bool = False,
    listed_physical: bool = False,
    offers: dict[str, dict[str, Any]] | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """Jeu de data.json ; `offer` va à la première édition, `offers` donne l'offre par édition."""
    offers = dict(offers or {})
    if offer is not None:
        offers[platforms[0]] = offer
    editions = [
        {
            "platform": p,
            "offer": None if p not in offers else offers[p] | {"platform": p},
            "sold_digital": sold_digital,
            "listed_physical": listed_physical,
        }
        for p in platforms
    ]
    with_offer = [e["offer"] for e in editions if e["offer"]]
    best = min(with_offer, key=lambda o: o["price_cents"]) if with_offer else None
    return {
        "slug": slug,
        "title": slug.replace("-", " ").title(),
        "platforms": list(platforms),
        "editions": editions,
        "best_offer": best,
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
    fresh = game_jsonld((site / GAMES_DIR / "frais" / "index.html").read_text(encoding="utf-8"))
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
        data = game_jsonld((site / GAMES_DIR / slug / "index.html").read_text(encoding="utf-8"))
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
    data = game_jsonld(text)
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


def test_jeu_a_venir_badge_date_et_statut_pc(site: Path) -> None:
    render(
        site,
        [
            game("gta", release_date="2026-11-19", release_date_source="igdb"),
            game("sorti", release_date="2026-10-26", release_date_source="igdb"),
        ],
    )
    index = (site / "index.html").read_text(encoding="utf-8")
    gta = re.search(r'<article class="card"[^>]*data-title="Gta".*?</article>', index).group(0)
    # « À venir » s'ajoute au statut PC sans le remplacer.
    assert '<span class="status ex">Exclusif console</span>' in gta
    assert (
        '<span class="status soon" data-release="2026-11-19">'
        '<span class="visually-hidden">À venir : </span>Sortie le 19/11/2026</span>'
    ) in gta
    assert "<span>À venir</span>" in gta
    sorti = re.search(r'<article class="card"[^>]*data-title="Sorti".*?</article>', index)
    assert "status soon" not in sorti.group(0)  # sorti le jour du build : plus à venir
    page = (site / GAMES_DIR / "gta" / "index.html").read_text(encoding="utf-8")
    assert "Sortie le 19/11/2026" in page and "<dd>19/11/2026 (à venir)</dd>" in page


def test_xbox_series_badge_et_jsonld(site: Path) -> None:
    render(site, [game("halo", platforms=("xbox_series",))])
    index = (site / "index.html").read_text(encoding="utf-8")
    assert XBOX_BADGE in index
    data = game_jsonld((site / GAMES_DIR / "halo" / "index.html").read_text(encoding="utf-8"))
    assert data["gamePlatform"] == ["Xbox Series X|S"]


def test_filtre_xbox_series_et_lien_de_pied_de_page() -> None:
    app = (SITE / "app.js").read_text(encoding="utf-8")
    assert 'xbox_series: { label: "Xbox Series", platforms: ["xbox_series"] }' in app
    index = (SITE / "index.html").read_text(encoding="utf-8")
    assert 'data-platform="xbox_series"' in index
    for page in root_pages():
        assert '<a href="./?console=xbox_series">Xbox Series</a>' in page.read_text(
            encoding="utf-8"
        )


def test_positionnement_jeux_console_absents_du_pc() -> None:
    index = (SITE / "index.html").read_text(encoding="utf-8")
    assert "<title>HorsPC, les jeux console absents du PC</title>" in index
    assert "Xbox Series" in re.search(r'<p class="tagline">(.*?)</p>', index).group(1)


def test_boite_avec_code_affichee(site: Path) -> None:
    box = offer(date(2026, 10, 20), stale=False) | {"format": "code_box"}
    render(site, [game("gta", offer=box)])
    index = (site / "index.html").read_text(encoding="utf-8")
    # Libellé court sur la carte, complet sur la page.
    assert '<span class="fmt">Code dans la boîte</span>' in index
    assert "Physique</span>" not in index
    page = (site / GAMES_DIR / "gta" / "index.html").read_text(encoding="utf-8")
    assert "<dt>Format de l'offre</dt><dd>Boîte avec code de téléchargement</dd>" in page


@pytest.mark.parametrize(
    ("fields", "offer_format", "badge"),
    [
        ({"sold_digital": True, "listed_physical": True}, None, True),
        ({"sold_digital": True}, "physical", True),  # offre physique relevée = preuve
        ({"sold_digital": True}, "code_box", False),  # une boîte avec code ne prouve rien
        ({"sold_digital": True}, None, False),
        ({"listed_physical": True}, "physical", False),  # jamais « Physique uniquement »
    ],
)
def test_badge_physique_et_numerique(
    site: Path, fields: dict, offer_format: str | None, badge: bool
) -> None:
    extra = (
        {}
        if offer_format is None
        else {"offer": offer(date(2026, 10, 20), stale=False) | {"format": offer_format}}
    )
    render(site, [game("jeu", **fields, **extra)])
    for path in (site / "index.html", site / GAMES_DIR / "jeu" / "index.html"):
        text = path.read_text(encoding="utf-8")
        assert ('<span class="status fmt">Physique et numérique</span>' in text) is badge
        assert "Numérique uniquement" not in text and "Physique uniquement" not in text


def test_ancien_slug_redirige_hors_sitemap(site: Path) -> None:
    render_site(
        DataFile.model_validate({"generated_at": NOW, "games": [game("gta")]}),
        site,
        {"gta-xbox": "gta", "inconnu": "absent"},
    )
    page = (site / GAMES_DIR / "gta-xbox" / "index.html").read_text(encoding="utf-8")
    target = f"{SITE_URL}{GAMES_DIR}/gta/"
    assert f'<link rel="canonical" href="{target}">' in page
    assert f'<meta http-equiv="refresh" content="0; url={target}">' in page
    sitemap = (site / "sitemap.xml").read_text(encoding="utf-8")
    assert "gta-xbox" not in sitemap and target in sitemap
    assert not (site / GAMES_DIR / "inconnu").exists()  # cible absente : pas de redirection


# --- Éditions -------------------------------------------------------------------------------


def multi(**kw: Any) -> dict[str, Any]:
    """GTA VI à deux éditions : PS5 relevée à 60 €, Xbox Series à 55 €, ou sans offre."""
    day = date(2026, 10, 20)
    offers = {
        "ps5": offer(day, stale=False, price=6000) | {"format": "code_box"},
        "xbox_series": offer(day, stale=False, price=5500),
    }
    return game("gta", platforms=("ps5", "xbox_series"), offers=offers) | kw


def card(index: str, title: str) -> str:
    return re.search(
        rf'<article class="card"[^>]*data-title="{title}".*?</article>', index, re.S
    ).group(0)


def test_carte_multi_editions_vues_par_filtre(site: Path) -> None:
    render(site, [multi()])
    html_card = card((site / "index.html").read_text(encoding="utf-8"), "Gta")
    assert html_card.count('<span class="console ps">PS5</span>') == 1
    assert XBOX_BADGE in html_card
    # Meilleure offre toutes éditions (5,50 € sur Xbox), avec sa console.
    default = re.search(r'data-view="all">(.*?)<div class="price-view"', html_card, re.S).group(1)
    assert "55<small>,00" in default and "Sur Xbox Series · Amazon" in default
    views = json.loads(html.unescape(re.search(r"data-views='([^']+)'", html_card).group(1)))
    assert views == {
        "ps5": {"price": 6000, "discount": 24, "format": "Code dans la boîte"},
        "xbox_series": {"price": 5500, "discount": 24, "format": "Physique"},
    }
    assert 'data-view="ps5" hidden' in html_card and 'data-view="switch2"' not in html_card


def test_carte_une_edition_sans_vue_ni_mention(site: Path) -> None:
    render(site, [game("astro", offer=offer(date(2026, 10, 20), stale=False))])
    html_card = card((site / "index.html").read_text(encoding="utf-8"), "Astro")
    assert "data-views" not in html_card and "Sur PS5" not in html_card


def test_page_prix_par_edition(site: Path) -> None:
    render(site, [multi(), game("solo", platforms=("ps5", "xbox_series"))])
    page = (site / GAMES_DIR / "gta" / "index.html").read_text(encoding="utf-8")
    table = re.search(r'<table class="editions">.*?</table>', page, re.S).group(0)
    assert '<th scope="row">PS5</th><td data-label="Prix">60,00\xa0€</td>' in table
    assert '<td data-label="Format">Boîte avec code de téléchargement</td>' in table
    assert table.count("Voir l'offre") == 2
    solo = (site / GAMES_DIR / "solo" / "index.html").read_text(encoding="utf-8")
    assert solo.count("Pas encore de prix relevé</td>") == 2
    data = game_jsonld(page)
    assert data["gamePlatform"] == ["PlayStation 5", "Xbox Series X|S"]
    assert data["offers"] == {
        "@type": "AggregateOffer",
        "lowPrice": "55.00",
        "highPrice": "60.00",
        "offerCount": 2,
        "priceCurrency": "EUR",
    }


def test_une_seule_edition_a_jour_offer_simple(site: Path) -> None:
    day = date(2026, 10, 20)
    offers = {"ps5": offer(day, stale=False), "xbox_series": offer(date(2026, 7, 1), stale=True)}
    render(site, [game("gta", platforms=("ps5", "xbox_series"), offers=offers)])
    data = game_jsonld((site / GAMES_DIR / "gta" / "index.html").read_text(encoding="utf-8"))
    assert data["offers"]["@type"] == "Offer"


def test_filtres_python_et_app_js_identiques() -> None:
    from horspc.render import FILTER_GROUPS

    app = (SITE / "app.js").read_text(encoding="utf-8")
    for key, platforms in FILTER_GROUPS.items():
        listed = ", ".join(f'"{p}"' for p in platforms)
        assert re.search(rf"{key}: \{{ label: \"[^\"]+\", platforms: \[{listed}\] \}}", app), key


def test_tableau_editions_absent_pour_une_seule_edition(site: Path) -> None:
    render(site, [game("astro", offer=offer(date(2026, 10, 20), stale=False))])
    page = (site / GAMES_DIR / "astro" / "index.html").read_text(encoding="utf-8")
    assert "Prix par édition" not in page and 'class="editions"' not in page


def test_badge_xbox_court_sur_mobile_nom_complet_lu() -> None:
    from horspc.render import CONSOLES_SHORT

    assert CONSOLES_SHORT == {"xbox_series": "Xbox"}
    css = (SITE / "style.css").read_text(encoding="utf-8")
    mobile = css.split("@container (max-width:640px)")[1].split("}\n}")[0]
    assert ".hp .card .c-short{display:inline}" in mobile
    assert ".hp .card .c-long{position:absolute" in mobile  # masqué visuellement, pas aux lecteurs
    assert ".hp .c-short{display:none}" in css  # desktop inchangé


@pytest.mark.parametrize(
    ("offer_format", "stale", "badge"),
    [
        ("code_box", False, False),  # GTA VI : relevés récents tous en boîte avec code
        ("code_box", True, True),  # relevé ancien : il ne contredit plus IGDB
        ("physical", False, True),
    ],
)
def test_preuve_physique_igdb_contredite_par_les_releves(
    site: Path, offer_format: str, stale: bool, badge: bool
) -> None:
    observed = date(2026, 7, 1) if stale else date(2026, 10, 20)
    box = offer(observed, stale=stale) | {"format": offer_format}
    gta = game(
        "gta",
        platforms=("ps5", "xbox_series"),
        offers={"ps5": box},
        sold_digital=True,
        listed_physical=True,
    )
    gta["editions"][1]["sold_digital"] = gta["editions"][1]["listed_physical"] = False
    render(site, [gta])
    index = (site / "index.html").read_text(encoding="utf-8")
    assert ('<span class="status fmt">Physique et numérique</span>' in index) is badge
