from datetime import date
from pathlib import Path

import httpx
import pytest
import respx
import yaml

from horspc.__main__ import main
from horspc.candidates import find_candidates, render_yaml, validate_entries
from horspc.http import HttpClient
from horspc.igdb import API_URL, TOKEN_URL, IgdbClient
from tests.conftest import NOW, load_fixture

DATA = load_fixture("igdb_candidates.json")


def games_api(request: httpx.Request) -> httpx.Response:
    """Répond selon la requête : éditions, liste par notes, ou liste par hypes."""
    body = request.content.decode()
    if "where version_parent = (" in body:
        parents = body.split("version_parent = (")[1].split(")")[0].split(",")
        rows = [e for e in DATA["editions"] if str(e["version_parent"]) in parents]
        return httpx.Response(200, json=rows)
    if "offset 0;" not in body:
        return httpx.Response(200, json=[])
    if "sort hypes desc" in body:
        assert "hypes != null" in body and "total_rating_count >=" not in body
        return httpx.Response(200, json=DATA["recent"])
    assert "sort total_rating_count desc" in body and "total_rating_count >= 20" in body
    assert "game_type = (0,8,9)" in body and "version_parent = null" in body
    assert "platforms = (130,167,169,508)" in body  # Switch, PS5, Xbox Series, Switch 2
    return httpx.Response(200, json=DATA["popular"])


@pytest.fixture
def igdb(http: HttpClient):
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).respond(json=load_fixture("igdb_token.json"))
        for endpoint, fixture in [
            ("platforms", "igdb_platforms.json"),
            ("release_date_regions", "igdb_release_regions.json"),
            ("release_date_statuses", "igdb_release_statuses.json"),
            ("game_types", "igdb_game_types.json"),
        ]:
            router.post(f"{API_URL}/{endpoint}").respond(json=load_fixture(fixture))
        router.post(f"{API_URL}/games").mock(side_effect=games_api)
        yield IgdbClient("id", "secret", http, clock=lambda: 1_000.0)


def run(igdb: IgdbClient, **kw):
    options = {"limit": 50, "min_ratings": 20, "recent_limit": 10} | kw
    return find_candidates(igdb, {119388}, NOW, **options)


def test_filtres(igdb: IgdbClient) -> None:
    popular, _ = run(igdb)
    assert [c.slug for c in popular] == [
        "exclu-ps5",
        "portage-annonce",
        "portage-annule",
        "remake-jeu-pc",
        "remaster-portage-annonce",
        "exclu-xbox",
    ]
    exclu, annonce, annule, *_ = popular
    assert (exclu.platforms, exclu.total_rating_count, exclu.notes) == (["ps5"], 900, [])
    assert exclu.first_release == date(2023, 11, 14)
    # Portage annoncé via une fiche liée : gardé, signalé en commentaire.
    assert annonce.notes == ["portage PC annoncé via « Portage annoncé Deluxe » (igdb 2004)"]
    # Seule ligne PC au statut Cancelled : gardé, sans commentaire.
    assert (annule.platforms, annule.notes) == (["switch2"], [])


def test_jeu_d_origine_ps4_garde_avec_include_legacy(igdb: IgdbClient) -> None:
    popular, _ = run(igdb, include_legacy=True)
    legacy = next(c for c in popular if c.slug == "jeu-ps4")
    assert legacy.platforms == ["ps5"]
    assert legacy.notes == ["première sortie hors PS5/Switch/Switch 2/Xbox Series"]


def test_limite_respectee(igdb: IgdbClient) -> None:
    popular, _ = run(igdb, limit=2)
    assert [c.slug for c in popular] == ["exclu-ps5", "portage-annonce"]


def test_sorties_recentes_sans_doublon(igdb: IgdbClient) -> None:
    _, recent = run(igdb)
    assert [(c.slug, c.hypes) for c in recent] == [("nouveaute", 50)]


def test_yaml_valide_et_commente(igdb: IgdbClient) -> None:
    popular, recent = run(igdb)
    text = render_yaml(popular, recent, date(2026, 10, 26), 20)
    entries = yaml.safe_load(text)
    validate_entries(entries)
    assert [e["slug"] for e in entries][-1] == "nouveaute"
    assert entries[-1]["title"] == 'Nouveauté "Switch 2"'
    assert all(e["launch_price"] is None for e in entries)
    assert "# --- Sorties récentes (365 derniers jours, triées par hypes" in text
    assert "- slug: exclu-ps5  # 900 notes · Main Game · sortie 2023-11-14" in text
    assert "  # portage PC annoncé via « Portage annoncé Deluxe » (igdb 2004)" in text
    assert "- slug: nouveaute  # 50 hypes" in text
    assert text.index("Sorties récentes") < text.index("- slug: nouveaute")


def test_cli_refuse_d_ecrire_dans_games_yaml(tmp_path: Path) -> None:
    games = tmp_path / "games.yaml"
    games.write_text("[]", encoding="utf-8")
    assert main(["candidates", "--games", str(games), "--output", str(games)]) == 2
    assert games.read_text(encoding="utf-8") == "[]"


def test_parent_game_controle_pour_remaster_pas_pour_remake(igdb: IgdbClient) -> None:
    popular, _ = run(igdb)
    slugs = [c.slug for c in popular]
    # Remaster dont l'original est sorti sur PC (cas réel : L.A. Noire) : écarté.
    assert "remaster-jeu-pc" not in slugs
    # Remake d'un jeu sorti sur PC : un autre jeu, gardé.
    assert "remake-jeu-pc" in slugs
    remaster = next(c for c in popular if c.slug == "remaster-portage-annonce")
    assert remaster.notes == ["portage PC annoncé via « jeu d'origine Original » (igdb 1911)"]


def test_xbox_series_meme_exclusion_pc(igdb: IgdbClient) -> None:
    popular, _ = run(igdb)
    xbox = next(c for c in popular if c.slug == "exclu-xbox")
    assert (xbox.platforms, xbox.notes) == (["xbox_series"], [])
    assert "xbox-et-pc" not in [c.slug for c in popular]  # sorti sur PC : écarté
