"""Consentement GA4 en navigateur headless : aucune requête Google avant accord ni après refus."""

import functools
import http.server
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

SITE = Path(__file__).parents[1] / "site"
GOOGLE_HOSTS = (
    "www.googletagmanager.com",
    "www.google-analytics.com",
    "region1.google-analytics.com",
)
GA_ID = "G-TMFL6HQ430"
PAGES = [
    "",
    "faq.html",
    "methodologie.html",
    "mentions-legales.html",
    "confidentialite.html",
    "jeux/astro-bot/",
]


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    handler = functools.partial(Quiet, directory=str(SITE))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()


@pytest.fixture(scope="module")
def browser() -> Iterator[object]:
    with playwright.sync_playwright() as p:
        try:
            instance = p.chromium.launch()
        except Exception as exc:  # navigateur non installé : `uv run playwright install`
            pytest.skip(f"Chromium indisponible : {exc}")
        yield instance
        instance.close()


class Recorder:
    """Contexte de navigation qui enregistre toutes les requêtes et simule Google."""

    def __init__(self, browser: object, base_url: str) -> None:
        self.base = base_url
        self.context = browser.new_context()
        self.requests: list[str] = []
        self.context.route("**/*", self._route)
        self.page = self.context.new_page()

    def _route(self, route: object) -> None:
        url = route.request.url
        self.requests.append(url)
        if url.startswith(self.base):
            route.continue_()
        elif "googletagmanager.com/gtag/js" in url:
            route.fulfill(status=200, content_type="text/javascript", body="/* gtag simulé */")
        else:
            route.fulfill(status=204, body="")

    def google(self) -> list[str]:
        return [u for u in self.requests if any(h in u for h in GOOGLE_HOSTS)]

    def external(self) -> list[str]:
        return [u for u in self.requests if not u.startswith(self.base)]

    def goto(self, path: str = "") -> None:
        self.page.goto(self.base + path)
        self.page.wait_for_load_state("networkidle")

    def close(self) -> None:
        self.context.close()


@pytest.fixture
def nav(browser: object, base_url: str) -> Iterator[Recorder]:
    recorder = Recorder(browser, base_url)
    yield recorder
    recorder.close()


@pytest.mark.parametrize("path", PAGES)
def test_aucune_requete_externe_avant_consentement(nav: Recorder, path: str) -> None:
    nav.goto(path)
    assert nav.page.locator("#consent").is_visible()
    assert nav.external() == []


def test_boutons_equivalents(nav: Recorder) -> None:
    nav.goto()
    refuse = nav.page.get_by_role("button", name="Refuser", exact=True)
    accept = nav.page.get_by_role("button", name="Accepter", exact=True)
    assert refuse.get_attribute("class") == accept.get_attribute("class")
    a, b = refuse.bounding_box(), accept.bounding_box()
    assert (round(a["width"]), round(a["height"])) == (round(b["width"]), round(b["height"]))
    assert nav.page.locator("#consent input[type=checkbox]").count() == 0


@pytest.mark.parametrize("button", ["Refuser", "Fermer (refuser)"])
def test_refus_ou_fermeture_rien_ne_charge(nav: Recorder, button: str) -> None:
    nav.goto()
    nav.page.get_by_role("button", name=button, exact=True).click()
    assert not nav.page.locator("#consent").count()
    nav.goto("faq.html")
    assert not nav.page.locator("#consent").count()  # choix mémorisé
    assert nav.google() == []
    assert nav.external() == []


def test_acceptation_charge_ga_sans_personnalisation(nav: Recorder) -> None:
    nav.goto()
    assert nav.google() == []
    nav.page.get_by_role("button", name="Accepter", exact=True).click()
    nav.page.wait_for_load_state("networkidle")
    assert any(f"googletagmanager.com/gtag/js?id={GA_ID}" in u for u in nav.google())
    assert set(nav.external()) <= set(nav.google())  # aucun autre domaine
    config = nav.page.evaluate(
        "() => window.dataLayer.map(a => Array.from(a)).find(a => a[0] === 'config')"
    )
    assert config[1] == GA_ID
    assert config[2]["allow_google_signals"] is False
    assert config[2]["allow_ad_personalization_signals"] is False
    # Visite suivante : GA chargé directement, sans bandeau.
    nav.requests.clear()
    nav.goto("methodologie.html")
    assert not nav.page.locator("#consent").count()
    assert nav.google()


def test_choix_redemande_apres_six_mois(nav: Recorder) -> None:
    nav.goto()
    old = (datetime.now(UTC) - timedelta(days=200)).isoformat()
    nav.page.evaluate(
        "(v) => localStorage.setItem('horspc-consent', v)",
        json.dumps({"choice": "granted", "date": old}),
    )
    nav.requests.clear()
    nav.goto()
    assert nav.page.locator("#consent").is_visible()
    assert nav.google() == []


def test_retrait_supprime_les_cookies_et_recharge_sans_ga(nav: Recorder, base_url: str) -> None:
    nav.goto()
    nav.page.get_by_role("button", name="Accepter", exact=True).click()
    nav.context.add_cookies(
        [
            {"name": "_ga", "value": "GA1.1.123.456", "url": base_url},
            {"name": "_ga_TMFL6HQ430", "value": "GS1.1.789", "url": base_url},
        ]
    )
    nav.page.locator("footer [data-consent-open]").click()
    assert nav.page.locator("#consent").is_visible()
    nav.requests.clear()
    with nav.page.expect_navigation():
        nav.page.get_by_role("button", name="Refuser", exact=True).click()
    nav.page.wait_for_load_state("networkidle")
    assert [c["name"] for c in nav.context.cookies() if c["name"].startswith("_ga")] == []
    assert nav.google() == []
    assert nav.page.evaluate("() => typeof window.gtag") == "undefined"


# --- Bande-annonce : aucune requête YouTube avant le clic --------------------------------

YOUTUBE_HOSTS = ("youtube.com", "youtube-nocookie.com", "ytimg.com", "googlevideo.com")


def youtube(nav: Recorder) -> list[str]:
    return [u for u in nav.requests if any(h in u for h in YOUTUBE_HOSTS)]


def test_bande_annonce_chargee_seulement_au_clic(nav: Recorder) -> None:
    nav.goto("jeux/astro-bot/")
    nav.page.get_by_role("button", name="Refuser", exact=True).click()
    facade = nav.page.locator(".trailer[data-youtube-id]")
    assert facade.count() == 1
    assert nav.page.locator("iframe").count() == 0
    assert youtube(nav) == [] and nav.external() == []
    video_id = facade.get_attribute("data-youtube-id")
    nav.page.locator(".trailer-play").click()
    nav.page.wait_for_selector(".trailer iframe")
    src = nav.page.locator(".trailer iframe").get_attribute("src")
    assert src.startswith(f"https://www.youtube-nocookie.com/embed/{video_id}")
    assert any("youtube-nocookie.com/embed/" in u for u in youtube(nav))
    # Seuls des domaines YouTube après le clic, aucune requête Google Analytics (refusé).
    assert set(nav.external()) <= set(youtube(nav))


# --- Bouton « Retour au catalogue » des pages de jeu -------------------------------------------


def test_retour_au_catalogue_retrouve_les_filtres(base_url: str, browser: object) -> None:
    context = browser.new_context()
    context.route(
        "**/*", lambda r: r.continue_() if r.request.url.startswith(base_url) else r.abort()
    )
    page = context.new_page()
    page.goto(base_url)
    page.click('[data-platform="switch2"]')
    page.evaluate("window.scrollTo(0, 400)")
    page.click('article[data-title="Mario Kart World"] .title a')
    page.wait_for_url("**/jeux/mario-kart-world/")
    page.click("[data-back]")
    # history.back() : retour à l'URL du catalogue filtré (le lien mènerait à « ./ »).
    page.wait_for_url(base_url + "?console=switch2")
    pressed = page.get_attribute('[data-platform="switch2"]', "aria-pressed")
    assert pressed == "true"
    context.close()


def test_retour_sans_historique_mene_au_catalogue(base_url: str, browser: object) -> None:
    context = browser.new_context()
    context.route(
        "**/*", lambda r: r.continue_() if r.request.url.startswith(base_url) else r.abort()
    )
    page = context.new_page()
    page.goto(base_url + "jeux/mario-kart-world/")  # arrivée directe (moteur, lien partagé)
    page.click("[data-back]")
    page.wait_for_url(base_url)
    assert page.get_attribute('[data-platform="all"]', "aria-pressed") == "true"
    context.close()
