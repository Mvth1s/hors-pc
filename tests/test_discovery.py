"""Découverte du catalogue : récolte, index IGDB, identification stricte et filtre PC."""

from collections import Counter
from datetime import date
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
import respx
from pydantic import ValidationError

from horspc import discovery
from horspc.discovery import IndexedGame, Matcher, Product
from horspc.http import HttpClient
from horspc.igdb import API_URL, TOKEN_URL, IgdbClient
from horspc.prices.sources import ListingItem
from tests.conftest import NOW, TODAY, load_fixture

PAST = int(NOW.timestamp()) - 86_400 * 365
PC, PS5, SWITCH = 6, 167, 130
FULL_RELEASE = 6


def game(igdb_id: int, name: str, platforms: set, *alt: str, companies=(), kind="Main Game"):
    return IndexedGame(igdb_id, name, (name, *alt), kind, set(platforms), tuple(companies))


def product(name: str, platform: str = "ps5", source: str = "leclerc") -> Product:
    return Product(source, platform, name, f"https://x/{name}", None)


# --- Identification ------------------------------------------------------------------------


def test_correspondance_exacte_et_nom_alternatif() -> None:
    matcher = Matcher({1: game(1, "Grand Theft Auto VI", {"ps5"}, "GTA VI")})
    assert matcher.identify(product("Grand Theft Auto VI PS5")).igdb_id == 1
    assert matcher.identify(product("GTA VI (PS5)")).igdb_id == 1


def test_edition_speciale_accessoire_et_autre_plateforme_ecartes() -> None:
    matcher = Matcher({1: game(1, "Astro Bot", {"ps5"})})
    for name, platform in [
        ("Astro Bot Edition Deluxe PS5", "ps5"),
        ("Manette DualSense Astro Bot PS5", "ps5"),
        ("Carte PlayStation Store 50 euros PS5", "ps5"),
        ("Astro Bot Switch", "switch"),  # le jeu n'est pas sur Switch
    ]:
        assert matcher.identify(product(name, platform)).status == "unmatched", name


def test_plateforme_d_une_edition_rattachee_au_jeu_parent() -> None:
    zelda = game(2, "The Legend of Zelda: Tears of the Kingdom", {"switch", "switch2"})
    matcher = Matcher({2: zelda})
    name = "The Legend of Zelda : Tears of the Kingdom - Nintendo Switch 2 Edition"
    assert matcher.identify(product(name, "switch2")).igdb_id == 2


def test_plusieurs_jeux_a_couverture_egale_ecartes() -> None:
    matcher = Matcher({3: game(3, "Tetris", {"switch"}), 4: game(4, "Tetris", {"switch"})})
    ident = matcher.identify(product("Tetris Switch", "switch"))
    assert (ident.status, ident.igdb_id, ident.candidates) == ("ambiguous", None, (3, 4))


def test_couverture_strictement_superieure_retenue() -> None:
    # « Legends: Z-A » passe aussi (« Pokémon » vient de son éditeur, mot toléré), mais le
    # titre de 5 couvre strictement plus de mots du nom marchand.
    matcher = Matcher(
        {
            5: game(5, "Pokémon Legends: Z-A", {"switch"}, companies=["Game Freak"]),
            6: game(6, "Legends: Z-A", {"switch"}, companies=["The Pokémon Company"]),
        }
    )
    ident = matcher.identify(product("Pokémon Legends Z-A Switch", "switch"))
    assert (ident.status, ident.igdb_id, ident.candidates) == ("matched", 5, (6,))


def test_plateforme_du_rayon_quand_le_nom_n_en_cite_aucune() -> None:
    matcher = Matcher({1: game(1, "Saros", {"ps5"}, companies=["Housemarque"])})
    assert matcher.identify(product("Saros")).igdb_id == 1
    assert matcher.identify(product("Saros Xbox Series X", "ps5")).status == "unmatched"


# --- Récolte -------------------------------------------------------------------------------


class FakeScraper:
    enabled: ClassVar[list[str]] = ["leclerc", "pixmania"]

    def leclerc_listing(self, platform: str) -> list[ListingItem]:
        if platform != "ps5":
            return []
        return [
            ListingItem("https://www.e.leclerc/fp/astro-bot-1", "0711719588611", "Astro Bot PS5"),
            ListingItem("https://www.e.leclerc/fp/astro-bot-2", "1", "Astro Bot PS5 Occasion"),
        ]

    def pixmania_sitemap(self) -> list[str]:
        base = "https://www.pixmania.com/fr/fr/"
        return [
            base + "saros-ps5-528089.html",
            base + "casque-sans-fil-123456.html",
            base + "jeu-ps5-et-xbox-series-x-654321.html",
        ]


def test_recolte_sans_ouvrir_de_fiche() -> None:
    products, rejected = discovery.harvest(FakeScraper())
    assert products == [
        Product(
            "leclerc",
            "ps5",
            "Astro Bot PS5",
            "https://www.e.leclerc/fp/astro-bot-1",
            "0711719588611",
        ),
        Product(
            "pixmania",
            "ps5",
            "saros ps5 528089",
            "https://www.pixmania.com/fr/fr/saros-ps5-528089.html",
            None,
        ),
    ]
    assert rejected == Counter(
        {"pixmania : slug sans plateforme cible unique": 2, "occasion ou reconditionné": 1}
    )


# --- Index, filtre et titres (IGDB simulé) ---------------------------------------------------

INDEX = [
    {"id": 1, "name": "Astro Bot", "platforms": [PS5], "game_type": {"type": "Main Game"}},
    {"id": 2, "name": "Death Stranding 2", "platforms": [PS5], "game_type": {"type": "Main Game"}},
    {"id": 3, "name": "Remaster X", "platforms": [SWITCH], "game_type": {"type": "Remaster"}},
    {"id": 4, "name": "Astro Bot Rescue", "platforms": [PS5], "game_type": {"type": "DLC"}},
    {"id": 5, "name": "Curated Game", "platforms": [PS5], "game_type": {"type": "Main Game"}},
    {"id": 6, "name": "False Positive", "platforms": [PS5], "game_type": {"type": "Main Game"}},
    {"id": 7, "name": "Old Game", "platforms": [SWITCH], "game_type": {"type": "Main Game"}},
]
PC_RELEASE = {"platform": PC, "date": PAST, "status": FULL_RELEASE}
DETAILS = {
    1: {"id": 1, "name": "Astro Bot", "game_type": {"type": "Main Game"}, "release_dates": []},
    2: {
        "id": 2,
        "name": "Death Stranding 2",
        "game_type": {"type": "Main Game"},
        "release_dates": [PC_RELEASE],
    },
    # Remaster dont le jeu d'origine est sorti sur PC : même jeu, écarté.
    3: {
        "id": 3,
        "name": "Remaster X",
        "game_type": {"type": "Remaster"},
        "release_dates": [{"platform": SWITCH, "date": PAST, "status": FULL_RELEASE}],
        "parent_game": {"id": 30, "name": "Original X", "release_dates": [PC_RELEASE]},
    },
    # Sorti d'abord sur PS4, jamais sur PC : gardé (seul le statut PC exclut).
    7: {
        "id": 7,
        "name": "Old Game",
        "game_type": {"type": "Main Game"},
        "platforms": [48, SWITCH],
        "release_dates": [{"platform": 48, "date": PAST - 86_400 * 2000, "status": FULL_RELEASE}],
    },
}


for _row in DETAILS.values():
    _row["slug"] = _row["name"].lower().replace(" ", "-")


def igdb_games(request: httpx.Request) -> httpx.Response:
    body = request.content.decode()
    if "version_parent != null" in body:
        return httpx.Response(200, json=[])
    if "version_parent = null" in body:
        last = int(body.split("id > ")[1].split(";")[0])
        return httpx.Response(
            200, json=[r for r in INDEX if r["id"] > last][: discovery.PAGE_LIMIT]
        )
    if "where version_parent = (" in body:
        return httpx.Response(200, json=[])
    ids = [int(i) for i in body.split("where id = (")[1].split(")")[0].split(",")]
    return httpx.Response(200, json=[DETAILS[i] for i in ids if i in DETAILS])


@pytest.fixture
def api():
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).respond(json=load_fixture("igdb_token.json"))
        for endpoint, fixture in [
            ("platforms", "igdb_platforms.json"),
            ("release_date_regions", "igdb_release_regions.json"),
            ("release_date_statuses", "igdb_release_statuses.json"),
            ("game_types", "igdb_game_types.json"),
        ]:
            router.post(f"{API_URL}/{endpoint}").respond(json=load_fixture(fixture))
        router.post(f"{API_URL}/regions").respond(json=[{"id": 4, "identifier": "EU"}])
        router.post(f"{API_URL}/game_localizations").respond(
            json=[{"game": 1, "name": "Astro Bot (Europe)", "region": 4}]
        )
        router.post(f"{API_URL}/games").mock(side_effect=igdb_games)
        yield router


@pytest.fixture
def igdb(http: HttpClient) -> IgdbClient:
    return IgdbClient("id", "secret", http, clock=lambda: 1_000.0)


def test_index_pagine_par_curseur(
    igdb: IgdbClient, api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery, "PAGE_LIMIT", 3)
    index = discovery.fetch_index(igdb)
    assert sorted(index) == [1, 2, 3, 4, 5, 6, 7]
    bodies = [
        c.request.content.decode()
        for c in api.calls
        if "version_parent = null" in str(c.request.content)
    ]
    assert [b.split("id > ")[1].split(";")[0] for b in bodies] == ["0", "3", "6"]


def test_filtre_pc_types_curation_et_exclusions(igdb: IgdbClient, api: respx.MockRouter) -> None:
    products = [
        product("Astro Bot PS5"),
        product("Death Stranding 2 PS5"),
        product("Remaster X Switch", "switch"),
        product("Astro Bot Rescue PS5"),
        product("Curated Game PS5"),
        product("False Positive PS5"),
        product("Old Game", "switch"),
    ]
    result = discovery.discover(igdb, products, Counter(), {5}, {6}, NOW)
    assert [g.igdb_id for g in result.kept] == [1, 7]
    astro = result.kept[0]
    assert (astro.title, astro.name, list(astro.products)) == (
        "Astro Bot (Europe)",
        "Astro Bot",
        ["ps5"],
    )
    old = result.kept[1]
    assert old.title == "Old Game"  # pas de localisation : nom IGDB
    assert old.notes == ["première sortie hors PS5/Switch/Switch 2/Xbox Series"]
    assert sorted(result.pc_released) == [2, 3]
    assert result.wrong_type == Counter({"DLC": 1})
    assert (result.curated, result.excluded) == ({5}, {6})

    report = discovery.summary(result)
    assert (report["kept"], report["kept_editions"], report["weekly_fiches"]) == (2, 2, 2)
    assert report["kept_pc_announced"] == 0
    assert report["first_run_images"] == 4  # jaquette + 1 capture par jeu
    assert report["site_bytes_added"] == 2 * (
        discovery.COVER_BYTES + discovery.SCREEN_BYTES + discovery.PAGE_BYTES
    )


# --- Exclusions ----------------------------------------------------------------------------


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "discovery.exclude.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_exclusions_validees(tmp_path: Path) -> None:
    assert discovery.load_exclusions(tmp_path / "absent.yaml", TODAY) == []
    path = write(tmp_path, "- {igdb_id: 6, reason: produit mal rattaché, date: 2026-10-20}\n")
    [exclusion] = discovery.load_exclusions(path, TODAY)
    assert (exclusion.igdb_id, exclusion.date) == (6, date(2026, 10, 20))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("- {igdb_id: 6, reason: a, date: 2026-10-20}\n" * 2, "igdb_id en double"),
        ("- {igdb_id: 6, reason: a, date: 2027-01-01}\n", "futur"),
        ("- {igdb_id: 6, reason: '', date: 2026-10-20}\n", "at least 1 character"),
        ("- {igdb_id: 6, date: 2026-10-20}\n", "reason"),
    ],
)
def test_exclusions_refusees(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        discovery.load_exclusions(write(tmp_path, text), TODAY)
