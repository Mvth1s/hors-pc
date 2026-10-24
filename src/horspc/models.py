"""Modèles Pydantic v2 : entrées YAML (jeux, observations de prix) et sortie data.json."""

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    PlainSerializer,
    PositiveInt,
    StrictBool,
    ValidationInfo,
    field_validator,
    model_validator,
)

Platform = Literal["ps5", "switch", "switch2"]
PcStatus = Literal["none", "announced", "released"]
PcStatusSource = Literal["igdb", "manual"]
OfferFormat = Literal["physical", "digital"]
Condition = Literal["new"]

Slug = Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
Euros = Annotated[Decimal, Field(gt=0, max_digits=7, decimal_places=2)]
EurosOrZero = Annotated[Decimal, Field(ge=0, max_digits=7, decimal_places=2)]
Cents = Annotated[int, Field(strict=True, ge=0)]
PositiveCents = Annotated[int, Field(strict=True, gt=0)]


def _require_utc(value: datetime) -> datetime:
    if value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError("la date doit être en UTC")
    return value.astimezone(UTC).replace(microsecond=0)


UtcDatetime = Annotated[
    datetime,
    AfterValidator(_require_utc),
    PlainSerializer(lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ"), return_type=str),
]


def to_cents(amount: Decimal) -> int:
    """Convertit un montant en euros (2 décimales max) en centimes."""
    return int((amount * 100).to_integral_value())


def _today(info: ValidationInfo) -> date:
    """Date de référence : `context={"today": ...}` si fourni, sinon aujourd'hui."""
    context = info.context or {}
    return context.get("today") or date.today()


def _host_matches(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


# --- Marchands ----------------------------------------------------------------


@dataclass(frozen=True)
class Merchant:
    """Enseigne autorisée : nom affiché, domaines acceptés et formats vendus."""

    name: str
    domains: tuple[str, ...]
    formats: frozenset[str]


_PHYSICAL = frozenset({"physical"})
_DIGITAL = frozenset({"digital"})

MERCHANTS: dict[str, Merchant] = {
    "amazon": Merchant("Amazon", ("amazon.fr",), _PHYSICAL),
    "fnac": Merchant("Fnac", ("fnac.com",), _PHYSICAL),
    "cdiscount": Merchant("Cdiscount", ("cdiscount.com",), _PHYSICAL),
    "leclerc": Merchant("E.Leclerc", ("e.leclerc",), _PHYSICAL),
    "auchan": Merchant("Auchan", ("auchan.fr",), _PHYSICAL),
    "carrefour": Merchant("Carrefour", ("carrefour.fr",), _PHYSICAL),
    "cultura": Merchant("Cultura", ("cultura.com",), _PHYSICAL),
    "micromania": Merchant("Micromania", ("micromania.fr",), _PHYSICAL),
    "boulanger": Merchant("Boulanger", ("boulanger.com",), _PHYSICAL),
    "playstation-store": Merchant("PlayStation Store", ("store.playstation.com",), _DIGITAL),
    "nintendo-eshop": Merchant("Nintendo eShop", ("nintendo.com", "nintendo.fr"), _DIGITAL),
}

_MARKETPLACE = "marketplace de vendeurs tiers"
_KEY_RESELLER = "revendeur de clés"

# id -> (raison du refus, domaines)
BLOCKED_MERCHANTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "rakuten": (_MARKETPLACE, ("rakuten.com", "rakuten.fr")),
    "ebay": (_MARKETPLACE, ("ebay.fr", "ebay.com")),
    "instant-gaming": (_KEY_RESELLER, ("instant-gaming.com",)),
    "eneba": (_KEY_RESELLER, ("eneba.com",)),
    "g2a": (_KEY_RESELLER, ("g2a.com",)),
    "kinguin": (_KEY_RESELLER, ("kinguin.net",)),
    "cdkeys": (_KEY_RESELLER, ("cdkeys.com", "loaded.com")),
    "gamivo": (_KEY_RESELLER, ("gamivo.com",)),
    "k4g": (_KEY_RESELLER, ("k4g.com",)),
    "hrkgame": (_KEY_RESELLER, ("hrkgame.com",)),
    "goclecd": (_KEY_RESELLER, ("goclecd.fr",)),
    "allkeyshop": (_KEY_RESELLER, ("allkeyshop.com",)),
}


def _blocked_reason(merchant_id: str | None, host: str | None) -> str | None:
    for blocked_id, (reason, domains) in BLOCKED_MERCHANTS.items():
        if merchant_id == blocked_id or (host and _host_matches(host, domains)):
            return f"{blocked_id} : {reason}, observation refusée"
    return None


# --- Entrées YAML -------------------------------------------------------------


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PcOverride(_Input):
    """Statut PC saisi à la main, prioritaire sur IGDB."""

    status: PcStatus
    source_url: HttpUrl
    checked_at: date

    @field_validator("checked_at")
    @classmethod
    def _not_in_future(cls, value: date, info: ValidationInfo) -> date:
        if value > _today(info):
            raise ValueError("checked_at est dans le futur")
        return value


class GameEntry(_Input):
    """Une entrée de games.yaml : une référence achetable."""

    slug: Slug
    title: str = Field(min_length=1)
    platforms: list[Platform] = Field(min_length=1)
    igdb_id: PositiveInt | None = None
    launch_price: Euros | None = None
    launch_price_source: HttpUrl | None = None
    pc_override: PcOverride | None = None

    @field_validator("platforms")
    @classmethod
    def _unique_platforms(cls, value: list[Platform]) -> list[Platform]:
        if len(set(value)) != len(value):
            raise ValueError("plateforme en double")
        return value

    @model_validator(mode="after")
    def _price_has_source(self) -> "GameEntry":
        if (self.launch_price is None) != (self.launch_price_source is None):
            raise ValueError("launch_price et launch_price_source vont ensemble")
        return self


class PriceObservation(_Input):
    """Un relevé de prix manuel dans prices.manual.yaml."""

    slug: Slug
    price: Euros
    shipping: EurosOrZero | None
    format: OfferFormat
    condition: Condition
    merchant: str
    sold_by_merchant: StrictBool
    url: HttpUrl
    observed_at: date

    @field_validator("merchant")
    @classmethod
    def _known_merchant(cls, value: str) -> str:
        if reason := _blocked_reason(value, None):
            raise ValueError(reason)
        if value not in MERCHANTS:
            raise ValueError(f"marchand inconnu, valeurs admises : {', '.join(sorted(MERCHANTS))}")
        return value

    @field_validator("sold_by_merchant")
    @classmethod
    def _sold_by_merchant(cls, value: bool) -> bool:
        if not value:
            raise ValueError("offre d'un vendeur tiers de la marketplace : observation refusée")
        return value

    @field_validator("observed_at")
    @classmethod
    def _not_in_future(cls, value: date, info: ValidationInfo) -> date:
        if value > _today(info):
            raise ValueError("observed_at est dans le futur")
        return value

    @model_validator(mode="after")
    def _url_matches_merchant(self) -> "PriceObservation":
        host = (urlsplit(str(self.url)).hostname or "").lower()
        if reason := _blocked_reason(None, host):
            raise ValueError(reason)
        merchant = MERCHANTS[self.merchant]
        if not _host_matches(host, merchant.domains):
            raise ValueError(f"l'URL ({host}) ne correspond pas au marchand {self.merchant}")
        if self.format not in merchant.formats:
            raise ValueError(f"{self.merchant} n'est pas admis pour le format {self.format}")
        return self


class Catalog(_Input):
    """Jeux et observations, avec les contrôles croisés entre les deux fichiers."""

    games: list[GameEntry]
    observations: list[PriceObservation]

    @model_validator(mode="after")
    def _consistency(self) -> "Catalog":
        dup_slugs = [s for s, n in Counter(g.slug for g in self.games).items() if n > 1]
        if dup_slugs:
            raise ValueError(f"slugs en double dans games.yaml : {', '.join(dup_slugs)}")
        known = {g.slug for g in self.games}
        unknown = sorted({o.slug for o in self.observations} - known)
        if unknown:
            raise ValueError(f"slugs inconnus dans prices.manual.yaml : {', '.join(unknown)}")
        if len(set(self.observations)) != len(self.observations):
            raise ValueError("observation en double dans prices.manual.yaml")
        return self


# --- Sortie data.json ---------------------------------------------------------


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OfferOut(_Output):
    price_cents: PositiveCents
    shipping_cents: Cents | None
    discount_pct: int | None
    format: OfferFormat
    condition: Condition
    merchant: str = Field(min_length=1)
    url: HttpUrl
    observed_at: date
    source: Literal["manual"]
    stale: bool


class GameOut(_Output):
    slug: Slug
    title: str
    platforms: list[Platform] = Field(min_length=1)
    release_date: date | None
    launch_price_cents: PositiveCents | None
    igdb_id: PositiveInt | None
    igdb_url: HttpUrl | None
    igdb_fetched_at: UtcDatetime | None
    cover_url: None = None
    pc_status: PcStatus
    pc_status_source: PcStatusSource
    offer: OfferOut | None


class DataFile(_Output):
    """Contenu de site/data.json (schéma v1)."""

    schema_version: Literal[1] = 1
    generated_at: UtcDatetime
    currency: Literal["EUR"] = "EUR"
    games: list[GameOut]

    @model_validator(mode="after")
    def _unique_slugs(self) -> "DataFile":
        if len({g.slug for g in self.games}) != len(self.games):
            raise ValueError("slug en double dans data.json")
        return self
