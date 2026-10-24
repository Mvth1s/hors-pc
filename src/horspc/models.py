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

Platform = Literal["ps5", "switch", "switch2", "xbox_series"]
PcStatus = Literal["none", "announced", "released"]
PcStatusSource = Literal["igdb", "manual"]
SummarySource = Literal["igdb", "manual"]
ReleaseDateSource = Literal["igdb", "manual"]
OfferFormat = Literal["physical", "digital"]
Condition = Literal["new"]

MerchantCategory = Literal[
    "grande_distribution", "specialiste", "culture_tech", "ecommerce", "store_officiel"
]
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
    """Enseigne autorisée : nom affiché, domaines acceptés, formats vendus et catégorie.

    N'y figurent que des enseignes dont le prix en ligne est national et livrable à domicile
    partout en France (pas de prix de drive ou de magasin).
    """

    name: str
    domains: tuple[str, ...]
    formats: frozenset[str]
    category: MerchantCategory


_PHYSICAL = frozenset({"physical"})
_DIGITAL = frozenset({"digital"})

MERCHANTS: dict[str, Merchant] = {
    "amazon": Merchant("Amazon", ("amazon.fr",), _PHYSICAL, "ecommerce"),
    "cdiscount": Merchant("Cdiscount", ("cdiscount.com",), _PHYSICAL, "ecommerce"),
    "fnac": Merchant("Fnac", ("fnac.com",), _PHYSICAL, "culture_tech"),
    "darty": Merchant("Darty", ("darty.com",), _PHYSICAL, "culture_tech"),
    "boulanger": Merchant("Boulanger", ("boulanger.com",), _PHYSICAL, "culture_tech"),
    "cultura": Merchant("Cultura", ("cultura.com",), _PHYSICAL, "culture_tech"),
    "micromania": Merchant("Micromania", ("micromania.fr",), _PHYSICAL, "specialiste"),
    "leclerc": Merchant("E.Leclerc", ("e.leclerc",), _PHYSICAL, "grande_distribution"),
    "auchan": Merchant("Auchan", ("auchan.fr",), _PHYSICAL, "grande_distribution"),
    # Écartés : Intermarché et Super U (prix du magasin choisi, livraison selon le magasin),
    # Carrefour (aucune fiche de jeu vendue par Carrefour trouvée), Lidl (pas de jeux en ligne).
    "playstation-store": Merchant(
        "PlayStation Store", ("store.playstation.com",), _DIGITAL, "store_officiel"
    ),
    "nintendo-eshop": Merchant(
        "Nintendo eShop", ("nintendo.com", "nintendo.fr"), _DIGITAL, "store_officiel"
    ),
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


class ReleaseDateOverride(_Input):
    """Date de sortie saisie à la main, prioritaire sur IGDB (peut être future)."""

    date: date
    source_url: HttpUrl


class GameEntry(_Input):
    """Une entrée de games.yaml : une référence achetable."""

    slug: Slug
    title: str = Field(min_length=1)
    platforms: list[Platform] = Field(min_length=1)
    igdb_id: PositiveInt | None = None
    launch_price: Euros | None = None
    launch_price_source: HttpUrl | None = None
    pc_override: PcOverride | None = None
    release_date_override: ReleaseDateOverride | None = None
    description_fr: str | None = Field(default=None, min_length=1)

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


ScrapedSource = Literal["leclerc", "boulanger", "pixmania"]


class ScrapedObservation(_Input):
    """Un relevé automatique (une ligne de data/prices.scraped.jsonl)."""

    slug: Slug
    merchant: ScrapedSource
    seller: str = Field(min_length=1)
    price: Euros
    shipping: EurosOrZero | None
    format: Literal["physical"]
    condition: Condition
    url: HttpUrl
    ean: str | None = Field(default=None, pattern=r"^\d{8,14}$")
    observed_at: date

    @model_validator(mode="after")
    def _trusted(self) -> "ScrapedObservation":
        from horspc.prices.trust import PLATFORMS, rejection  # import tardif (cycle)

        host = (urlsplit(str(self.url)).hostname or "").lower()
        if not _host_matches(host, PLATFORMS[self.merchant].domains):
            raise ValueError(f"l'URL ({host}) ne correspond pas à la source {self.merchant}")
        reason = rejection(self.merchant, self.seller, self.condition, self.format)
        if reason:
            raise ValueError(reason)
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
    seller: str | None = Field(min_length=1)  # vendeur pro de la place de marché, sinon null
    url: HttpUrl
    observed_at: date
    source: Literal["manual", "scraped"]
    stale: bool


class ScreenshotOut(_Output):
    image_id: str = Field(min_length=1)
    url: str = Field(pattern=r"^screens/[a-z0-9-]+/[A-Za-z0-9_-]+\.webp$")


class GameOut(_Output):
    slug: Slug
    title: str
    platforms: list[Platform] = Field(min_length=1)
    release_date: date | None
    release_date_source: ReleaseDateSource | None
    launch_price_cents: PositiveCents | None
    igdb_id: PositiveInt | None
    igdb_url: HttpUrl | None
    igdb_fetched_at: UtcDatetime | None
    cover_url: str | None = Field(pattern=r"^covers/[a-z0-9]+(?:-[a-z0-9]+)*\.webp$")
    cover_image_id: str | None = Field(min_length=1)
    pc_status: PcStatus
    pc_status_source: PcStatusSource
    developers: list[str]
    publishers: list[str]
    summary: str | None
    summary_source: SummarySource | None
    screenshots: list[ScreenshotOut] = Field(max_length=4)
    trailer_youtube_id: str | None = Field(pattern=r"^[A-Za-z0-9_-]{11}$")
    offer: OfferOut | None

    @model_validator(mode="after")
    def _release_source(self) -> "GameOut":
        if (self.release_date is None) != (self.release_date_source is None):
            raise ValueError("release_date_source est null si et seulement si release_date l'est")
        return self

    @model_validator(mode="after")
    def _summary_and_screens(self) -> "GameOut":
        if (self.summary is None) != (self.summary_source is None):
            raise ValueError("summary_source est null si et seulement si summary l'est")
        for shot in self.screenshots:
            if shot.url != f"screens/{self.slug}/{shot.image_id}.webp":
                raise ValueError(f"url de capture inattendue : {shot.url}")
        return self

    @model_validator(mode="after")
    def _cover(self) -> "GameOut":
        expected = None if self.cover_image_id is None else f"covers/{self.slug}.webp"
        if self.cover_url != expected:
            raise ValueError(f"cover_url doit valoir {expected!r} (cover_image_id, slug)")
        return self


class DataFile(_Output):
    """Contenu de site/data.json (schéma v5)."""

    schema_version: Literal[5] = 5
    generated_at: UtcDatetime
    currency: Literal["EUR"] = "EUR"
    games: list[GameOut]

    @model_validator(mode="after")
    def _unique_slugs(self) -> "DataFile":
        if len({g.slug for g in self.games}) != len(self.games):
            raise ValueError("slug en double dans data.json")
        return self
