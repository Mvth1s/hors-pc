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
        router.post(f"{API_URL}/release_date_regions").respond(
            json=load_fixture("igdb_release_regions.json")
        )
        router.post(f"{API_URL}/release_date_statuses").respond(
            json=load_fixture("igdb_release_statuses.json")
        )
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


def test_secret_dans_le_corps_pas_dans_l_url(igdb: IgdbClient, api: respx.MockRouter) -> None:
    igdb.platform_ids()
    request = api.routes[0].calls.last.request
    assert "secret-test" not in str(request.url)
    assert b"client_secret=secret-test" in request.content


def test_release_date_europe_ou_monde_en_version_complete(
    igdb: IgdbClient, api: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    api.post(GAMES_URL).respond(json=load_fixture("igdb_games.json"))
    games = [
        # Advanced Access (28/04) et sortie Japon (26/04) écartées : reste le 30/04.
        entry("saros", "Saros", ["ps5"], 331215),
        # Statut absent accepté ; la ligne Switch 2 (autre plateforme) est ignorée.
        entry("pokemon-ecarlate", "Pokémon Écarlate", ["switch"], 191931),
        entry("sans-date", "Jeu sans date retenue", ["ps5"], 400001),
    ]
    result = igdb.fetch_games(games, NOW)
    assert result["saros"].release_date == date(2026, 4, 30)
    assert result["pokemon-ecarlate"].release_date == date(2022, 11, 18)
    assert result["sans-date"].release_date is None
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == [
        "IGDB : sans-date, aucune date de sortie retenue (3 date(s) écartée(s) : "
        "1 région, 1 sans date, 1 statut) ; ajouter un release_date_override"
    ]


def test_pas_d_avertissement_si_release_date_override(
    igdb: IgdbClient, api: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    api.post(GAMES_URL).respond(json=load_fixture("igdb_games.json"))
    game = GameEntry(
        slug="sans-date",
        title="Jeu sans date retenue",
        platforms=["ps5"],
        igdb_id=400001,
        release_date_override={"date": "2023-11-17", "source_url": "https://example.org/sortie"},
    )
    igdb.fetch_games([game], NOW)
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_region_ou_statut_introuvable(igdb: IgdbClient, api: respx.MockRouter) -> None:
    api.post(f"{API_URL}/release_date_regions").respond(json=[{"id": 8, "region": "worldwide"}])
    with pytest.raises(IgdbError, match="europe"):
        igdb.fetch_games([entry("astro-bot", "Astro Bot", ["ps5"])], NOW)


@pytest.mark.parametrize(
    ("slug", "igdb_id", "expected"),
    [
        # Seule ligne PC au statut Cancelled : ignorée, même si le PC est dans platforms.
        ("portage-annule", 400002, "none"),
        # Early Access passé (région Amérique du Nord : la région ne compte pas pour le PC).
        ("pc-acces-anticipe", 400003, "released"),
        # Bêta passée (hors liste released) + ligne Cancelled ignorée.
        ("pc-beta", 400004, "announced"),
        # Ligne PC sans date ni statut.
        ("ghost-of-yotei", 250634, "announced"),
        # Full Release passée.
        ("death-stranding-2", 152063, "released"),
        ("astro-bot", 255340, "none"),
    ],
)
def test_pc_status_selon_les_statuts(
    igdb: IgdbClient, api: respx.MockRouter, slug: str, igdb_id: int, expected: str
) -> None:
    api.post(GAMES_URL).respond(json=load_fixture("igdb_games.json"))
    result = igdb.fetch_games([entry(slug, slug, ["ps5"], igdb_id)], NOW)
    assert result[slug].pc_status == expected
