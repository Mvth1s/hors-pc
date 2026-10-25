from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from horspc.__main__ import main
from horspc.models import Catalog
from horspc.stale import format_report, stale_games
from tests.conftest import TODAY

GAMES = [
    {"slug": "astro-bot", "title": "Astro Bot", "editions": [{"platform": "ps5"}]},
    {"slug": "saros", "title": "Saros", "editions": [{"platform": "ps5"}]},
    {"slug": "gran-turismo-7", "title": "Gran Turismo 7", "editions": [{"platform": "ps5"}]},
    {
        "slug": "mario-kart-world",
        "title": "Mario Kart World",
        "editions": [{"platform": "switch2"}],
    },
]
URLS = {
    "amazon": "https://www.amazon.fr/dp/{}",
    "fnac": "https://www.fnac.com/{}",
    "playstation-store": "https://store.playstation.com/fr-fr/product/{}",
}


def obs(slug: str, merchant: str, days_ago: int, price: str = "50") -> dict[str, Any]:
    return {
        "slug": slug,
        "price": price,
        "shipping": None,
        "format": "digital" if merchant == "playstation-store" else "physical",
        "condition": "new",
        "merchant": merchant,
        "sold_by_merchant": True,
        "url": URLS[merchant].format(f"{slug}-{days_ago}"),
        "observed_at": TODAY - timedelta(days=days_ago),
    }


OBSERVATIONS = [
    obs("astro-bot", "amazon", 90),
    obs("astro-bot", "amazon", 45, "49.99"),
    obs("astro-bot", "playstation-store", 60),
    obs("gran-turismo-7", "fnac", 5),  # récent : pas à relever
    obs("mario-kart-world", "fnac", 31),
]


def catalog() -> Catalog:
    return Catalog.model_validate(
        {"games": GAMES, "observations": OBSERVATIONS}, context={"today": TODAY}
    )


def test_selection_et_tri() -> None:
    games = stale_games(catalog(), TODAY)
    # Sans relevé d'abord, puis du plus ancien dernier relevé au plus récent.
    assert [g.game.slug for g in games] == ["saros", "astro-bot", "mario-kart-world"]
    astro = games[1]
    assert astro.last is not None and astro.last.observed_at == TODAY - timedelta(days=45)
    assert [(o.merchant, o.observed_at) for o in astro.per_merchant] == [
        ("amazon", TODAY - timedelta(days=45)),
        ("playstation-store", TODAY - timedelta(days=60)),
    ]


def test_jeu_avec_physique_recent_et_demat_ancien_non_liste() -> None:
    observations = [obs("astro-bot", "fnac", 3), obs("astro-bot", "playstation-store", 200)]
    cat = Catalog.model_validate(
        {"games": GAMES[:1], "observations": observations}, context={"today": TODAY}
    )
    assert stale_games(cat, TODAY) == []
    assert format_report([], TODAY) == "Tous les jeux ont un relevé de moins de 30 jours."


def test_rapport_lisible() -> None:
    report = format_report(stale_games(catalog(), TODAY), TODAY)
    assert report.startswith("3 jeu(x) à relever")
    assert "Saros  [saros]\n  aucun relevé" in report
    assert "dernier relevé : 11/09/2026 (il y a 45 j)" in report
    assert "https://www.amazon.fr/dp/astro-bot-45" in report
    assert "Amazon (physique)" in report
    assert "PlayStation Store (démat)" in report
    assert "49,99 €" in report
    assert "astro-bot-90" not in report  # remplacé par le relevé Amazon plus récent


def test_cli_stale(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "games.yaml").write_text(yaml.safe_dump(GAMES), encoding="utf-8")
    (tmp_path / "prices.yaml").write_text("[]", encoding="utf-8")
    code = main(
        [
            "stale",
            "--games",
            str(tmp_path / "games.yaml"),
            "--prices",
            str(tmp_path / "prices.yaml"),
        ]
    )
    assert code == 0
    assert "4 jeu(x) à relever" in capsys.readouterr().out
