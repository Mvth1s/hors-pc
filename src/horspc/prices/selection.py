"""Règle de sélection : physique neuf, sinon boîte avec code, sinon démat officiel."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol


class PriceObservation(Protocol):
    """Ce que la sélection lit d'un relevé (manuel ou automatique)."""

    merchant: str
    format: str
    price: Decimal
    shipping: Decimal | None
    observed_at: date


RECENT_DAYS = 30
# Physique d'abord, puis boîte avec code, puis démat.
_FORMAT_RANK = {"physical": 0, "code_box": 1, "digital": 2}


@dataclass(frozen=True)
class Selection:
    observation: PriceObservation
    stale: bool


def comparison_price(obs: PriceObservation) -> Decimal:
    """Prix comparé : article + port si le port est connu, sinon article seul."""
    return obs.price + (obs.shipping or Decimal(0))


def latest_per_offer(observations: Iterable[PriceObservation]) -> list[PriceObservation]:
    """Ne garde que le relevé le plus récent par couple (marchand, format)."""
    latest: dict[tuple[str, str], PriceObservation] = {}
    for obs in observations:
        key = (obs.merchant, obs.format)
        if key not in latest or obs.observed_at > latest[key].observed_at:
            latest[key] = obs
    return list(latest.values())


def _rank(obs: PriceObservation) -> tuple[int, Decimal, int, str]:
    return (
        _FORMAT_RANK[obs.format],
        comparison_price(obs),
        -obs.observed_at.toordinal(),
        obs.merchant,
    )


def select_best(
    observations: Iterable[PriceObservation], today: date, max_age_days: int = RECENT_DAYS
) -> Selection | None:
    """Choisit l'offre à afficher parmi l'historique des relevés d'un jeu.

    Parmi les relevés de moins de `max_age_days` jours : physique neuf le moins cher, à
    défaut boîte avec code la moins chère, à défaut démat le moins cher. Sans relevé récent :
    le plus récent, marqué `stale`.
    """
    current = latest_per_offer(observations)
    if not current:
        return None
    recent = [o for o in current if (today - o.observed_at).days < max_age_days]
    if recent:
        return Selection(min(recent, key=_rank), stale=False)
    newest = max(o.observed_at for o in current)
    return Selection(min((o for o in current if o.observed_at == newest), key=_rank), stale=True)
