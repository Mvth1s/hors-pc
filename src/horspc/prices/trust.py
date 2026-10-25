"""Plateforme de confiance : règle commune à toutes les offres retenues (relevées ou automatiques).

Une offre n'est retenue que si :
1. l'achat est livrable en France ;
2. le produit est neuf ;
3. le vendeur est professionnel : l'enseigne elle-même, ou un vendeur de sa place de marché quand
   celle-ci est réservée aux professionnels (vérifié dans ses CGU) ;
4. la plateforme est établie dans l'Union européenne (garantie légale de conformité et droit de
   rétractation du droit de la consommation européen) ;
5. le vendeur n'est pas un revendeur de clés (Instant Gaming, Eneba, G2A, Kinguin…).
"""

import re
import unicodedata
from dataclasses import dataclass

EU_COUNTRIES = frozenset(
    {
        "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU",
        "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK",
    }
)  # fmt: skip

# Noms (normalisés) de revendeurs de clés ou de marketplaces de particuliers, jamais acceptés.
KEY_RESELLERS = frozenset(
    {
        "instantgaming",
        "eneba",
        "g2a",
        "kinguin",
        "cdkeys",
        "gamivo",
        "k4g",
        "hrkgame",
        "goclecd",
        "allkeyshop",
        "loaded",
    }
)


@dataclass(frozen=True)
class SalesPlatform:
    """Plateforme où l'achat se fait, avec les faits qui fondent la confiance."""

    id: str
    name: str
    domains: tuple[str, ...]
    legal_entity: str
    country: str  # siège de l'exploitant (code ISO 3166-1)
    delivers_to_france: bool
    marketplace_pros_only: bool  # place de marché réservée aux professionnels (CGU vérifiées)


PLATFORMS: dict[str, SalesPlatform] = {
    "leclerc": SalesPlatform(
        "leclerc",
        "E.Leclerc",
        ("e.leclerc",),
        "L COMMERCE SNC, Ivry-sur-Seine (RCS Créteil 882 736 077)",
        "FR",
        delivers_to_france=True,
        marketplace_pros_only=False,  # non vérifié : seules les offres de l'enseigne comptent
    ),
    "boulanger": SalesPlatform(
        "boulanger",
        "Boulanger",
        ("boulanger.com",),
        "société Boulanger (CGV de boulanger.com, droit français)",
        "FR",
        delivers_to_france=True,
        marketplace_pros_only=False,  # non vérifié : seules les offres de l'enseigne comptent
    ),
    "pixmania": SalesPlatform(
        "pixmania",
        "Pixmania",
        ("pixmania.com",),
        "PIXMANIA GROUP SAS, Paris (RCS Paris 910 199 835)",
        "FR",
        delivers_to_france=True,
        marketplace_pros_only=True,  # CGU : « Vendeur(s) désigne(nt) tout(s) professionnel(s) »
    ),
}


def normalize(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", ascii_name.lower())


def is_platform_seller(platform: SalesPlatform, seller: str) -> bool:
    """Le vendeur est-il l'enseigne elle-même (« E.Leclerc », « boulanger », « Pixmania ») ?"""
    return normalize(seller) in {normalize(platform.name), normalize(platform.id)}


def rejection(
    platform_id: str, seller: str | None, condition: str, offer_format: str
) -> str | None:
    """Motif de rejet d'une offre selon la règle de plateforme de confiance, ou None si acceptée."""
    platform = PLATFORMS.get(platform_id)
    if platform is None:
        return f"plateforme inconnue : {platform_id}"
    if platform.country not in EU_COUNTRIES:
        return f"{platform.name} : exploitant hors UE ({platform.country})"
    if not platform.delivers_to_france:
        return f"{platform.name} : pas de livraison en France"
    if condition != "new":
        return f"article non neuf ({condition})"
    if offer_format not in ("physical", "code_box"):
        return "seuls les stores officiels vendent du démat (pas de source automatique)"
    if not seller:
        return "vendeur non identifié"
    if normalize(seller) in KEY_RESELLERS:
        return f"revendeur de clés : {seller}"
    if not is_platform_seller(platform, seller) and not platform.marketplace_pros_only:
        return f"vendeur tiers ({seller}) sur une place de marché non réservée aux pros"
    return None
