"""Fournisseurs de prix alimentés par les relevés : manuels (YAML) et automatiques (JSONL)."""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from horspc.models import (
    MERCHANTS,
    GameEntry,
    Platform,
    PriceObservation,
    ScrapedObservation,
    to_cents,
)
from horspc.prices.base import Offer
from horspc.prices.selection import select_best
from horspc.prices.trust import PLATFORMS, normalize


@dataclass(frozen=True)
class Record:
    """Vue commune d'un relevé, manuel ou automatique, pour la règle de sélection."""

    slug: str
    platform: Platform
    merchant: str
    merchant_name: str
    seller: str | None
    price: Decimal
    shipping: Decimal | None
    format: str
    condition: str
    url: str
    observed_at: date
    source: str


def from_manual(obs: PriceObservation, platform: Platform) -> Record:
    return Record(
        obs.slug,
        platform,
        obs.merchant,
        MERCHANTS[obs.merchant].name,
        None,
        obs.price,
        obs.shipping,
        obs.format,
        obs.condition,
        str(obs.url),
        obs.observed_at,
        "manual",
    )


def from_scraped(obs: ScrapedObservation) -> Record:
    name = PLATFORMS[obs.merchant].name
    seller = None if normalize(obs.seller) in {normalize(name), obs.merchant} else obs.seller
    return Record(
        obs.slug,
        obs.platform,
        obs.merchant,
        name,
        seller,
        obs.price,
        obs.shipping,
        obs.format,
        obs.condition,
        str(obs.url),
        obs.observed_at,
        "scraped",
    )


class ManualPriceProvider:
    """Sélectionne la meilleure offre parmi les relevés d'une édition d'un jeu.

    Les relevés manuels restent prioritaires : un relevé automatique de la même édition, du même
    marchand et de la même date qu'un relevé manuel est ignoré. `single_editions` donne
    l'édition d'un relevé manuel sans `platform` (jeu à une seule édition).
    """

    name = "manual"

    def __init__(
        self,
        observations: Iterable[PriceObservation],
        scraped: Iterable[ScrapedObservation] = (),
        single_editions: Mapping[str, Platform] | None = None,
    ) -> None:
        single = single_editions or {}
        manual = [from_manual(o, o.platform or single[o.slug]) for o in observations]
        manual_keys = {(r.slug, r.platform, r.merchant, r.observed_at) for r in manual}
        auto = [
            r
            for r in map(from_scraped, scraped)
            if (r.slug, r.platform, r.merchant, r.observed_at) not in manual_keys
        ]
        self._by_edition: dict[tuple[str, str], list[Record]] = defaultdict(list)
        for record in [*manual, *auto]:
            self._by_edition[(record.slug, record.platform)].append(record)

    def get_offer(self, game: GameEntry, platform: Platform, today: date) -> Offer | None:
        selection = select_best(self._by_edition.get((game.slug, platform), []), today)
        if selection is None:
            return None
        rec = selection.observation
        return Offer(
            platform=rec.platform,
            price_cents=to_cents(rec.price),
            shipping_cents=None if rec.shipping is None else to_cents(rec.shipping),
            format=rec.format,
            condition=rec.condition,
            merchant=rec.merchant_name,
            url=rec.url,
            observed_at=rec.observed_at,
            source=rec.source,
            stale=selection.stale,
            seller=rec.seller,
        )
