import httpx
import pytest
import respx

from horspc.http import HttpClient
from horspc.robots import RobotsCache, parse

AUCHAN = """
User-agent: Googlebot
Disallow: /

User-agent: *
#RECHERCHE
Disallow: /recherche*
Disallow: /*/pr-91500342
Allow: /recherche/autorisee$
"""


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/recherche?text=0045496312336", False),  # joker * : urllib.robotparser l'autorisait
        ("/recherchefoo", False),
        ("/nintendo-mario-kart-world/pr-C1730911", True),
        ("/jeux/pr-91500342", False),
        ("/recherche/autorisee", True),  # Allow plus long et ancré par $
        ("/recherche/autorisee/suite", False),  # $ : fin de chemin exigée
    ],
)
def test_jokers_et_ancre(path: str, allowed: bool) -> None:
    assert parse(AUCHAN).allowed("https://www.auchan.fr" + path) is allowed


def test_regle_la_plus_longue_et_allow_en_cas_d_egalite() -> None:
    robots = parse("User-agent: *\nDisallow: /ref/\nAllow: /ref/*/fiche\nDisallow: /p\nAllow: /p\n")
    assert not robots.allowed("https://x.fr/ref/1/avis")
    assert robots.allowed("https://x.fr/ref/1/fiche")
    assert robots.allowed("https://x.fr/page")  # égalité Disallow /p et Allow /p : Allow


def test_groupe_specifique_prioritaire() -> None:
    robots = parse("User-agent: *\nDisallow: /\n\nUser-agent: HorsPC\nDisallow: /prive\n")
    assert robots.allowed("https://x.fr/fiche")
    assert not robots.allowed("https://x.fr/prive/1")


def test_groupes_avec_plusieurs_user_agents() -> None:
    robots = parse("User-agent: a\nUser-agent: *\nDisallow: /recherche\nUser-agent: b\nAllow: /\n")
    assert not robots.allowed("https://x.fr/recherche")


def test_verdict_cite_la_ligne() -> None:
    allowed, rule = parse(AUCHAN).verdict("https://www.auchan.fr/recherche?q=x")
    assert not allowed and rule is not None
    assert (rule.line, rule.raw) == (7, "Disallow: /recherche*")


def test_robots_vide_tout_autorise() -> None:
    assert parse("<!DOCTYPE html><html>…</html>").allowed("https://x.fr/tout")


@pytest.mark.parametrize(("status", "allowed"), [(404, True), (403, True), (503, False)])
def test_robots_injoignable(http: HttpClient, status: int, allowed: bool) -> None:
    with respx.mock:
        respx.get("https://x.fr/robots.txt").respond(status)
        assert RobotsCache(http).get("https://x.fr/a").allowed("https://x.fr/a") is allowed


def test_robots_erreur_reseau_tout_interdit(http: HttpClient) -> None:
    with respx.mock:
        respx.get("https://x.fr/robots.txt").mock(side_effect=httpx.ConnectError("hors ligne"))
        assert not RobotsCache(http).get("https://x.fr/a").allowed("https://x.fr/a")


def test_robots_lu_une_fois_par_hote(http: HttpClient) -> None:
    with respx.mock:
        route = respx.get("https://x.fr/robots.txt").respond(200, text="User-agent: *\nAllow: /\n")
        cache = RobotsCache(http)
        cache.get("https://x.fr/a")
        cache.get("https://x.fr/b")
        assert route.call_count == 1
