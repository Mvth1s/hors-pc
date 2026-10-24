"""Fournisseur de prix alimenté par data/prices.manual.yaml."""

from collections import defaultdict
from collections.abc import Iterable
from datetime import date

from horspc.models import MERCHANTS, GameEntry, PriceObservation, to_cents
from horspc.prices.base import Offer
from horspc.prices.selection import select_best


class ManualPriceProvider:
    """Sélectionne la meilleure offre parmi les relevés manuels d'un jeu."""

    name = "manual"

    def __init__(self, observations: Iterable[PriceObservation]) -> None:
        self._by_slug: dict[str, list[PriceObservation]] = defaultdict(list)
        for obs in observations:
            self._by_slug[obs.slug].append(obs)

    def get_best_offer(self, game: GameEntry, today: date) -> Offer | None:
        selection = select_best(self._by_slug.get(game.slug, []), today)
        if selection is None:
            return None
        obs = selection.observation
        return Offer(
            price_cents=to_cents(obs.price),
            shipping_cents=None if obs.shipping is None else to_cents(obs.shipping),
            format=obs.format,
            condition=obs.condition,
            merchant=MERCHANTS[obs.merchant].name,
            url=str(obs.url),
            observed_at=obs.observed_at,
            source=self.name,
            stale=selection.stale,
        )
