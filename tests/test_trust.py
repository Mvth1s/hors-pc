import pytest

from horspc.prices.trust import PLATFORMS, rejection


@pytest.mark.parametrize(
    ("platform", "seller", "condition", "fmt", "expected"),
    [
        ("leclerc", "E.Leclerc", "new", "physical", None),
        ("boulanger", "boulanger", "new", "physical", None),
        ("pixmania", "ShopVite", "new", "physical", None),  # marketplace réservée aux pros
        ("leclerc", "Vendeur Tiers SARL", "new", "physical", "vendeur tiers"),
        ("boulanger", "Electro Network", "new", "physical", "vendeur tiers"),
        ("pixmania", "ShopVite", "used", "physical", "non neuf"),
        ("pixmania", "ShopVite", "refurbished", "physical", "non neuf"),
        ("pixmania", None, "new", "physical", "vendeur non identifié"),
        ("pixmania", "Eneba", "new", "physical", "revendeur de clés"),
        ("pixmania", "Instant-Gaming", "new", "physical", "revendeur de clés"),
        ("leclerc", "E.Leclerc", "new", "digital", "stores officiels"),
        ("amazon", "Amazon", "new", "physical", "plateforme inconnue"),
    ],
)
def test_regle_plateforme_de_confiance(
    platform: str, seller: str | None, condition: str, fmt: str, expected: str | None
) -> None:
    reason = rejection(platform, seller, condition, fmt)
    if expected is None:
        assert reason is None
    else:
        assert reason is not None and expected in reason


def test_plateformes_etablies_dans_l_ue_et_livrant_la_france() -> None:
    for platform in PLATFORMS.values():
        assert platform.country == "FR" and platform.delivers_to_france
        assert platform.legal_entity


def test_plateforme_hors_ue_refusee(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    monkeypatch.setitem(PLATFORMS, "pixmania", replace(PLATFORMS["pixmania"], country="US"))
    assert "hors UE" in rejection("pixmania", "ShopVite", "new", "physical")
