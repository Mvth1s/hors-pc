import json
import shutil
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import respx
import yaml
from pydantic import ValidationError

from horspc import build
from horspc.__main__ import main
from horspc.http import HttpClient
from horspc.igdb import API_URL, TOKEN_URL, IgdbClient, IgdbGame
from horspc.models import Catalog, DataFile
from horspc.prices import ManualPriceProvider, Offer
from tests.conftest import FIXTURES, NOW, load_fixture

GAMES: list[dict[str, Any]] = [
    {
        "slug": "astro-bot",
        "title": "Astro Bot",
        "platforms": ["ps5"],
        "igdb_id": 255340,
        "launch_price": 69.99,
        "launch_price_source": "https://example.org/astro-bot",
    },
    {"slug": "saros", "title": "Saros", "platforms": ["ps5"], "launch_price": None},
]

OBSERVATION: dict[str, Any] = {
    "slug": "astro-bot",
    "price": 53.30,
    "shipping": None,
    "format": "physical",
    "condition": "new",
    "merchant": "amazon",
    "sold_by_merchant": True,
    "url": "https://www.amazon.fr/dp/B0D1",
    "observed_at": date(2026, 7, 18),
}


def catalog(games: list[dict[str, Any]] = GAMES, observations: list[dict[str, Any]] | None = None):
    return Catalog.model_validate(
        {"games": games, "observations": [OBSERVATION] if observations is None else observations},
        context={"today": NOW.date()},
    )


def previous() -> dict:
    data = build.load_previous(FIXTURES / "data_previous.json")
    assert data is not None
    return {g.slug: g for g in data.games}


def make(cat: Catalog, igdb: dict[str, IgdbGame | None] | None, prev: dict | None = None):
    return build.build_data(cat, ManualPriceProvider(cat.observations), igdb, prev or {}, NOW)


ASTRO = IgdbGame(255340, "https://www.igdb.com/games/astro-bot", date(2024, 9, 6), "none")


# --- Assemblage ---------------------------------------------------------------


def test_schema_complet_et_offre() -> None:
    data = make(catalog(), {"astro-bot": ASTRO, "saros": None})
    dumped = data.model_dump(mode="json")
    assert dumped["schema_version"] == 4
    assert dumped["generated_at"] == "2026-10-26T06:17:42Z"
    assert dumped["currency"] == "EUR"
    astro = dumped["games"][0]
    assert astro["igdb_fetched_at"] == "2026-10-26T06:17:42Z"
    assert astro["release_date"] == "2024-09-06"
    assert astro["release_date_source"] == "igdb"
    assert astro["launch_price_cents"] == 6999
    assert astro["cover_url"] is None
    assert astro["cover_image_id"] is None
    assert astro["offer"] == {
        "price_cents": 5330,
        "shipping_cents": None,
        "discount_pct": 24,
        "format": "physical",
        "condition": "new",
        "merchant": "Amazon",
        "url": "https://www.amazon.fr/dp/B0D1",
        "observed_at": "2026-07-18",
        "source": "manual",
        "stale": True,
    }
    saros = dumped["games"][1]
    assert saros["offer"] is None
    assert saros["igdb_id"] is None
    assert saros["release_date"] is None
    assert saros["release_date_source"] is None


@pytest.mark.parametrize(
    ("price", "launch", "expected"),
    [(5330, 6999, 24), (6999, 6999, 0), (7500, 6999, -7), (5000, None, None), (3500, 7000, 50)],
)
def test_discount_pct(price: int, launch: int | None, expected: int | None) -> None:
    assert build.discount_pct(price, launch) == expected


def test_igdb_injoignable_reprend_les_valeurs_precedentes() -> None:
    data = make(catalog(), None, previous())
    astro, saros = data.games
    assert astro.igdb_id == 255340
    assert astro.release_date == date(2024, 9, 6)
    assert astro.pc_status == "announced"
    assert astro.igdb_fetched_at.isoformat() == "2026-10-19T06:17:38+00:00"
    # Les valeurs manuelles précédentes ne sont pas reprises sans override.
    assert saros.pc_status == "none"
    assert saros.pc_status_source == "igdb"
    assert astro.release_date_source == "igdb"


def test_pc_override_prioritaire_sur_igdb() -> None:
    games = [
        GAMES[0]
        | {
            "pc_override": {
                "status": "announced",
                "source_url": "https://example.org/annonce",
                "checked_at": "2026-10-20",
            }
        }
    ]
    data = make(catalog(games), {"astro-bot": ASTRO})
    assert data.games[0].pc_status == "announced"
    assert data.games[0].pc_status_source == "manual"


def test_sortie_invalide_refusee() -> None:
    class BrokenProvider:
        name = "manual"

        def get_best_offer(self, game, today) -> Offer:
            return Offer(
                0, None, "physical", "new", "Amazon", "https://x.fr", today, "manual", False
            )

    with pytest.raises(ValidationError):
        build.build_data(catalog(), BrokenProvider(), None, {}, NOW)


# --- Validation des entrées -----------------------------------------------------


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"merchant": "rakuten"}, "marketplace de vendeurs tiers"),
        ({"merchant": "ebay"}, "marketplace de vendeurs tiers"),
        ({"merchant": "instant-gaming"}, "revendeur de clés"),
        ({"url": "https://www.eneba.com/fr/x"}, "revendeur de clés"),
        ({"url": "https://www.fnac.com/a1"}, "ne correspond pas au marchand"),
        ({"sold_by_merchant": False}, "vendeur tiers"),
        ({"sold_by_merchant": "oui"}, "sold_by_merchant"),
        ({"format": "digital"}, "format digital"),
        ({"merchant": "inconnu"}, "marchand inconnu"),
        ({"observed_at": date(2026, 10, 27)}, "futur"),
        ({"slug": "zelda"}, "slugs inconnus"),
    ],
)
def test_observation_refusee(change: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        catalog(observations=[OBSERVATION | change])


def test_sold_by_merchant_obligatoire() -> None:
    obs = {k: v for k, v in OBSERVATION.items() if k != "sold_by_merchant"}
    with pytest.raises(ValidationError, match="sold_by_merchant"):
        catalog(observations=[obs])


def test_observation_en_double_refusee() -> None:
    with pytest.raises(ValidationError, match="en double"):
        catalog(observations=[OBSERVATION, OBSERVATION])


@pytest.mark.parametrize(
    ("games", "message"),
    [
        ([GAMES[0], GAMES[0]], "slugs en double"),
        ([GAMES[0] | {"launch_price_source": None}], "vont ensemble"),
        ([GAMES[1] | {"platforms": []}], "platforms"),
        ([GAMES[1] | {"platforms": ["ps5", "ps5"]}], "en double"),
        ([GAMES[1] | {"platforms": ["pc"]}], "platforms"),
        ([GAMES[1] | {"slug": "Saros"}], "slug"),
    ],
)
def test_jeu_refuse(games: list[dict[str, Any]], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        catalog(games, observations=[])


# --- Build complet et CLI ------------------------------------------------------


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    (tmp_path / "games.yaml").write_text(yaml.safe_dump(GAMES), encoding="utf-8")
    (tmp_path / "prices.yaml").write_text(
        yaml.safe_dump([OBSERVATION | {"observed_at": "2026-10-20"}]), encoding="utf-8"
    )
    shutil.copy(FIXTURES / "data_previous.json", tmp_path / "data.json")
    return tmp_path


def run(workdir: Path, igdb: IgdbClient | None) -> DataFile:
    return build.run(
        now=NOW,
        igdb=igdb,
        games_path=workdir / "games.yaml",
        prices_path=workdir / "prices.yaml",
        output_path=workdir / "data.json",
    )


def test_run_avec_igdb_mocke(workdir: Path, http: HttpClient) -> None:
    with respx.mock:
        respx.post(TOKEN_URL).respond(json=load_fixture("igdb_token.json"))
        respx.post(f"{API_URL}/platforms").respond(json=load_fixture("igdb_platforms.json"))
        respx.post(f"{API_URL}/release_date_regions").respond(
            json=load_fixture("igdb_release_regions.json")
        )
        respx.post(f"{API_URL}/release_date_statuses").respond(
            json=load_fixture("igdb_release_statuses.json")
        )
        respx.post(f"{API_URL}/games").respond(json=load_fixture("igdb_games.json"))
        run(workdir, IgdbClient("id", "secret", http))
    written = json.loads((workdir / "data.json").read_text(encoding="utf-8"))
    DataFile.model_validate(written)
    astro = written["games"][0]
    assert astro["pc_status"] == "none"
    assert astro["igdb_fetched_at"] == "2026-10-26T06:17:42Z"
    assert astro["offer"]["stale"] is False


def test_run_igdb_hors_service_garde_l_ancien_fetched_at(workdir: Path, http: HttpClient) -> None:
    with respx.mock:
        respx.post(TOKEN_URL).respond(503)
        run(workdir, IgdbClient("id", "secret", http))
    written = json.loads((workdir / "data.json").read_text(encoding="utf-8"))
    assert written["generated_at"] == "2026-10-26T06:17:42Z"
    assert written["games"][0]["igdb_fetched_at"] == "2026-10-19T06:17:38Z"


def test_cli_validation_refusee_code_non_nul(workdir: Path, capsys) -> None:
    (workdir / "prices.yaml").write_text(
        yaml.safe_dump([OBSERVATION | {"merchant": "rakuten"}]), encoding="utf-8"
    )
    before = (workdir / "data.json").read_bytes()
    code = main(
        [
            "build",
            "--offline",
            "--games",
            str(workdir / "games.yaml"),
            "--prices",
            str(workdir / "prices.yaml"),
            "--output",
            str(workdir / "data.json"),
        ]
    )
    assert code == 1
    assert "rakuten" in capsys.readouterr().err
    assert (workdir / "data.json").read_bytes() == before


def test_cli_identifiants_manquants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("horspc.__main__.load_dotenv", lambda path: None)
    monkeypatch.delenv("IGDB_CLIENT_ID", raising=False)
    monkeypatch.delenv("IGDB_CLIENT_SECRET", raising=False)
    assert main(["build"]) == 2


def test_offline_reprend_generated_at_precedent(workdir: Path) -> None:
    data = run(workdir, None)
    assert data.model_dump(mode="json")["generated_at"] == "2026-10-19T06:17:40Z"
    # offer.stale reste calculé avec l'heure courante (relevé du 20/10, 6 jours avant NOW).
    assert data.games[0].offer is not None
    assert data.games[0].offer.stale is False


def test_offline_relevé_ancien_devient_stale_malgre_generated_at(workdir: Path) -> None:
    (workdir / "prices.yaml").write_text(
        yaml.safe_dump([OBSERVATION | {"observed_at": "2026-09-20"}]), encoding="utf-8"
    )
    data = run(workdir, None)
    # 2026-09-20 est récent par rapport au generated_at repris (19/10), mais pas à NOW (26/10).
    assert data.games[0].offer is not None
    assert data.games[0].offer.stale is True


def test_offline_sans_data_json_precedent_utilise_l_heure_courante(workdir: Path) -> None:
    (workdir / "data.json").unlink()
    data = run(workdir, None)
    assert data.model_dump(mode="json")["generated_at"] == "2026-10-26T06:17:42Z"


def test_release_date_override_prioritaire() -> None:
    games = [
        GAMES[0]
        | {
            "release_date_override": {
                "date": "2024-09-05",
                "source_url": "https://example.org/sortie",
            }
        }
    ]
    data = make(catalog(games), {"astro-bot": ASTRO})
    assert data.games[0].release_date == date(2024, 9, 5)
    assert data.games[0].release_date_source == "manual"


def test_release_date_override_peut_etre_future() -> None:
    override = {"date": "2027-03-01", "source_url": "https://example.org/sortie"}
    games = [GAMES[1] | {"release_date_override": override}]
    data = make(catalog(games, observations=[]), {"saros": None})
    assert data.games[0].release_date == date(2027, 3, 1)


def test_release_date_source_coherente() -> None:
    data = make(catalog(), {"astro-bot": ASTRO, "saros": None})
    raw = data.model_dump(mode="json")
    raw["games"][0]["release_date_source"] = None
    with pytest.raises(ValidationError, match="release_date_source"):
        DataFile.model_validate(raw)


def test_release_date_manuelle_precedente_non_reprise() -> None:
    prev = previous()
    prev["saros"] = prev["saros"].model_copy(update={"release_date_source": "manual"})
    data = make(catalog(), None, prev)
    saros = data.games[1]
    assert (saros.release_date, saros.release_date_source) == (None, None)


@pytest.mark.parametrize("version", [3, None, 5])
def test_data_json_precedent_hors_schema_courant_refuse(workdir: Path, version: int | None) -> None:
    raw = json.loads((workdir / "data.json").read_text(encoding="utf-8"))
    if version is None:
        del raw["schema_version"]
    else:
        raw["schema_version"] = version
    (workdir / "data.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(build.PreviousDataError, match=f"schema_version {version!r}, 4 attendu"):
        run(workdir, None)


def test_cli_data_json_ancien_schema_code_non_nul(workdir: Path, capsys) -> None:
    raw = json.loads((workdir / "data.json").read_text(encoding="utf-8"))
    raw["schema_version"] = 2
    (workdir / "data.json").write_text(json.dumps(raw), encoding="utf-8")
    before = (workdir / "data.json").read_bytes()
    code = main(
        [
            "build",
            "--offline",
            "--games",
            str(workdir / "games.yaml"),
            "--prices",
            str(workdir / "prices.yaml"),
            "--output",
            str(workdir / "data.json"),
        ]
    )
    assert code == 1
    assert "schema_version 2, 4 attendu" in capsys.readouterr().err
    assert (workdir / "data.json").read_bytes() == before


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{pas du json", "n'est pas un JSON valide"),
        (
            '{"schema_version": 4, "generated_at": "hier", "currency": "EUR", "games": []}',
            "refusé par le modèle",
        ),
    ],
)
def test_data_json_precedent_mal_forme_refuse(workdir: Path, content: str, message: str) -> None:
    (workdir / "data.json").write_text(content, encoding="utf-8")
    with pytest.raises(build.PreviousDataError, match=message):
        run(workdir, None)
    assert (workdir / "data.json").read_text(encoding="utf-8") == content


@pytest.mark.parametrize("merchant", ["carrefour", "intermarche", "superu", "lidl"])
def test_enseignes_sans_prix_national_refusees(merchant: str) -> None:
    with pytest.raises(ValidationError, match="marchand inconnu"):
        catalog(observations=[OBSERVATION | {"merchant": merchant}])


def test_description_fr_prioritaire_sur_le_resume_igdb() -> None:
    from horspc.igdb import IgdbGame

    info = IgdbGame(
        255340, None, None, "none", summary="English summary", developers=("Team Asobi",)
    )
    games = [GAMES[0] | {"description_fr": "Résumé français."}]
    data = make(catalog(games, observations=[]), {"astro-bot": info})
    astro = data.games[0]
    assert (astro.summary, astro.summary_source) == ("Résumé français.", "manual")
    assert astro.developers == ["Team Asobi"]
    data = make(catalog(observations=[]), {"astro-bot": info, "saros": None})
    assert (data.games[0].summary, data.games[0].summary_source) == ("English summary", "igdb")
