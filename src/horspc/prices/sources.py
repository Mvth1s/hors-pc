"""Lecture des pages des sources automatiques (JSON-LD d'abord) et découverte de leurs fiches.

Sources (voir docs/sources.md) :
- E.Leclerc : fiche `/fp/<slug>-<EAN>` (le slug est libre : l'EAN suffit), JSON-LD avec vendeur ;
- Boulanger : pages catégories `/c/jeu-…?numPage=N` (nom, éditeur, EAN dans l'URL de l'image),
  fiche `/ref/<id>` (JSON-LD `Offer` + `gtin13`, vendeur dans le HTML) ;
- Pixmania : sitemaps produits (slugs lisibles), fiche avec JSON-LD `AggregateOffer` + `gtin13`,
  vendeur professionnel dans le HTML.
"""

import gzip
import html
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from horspc.models import Platform

LECLERC = "https://www.e.leclerc"
BOULANGER = "https://www.boulanger.com"
PIXMANIA = "https://www.pixmania.com"
PIXMANIA_SITEMAP = "https://feeds.pixmania.com/sitemap/FR/sitemap.xml"
BOULANGER_CATEGORIES: dict[Platform, str] = {
    "ps5": "/c/jeu-playstation-5",
    "switch": "/c/jeu-switch",
    "switch2": "/c/jeu-switch",
    "xbox_series": "/c/jeu-xbox-one",
}
BOULANGER_MAX_PAGES = 40


class MissingData(Exception):
    """La page ne contient plus les données structurées attendues (sélecteur à revoir)."""


@dataclass
class ProductPage:
    name: str
    eans: set[str]
    price: Decimal | None
    currency: str | None
    condition: str | None  # new | used | refurbished
    available: bool | None
    seller: str | None
    shipping: Decimal | None = None


@dataclass
class ListingItem:
    url: str
    ean: str | None
    name: str
    extra: list[str] = field(default_factory=list)  # mots tolérés (éditeur)


# --- JSON-LD -----------------------------------------------------------------------------


def jsonld_objects(page: str) -> Iterator[dict[str, Any]]:
    """Tous les objets JSON-LD de la page (graphes et listes aplatis)."""
    for block in re.findall(
        r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", page, re.S | re.I
    ):
        try:
            data = json.loads(html.unescape(block.strip()))
        except ValueError:
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                yield item
                stack.extend(v for k, v in item.items() if k == "@graph")


def _types(obj: dict[str, Any]) -> set[str]:
    value = obj.get("@type")
    return set(value) if isinstance(value, list) else {value}


def _product(page: str) -> dict[str, Any]:
    for obj in jsonld_objects(page):
        if "Product" in _types(obj) and obj.get("offers"):
            return obj
    raise MissingData("JSON-LD Product avec offres absent")


def _eans(product: dict[str, Any]) -> set[str]:
    eans = {
        str(product[k]).strip()
        for k in ("gtin", "gtin8", "gtin12", "gtin13", "gtin14")
        if product.get(k)
    }
    match = re.match(r"gtin1?\d:(\d+)", str(product.get("productID", "")))
    if match:
        eans.add(match.group(1))
    return {normalize_ean(e) for e in eans if e.isdigit()}


def normalize_ean(value: str) -> str:
    """EAN-13 canonique : zéros de tête rétablis (UPC, GTIN-14), EAN-8 inchangé."""
    if len(value) <= 8:
        return value
    padded = value.zfill(14)
    return padded[1:] if padded[0] == "0" else padded


def _offers(product: dict[str, Any]) -> list[dict[str, Any]]:
    offers = product.get("offers")
    return [o for o in (offers if isinstance(offers, list) else [offers]) if isinstance(o, dict)]


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", ".")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _condition(value: Any) -> str | None:
    text = str(value or "")
    if "NewCondition" in text:
        return "new"
    if "Refurbished" in text:
        return "refurbished"
    if "Used" in text or "Damaged" in text:
        return "used"
    return None


def _available(value: Any) -> bool | None:
    text = re.sub(r"\s", "", str(value or "")).lower()
    if not text:
        return None
    # Précommande acceptée : le jeu est achetable dès maintenant.
    return any(k in text for k in ("instock", "preorder", "limitedavailability", "onlineonly"))


def _seller_name(offer: dict[str, Any]) -> str | None:
    seller = offer.get("seller")
    if isinstance(seller, dict):
        return (seller.get("name") or "").strip() or None
    return str(seller).strip() or None if seller else None


def _text(page: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page)))


# --- Fiches produit ------------------------------------------------------------------------


def parse_leclerc(page: str) -> ProductPage:
    product = _product(page)
    offers = [o for o in _offers(product) if "Offer" in _types(o)]
    if not offers:
        raise MissingData("JSON-LD Offer absent (seulement AggregateOffer)")
    offer = offers[0]
    return ProductPage(
        name=str(product.get("name", "")),
        eans=_eans(product),
        price=_decimal(offer.get("price")),
        currency=offer.get("priceCurrency"),
        condition=_condition(offer.get("itemCondition")),
        available=_available(offer.get("availability")),
        seller=_seller_name(offer),
    )


def parse_boulanger(page: str) -> ProductPage:
    product = _product(page)
    offer = _offers(product)[0]
    seller = re.search(r'data-analytics_product_seller="([^"]+)"', page)
    return ProductPage(
        name=str(product.get("name", "")),
        eans=_eans(product),
        price=_decimal(offer.get("price") or offer.get("lowPrice")),
        currency=offer.get("priceCurrency"),
        condition=_condition(offer.get("itemCondition")),
        available=_available(offer.get("availability")),
        seller=seller.group(1).strip() if seller else _seller_name(offer),
    )


def parse_pixmania(page: str) -> ProductPage:
    product = _product(page)
    offer = _offers(product)[0]
    shipping = (offer.get("shippingDetails") or {}).get("shippingRate") or {}
    # Le JSON-LD nomme la plateforme ; le vendeur professionnel réel est le lien « marchand ».
    seller = re.search(
        r"Vendu par.{0,200}?href=['\"]/fr/fr/marchand/[^'\"]+['\"][^>]*>([^<]+)</a>", page, re.S
    )
    return ProductPage(
        name=str(product.get("name", "")),
        eans=_eans(product),
        price=_decimal(offer.get("price") or offer.get("lowPrice")),
        currency=offer.get("priceCurrency"),
        condition=_condition(offer.get("itemCondition")),
        available=_available(offer.get("availability")),
        seller=seller.group(1).strip() if seller else _seller_name(offer),
        shipping=_decimal(shipping.get("value")) if shipping.get("value") is not None else None,
    )


PARSERS = {"leclerc": parse_leclerc, "boulanger": parse_boulanger, "pixmania": parse_pixmania}


# --- Découverte ------------------------------------------------------------------------------


def leclerc_url(ean: str) -> str:
    """Fiche Leclerc à partir d'un EAN : le slug est libre, le site redirige vers la vraie fiche."""
    return f"{LECLERC}/fp/x-{ean}"


def boulanger_listing(page: str) -> list[ListingItem]:
    """Produits d'une page catégorie : lien, EAN (URL d'image), nom et éditeur (attributs)."""
    items: dict[str, ListingItem] = {}
    for match in re.finditer(r'<a\b[^>]*href="(/ref/\d+)"[^>]*>', page):
        tag = match.group(0)
        name = re.search(r'data-analytics_product_name="([^"]+)"', tag)
        if not name or match.group(1) in items:
            continue
        before = page[max(0, match.start() - 4000) : match.start()]
        eans = re.findall(r"/Boulanger/(\d{8,14})_", before)
        brand = re.search(r'data-analytics_product_trademark="([^"]*)"', tag)
        grade = re.search(r'data-analytics_product_grade="([^"]*)"', tag)
        if grade and grade.group(1) not in ("neuf", ""):
            continue  # reconditionné ou occasion
        items[match.group(1)] = ListingItem(
            url=BOULANGER + match.group(1),
            ean=eans[-1] if eans else None,
            name=name.group(1).replace("_", " "),
            extra=[brand.group(1).replace("_", " ")] if brand else [],
        )
    return list(items.values())


def sitemap_locs(content: bytes) -> list[str]:
    body = gzip.decompress(content) if content[:2] == b"\x1f\x8b" else content
    return re.findall(r"<loc>\s*([^<]+?)\s*</loc>", body.decode("utf-8", "ignore"))


def pixmania_slug_name(url: str) -> str:
    """« …/mario-kart-world-switch-2-397665.html » → « mario kart world switch 2 397665 »."""
    slug = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".html")
    return slug.replace("-", " ")
