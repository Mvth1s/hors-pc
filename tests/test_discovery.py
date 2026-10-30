"""Découverte du catalogue : récolte, index IGDB, identification stricte et filtre PC."""

import re
from collections import Counter
from datetime import date, timedelta
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
from horspc.models import DiscoveredGame
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


class FakeFetcher:
    def __init__(self, blocked: dict[str, str] | None = None) -> None:
        self.blocked = blocked or {}


class FakeScraper:
    enabled: ClassVar[list[str]] = ["leclerc", "pixmania"]

    def __init__(self, blocked: dict[str, str] | None = None) -> None:
        self.fetcher = FakeFetcher(blocked)

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
    {
        "id": 7,
        "name": "Old Game",
        "platforms": [SWITCH],
        "game_type": {"type": "Main Game"},
        "alternative_names": [
            {"name": "Viejo Juego", "comment": "Spanish title"},
            {"name": "Vieux Jeu", "comment": "French title"},
            {"name": "オールドゲーム", "comment": "Japanese title"},
            {"name": "올드 R", "comment": "Korean title"},
        ],
    },
    {"id": 8, "name": "Multi Game", "platforms": [PS5, PC], "game_type": {"type": "Main Game"}},
    {"id": 9, "name": "Remaster Y", "platforms": [PS5], "game_type": {"type": "Remaster"}},
    {"id": 10, "name": "Missing Game", "platforms": [PS5], "game_type": {"type": "Main Game"}},
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


DETAILS |= {
    # PC dans platforms sans aucune date PC : écarté pour un jeu découvert.
    8: {"id": 8, "name": "Multi Game", "game_type": {"type": "Main Game"}, "platforms": [PS5, PC]},
    9: {
        "id": 9,
        "name": "Remaster Y",
        "game_type": {"type": "Remaster"},
        "platforms": [PS5],
        "parent_game": {"id": 90, "name": "Original Y", "platforms": [PC]},
    },
    # Jeu curé : même détection, simple avertissement.
    5: {"id": 5, "name": "Curated Game", "game_type": {"type": "Main Game"}, "platforms": [PC]},
}
for _row in DETAILS.values():
    _row["slug"] = _row["name"].lower().replace(" ", "-")


def igdb_games(request: httpx.Request) -> httpx.Response:
    body = request.content.decode()
    if "version_parent != null" in body:
        return httpx.Response(200, json=[])
    if "version_parent = null" in body:
        last = int(body.split("id > ")[1].split(";")[0])
        only = re.search(r"& id = \(([\d,]+)\)", body)
        wanted = None if only is None else {int(i) for i in only.group(1).split(",")}
        rows = [r for r in INDEX if r["id"] > last and (wanted is None or r["id"] in wanted)]
        return httpx.Response(200, json=rows[: discovery.PAGE_LIMIT])
    if "version_parent = (" in body:
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
    assert sorted(index) == list(range(1, 11))
    bodies = [
        c.request.content.decode()
        for c in api.calls
        if "version_parent = null" in str(c.request.content)
    ]
    assert [b.split("id > ")[1].split(";")[0] for b in bodies] == ["0", "3", "6", "9"]


def test_filtre_pc_types_curation_et_exclusions(igdb: IgdbClient, api: respx.MockRouter) -> None:
    products = [
        product("Astro Bot PS5"),
        product("Death Stranding 2 PS5"),
        product("Remaster X Switch", "switch"),
        product("Astro Bot Rescue PS5"),
        product("Curated Game PS5"),
        product("False Positive PS5"),
        product("Old Game", "switch"),
        product("Multi Game PS5"),
        product("Remaster Y PS5"),
        product("Jeu Introuvable PS5"),
    ]
    curated = [
        discovery.CuratedGame(5, "curated-game", "Curated Game", ("ps5",)),
        discovery.CuratedGame(10, "missing-game", "Jeu Introuvable", ("ps5",)),
    ]
    result = discovery.discover(igdb, products, Counter(), curated, {6}, NOW)
    assert [g.igdb_id for g in result.kept] == [1, 7]
    assert result.pc_listed == {8: "sur la fiche", 9: "via le jeu d'origine « Original Y »"}
    assert result.curated_pc_listed == {"curated-game": "sur la fiche"}
    [missing] = result.curated_missing
    assert (missing["slug"], missing["in_index"], missing["igdb_name"]) == (
        "missing-game",
        True,
        "Missing Game",
    )
    assert [(m["name"], m["identification"]) for m in missing["products_matching_title"]] == [
        ("Jeu Introuvable PS5", "unmatched")
    ]
    astro = result.kept[0]
    assert (astro.title, astro.name, list(astro.products)) == (
        "Astro Bot (Europe)",
        "Astro Bot",
        ["ps5"],
    )
    old = result.kept[1]
    # Sans localisation ni nom Leclerc : le nom alternatif commenté « French title ».
    assert (old.title, old.title_source) == ("Vieux Jeu", "titre_francais_igdb")
    assert old.notes == ["première sortie hors PS5/Switch/Switch 2/Xbox Series"]
    assert sorted(result.pc_released) == [2, 3]
    assert result.wrong_type == Counter({"DLC": 1})
    assert (result.curated, result.excluded) == ({5}, {6})

    report = discovery.summary(result, max_new=1)
    assert (report["max_new"], report["over_max_new"], report["pc_listed_without_date"]) == (
        1,
        1,
        2,
    )
    assert report["title_sources"] == {
        "localisation_eu": 1,
        "nom_leclerc": 0,
        "titre_francais_igdb": 1,
        "nom_igdb": 0,
    }
    assert report["unmatched"]["titre absent d'IGDB"]["count"] == 1
    assert (report["kept"], report["kept_editions"], report["weekly_fiches"]) == (2, 2, 2)
    assert report["kept_pc_announced"] == 0
    assert report["first_run_images"] == 4  # jaquette + 1 capture par jeu
    assert report["site_bytes_added"] == 2 * (
        discovery.COVER_BYTES + discovery.SCREEN_BYTES + discovery.PAGE_BYTES
    )


def test_noms_alternatifs_non_latins_ignores(igdb: IgdbClient, api: respx.MockRouter) -> None:
    index = discovery.fetch_index(igdb)
    assert index[7].names == ("Old Game", "Viejo Juego", "Vieux Jeu")
    assert not discovery._latin("スーパー R") and discovery._latin("Pokémon Écarlate")


# --- Mise à jour de games.discovered.yaml ----------------------------------------------------


def known(igdb_id: int, slug: str, platform: str = "ps5", missed: int = 0) -> DiscoveredGame:
    return DiscoveredGame(
        slug=slug,
        title=slug,
        title_source="nom_igdb",
        igdb_id=igdb_id,
        editions=[{"platform": platform}],
        first_seen=date(2026, 9, 1),
        last_seen=date(2026, 9, 1),
        missed_runs=missed,
    )


CURATED = [discovery.CuratedGame(5, "curated-game", "Curated Game", ("ps5",))]


def test_mise_a_jour_des_jeux_connus(igdb: IgdbClient, api: respx.MockRouter) -> None:
    previous = [
        known(1, "astro-bot", "switch", missed=2),  # en rayon : remis à zéro, éditions fusionnées
        known(7, "old-game", "switch", missed=1),  # absent : une absence de plus
        known(10, "missing-game", missed=3),  # absent pour la 4e fois : retiré
        known(3, "remaster-x", "switch"),  # sorti sur PC (jeu d'origine) : retiré
        known(5, "curated-game-2"),  # passé dans games.yaml : retiré
        known(6, "false-positive"),  # exclu : retiré
    ]
    out = discovery.refresh(igdb, FakeScraper(), CURATED, previous, {6}, NOW)
    assert out.complete
    assert out.added == []
    by_slug = {g.slug: g for g in out.games}
    assert sorted(by_slug) == ["astro-bot", "old-game"]
    astro = by_slug["astro-bot"]
    assert (astro.title, astro.last_seen, astro.missed_runs) == ("Astro Bot (Europe)", TODAY, 0)
    assert [e.platform for e in astro.editions] == ["ps5", "switch"]
    assert astro.first_seen == date(2026, 9, 1)
    assert (by_slug["old-game"].missed_runs, by_slug["old-game"].last_seen) == (
        2,
        date(2026, 9, 1),
    )
    assert out.removed == {
        "missing-game": "absent des rayons 4 runs consécutifs",
        "remaster-x": "sorti sur PC",
        "curated-game-2": "présent dans games.yaml",
        "false-positive": "discovery.exclude.yaml",
    }


def test_absence_non_comptee_si_une_source_est_bloquee(
    igdb: IgdbClient, api: respx.MockRouter
) -> None:
    previous = [known(10, "missing-game", missed=3)]
    out = discovery.refresh(igdb, FakeScraper({"pixmania": "HTTP 403"}), [], previous, set(), NOW)
    assert not out.complete
    assert {g.slug: g.missed_runs for g in out.games}["missing-game"] == 3


def test_nouveau_jeu_ajoute_dans_la_limite_de_max_new(
    igdb: IgdbClient, api: respx.MockRouter
) -> None:
    out = discovery.refresh(igdb, FakeScraper(), [], [], set(), NOW)
    [astro] = out.games
    assert out.added == ["astro-bot"]
    assert (astro.slug, astro.igdb_id, astro.first_seen, astro.title_source) == (
        "astro-bot",
        1,
        TODAY,
        "localisation_eu",
    )
    capped = discovery.refresh(igdb, FakeScraper(), [], [], set(), NOW, max_new=0)
    assert (capped.games, capped.capped) == ([], 1)


def test_slug_deja_pris_suffixe_par_l_igdb_id(igdb: IgdbClient, api: respx.MockRouter) -> None:
    curated = [discovery.CuratedGame(99, "astro-bot", "Astro Bot (curé)", ("ps5",))]
    out = discovery.refresh(igdb, FakeScraper(), curated, [], set(), NOW)
    assert out.added == ["astro-bot-1"]


def test_fichier_genere_relu_a_l_identique(tmp_path: Path) -> None:
    path = tmp_path / "games.discovered.yaml"
    games = [known(7, "old-game", missed=1), known(1, "astro-bot")]
    discovery.write_discovered_games(path, games)
    assert path.read_text(encoding="utf-8").startswith("# Généré par la découverte")
    assert discovery.load_discovered_games(path) == sorted(games, key=lambda g: g.slug)
    discovery.write_discovered_games(path, [])
    assert discovery.load_discovered_games(path) == []


# --- Cache d'identification ----------------------------------------------------------------


def game_bodies(api: respx.MockRouter) -> list[str]:
    return [c.request.content.decode() for c in api.calls if c.request.url.path.endswith("/games")]


def full_index(bodies: list[str]) -> bool:
    return any("version_parent = null & id > 0" in b for b in bodies)


def test_second_run_sans_reidentification_ni_index_complet(
    igdb: IgdbClient, api: respx.MockRouter
) -> None:
    cache = discovery.IdentifyCache()
    first = discovery.refresh(igdb, FakeScraper(), [], [], set(), NOW, cache=cache)
    assert full_index(game_bodies(api))
    leclerc = cache.products["https://www.e.leclerc/fp/astro-bot-1"]
    assert (leclerc["status"], leclerc["igdb_id"], leclerc["checked"]) == (
        "matched",
        1,
        TODAY.isoformat(),
    )
    assert cache.products["https://www.pixmania.com/fr/fr/saros-ps5-528089.html"]["status"] == (
        "unmatched"
    )
    api.reset()
    second = discovery.refresh(igdb, FakeScraper(), [], first.games, set(), NOW, cache=cache)
    bodies = game_bodies(api)
    assert not full_index(bodies)  # seules les fiches utiles sont lues
    assert any("& id = (1)" in b for b in bodies)
    assert [(g.slug, g.missed_runs, g.last_seen) for g in second.games] == [("astro-bot", 0, TODAY)]


def test_verdict_pc_definitif_non_redemande(igdb: IgdbClient, api: respx.MockRouter) -> None:
    cache = discovery.IdentifyCache()
    products = [product("Death Stranding 2 PS5")]
    discovery.discover(igdb, products, Counter(), [], set(), NOW, cache=cache, explain=False)
    assert cache.games[2]["verdict"] == "pc_released"
    api.reset()
    result = discovery.discover(
        igdb, products, Counter(), [], set(), NOW, cache=cache, explain=False
    )
    assert result.pc_released == [2]
    bodies = game_bodies(api)
    assert not any("where id = (" in b for b in bodies), bodies


def test_produit_sans_correspondance_reessaye_apres_14_jours(
    igdb: IgdbClient, api: respx.MockRouter
) -> None:
    cache = discovery.IdentifyCache()
    saros = product("Saros PS5")
    cache.store(saros, discovery.Identification("unmatched"), TODAY - timedelta(days=13))
    assert cache.lookup(saros, TODAY) == discovery.Identification("unmatched")
    cache.store(saros, discovery.Identification("unmatched"), TODAY - timedelta(days=14))
    assert cache.lookup(saros, TODAY) is None
    # Même URL vue pour une autre console : rattachement à refaire.
    moved = Product("leclerc", "switch", saros.name, saros.url, None)
    cache.store(saros, discovery.Identification("matched", 1, (), "Saros"), TODAY)
    assert cache.lookup(moved, TODAY) is None


def test_cache_relu_a_l_identique_et_produits_hors_rayon_oublies(tmp_path: Path) -> None:
    path = tmp_path / "discovery.cache.json"
    cache = discovery.IdentifyCache()
    cache.store(
        product("Astro Bot PS5"), discovery.Identification("matched", 1, (), "Astro"), TODAY
    )
    cache.store(product("Vieux PS5"), discovery.Identification("unmatched"), TODAY)
    cache.games[2] = {"verdict": "pc_released", "checked": TODAY.isoformat()}
    discovery.write_cache(path, cache)
    assert discovery.load_cache(path) == cache
    cache.keep_only({"https://x/Astro Bot PS5"})
    assert list(cache.products) == ["https://x/Astro Bot PS5"]
    path.write_text('{"schema": 99}', encoding="utf-8")
    assert discovery.load_cache(path) == discovery.IdentifyCache()


# --- Titre affiché et motifs d'échec --------------------------------------------------------


def test_titre_par_priorite() -> None:
    indexed = game(1, "Pokémon Shield", {"switch"}, "Pokémon Bouclier", "Pokemon Shield")
    indexed.french_names = ("Pokémon Bouclier",)
    found = discovery.Discovered(1, "Pokémon Shield", "Pokémon Shield", "Main Game", {})

    def ident(source: str, name: str) -> dict:
        return {
            product(f"{source} {name}", "switch", source): discovery.Identification(
                "matched", 1, (), name
            )
        }

    title = discovery.display_title
    assert title(found, "Pokémon Bouclier (EU)", indexed, {}) == (
        "Pokémon Bouclier (EU)",
        "localisation_eu",
    )
    # Nom alternatif rapproché d'un produit Leclerc, différent du nom principal.
    assert title(found, None, indexed, ident("leclerc", "Pokémon Bouclier")) == (
        "Pokémon Bouclier",
        "nom_leclerc",
    )
    # Variante d'écriture du nom principal (mêmes mots) ou produit Pixmania : ignorés.
    assert title(found, None, indexed, ident("leclerc", "Pokemon Shield"))[1] == (
        "titre_francais_igdb"
    )
    assert title(found, None, indexed, ident("pixmania", "Pokémon Bouclier"))[1] == (
        "titre_francais_igdb"
    )
    indexed.french_names = ()
    assert title(found, None, indexed, {}) == ("Pokémon Shield", "nom_igdb")


@pytest.mark.parametrize(
    ("name", "platform", "reason"),
    [
        ("Carte PlayStation Store 20 EUR PS5", "ps5", "carte prépayée ou abonnement"),
        ("Manette sans fil DualSense PS5", "ps5", "accessoire"),
        ("Console PS5 Slim + Astro Bot", "ps5", "pack ou console"),
        ("Astro Bot Edition Deluxe PS5", "ps5", "édition spéciale"),
        ("Astro Bot Microids PS5", "ps5", "mots en trop"),
        ("Astro Bot Switch", "switch", "jeu IGDB sans cette plateforme"),
        ("Astro Bott Galaxy PS5", "ps5", "variante d'écriture d'un titre IGDB"),
        ("Les Aventures de Toto PS5", "ps5", "titre FR probable non rattaché"),
        ("Zorglub PS5", "ps5", "titre absent d'IGDB"),
    ],
)
def test_motifs_des_produits_sans_correspondance(name: str, platform: str, reason: str) -> None:
    matcher = Matcher({1: game(1, "Astro Bot Galaxy", {"ps5"}, "Astro Bot")})
    assert matcher.identify(product(name, platform)).status == "unmatched"
    assert matcher.explain(product(name, platform))[0] == reason


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
