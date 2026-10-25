"""Interface commune des fournisseurs de prix."""

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from horspc.models import Condition, GameEntry, OfferFormat, Platform


@dataclass(frozen=True)
class Offer:
    """Meilleure offre retenue pour une édition d'un jeu, montants en centimes."""

    platform: Platform
    price_cents: int
    shipping_cents: int | None
    format: OfferFormat
    condition: Condition
    merchant: str
    url: str
    observed_at: date
    source: str
    stale: bool
    seller: str | None = None  # vendeur de la place de marché quand ce n'est pas l'enseigne


class PriceProvider(Protocol):
    """Source de prix : renvoie la meilleure offre d'une édition d'un jeu, ou None."""

    name: str

    def get_offer(self, game: GameEntry, platform: Platform, today: date) -> Offer | None: ...
