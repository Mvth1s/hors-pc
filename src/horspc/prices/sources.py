"""Lecture des pages des sources automatiques (JSON-LD d'abord) et découverte de leurs fiches.

Sources (voir docs/sources.md) :
- E.Leclerc : pages catégories `/cat/jeux-…?page=N` (nom et EAN de chaque produit), trouvées
  dans le sitemap de navigation ; fiche `/fp/<slug>-<EAN>` (le slug est libre : l'EAN suffit),
  JSON-LD avec vendeur ;
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
from urllib.parse import urlsplit

from horspc.models import Platform

LECLERC = "https://www.e.leclerc"
LECLERC_SITEMAP = "https://www.e.leclerc/sitemap/sitemap-index.xml"
# Catégories de jeux par console, retrouvées par leur chemin dans le sitemap de navigation
# (pas de recherche interne : `Disallow: /recherche`). Ni tri ni filtre : interdits par robots.txt.
LECLERC_CATEGORIES: dict[Platform, str] = {
    "ps5": "/cat/jeux-ps5",
    "switch": "/cat/jeux-switch",
    "switch2": "/cat/jeux-switch-2",
    "xbox_series": "/cat/jeux-xbox-series",
}
LECLERC_MAX_PAGES = 100  # garde-fou : 57 pages au plus constatées (Switch, 25/10/2026)
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
    description: str = ""  # texte brut, pour repérer une boîte avec code
    third_party_only: bool = False  # seulement des offres de vendeurs tiers (Leclerc)


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
        data = _json(block.strip())
        if data is None:
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                yield item
                stack.extend(v for k, v in item.items() if k == "@graph")


def _json(text: str) -> Any:
    """JSON brut d'abord : un `&quot;` décodé trop tôt casserait une chaîne (Boulanger)."""
    for candidate in (text, html.unescape(text)):
        try:
            return json.loads(candidate, strict=False)
        except ValueError:
            continue
    return None


def _types(obj: dict[str, Any]) -> set[str]:
    value = obj.get("@type")
    return set(value) if isinstance(value, list) else {value}


def _product(page: str) -> dict[str, Any]:
    """Product JSON-LD, de préférence avec offres (une précommande peut ne pas en avoir)."""
    products = [o for o in jsonld_objects(page) if "Product" in _types(o)]
    if not products:
        raise MissingData("JSON-LD Product absent")
    return next((p for p in products if p.get("offers")), products[0])


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


def _description(product: dict[str, Any]) -> str:
    """Description JSON-LD en texte brut (entités parfois doublement encodées, balises HTML)."""
    text = html.unescape(html.unescape(str(product.get("description") or "")))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def _text(page: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page)))


# --- Fiches produit ------------------------------------------------------------------------


def parse_leclerc(page: str) -> ProductPage:
    product = _product(page)
    offers = _offers(product)
    own = [o for o in offers if "Offer" in _types(o)]
    offer = own[0] if own else {}
    return ProductPage(
        name=html.unescape(str(product.get("name", ""))),
        description=_description(product),
        eans=_eans(product),
        price=_decimal(offer.get("price")),
        currency=offer.get("priceCurrency"),
        condition=_condition(offer.get("itemCondition")),
        available=_available(offer.get("availability")),
        seller=_seller_name(offer),
        # Place de marché seule (AggregateOffer sans Offer de l'enseigne) : écartée au relevé.
        third_party_only=bool(offers) and not own,
    )


def parse_boulanger(page: str) -> ProductPage:
    product = _product(page)
    offer = next(iter(_offers(product)), {})
    seller = re.search(r'data-analytics_product_seller="([^"]+)"', page)
    return ProductPage(
        name=html.unescape(str(product.get("name", ""))),
        description=_description(product),
        eans=_eans(product),
        price=_decimal(offer.get("price") or offer.get("lowPrice")),
        currency=offer.get("priceCurrency"),
        condition=_condition(offer.get("itemCondition")),
        available=_available(offer.get("availability")),
        seller=seller.group(1).strip() if seller else _seller_name(offer),
    )


def parse_pixmania(page: str) -> ProductPage:
    product = _product(page)
    offer = next(iter(_offers(product)), {})
    shipping = (offer.get("shippingDetails") or {}).get("shippingRate") or {}
    # Le JSON-LD nomme la plateforme ; le vendeur professionnel réel est le lien « marchand ».
    seller = re.search(
        r"Vendu par.{0,200}?href=['\"]/fr/fr/marchand/[^'\"]+['\"][^>]*>([^<]+)</a>", page, re.S
    )
    return ProductPage(
        name=html.unescape(str(product.get("name", ""))),
        description=_description(product),
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


def leclerc_category_urls(locs: list[str]) -> dict[Platform, str]:
    """URL des catégories de jeux présentes dans le sitemap de navigation."""
    paths = {urlsplit(loc).path.rstrip("/"): loc for loc in locs}
    return {p: paths[path] for p, path in LECLERC_CATEGORIES.items() if path in paths}


def leclerc_listing(page: str) -> list[ListingItem]:
    """Produits d'une page catégorie Leclerc : fiche, EAN (fin du lien) et nom (`title`)."""
    items: dict[str, ListingItem] = {}
    for tag in re.findall(r"<a\b[^>]*data-product-card-title[^>]*>", page):
        href = re.search(r'href="(/fp/[a-z0-9-]+-(\d{8,14}))"', tag)
        title = re.search(r'title="([^"]+)"', tag)
        if href and title and href.group(1) not in items:
            items[href.group(1)] = ListingItem(
                url=LECLERC + href.group(1),
                ean=normalize_ean(href.group(2)),
                name=html.unescape(title.group(1)).strip(),
            )
    return list(items.values())


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
