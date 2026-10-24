from datetime import date

import httpx
import pytest
import respx

from horspc.http import HttpClient
from horspc.igdb import API_URL, TOKEN_URL, IgdbClient, IgdbError
from horspc.models import GameEntry
from tests.conftest import NOW, load_fixture

GAMES_URL = f"{API_URL}/games"
PLATFORMS_URL = f"{API_URL}/platforms"


def entry(slug: str, title: str, platforms: list[str], igdb_id: int | None = None) -> GameEntry:
    return GameEntry(slug=slug, title=title, platforms=platforms, igdb_id=igdb_id)


@pytest.fixture
def igdb(http: HttpClient) -> IgdbClient:
    return IgdbClient("id-test", "secret-test", http, clock=lambda: 1_000.0)


@pytest.fixture
def api():
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).respond(json=load_fixture("igdb_token.json"))
        router.post(PLATFORMS_URL).respond(json=load_fixture("igdb_platforms.json"))
        yield router


def test_statuts_pc_et_dates(igdb: IgdbClient, api: respx.MockRouter) -> None:
    api.post(GAMES_URL).respond(json=load_fixture("igdb_games.json"))
    games = [
        entry("astro-bot", "Astro Bot", ["ps5"], 255340),
        entry("ghost-of-yotei", "Ghost of Yōtei", ["ps5"], 250634),
        entry("death-stranding-2", "Death Stranding 2", ["ps5"], 152063),
    ]
    result = igdb.fetch_games(games, NOW)

    astro = result["astro-bot"]
    assert astro is not None
    assert astro.pc_status == "none"
    assert astro.release_date == date(2024, 9, 6)
    assert astro.url == "https://www.igdb.com/games/astro-bot"
    assert result["ghost-of-yotei"].pc_status == "announced"
    ds2 = result["death-stranding-2"]
    assert ds2.pc_status == "released"
    assert ds2.release_date == date(2025, 6, 20)


def test_id_pc_lu_via_platforms_et_auth(igdb: IgdbClient, api: respx.MockRouter) -> None:
    assert igdb.platform_ids()["win"] == 6
    request = api.routes[1].calls.last.request
    assert request.headers["Client-ID"] == "id-test"
    assert request.headers["Authorization"] == "Bearer jeton-de-test"
    assert b'"win"' in request.content


def test_token_mis_en_cache(igdb: IgdbClient, api: respx.MockRouter) -> None:
    api.post(GAMES_URL).respond(json=[])
    igdb.query("games", "fields id;")
    igdb.query("games", "fields id;")
    assert api.routes[0].call_count == 1


def test_token_renouvele_sur_401(igdb: IgdbClient, api: respx.MockRouter) -> None:
    route = api.post(GAMES_URL).mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json=[])]
    )
    assert igdb.query("games", "fields id;") == []
    assert route.call_count == 2
    assert api.routes[0].call_count == 2


def test_recherche_par_titre_correspondance_exacte(igdb: IgdbClient, api: respx.MockRouter) -> None:
    route = api.post(GAMES_URL).respond(json=load_fixture("igdb_search_mario_kart.json"))
    result = igdb.fetch_games([entry("mario-kart-world", "Mario Kart World", ["switch2"])], NOW)
    found = result["mario-kart-world"]
    assert found is not None and found.igdb_id == 338067
    assert found.release_date == date(2025, 6, 5)
    body = route.calls.last.request.content.decode()
    assert 'search "Mario Kart World"' in body
    assert "where platforms = (508)" in body


def test_jeu_introuvable(igdb: IgdbClient, api: respx.MockRouter) -> None:
    api.post(GAMES_URL).respond(json=[])
    assert igdb.fetch_games([entry("inconnu", "Inconnu", ["ps5"])], NOW) == {"inconnu": None}


def test_plateforme_manquante(igdb: IgdbClient, api: respx.MockRouter) -> None:
    api.post(PLATFORMS_URL).respond(json=[{"id": 6, "slug": "win"}])
    with pytest.raises(IgdbError, match="switch-2"):
        igdb.fetch_games([entry("astro-bot", "Astro Bot", ["ps5"])], NOW)


def test_igdb_injoignable(igdb: IgdbClient, api: respx.MockRouter) -> None:
    api.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("hors ligne"))
    with pytest.raises(IgdbError):
        igdb.fetch_games([entry("astro-bot", "Astro Bot", ["ps5"])], NOW)
