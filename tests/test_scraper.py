"""Relevé automatique : fiches figées par enseigne, aucune requête réseau réelle."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from horspc.http import HttpClient
from horspc.models import GameEntry, PriceObservation, ScrapedObservation
from horspc.prices import ManualPriceProvider, probe
from horspc.prices import sources as src
from horspc.prices.fetch import SERVER_ERRORS, Fetcher
from horspc.prices.scraper import (
    RETRY_DAYS,
    Report,
    Scraper,
    load_history,
    write_history,
)
from horspc.render import merchant_label
from horspc.robots import RobotsCache
from tests.conftest import FIXTURES, TODAY

PAGES = FIXTURES / "sources"
EAN_LECLERC = "0045496312336"  # même EAN chez Boulanger
EAN_PIXMANIA = "0045496312329"
LECLERC_FP = "https://www.e.leclerc/fp/mario-kart-world-nintendo-switch-2-0045496312336"
BOULANGER_REF = "https://www.boulanger.com/ref/1220885"
PIXMANIA_FP = "https://www.pixmania.com/fr/fr/mario-kart-world-switch-2-397665.html"
ALL = {"leclerc": True, "boulanger": True, "pixmania": True}
MKW = GameEntry(
    slug="mario-kart-world", title="Mario Kart World", editions=[{"platform": "switch2"}]
)
DATADOME = '<html><script src="https://geo.captcha-delivery.com/captcha/"></script></html>'


def page(name: str) -> str:
    return (PAGES / name).read_text(encoding="utf-8")


def without_seller(html: str) -> str:
    return html.replace('"seller"', '"vendeur"').replace("Vendu par", "Vendu")


@pytest.fixture
def fetcher():
    http = HttpClient(sleep=lambda _: None, clock=lambda: 0.0, retry_statuses=SERVER_ERRORS)
    yield Fetcher(http, RobotsCache(http))
    http.close()


def mock_robots(**overrides: str) -> None:
    for host in ("www.e.leclerc", "www.boulanger.com", "www.pixmania.com", "feeds.pixmania.com"):
        text = overrides.get(host, "User-agent: *\nDisallow: /panier\n")
        respx.get(f"https://{host}/robots.txt").respond(text=text)


def mock_sites(
    leclerc: str | None = None, boulanger: str | None = None, categories: bool = True
) -> dict[str, respx.Route]:
    routes = {
        "listing": respx.route(host="www.boulanger.com", path="/c/jeu-switch").respond(
            text=page("boulanger_listing.html")
        ),
        "boulanger": respx.get(BOULANGER_REF).respond(text=boulanger or page("boulanger.html")),
        "collector": respx.get("https://www.boulanger.com/ref/1220999").respond(404),
        "leclerc_ean": respx.get(src.leclerc_url(EAN_LECLERC)).respond(
            301, headers={"Location": LECLERC_FP}
        ),
        "leclerc": respx.get(LECLERC_FP).respond(text=leclerc or page("leclerc.html")),
        "leclerc_other": respx.get(src.leclerc_url(EAN_PIXMANIA)).respond(404),
        "index": respx.get(src.PIXMANIA_SITEMAP).respond(text=page("pixmania_sitemap_index.xml")),
        "products": respx.get(
            "https://feeds.pixmania.com/sitemap/FR/sitemap-products-1.xml"
        ).respond(text=page("pixmania_sitemap_products.xml")),
        "pixmania": respx.get(PIXMANIA_FP).respond(text=page("pixmania.html")),
        **mock_leclerc_categories(categories),
    }
    return routes


def category_page(request: httpx.Request, filled: bool = True) -> httpx.Response:
    """/cat/jeux-switch-2 : pages 1 et 2 figées, puis page vide ; autres catégories vides."""
    number = request.url.params.get("page", "1")
    if filled and request.url.path == "/cat/jeux-switch-2" and number in ("1", "2"):
        return httpx.Response(200, text=page(f"leclerc_category_p{number}.html"))
    return httpx.Response(200, text=page("leclerc_category_empty.html"))


def mock_leclerc_categories(filled: bool = True) -> dict[str, respx.Route]:
    return {
        "leclerc_sitemap": respx.get(src.LECLERC_SITEMAP).respond(
            text=page("leclerc_sitemap_index.xml")
        ),
        "leclerc_navigation": respx.get(
            "https://www.e.leclerc/sitemap/sitemap-navigation-1.xml"
        ).respond(text=page("leclerc_sitemap_navigation.xml")),
        "leclerc_categories": respx.route(host="www.e.leclerc", path__startswith="/cat/").mock(
            side_effect=lambda request: category_page(request, filled)
        ),
    }


def empty() -> dict:
    return {"schema": 2, "games": {}}


# --- Lecture des fiches ----------------------------------------------------------------------


def test_fiches_figees_par_enseigne() -> None:
    leclerc = src.parse_leclerc(page("leclerc.html"))
    assert (leclerc.price, leclerc.seller, leclerc.eans) == (
        Decimal("66.25"),
        "E.Leclerc",
        {EAN_LECLERC},
    )
    assert (leclerc.condition, leclerc.available, leclerc.currency) == ("new", True, "EUR")
    boulanger = src.parse_boulanger(page("boulanger.html"))
    assert (boulanger.price, boulanger.seller, boulanger.eans) == (
        Decimal("89.99"),
        "boulanger",
        {EAN_LECLERC},
    )
    pixmania = src.parse_pixmania(page("pixmania.html"))
    # Le JSON-LD nomme « Pixmania » ; le vrai vendeur est le marchand pro de la fiche.
    assert (pixmania.price, pixmania.seller, pixmania.shipping) == (
        Decimal("90.89"),
        "ShopVite",
        Decimal("0.00"),
    )
    assert pixmania.eans == {EAN_PIXMANIA}


def test_json_ld_absent_signale_une_fiche_cassee() -> None:
    with pytest.raises(src.MissingData):
        src.parse_boulanger("<html><body>Plus de données structurées</body></html>")


def test_page_categorie_boulanger_ecarte_le_reconditionne() -> None:
    items = src.boulanger_listing(page("boulanger_listing.html"))
    assert [(i.url, i.ean) for i in items] == [
        (BOULANGER_REF, EAN_LECLERC),
        ("https://www.boulanger.com/ref/1220999", "0045496313111"),
    ]
    assert items[0].name == "jeu switch 2 nintendo mario kart world"


# --- Run complet -------------------------------------------------------------------------------


@respx.mock
def test_decouverte_puis_releve_sur_les_trois_sources(fetcher: Fetcher) -> None:
    mock_robots()
    routes = mock_sites()
    discovered = empty()
    observations, report = Scraper(ALL, fetcher).run([MKW], {}, discovered, TODAY)

    entry = discovered["games"]["mario-kart-world"]["switch2"]
    assert entry["eans"] == [EAN_PIXMANIA, EAN_LECLERC]  # un ensemble d'EAN par jeu
    assert entry["sources"] == {
        "boulanger": BOULANGER_REF,
        "pixmania": PIXMANIA_FP,
        "leclerc": LECLERC_FP,  # URL canonique après redirection
    }
    assert not routes["collector"].called  # l'édition collector ne correspond pas au titre
    by_source = {o.merchant: o for o in observations}
    assert by_source["leclerc"].price == Decimal("66.25")
    assert by_source["boulanger"].seller == "boulanger"
    assert (by_source["pixmania"].seller, by_source["pixmania"].ean) == ("ShopVite", EAN_PIXMANIA)
    assert report.ok == 3 and not report.needs_attention


@respx.mock
def test_run_suivant_reutilise_le_cache_sans_rechercher(fetcher: Fetcher) -> None:
    mock_robots()
    routes = mock_sites()
    discovered = empty()
    Scraper(ALL, fetcher).run([MKW], {}, discovered, TODAY)
    for route in routes.values():
        route.reset()
    Scraper(ALL, fetcher).run([MKW], {}, discovered, TODAY)
    assert not routes["listing"].called and not routes["index"].called
    assert routes["boulanger"].call_count == 1 and routes["pixmania"].call_count == 1


@respx.mock
def test_recherche_infructueuse_relancee_apres_quelques_jours(fetcher: Fetcher) -> None:
    mock_robots()
    routes = mock_sites()
    other = GameEntry(slug="astro-bot", title="Astro Bot", editions=[{"platform": "ps5"}])
    respx.route(host="www.boulanger.com", path="/c/jeu-playstation-5").respond(text="<html/>")
    discovered = empty()
    Scraper({"boulanger": True}, fetcher).run([other], {}, discovered, TODAY)
    assert discovered["games"]["astro-bot"]["ps5"]["checked"] == {"boulanger": TODAY.isoformat()}

    routes["listing"].reset()
    fresh = Scraper({"boulanger": True}, fetcher)
    fresh.run([other], {}, discovered, date.fromordinal(TODAY.toordinal() + 1))
    assert fresh._listings == {}  # pas de nouvelle recherche le lendemain
    fresh.run([other], {}, discovered, date.fromordinal(TODAY.toordinal() + RETRY_DAYS))
    assert "/c/jeu-playstation-5" in fresh._listings


@respx.mock
def test_nom_alternatif_igdb_pour_la_correspondance(fetcher: Fetcher) -> None:
    mock_robots()
    mock_sites()
    game = GameEntry(slug="mkw", title="Mario Kart Monde", editions=[{"platform": "switch2"}])
    discovered = empty()
    Scraper({"boulanger": True}, fetcher).run(
        [game], {"mkw": ["Mario Kart World"]}, discovered, TODAY
    )
    assert discovered["games"]["mkw"]["switch2"]["sources"] == {"boulanger": BOULANGER_REF}


@respx.mock
def test_robots_interdit_le_chemin_aucune_requete(fetcher: Fetcher) -> None:
    mock_robots(**{"www.boulanger.com": "User-agent: *\nDisallow: /c/\nDisallow: /ref/\n"})
    routes = mock_sites()
    discovered = empty()
    _, report = Scraper({"boulanger": True}, fetcher).run([MKW], {}, discovered, TODAY)
    assert not routes["listing"].called
    assert any(e["kind"] == "disallowed" and e["detail"] == "Disallow: /c/" for e in report.events)


@respx.mock
def test_robots_specifique_horspc_prioritaire(fetcher: Fetcher) -> None:
    robots = "User-agent: *\nAllow: /\n\nUser-agent: HorsPC\nDisallow: /fp/\n"
    mock_robots(**{"www.e.leclerc": robots})
    routes = mock_sites()
    discovered = {"schema": 2, "games": {"mario-kart-world": {"switch2": {
        "eans": [EAN_LECLERC], "sources": {"leclerc": LECLERC_FP}, "checked": {}
    }}}}  # fmt: skip
    observations, _ = Scraper({"leclerc": True}, fetcher).run([MKW], {}, discovered, TODAY)
    assert observations == [] and not routes["leclerc"].called


@respx.mock
def test_page_de_defi_bloque_la_source_pour_le_run(fetcher: Fetcher) -> None:
    mock_robots()
    routes = mock_sites(leclerc=DATADOME)
    discovered = empty()
    games = [MKW, MKW.model_copy(update={"slug": "mkw-bis"})]
    for slug in ("mario-kart-world", "mkw-bis"):
        discovered["games"][slug] = {
            "switch2": {"eans": [EAN_LECLERC], "sources": {"leclerc": LECLERC_FP}, "checked": {}}
        }
    observations, report = Scraper({"leclerc": True}, fetcher).run(games, {}, discovered, TODAY)
    assert observations == []
    assert report.blocked == {"leclerc": "page de défi (geo.captcha-delivery.com)"}
    assert routes["leclerc"].call_count == 1  # plus aucune requête après le blocage
    assert report.needs_attention


@respx.mock
def test_403_bloque_sans_nouvel_essai(fetcher: Fetcher) -> None:
    mock_robots()
    route = respx.get(BOULANGER_REF).respond(403)
    discovered = {"schema": 2, "games": {"mario-kart-world": {"switch2": {
        "eans": [EAN_LECLERC], "sources": {"boulanger": BOULANGER_REF}, "checked": {}
    }}}}  # fmt: skip
    _, report = Scraper({"boulanger": True}, fetcher).run([MKW], {}, discovered, TODAY)
    assert route.call_count == 1
    assert report.blocked == {"boulanger": "HTTP 403"}


@respx.mock
def test_5xx_nouvel_essai_puis_releve(fetcher: Fetcher) -> None:
    mock_robots()
    route = respx.get(BOULANGER_REF)
    route.side_effect = [httpx.Response(503), httpx.Response(200, text=page("boulanger.html"))]
    discovered = {"schema": 2, "games": {"mario-kart-world": {"switch2": {
        "eans": [EAN_LECLERC], "sources": {"boulanger": BOULANGER_REF}, "checked": {}
    }}}}  # fmt: skip
    observations, _ = Scraper({"boulanger": True}, fetcher).run([MKW], {}, discovered, TODAY)
    assert route.call_count == 2 and len(observations) == 1


@respx.mock
def test_json_ld_sans_vendeur_refuse(fetcher: Fetcher) -> None:
    mock_robots()
    mock_sites(leclerc=without_seller(page("leclerc.html")))
    discovered = {"schema": 2, "games": {"mario-kart-world": {"switch2": {
        "eans": [EAN_LECLERC], "sources": {"leclerc": LECLERC_FP}, "checked": {}
    }}}}  # fmt: skip
    observations, report = Scraper({"leclerc": True}, fetcher).run([MKW], {}, discovered, TODAY)
    assert observations == []
    assert ("rejected", "vendeur non identifié") in {
        (e["kind"], e["detail"]) for e in report.events
    }


@respx.mock
def test_json_ld_absent_remonte_dans_le_bilan(fetcher: Fetcher) -> None:
    mock_robots()
    mock_sites(boulanger="<html><body>Nouvelle maquette</body></html>")
    discovered = {"schema": 2, "games": {"mario-kart-world": {"switch2": {
        "eans": [EAN_LECLERC], "sources": {"boulanger": BOULANGER_REF}, "checked": {}
    }}}}  # fmt: skip
    _, report = Scraper({"boulanger": True}, fetcher).run([MKW], {}, discovered, TODAY)
    assert report.broken == [f"boulanger : {BOULANGER_REF}"]
    assert json.loads(report.to_json())["counts"] == {"broken": 1}


@respx.mock
def test_vendeur_tiers_refuse_hors_place_de_marche_pro(fetcher: Fetcher) -> None:
    mock_robots()
    third = page("boulanger.html").replace(
        'data-analytics_product_seller="boulanger"', 'data-analytics_product_seller="GameShop"'
    )
    mock_sites(boulanger=third)
    discovered = {"schema": 2, "games": {"mario-kart-world": {"switch2": {
        "eans": [EAN_LECLERC], "sources": {"boulanger": BOULANGER_REF}, "checked": {}
    }}}}  # fmt: skip
    observations, report = Scraper({"boulanger": True}, fetcher).run([MKW], {}, discovered, TODAY)
    assert observations == []
    assert any("vendeur tiers (GameShop)" in e["detail"] for e in report.events)


@respx.mock
def test_autre_produit_derriere_l_url_refuse(fetcher: Fetcher) -> None:
    mock_robots()
    other = (
        page("boulanger.html")
        .replace(EAN_LECLERC, "0045496999999")
        .replace("Mario Kart World", "Mario Kart World Edition Collector")
    )
    mock_sites(boulanger=other)
    discovered = {"schema": 2, "games": {"mario-kart-world": {"switch2": {
        "eans": [EAN_LECLERC], "sources": {"boulanger": BOULANGER_REF}, "checked": {}
    }}}}  # fmt: skip
    observations, report = Scraper({"boulanger": True}, fetcher).run([MKW], {}, discovered, TODAY)
    assert observations == []
    assert any(e["detail"].startswith("autre produit") for e in report.events)


@respx.mock
def test_fiche_retiree_rechercher_a_nouveau(fetcher: Fetcher) -> None:
    mock_robots()
    respx.get(BOULANGER_REF).respond(404)
    discovered = {"schema": 2, "games": {"mario-kart-world": {"switch2": {
        "eans": [EAN_LECLERC], "sources": {"boulanger": BOULANGER_REF},
        "checked": {"boulanger": TODAY.isoformat()},
    }}}}  # fmt: skip
    Scraper({"boulanger": True}, fetcher).run([MKW], {}, discovered, TODAY)
    assert discovered["games"]["mario-kart-world"]["switch2"]["sources"] == {}
    assert discovered["games"]["mario-kart-world"]["switch2"]["checked"] == {}


def test_source_desactivee_jamais_sollicitee(fetcher: Fetcher) -> None:
    with respx.mock(assert_all_called=False) as mock:
        observations, _ = Scraper({"leclerc": False}, fetcher).run([MKW], {}, empty(), TODAY)
    assert observations == [] and not mock.calls


# --- Historique et fusion ------------------------------------------------------------------------


def scraped(**kw) -> ScrapedObservation:
    base = {
        "slug": "mario-kart-world",
        "platform": "switch2",
        "merchant": "leclerc",
        "seller": "E.Leclerc",
        "price": "66.25",
        "shipping": None,
        "format": "physical",
        "condition": "new",
        "url": LECLERC_FP,
        "ean": EAN_LECLERC,
        "observed_at": TODAY,
    }
    return ScrapedObservation.model_validate(base | kw)


def test_historique_une_ligne_par_jeu_marchand_et_jour(tmp_path: Path) -> None:
    path = tmp_path / "prices.scraped.jsonl"
    write_history(
        path, [scraped(price="70.00"), scraped(), scraped(observed_at=date(2026, 10, 19))]
    )
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [o.price for o in load_history(path)] == [Decimal("66.25"), Decimal("66.25")]
    assert json.loads(lines[1])["observed_at"] == "2026-10-26"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"url": "https://www.boulanger.com/ref/1"}, "ne correspond pas à la source"),
        ({"seller": "Instant Gaming", "merchant": "pixmania", "url": PIXMANIA_FP}, "clés"),
        ({"seller": "GameShop"}, "vendeur tiers"),
    ],
)
def test_releve_automatique_soumis_a_la_regle_de_confiance(change: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        scraped(**change)


SINGLE = {"mario-kart-world": "switch2"}  # édition des relevés manuels sans platform


def test_releve_manuel_prioritaire_meme_marchand_meme_jour() -> None:
    manual = PriceObservation.model_validate(
        {
            "slug": "mario-kart-world",
            "price": "69.99",
            "shipping": None,
            "format": "physical",
            "condition": "new",
            "merchant": "leclerc",
            "sold_by_merchant": True,
            "url": LECLERC_FP,
            "observed_at": TODAY,
        },
        context={"today": TODAY},
    )
    offer = ManualPriceProvider([manual], [scraped()], SINGLE).get_offer(MKW, "switch2", TODAY)
    assert (offer.price_cents, offer.source) == (6999, "manual")
    # Un autre jour, le relevé automatique moins cher l'emporte.
    later = date.fromordinal(TODAY.toordinal() + 1)
    offer = ManualPriceProvider([manual], [scraped(observed_at=later)], SINGLE).get_offer(
        MKW, "switch2", later
    )
    assert (offer.price_cents, offer.source, offer.seller) == (6625, "scraped", None)


def test_vendeur_de_place_de_marche_affiche() -> None:
    obs = scraped(merchant="pixmania", seller="ShopVite", url=PIXMANIA_FP, price="90.89")
    offer = ManualPriceProvider([], [obs]).get_offer(MKW, "switch2", TODAY)
    assert (offer.merchant, offer.seller) == ("Pixmania", "ShopVite")

    class Out:
        merchant = "Pixmania"
        seller = "ShopVite"

    assert merchant_label(Out()) == "ShopVite via Pixmania"


def test_bilan_vide() -> None:
    assert not Report().needs_attention


@pytest.mark.parametrize(
    ("raw", "ean"),
    [
        ("45496510732", "0045496510732"),  # zéros de tête perdus
        ("045496510732", "0045496510732"),  # UPC-A
        ("00045496510732", "0045496510732"),  # GTIN-14
        ("0045496510732", "0045496510732"),
        ("12345670", "12345670"),  # EAN-8
    ],
)
def test_ean_normalise(raw: str, ean: str) -> None:
    assert src.normalize_ean(raw) == ean


@respx.mock
def test_fiche_lue_une_seule_fois_par_run(fetcher: Fetcher) -> None:
    mock_robots()
    routes = mock_sites()
    Scraper(ALL, fetcher).run([MKW], {}, empty(), TODAY)
    assert routes["pixmania"].call_count == 1 and routes["boulanger"].call_count == 1


def test_json_ld_avec_entites_html_dans_les_chaines() -> None:
    # Boulanger : « (&quot;CGU&quot;) » en description ; décodé avant parsing, le JSON cassait.
    html = page("boulanger.html").replace(
        '"name": "Jeu Switch 2 NINTENDO Mario Kart World"',
        '"name": "Jeu Switch 2 NINTENDO Mario Kart World", '
        '"description": "Code soumis aux Conditions (&quot;CGU&quot;) d&#39;utilisation"',
    )
    product = src.parse_boulanger(html)
    assert (product.price, product.eans) == (Decimal("89.99"), {EAN_LECLERC})


@respx.mock
def test_precommande_sans_offre_ecartee_sans_alerte(fetcher: Fetcher) -> None:
    mock_robots()
    no_offer = page("boulanger.html").replace('"offers"', '"offres"')
    mock_sites(boulanger=no_offer)
    discovered = {"schema": 2, "games": {"mario-kart-world": {"switch2": {
        "eans": [EAN_LECLERC], "sources": {"boulanger": BOULANGER_REF}, "checked": {}
    }}}}  # fmt: skip
    observations, report = Scraper({"boulanger": True}, fetcher).run([MKW], {}, discovered, TODAY)
    assert observations == [] and not report.needs_attention
    assert ("rejected", "aucune offre en ligne (rupture ou précommande pas encore ouverte)") in {
        (e["kind"], e["detail"]) for e in report.events
    }


# --- Bilan et diagnostic -------------------------------------------------------------------------


def test_bilan_markdown_et_discord() -> None:
    report = Report(
        5,
        {"leclerc": "page de défi (geo.captcha-delivery.com)"},
        ["boulanger : https://www.boulanger.com/ref/1"],
        [{"source": "pixmania", "kind": "rejected", "url": "u", "detail": "d"}],
    )
    again = Report.from_json(report.to_json())
    assert again == report
    text = report.markdown()
    assert "- Prix relevés : 5" in text
    assert "leclerc (page de défi (geo.captcha-delivery.com))" in text
    assert "écartées (règle de confiance, autre produit, sans offre) : 1" in text
    message = report.discord("https://run/1")
    assert message.startswith("⚠️ HorsPC : relevé des prix incomplet (5 prix relevés")
    assert message.endswith(" https://run/1")
    assert Report(5).discord("https://run/1") is None
    long = Report(0, {}, [f"boulanger : https://www.boulanger.com/ref/{i}" for i in range(200)])
    assert len(long.discord("https://run/1")) <= 1900
    assert long.discord("https://run/1").endswith("… https://run/1")


@respx.mock
def test_diagnostic_par_source(fetcher: Fetcher) -> None:
    mock_robots(**{"www.pixmania.com": "User-agent: *\nDisallow: /fr/\n"})
    respx.get(LECLERC_FP).respond(text=page("leclerc.html"))
    respx.route(host="www.boulanger.com", path="/c/jeu-playstation-5").respond(403)
    respx.get(src.PIXMANIA_SITEMAP).respond(text=page("pixmania_sitemap_index.xml"))
    discovered = {"schema": 2, "games": {"mkw": {"switch2": {
        "eans": [EAN_LECLERC], "checked": {},
        "sources": {"leclerc": LECLERC_FP, "boulanger": BOULANGER_REF, "pixmania": PIXMANIA_FP},
    }}}}  # fmt: skip
    rows = probe.probe(fetcher, ["leclerc", "boulanger", "pixmania"], discovered)
    table = {(r.source, r.kind): (r.result, r.detail) for r in rows}
    assert table[("leclerc", "fiche")] == ("HTTP 200", "JSON-LD OK : 66.25 €, vendeur E.Leclerc")
    assert table[("boulanger", "catégorie")] == ("bloqué", "HTTP 403")
    # Source bloquée : la fiche n'est plus demandée.
    assert table[("boulanger", "fiche")][0] == "bloqué"
    assert table[("pixmania", "sitemap")] == ("HTTP 200", "2 sitemaps listés")
    assert table[("pixmania", "fiche")] == ("interdit par robots.txt", "Disallow: /fr/")
    assert table[("leclerc", "robots.txt")][0] == "lu (1 règles pour HorsPC)"
    text = probe.markdown(rows, "HorsPC/test")
    assert (
        "| boulanger | catégorie | https://www.boulanger.com/c/jeu-playstation-5 | bloqué |" in text
    )
    assert "User-Agent : `HorsPC/test`" in text


def test_diagnostic_sans_fiche_connue_utilise_la_fiche_temoin() -> None:
    assert probe.probe_urls("leclerc", {"games": {}}) == [
        ("fiche", src.leclerc_url(probe.FALLBACK_EAN))
    ]
    assert probe.probe_urls("boulanger", {"games": {}}) == [
        ("catégorie", "https://www.boulanger.com/c/jeu-playstation-5")
    ]


# --- Sources inaccessibles depuis GitHub Actions --------------------------------------------------


@respx.mock
@pytest.mark.parametrize("status", [400, 403, 500])
def test_robots_non_verifiable_aucune_requete_vers_la_source(fetcher: Fetcher, status: int) -> None:
    respx.get("https://www.boulanger.com/robots.txt").respond(status)
    routes = mock_sites()
    _, report = Scraper({"boulanger": True}, fetcher).run([MKW], {}, empty(), TODAY)
    assert report.blocked == {"boulanger": f"robots.txt non vérifiable (HTTP {status})"}
    assert not routes["listing"].called and not routes["boulanger"].called


@respx.mock
def test_400_sur_une_page_bloque_la_source_sans_nouvel_essai(fetcher: Fetcher) -> None:
    mock_robots()
    route = respx.get(LECLERC_FP).respond(400)
    discovered = {"schema": 2, "games": {"mario-kart-world": {"switch2": {
        "eans": [EAN_LECLERC], "sources": {"leclerc": LECLERC_FP}, "checked": {}
    }}}}  # fmt: skip
    _, report = Scraper({"leclerc": True}, fetcher).run([MKW], {}, discovered, TODAY)
    assert route.call_count == 1 and report.blocked == {"leclerc": "HTTP 400"}


@respx.mock
def test_decouverte_leclerc_categorie_puis_pixmania(fetcher: Fetcher) -> None:
    mock_robots()
    mock_sites()
    discovered = empty()
    enabled = {"leclerc": True, "boulanger": False, "pixmania": True}
    observations, _ = Scraper(enabled, fetcher).run([MKW], {}, discovered, TODAY)
    entry = discovered["games"]["mario-kart-world"]["switch2"]
    # Leclerc par sa catégorie, puis Pixmania : chacun apporte son EAN.
    assert entry["sources"] == {"leclerc": LECLERC_FP, "pixmania": PIXMANIA_FP}
    assert entry["eans"] == [EAN_PIXMANIA, EAN_LECLERC]
    assert {o.merchant for o in observations} == {"leclerc", "pixmania"}


@respx.mock
def test_decouverte_sans_boulanger_pixmania_puis_leclerc(fetcher: Fetcher) -> None:
    mock_robots()
    routes = mock_sites(categories=False)  # absent de la catégorie : Leclerc par EAN
    respx.get(src.leclerc_url(EAN_PIXMANIA)).respond(301, headers={"Location": LECLERC_FP})
    discovered = empty()
    enabled = {"leclerc": True, "boulanger": False, "pixmania": True}
    observations, _ = Scraper(enabled, fetcher).run([MKW], {}, discovered, TODAY)
    entry = discovered["games"]["mario-kart-world"]["switch2"]
    assert entry["eans"] == [EAN_PIXMANIA]  # EAN lu sur la fiche Pixmania (gtin13)
    assert not routes["listing"].called and not routes["boulanger"].called
    # La fiche Leclerc ouverte par cet EAN porte un autre gtin13 : elle n'est pas retenue.
    assert set(entry["sources"]) == {"pixmania"}
    assert [o.merchant for o in observations] == ["pixmania"]


@respx.mock
def test_ean_en_cache_reste_valable_sans_boulanger(fetcher: Fetcher) -> None:
    mock_robots()
    mock_sites()
    discovered = {"schema": 2, "games": {"mario-kart-world": {"switch2": {
        "eans": [EAN_LECLERC], "sources": {"boulanger": BOULANGER_REF}, "checked": {}
    }}}}  # fmt: skip
    enabled = {"leclerc": True, "boulanger": False, "pixmania": False}
    observations, _ = Scraper(enabled, fetcher).run([MKW], {}, discovered, TODAY)
    assert [o.merchant for o in observations] == ["leclerc"]


# --- Boîte avec code de téléchargement --------------------------------------------------------

GTA_BOULANGER_DESCRIPTION = (  # extrait réel, 25/10/2026
    "Grand Theft Auto VI La boîte de ce produit ne contiendra qu&#39;un code de téléchargement "
    "permettant de lancer le préchargement à partir du 12 novembre. Aucun disque ne sera inclus "
    "dans la boîte."
)
MKW_BOULANGER_DESCRIPTION = (  # extrait réel : le code concerne le pack console, pas ce jeu
    "Découvrez également le pack Nintendo Switch 2 + Mario Kart World*1, qui inclut la console "
    "et un code de téléchargement pour Mario Kart World, disponible le 5 juin"
)


@pytest.mark.parametrize(
    ("name", "description", "expected"),
    [
        ("Grand Theft Auto VI (Code in a Box) (PS5)", "", True),  # E.Leclerc
        ("Jeu PS5 TAKE 2 GTA VI", GTA_BOULANGER_DESCRIPTION, True),  # Boulanger
        ("Grand Theft Auto VI - Code-in-a-Box PS5", "", True),
        ("Assassin's Creed Shadows PS5 (code dans la boîte)", "", True),
        ("EA Sports FC 26 PS5 - Code de téléchargement", "", True),
        ("Jeu Switch 2 NINTENDO Mario Kart World", MKW_BOULANGER_DESCRIPTION, False),
        ("Mario Kart World (NINTENDO SWITCH 2)", "", False),  # E.Leclerc
        ("Microids Astro Bot", "", False),  # Pixmania
        ("Sony Gran Turismo 7 Standard (PlayStation 5)", "", False),
    ],
)
def test_boite_avec_code_detectee(name: str, description: str, expected: bool) -> None:
    from horspc.prices.matching import is_code_box

    assert is_code_box(name, src._description({"description": description})) is expected


def test_titre_reconnu_malgre_la_mention_code_in_a_box() -> None:
    from horspc.prices.matching import title_matches

    assert title_matches("Grand Theft Auto VI (Code in a Box) (PS5)", "Grand Theft Auto VI", "ps5")
    assert not title_matches("Grand Theft Auto VI (Code in a Box) (PS5)", "GTA VI", "xbox_series")


@respx.mock
def test_releve_boite_avec_code(fetcher: Fetcher) -> None:
    mock_robots()
    code_box = page("leclerc.html").replace(
        '"name": "Mario Kart World (NINTENDO SWITCH 2)"',
        '"name": "Mario Kart World (Code in a Box) (NINTENDO SWITCH 2)"',
    )
    mock_sites(leclerc=code_box)
    discovered = {"schema": 2, "games": {"mario-kart-world": {"switch2": {
        "eans": [EAN_LECLERC], "sources": {"leclerc": LECLERC_FP}, "checked": {}
    }}}}  # fmt: skip
    (obs,) = Scraper({"leclerc": True}, fetcher).run([MKW], {}, discovered, TODAY)[0]
    assert obs.format == "code_box"


def test_physique_prioritaire_sur_la_boite_avec_code() -> None:
    box = scraped(format="code_box", price="40.00")
    physical = scraped(merchant="pixmania", seller="ShopVite", url=PIXMANIA_FP, price="90.89")
    offer = ManualPriceProvider([], [box, physical]).get_offer(MKW, "switch2", TODAY)
    assert (offer.format, offer.merchant) == ("physical", "Pixmania")
    offer = ManualPriceProvider([], [box]).get_offer(MKW, "switch2", TODAY)
    assert offer.format == "code_box"


# --- Une entrée par console : EAN séparés ---------------------------------------------------------

GTA_PS5 = GameEntry(slug="gta-vi", title="Grand Theft Auto VI", editions=[{"platform": "ps5"}])
GTA_XBOX = GameEntry(
    slug="gta-vi-xbox", title="Grand Theft Auto VI", editions=[{"platform": "xbox_series"}]
)
EAN_GTA_PS5 = "5026555439824"
EAN_GTA_XBOX = "5026555368278"  # EAN fictif pour le test


def pixmania_page(name: str, ean: str) -> str:
    return (
        page("pixmania.html")
        .replace('"name": "Mario Kart World (Switch 2)"', f'"name": "{name}"')
        .replace(EAN_PIXMANIA, ean)
    )


@respx.mock
def test_ean_ps5_et_xbox_jamais_melanges(fetcher: Fetcher) -> None:
    mock_robots()
    mock_leclerc_categories()
    base = "https://www.pixmania.com/fr/fr/"
    urls = {
        "ps5": base + "grand-theft-auto-vi-ps5-610001.html",
        "xbox": base + "grand-theft-auto-vi-xbox-series-x-s-610002.html",
        "both": base + "grand-theft-auto-vi-ps5-xbox-series-610003.html",
    }
    sitemap = "".join(f"<url><loc>{u}</loc></url>" for u in urls.values())
    respx.get(src.PIXMANIA_SITEMAP).respond(
        text="<sitemapindex><sitemap><loc>https://feeds.pixmania.com/sitemap/FR/"
        "sitemap-products-1.xml</loc></sitemap></sitemapindex>"
    )
    respx.get("https://feeds.pixmania.com/sitemap/FR/sitemap-products-1.xml").respond(
        text=f"<urlset>{sitemap}</urlset>"
    )
    respx.get(urls["ps5"]).respond(text=pixmania_page("Grand Theft Auto VI (PS5)", EAN_GTA_PS5))
    respx.get(urls["xbox"]).respond(
        text=pixmania_page("Grand Theft Auto VI (Xbox Series X|S)", EAN_GTA_XBOX)
    )
    both = respx.get(urls["both"]).respond(text="<html/>")
    for ean in (EAN_GTA_PS5, EAN_GTA_XBOX):
        respx.get(src.leclerc_url(ean)).respond(404)
    discovered = empty()
    names = {"gta-vi": ["GTA VI"], "gta-vi-xbox": ["GTA VI"]}
    enabled = {"leclerc": True, "pixmania": True}
    observations, _ = Scraper(enabled, fetcher).run([GTA_PS5, GTA_XBOX], names, discovered, TODAY)
    ps5, xbox = (
        discovered["games"]["gta-vi"]["ps5"],
        discovered["games"]["gta-vi-xbox"]["xbox_series"],
    )
    assert (ps5["eans"], ps5["sources"]) == (
        [EAN_GTA_PS5],
        {"pixmania": urls["ps5"]},
    )
    assert (xbox["eans"], xbox["sources"]) == (
        [EAN_GTA_XBOX],
        {"pixmania": urls["xbox"]},
    )
    assert not both.called  # nom citant deux consoles : ambigu, jamais ouvert
    assert {(o.slug, o.ean) for o in observations} == {
        ("gta-vi", EAN_GTA_PS5),
        ("gta-vi-xbox", EAN_GTA_XBOX),
    }


@respx.mock
def test_fiche_d_une_autre_console_refusee_au_releve(fetcher: Fetcher) -> None:
    mock_robots()
    url = "https://www.pixmania.com/fr/fr/grand-theft-auto-vi-ps5-610001.html"
    respx.get(url).respond(text=pixmania_page("Grand Theft Auto VI (PS5)", EAN_GTA_PS5))
    discovered = {"schema": 2, "games": {"gta-vi-xbox": {"xbox_series": {
        "eans": [EAN_GTA_XBOX], "sources": {"pixmania": url}, "checked": {}
    }}}}  # fmt: skip
    observations, report = Scraper({"pixmania": True}, fetcher).run(
        [GTA_XBOX], {"gta-vi-xbox": ["GTA VI"]}, discovered, TODAY
    )
    assert observations == []
    assert any(e["detail"].startswith("autre produit") for e in report.events)


# --- Pixmania : nom sans plateforme ---------------------------------------------------------------

PIX = "https://www.pixmania.com/fr/fr/"


@pytest.mark.parametrize(
    ("name", "slug", "title", "platform", "companies", "expected"),
    [
        # Noms réels relevés le 25/10/2026.
        ("Sony Interactive Entertainment SAROS", "saros-ps5-528089", "Saros", "ps5", [], True),
        (
            "Sony Interactive Entertainment SAROS",
            "saros-ps5-528089",
            "Saros",
            "ps5",
            ["Sony Interactive Entertainment", "Housemarque"],
            True,
        ),
        # Microids n'est ni éditeur ni studio d'Astro Bot dans IGDB : refusé sur le nom.
        (
            "Microids Astro Bot",
            "astro-bot-ps5-305180",
            "Astro Bot",
            "ps5",
            ["Sony Interactive Entertainment", "Team Asobi"],
            False,
        ),
        # Éditeur IGDB toléré.
        ("Microids Astro Bot", "astro-bot-ps5-305180", "Astro Bot", "ps5", ["Microids"], True),
        # Négatifs : autre jeu, édition collector, slug multi-plateforme, autre console.
        ("Sony Interactive Entertainment SAROS", "saros-ps5-528089", "Astro Bot", "ps5", [], False),
        ("SAROS Collector", "saros-collector-ps5-528090", "Saros", "ps5", [], False),
        ("SAROS", "saros-ps5-xbox-series-528091", "Saros", "ps5", [], False),
        ("SAROS", "saros-528092", "Saros", "ps5", [], False),  # slug sans plateforme
        ("GTA VI", "gta-vi-ps5-528093", "GTA VI", "xbox_series", [], False),
        # Le nom cite une plateforme : le slug n'est pas consulté.
        ("Saros (Switch 2)", "saros-ps5-528094", "Saros", "ps5", [], False),
    ],
)
def test_nom_pixmania_sans_plateforme(
    name: str, slug: str, title: str, platform: str, companies: list, expected: bool
) -> None:
    from horspc.prices.matching import title_matches, url_platforms

    url = f"{PIX}{slug}.html"
    assert title_matches(name, title, platform, companies, url_platforms(url)) is expected


@respx.mock
def test_decouverte_saros_et_astro_bot_cache_vide(fetcher: Fetcher) -> None:
    mock_robots()
    saros_url, astro_url = f"{PIX}saros-ps5-528089.html", f"{PIX}astro-bot-ps5-305180.html"
    respx.get(src.PIXMANIA_SITEMAP).respond(
        text="<sitemapindex><sitemap><loc>https://feeds.pixmania.com/sitemap/FR/"
        "sitemap-products-1.xml</loc></sitemap></sitemapindex>"
    )
    respx.get("https://feeds.pixmania.com/sitemap/FR/sitemap-products-1.xml").respond(
        text=f"<urlset><url><loc>{saros_url}</loc></url><url><loc>{astro_url}</loc></url></urlset>"
    )
    respx.get(saros_url).respond(
        text=pixmania_page("Sony Interactive Entertainment SAROS", "0711719026990")
    )
    respx.get(astro_url).respond(text=pixmania_page("Microids Astro Bot", "0711719591818"))
    saros = GameEntry(slug="saros", title="Saros", editions=[{"platform": "ps5"}])
    astro = GameEntry(slug="astro-bot", title="Astro Bot", editions=[{"platform": "ps5"}])
    companies = {
        "saros": ["Sony Interactive Entertainment", "Housemarque"],
        "astro-bot": ["Sony Interactive Entertainment", "Team Asobi"],
    }
    discovered = empty()
    Scraper({"pixmania": True}, fetcher).run([saros, astro], {}, discovered, TODAY, companies)
    assert discovered["games"]["saros"]["ps5"]["sources"] == {"pixmania": saros_url}
    assert discovered["games"]["saros"]["ps5"]["eans"] == ["0711719026990"]
    # « Microids » inconnu d'IGDB et aucun EAN connu : pas de rattachement.
    assert discovered["games"]["astro-bot"]["ps5"]["sources"] == {}

    # Avec l'EAN déjà connu (cache), la fiche est reconnue par son EAN.
    discovered["games"]["astro-bot"]["ps5"] = {
        "eans": ["0711719591818"],
        "sources": {},
        "checked": {},
    }
    Scraper({"pixmania": True}, fetcher).run([astro], {}, discovered, TODAY, companies)
    assert discovered["games"]["astro-bot"]["ps5"]["sources"] == {"pixmania": astro_url}


@pytest.mark.parametrize(
    ("name", "title", "platform"),
    [
        # Fiches réelles Pixmania (25/10/2026) : codes envoyés sans boîte, jamais rattachés.
        ("Astro Bot   PS5 [Code de téléchargement]", "Astro Bot", "ps5"),
        ("pokemon scarlet switch code de telechargement 511760", "Pokémon Scarlet", "switch"),
        (
            "Pokémon Écarlate : Le trésor enfoui de la Zone Zéro DLC SWITCH "
            "[Code de téléchargement]",
            "Pokémon Écarlate",
            "switch",
        ),
        ("Super Mario Odyssey Switch Digital Code", "Super Mario Odyssey", "switch"),
    ],
)
def test_code_de_telechargement_sans_boite_refuse(name: str, title: str, platform: str) -> None:
    from horspc.prices.matching import title_matches

    assert not title_matches(name, title, platform)


def test_boite_avec_code_toujours_reconnue() -> None:
    from horspc.prices.matching import title_matches

    assert title_matches("Grand Theft Auto VI (Code in a Box) (PS5)", "Grand Theft Auto VI", "ps5")
    assert title_matches("Saros PS5 (code dans la boîte)", "Saros", "ps5")


@respx.mock
def test_un_jeu_deux_editions_ean_et_releves_separes(fetcher: Fetcher) -> None:
    mock_robots()
    base = "https://www.pixmania.com/fr/fr/"
    urls = {
        "ps5": base + "grand-theft-auto-vi-ps5-610001.html",
        "xbox_series": base + "grand-theft-auto-vi-xbox-series-x-s-610002.html",
    }
    respx.get(src.PIXMANIA_SITEMAP).respond(
        text="<sitemapindex><sitemap><loc>https://feeds.pixmania.com/sitemap/FR/"
        "sitemap-products-1.xml</loc></sitemap></sitemapindex>"
    )
    respx.get("https://feeds.pixmania.com/sitemap/FR/sitemap-products-1.xml").respond(
        text="<urlset>" + "".join(f"<url><loc>{u}</loc></url>" for u in urls.values()) + "</urlset>"
    )
    respx.get(urls["ps5"]).respond(text=pixmania_page("Grand Theft Auto VI (PS5)", EAN_GTA_PS5))
    respx.get(urls["xbox_series"]).respond(
        text=pixmania_page("Grand Theft Auto VI (Xbox Series X|S)", EAN_GTA_XBOX)
    )
    gta = GameEntry(
        slug="gta-vi",
        title="Grand Theft Auto VI",
        editions=[{"platform": "ps5"}, {"platform": "xbox_series"}],
    )
    discovered = empty()
    observations, _ = Scraper({"pixmania": True}, fetcher).run([gta], {}, discovered, TODAY)
    editions = discovered["games"]["gta-vi"]
    assert {p: (e["eans"], e["sources"]) for p, e in editions.items()} == {
        "ps5": ([EAN_GTA_PS5], {"pixmania": urls["ps5"]}),
        "xbox_series": ([EAN_GTA_XBOX], {"pixmania": urls["xbox_series"]}),
    }
    assert {(o.platform, o.ean) for o in observations} == {
        ("ps5", EAN_GTA_PS5),
        ("xbox_series", EAN_GTA_XBOX),
    }


def test_cache_de_decouverte_ancien_schema_refuse(tmp_path: Path) -> None:
    from horspc.prices.scraper import load_discovered

    path = tmp_path / "discovered.json"
    path.write_text('{"schema": 1, "games": {}}', encoding="utf-8")
    with pytest.raises(ValueError, match="2 attendu"):
        load_discovered(path)


# --- E.Leclerc : découverte par catégories ------------------------------------------------------

DK = GameEntry(
    slug="donkey-kong-bananza", title="Donkey Kong Bananza", editions=[{"platform": "switch2"}]
)
TOTK = GameEntry(
    slug="zelda-tears-of-the-kingdom",
    title="The Legend of Zelda: Tears of the Kingdom",
    editions=[{"platform": "switch2"}],
)
DK_FP = "https://www.e.leclerc/fp/donkey-kong-bananza-nintendo-switch-2-0045496312770"


def test_cartes_de_categorie_leclerc() -> None:
    items = src.leclerc_listing(page("leclerc_category_p1.html"))
    assert [(i.ean, i.name) for i in items][:2] == [
        ("0045496312336", "Mario Kart World (NINTENDO SWITCH 2)"),
        ("0196388879953", "Call Of Duty : Modern Warfare 4 (Code in a Box) (NINTENDO SWITCH 2)"),
    ]
    assert items[0].url == LECLERC_FP
    assert src.leclerc_listing(page("leclerc_category_empty.html")) == []
    locs = src.sitemap_locs(page("leclerc_sitemap_navigation.xml").encode())
    assert src.leclerc_category_urls(locs)["switch2"] == "https://www.e.leclerc/cat/jeux-switch-2"


@respx.mock
def test_decouverte_leclerc_par_categorie(fetcher: Fetcher) -> None:
    mock_robots()
    routes = mock_leclerc_categories()
    dk_page = (
        page("leclerc.html")
        .replace(EAN_LECLERC, "0045496312770")
        .replace("Mario Kart World", "Donkey Kong : Bananza")
    )
    dk = respx.get(DK_FP).respond(text=dk_page)
    collector = respx.get(url__regex=r".*edition-collector.*").respond(text="<html/>")
    discovered = empty()
    observations, _ = Scraper({"leclerc": True}, fetcher).run([DK], {}, discovered, TODAY)
    entry = discovered["games"]["donkey-kong-bananza"]["switch2"]
    assert entry["sources"] == {"leclerc": DK_FP} and entry["eans"] == ["0045496312770"]
    assert [o.merchant for o in observations] == ["leclerc"] and dk.call_count == 1
    assert not collector.called  # édition collector : refusée sur le nom de la carte
    pages = [c.request.url.params.get("page", "1") for c in routes["leclerc_categories"].calls]
    assert pages == ["1", "2", "3"]  # jusqu'à la page vide
    assert all(
        "sort" not in str(c.request.url) and "facet" not in str(c.request.url)
        for c in routes["leclerc_categories"].calls
    )


@respx.mock
def test_categorie_lue_une_fois_et_seulement_si_besoin(fetcher: Fetcher) -> None:
    mock_robots()
    routes = mock_leclerc_categories()
    respx.get(DK_FP).respond(text=page("leclerc.html").replace(EAN_LECLERC, "0045496312770"))
    totk = "https://www.e.leclerc/fp/the-legend-of-zelda-tears-of-the-kingdom-nintendo-switch-2-edition-nintendo-switch-2-0045496312589"
    respx.get(totk).respond(text=page("leclerc.html").replace(EAN_LECLERC, "0045496312589"))
    respx.get(LECLERC_FP).respond(text=page("leclerc.html"))
    discovered = empty()
    discovered["games"]["mario-kart-world"] = {
        "switch2": {"eans": [EAN_LECLERC], "sources": {"leclerc": LECLERC_FP}, "checked": {}}
    }
    Scraper({"leclerc": True}, fetcher).run([MKW, DK, TOTK], {}, discovered, TODAY)
    # Deux éditions à découvrir, une seule lecture de la catégorie (3 pages) ; MKW, déjà trouvé,
    # passe directement par sa fiche.
    assert routes["leclerc_categories"].call_count == 3
    assert routes["leclerc_sitemap"].call_count == 1
    assert discovered["games"]["zelda-tears-of-the-kingdom"]["switch2"]["sources"] == {
        "leclerc": totk
    }
    routes["leclerc_categories"].reset()
    Scraper({"leclerc": True}, fetcher).run([MKW, DK, TOTK], {}, discovered, TODAY)
    assert not routes["leclerc_categories"].called  # tout est trouvé : aucune catégorie lue


@respx.mock
def test_fiche_avec_vendeurs_tiers_seulement_ecartee_sans_alerte(fetcher: Fetcher) -> None:
    mock_robots()
    marketplace = page("leclerc.html").replace('"@type": "Offer"', '"@type": "Demande"')
    mock_sites(leclerc=marketplace)
    discovered = {"schema": 2, "games": {"mario-kart-world": {"switch2": {
        "eans": [EAN_LECLERC], "sources": {"leclerc": LECLERC_FP}, "checked": {}
    }}}}  # fmt: skip
    observations, report = Scraper({"leclerc": True}, fetcher).run([MKW], {}, discovered, TODAY)
    assert observations == [] and not report.needs_attention
    assert ("rejected", "offres de vendeurs tiers seulement") in {
        (e["kind"], e["detail"]) for e in report.events
    }


@respx.mock
def test_rupture_memorisee_puis_effacee_au_retour_du_stock(fetcher: Fetcher) -> None:
    from horspc.prices.scraper import out_of_stock

    mock_robots()
    no_offer = page("leclerc.html").replace('"offers"', '"offres"')
    route = respx.get(LECLERC_FP)
    route.side_effect = [
        httpx.Response(200, text=no_offer),
        httpx.Response(200, text=page("leclerc.html")),
    ]
    discovered = {"schema": 2, "games": {"mario-kart-world": {"switch2": {
        "eans": [EAN_LECLERC], "sources": {"leclerc": LECLERC_FP}, "checked": {}
    }}}}  # fmt: skip
    Scraper({"leclerc": True}, fetcher).run([MKW], {}, discovered, TODAY)
    entry = discovered["games"]["mario-kart-world"]["switch2"]
    assert entry["out_of_stock"] == {"leclerc": TODAY.isoformat()}
    assert entry["sources"] == {"leclerc": LECLERC_FP}  # la fiche reste relevée
    assert out_of_stock(discovered) == {("mario-kart-world", "switch2"): ("leclerc", TODAY)}
    later = date.fromordinal(TODAY.toordinal() + 7)
    observations, _ = Scraper({"leclerc": True}, fetcher).run([MKW], {}, discovered, later)
    assert [o.price for o in observations] == [Decimal("66.25")]
    assert "out_of_stock" not in entry and out_of_stock(discovered) == {}


@respx.mock
def test_categorie_prefere_le_physique_avec_offre(fetcher: Fetcher) -> None:
    mock_robots()
    base = "https://www.e.leclerc/fp/"
    box = base + "mario-kart-world-code-in-a-box-nintendo-switch-2-0045496300001"
    rupture = base + "mario-kart-world-nintendo-switch-2-0045496300002"
    ok = base + "mario-kart-world-nintendo-switch-2-0045496300003"
    cards = "".join(
        f'<a href="{u.removeprefix("https://www.e.leclerc")}" data-product-card-title '
        f'title="{n}">{n}</a>'
        for u, n in (
            (box, "Mario Kart World (Code in a Box) (NINTENDO SWITCH 2)"),
            (rupture, "Mario Kart World (NINTENDO SWITCH 2)"),
            (ok, "Mario Kart World (NINTENDO SWITCH 2)"),
        )
    )
    respx.get(src.LECLERC_SITEMAP).respond(text=page("leclerc_sitemap_index.xml"))
    respx.get("https://www.e.leclerc/sitemap/sitemap-navigation-1.xml").respond(
        text=page("leclerc_sitemap_navigation.xml")
    )
    respx.route(host="www.e.leclerc", path__startswith="/cat/").mock(
        side_effect=lambda r: httpx.Response(
            200, text=cards if r.url.params.get("page", "1") == "1" else "<html/>"
        )
    )
    fiche = page("leclerc.html")
    respx.get(box).respond(text=fiche.replace(EAN_LECLERC, "0045496300001"))
    respx.get(rupture).respond(
        text=fiche.replace(EAN_LECLERC, "0045496300002").replace('"offers"', '"offres"')
    )
    respx.get(ok).respond(text=fiche.replace(EAN_LECLERC, "0045496300003"))
    discovered = empty()
    Scraper({"leclerc": True}, fetcher).run([MKW], {}, discovered, TODAY)
    assert discovered["games"]["mario-kart-world"]["switch2"]["sources"] == {"leclerc": ok}
