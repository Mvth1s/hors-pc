"""Fournisseurs de prix et règle de sélection de la meilleure offre."""

from horspc.prices.base import Offer, PriceProvider
from horspc.prices.manual import ManualPriceProvider

__all__ = ["ManualPriceProvider", "Offer", "PriceProvider"]
