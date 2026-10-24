from datetime import date, timedelta
from typing import Any

from horspc.models import GameEntry, PriceObservation
from horspc.prices import ManualPriceProvider
from horspc.prices.selection import select_best
from tests.conftest import TODAY

URLS = {
    "amazon": "https://www.amazon.fr/dp/X",
    "fnac": "https://www.fnac.com/a1",
    "leclerc": "https://www.e.leclerc/fp/x",
    "playstation-store": "https://store.playstation.com/fr-fr/product/X",
}


def obs(merchant: str = "amazon", price: str = "50", days_ago: int = 1, **kw: Any):
    data = {
        "slug": "astro-bot",
        "price": price,
        "shipping": None,
        "format": "digital" if merchant == "playstation-store" else "physical",
        "condition": "new",
        "merchant": merchant,
        "sold_by_merchant": True,
        "url": URLS[merchant],
        "observed_at": TODAY - timedelta(days=days_ago),
    } | kw
    return PriceObservation.model_validate(data, context={"today": TODAY})


def test_aucune_observation() -> None:
    assert select_best([], TODAY) is None


def test_physique_le_moins_cher() -> None:
    best = select_best([obs("amazon", "55"), obs("fnac", "49.99"), obs("leclerc", "52")], TODAY)
    assert best is not None
    assert best.observation.merchant == "fnac"
    assert not best.stale


def test_physique_prioritaire_sur_demat_moins_cher() -> None:
    best = select_best([obs("playstation-store", "29.99"), obs("amazon", "60")], TODAY)
    assert best is not None and best.observation.merchant == "amazon"


def test_demat_a_defaut_de_physique_recent() -> None:
    best = select_best([obs("amazon", "40", days_ago=45), obs("playstation-store", "59.99")], TODAY)
    assert best is not None
    assert best.observation.merchant == "playstation-store"
    assert not best.stale


def test_port_inclus_quand_connu() -> None:
    best = select_best([obs("amazon", "48", shipping="5"), obs("fnac", "50")], TODAY)
    assert best is not None and best.observation.merchant == "fnac"


def test_port_inconnu_compare_sur_prix_seul() -> None:
    best = select_best([obs("amazon", "48", shipping=None), obs("fnac", "49", shipping="0")], TODAY)
    assert best is not None and best.observation.merchant == "amazon"


def test_seuil_de_30_jours() -> None:
    best = select_best([obs("amazon", "40", days_ago=30), obs("fnac", "50", days_ago=29)], TODAY)
    assert best is not None and best.observation.merchant == "fnac"


def test_sans_relevé_recent_le_plus_recent_est_stale() -> None:
    best = select_best([obs("amazon", "40", days_ago=90), obs("fnac", "50", days_ago=40)], TODAY)
    assert best is not None
    assert best.observation.merchant == "fnac"
    assert best.stale


def test_dernier_relevé_par_marchand_remplace_l_ancien() -> None:
    best = select_best(
        [
            obs("amazon", "40", days_ago=10),
            obs("amazon", "65", days_ago=2),
            obs("fnac", "60", days_ago=3),
        ],
        TODAY,
    )
    assert best is not None
    assert best.observation.merchant == "fnac"


def test_egalite_departagee_par_date_puis_marchand() -> None:
    best = select_best([obs("fnac", "50", days_ago=5), obs("leclerc", "50", days_ago=2)], TODAY)
    assert best is not None and best.observation.merchant == "leclerc"
    best = select_best([obs("fnac", "50"), obs("amazon", "50")], TODAY)
    assert best is not None and best.observation.merchant == "amazon"


def test_provider_manuel_convertit_en_centimes() -> None:
    game = GameEntry(slug="astro-bot", title="Astro Bot", platforms=["ps5"])
    provider = ManualPriceProvider([obs("amazon", "53.30", shipping="2.99")])
    offer = provider.get_best_offer(game, TODAY)
    assert offer is not None
    assert (offer.price_cents, offer.shipping_cents) == (5330, 299)
    assert offer.merchant == "Amazon"
    assert offer.source == "manual"
    assert offer.observed_at == date(2026, 10, 25)


def test_provider_manuel_sans_relevé() -> None:
    game = GameEntry(slug="saros", title="Saros", platforms=["ps5"])
    assert ManualPriceProvider([obs()]).get_best_offer(game, TODAY) is None
