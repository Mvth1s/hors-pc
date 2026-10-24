import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

from horspc.build import GAMES_PATH, PRICES_PATH
from horspc.models import Catalog
from horspc.price_add import price_add
from tests.conftest import TODAY


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    games = tmp_path / "games.yaml"
    prices = tmp_path / "prices.manual.yaml"
    shutil.copy(GAMES_PATH, games)
    shutil.copy(PRICES_PATH, prices)  # vrai fichier : en-tête commenté puis `[]`
    return games, prices


def run(files: tuple[Path, Path], answers: list[str]) -> tuple[int, list[str]]:
    replies = iter(answers)
    said: list[str] = []
    code = price_add(*files, TODAY, ask=lambda _: next(replies), say=said.append)
    return code, said


def load(files: tuple[Path, Path]) -> Catalog:
    games, prices = files
    return Catalog.model_validate(
        {
            "games": yaml.safe_load(games.read_text(encoding="utf-8")),
            "observations": yaml.safe_load(prices.read_text(encoding="utf-8")),
        },
        context={"today": TODAY},
    )


AMAZON = ["2", "amazon", "53,30", "", "o", "https://www.amazon.fr/dp/B0GHOST"]


def test_ajout_physique_preserve_les_commentaires(files: tuple[Path, Path]) -> None:
    header = files[1].read_text(encoding="utf-8").split("[]")[0]
    code, said = run(files, AMAZON)
    assert code == 0, said
    text = files[1].read_text(encoding="utf-8")
    assert text.startswith(header)  # commentaires d'en-tête intacts
    assert "[]" not in text
    assert "- slug: ghost-of-yotei\n  price: 53.30\n  shipping: null\n" in text
    (obs,) = load(files).observations
    assert obs.slug == "ghost-of-yotei"
    assert str(obs.price) == "53.3"
    assert obs.shipping is None
    assert obs.format == "physical"
    assert obs.observed_at == TODAY
    assert any("Format : physique" in line for line in said)


def test_ajouts_successifs_gardent_l_ordre(files: tuple[Path, Path]) -> None:
    run(files, AMAZON)
    with files[1].open("a", encoding="utf-8") as f:
        f.write("# relevé suivant à faire en magasin\n")
    code, _ = run(
        files,
        [
            "astro-bot",
            "playstation-store",
            "59.99",
            "0",
            "oui",
            "https://store.playstation.com/fr-fr/product/X",
        ],
    )
    assert code == 0
    text = files[1].read_text(encoding="utf-8")
    assert "# relevé suivant à faire en magasin" in text
    observations = load(files).observations
    assert [(o.slug, o.merchant, o.format) for o in observations] == [
        ("ghost-of-yotei", "amazon", "physical"),
        ("astro-bot", "playstation-store", "digital"),
    ]
    assert str(observations[1].shipping) == "0.0"
    assert "  shipping: 0.00\n" in text


def test_refus_si_vendeur_tiers(files: tuple[Path, Path]) -> None:
    before = files[1].read_bytes()
    code, said = run(files, ["2", "amazon", "53,30", "", "n"])
    assert code == 1
    assert "vendues par l'enseigne elle-même" in said[-1]
    assert files[1].read_bytes() == before


def test_validation_echouee_n_ecrit_rien(files: tuple[Path, Path]) -> None:
    before = files[1].read_bytes()
    code, said = run(files, ["2", "amazon", "53,30", "", "o", "https://www.fnac.com/a1"])
    assert code == 1
    assert "ne correspond pas au marchand" in said[-1]
    assert files[1].read_bytes() == before


def test_marchand_hors_liste_blanche(files: tuple[Path, Path]) -> None:
    before = files[1].read_bytes()
    code, said = run(files, ["2", "rakuten", "ebay", "99"])
    assert code == 1
    assert "choix invalide" in said[-1]
    assert files[1].read_bytes() == before


def test_montant_invalide_redemande(files: tuple[Path, Path]) -> None:
    code, _ = run(files, ["2", "fnac", "abc", "49,90 €", "4,99", "o", "https://www.fnac.com/a1"])
    assert code == 0
    (obs,) = load(files).observations
    assert (str(obs.price), str(obs.shipping)) == ("49.9", "4.99")
    assert obs.observed_at == date(2026, 10, 26)
