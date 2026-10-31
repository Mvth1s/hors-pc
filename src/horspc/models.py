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
GameOrigin = Literal["curated", "discovered"]
# Source du titre d'un jeu découvert, par priorité (voir horspc.discovery.display_title).
TitleSource = Literal["localisation_eu", "nom_leclerc", "titre_francais_igdb", "nom_igdb"]
# Un jeu découvert absent des rayons ce nombre de runs consécutifs est retiré.
MAX_MISSED_RUNS = 4
# code_box : boîte sans disque ni cartouche, contenant seulement un code de téléchargement.
OfferFormat = Literal["physical", "code_box", "digital"]
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


class Edition(_Input):
    """Une édition d'un jeu : sa version pour une console (EAN, fiches et offres propres)."""

    platform: Platform


class GameEntry(_Input):
    """Une entrée de games.yaml : un jeu, avec une édition par console sans version PC."""

    slug: Slug
    title: str = Field(min_length=1)
    editions: list[Edition] = Field(min_length=1)
    igdb_id: PositiveInt | None = None
    launch_price: Euros | None = None
    launch_price_source: HttpUrl | None = None
    pc_override: PcOverride | None = None
    release_date_override: ReleaseDateOverride | None = None
    description_fr: str | None = Field(default=None, min_length=1)

    @field_validator("editions")
    @classmethod
    def _unique_platforms(cls, value: list[Edition]) -> list[Edition]:
        if len({e.platform for e in value}) != len(value):
            raise ValueError("édition en double pour une même plateforme")
        return value

    @property
    def platforms(self) -> list[Platform]:
        return [e.platform for e in self.editions]

    @model_validator(mode="after")
    def _price_has_source(self) -> "GameEntry":
        if (self.launch_price is None) != (self.launch_price_source is None):
            raise ValueError("launch_price et launch_price_source vont ensemble")
        return self


class PriceObservation(_Input):
    """Un relevé de prix manuel dans prices.manual.yaml."""

    slug: Slug
    platform: Platform | None = None  # édition ; facultatif si le jeu n'en a qu'une
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
    platform: Platform
    merchant: ScrapedSource
    seller: str = Field(min_length=1)
    price: Euros
    shipping: EurosOrZero | None
    format: Literal["physical", "code_box"]
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


class DiscoveredGame(_Input):
    """Une entrée de data/games.discovered.yaml (générée par la découverte, jamais saisie)."""

    slug: Slug
    title: str = Field(min_length=1)
    title_source: TitleSource
    igdb_id: PositiveInt
    editions: list[Edition] = Field(min_length=1)
    first_seen: date
    last_seen: date
    missed_runs: int = Field(ge=0, lt=MAX_MISSED_RUNS)  # runs consécutifs absent des rayons

    @field_validator("editions")
    @classmethod
    def _unique_platforms(cls, value: list[Edition]) -> list[Edition]:
        if len({e.platform for e in value}) != len(value):
            raise ValueError("édition en double pour une même plateforme")
        return value

    def entry(self) -> GameEntry:
        """Entrée de catalogue : ni prix de lancement ni override (aucune saisie manuelle)."""
        return GameEntry(
            slug=self.slug, title=self.title, editions=self.editions, igdb_id=self.igdb_id
        )


class DiscoveredGames(_Input):
    """Contenu de data/games.discovered.yaml : slug et igdb_id uniques."""

    games: list[DiscoveredGame]

    @model_validator(mode="after")
    def _unique(self) -> "DiscoveredGames":
        for name, values in (
            ("slugs", [g.slug for g in self.games]),
            ("igdb_id", [g.igdb_id for g in self.games]),
        ):
            dup = sorted(str(v) for v, n in Counter(values).items() if n > 1)
            if dup:
                raise ValueError(f"{name} en double dans games.discovered.yaml : {', '.join(dup)}")
        return self


class Catalog(_Input):
    """Jeux et observations, avec les contrôles croisés entre les deux fichiers."""

    games: list[GameEntry]
    observations: list[PriceObservation]
    discovered: frozenset[str] = frozenset()  # slugs venus de games.discovered.yaml

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
        editions = {g.slug: g.platforms for g in self.games}
        for obs in self.observations:
            platforms = editions[obs.slug]
            if obs.platform is None and len(platforms) > 1:
                raise ValueError(f"{obs.slug} a plusieurs éditions : platform est obligatoire")
            if obs.platform is not None and obs.platform not in platforms:
                raise ValueError(f"{obs.slug} n'a pas d'édition {obs.platform}")
        return self

    def observation_platform(self, obs: PriceObservation) -> Platform:
        """Édition d'un relevé manuel : la sienne, sinon l'unique édition du jeu."""
        if obs.platform is not None:
            return obs.platform
        return next(g.platforms[0] for g in self.games if g.slug == obs.slug)


class DiscoveryExclusion(_Input):
    """Faux positif de la découverte automatique (data/discovery.exclude.yaml)."""

    igdb_id: PositiveInt
    reason: str = Field(min_length=1)
    date: date

    @field_validator("date")
    @classmethod
    def _not_in_future(cls, value: date, info: ValidationInfo) -> date:
        if value > _today(info):
            raise ValueError("date est dans le futur")
        return value


class DiscoveryExclusions(_Input):
    """Liste des exclusions, un igdb_id au plus une fois."""

    exclusions: list[DiscoveryExclusion]

    @model_validator(mode="after")
    def _unique(self) -> "DiscoveryExclusions":
        dup = sorted(i for i, n in Counter(e.igdb_id for e in self.exclusions).items() if n > 1)
        if dup:
            raise ValueError(f"igdb_id en double : {', '.join(map(str, dup))}")
        return self


# --- Sortie data.json ---------------------------------------------------------


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OfferOut(_Output):
    platform: Platform  # édition à laquelle l'offre se rapporte
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


class OutOfStockOut(_Output):
    """Fiche trouvée mais sans offre en ligne lors du dernier relevé."""

    merchant: str = Field(min_length=1)
    observed_at: date


class EditionOut(_Output):
    platform: Platform
    offer: OfferOut | None
    out_of_stock: OutOfStockOut | None = None  # seulement sans offre
    # Preuves IGDB (external_games) pour le badge « Physique et numérique ».
    sold_digital: bool
    listed_physical: bool

    @model_validator(mode="after")
    def _offer_platform(self) -> "EditionOut":
        if self.offer is not None and self.offer.platform != self.platform:
            raise ValueError("offre d'une autre édition")
        if self.offer is not None and self.out_of_stock is not None:
            raise ValueError("out_of_stock n'est renseigné que pour une édition sans offre")
        return self


class GameOut(_Output):
    slug: Slug
    title: str
    origin: GameOrigin  # curated : games.yaml ; discovered : découverte automatique (v7)
    platforms: list[Platform] = Field(min_length=1)  # dérivé des éditions (filtres)
    editions: list[EditionOut] = Field(min_length=1)
    best_offer: OfferOut | None  # meilleure offre toutes éditions
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

    @model_validator(mode="after")
    def _editions(self) -> "GameOut":
        if self.platforms != [e.platform for e in self.editions]:
            raise ValueError("platforms doit lister les plateformes des éditions, dans l'ordre")
        offers = [e.offer for e in self.editions if e.offer is not None]
        if (self.best_offer is None) != (not offers) or (
            self.best_offer is not None and self.best_offer not in offers
        ):
            raise ValueError("best_offer doit être l'une des offres des éditions")
        return self

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
    """Contenu de site/data.json (schéma v7 : origine du jeu, curé ou découvert)."""

    schema_version: Literal[7] = 7
    generated_at: UtcDatetime
    currency: Literal["EUR"] = "EUR"
    games: list[GameOut]

    @model_validator(mode="after")
    def _unique_slugs(self) -> "DataFile":
        if len({g.slug for g in self.games}) != len(self.games):
            raise ValueError("slug en double dans data.json")
        return self
