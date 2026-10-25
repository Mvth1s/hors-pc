"""Correspondance stricte entre un produit vendu et un jeu du catalogue.

Une offre n'est rattachée à un jeu que si son EAN est l'un des EAN connus du jeu, ou si son nom
correspond au titre, à la plateforme et à l'édition standard (aucun mot en trop hors liste blanche).
"""

import re
import unicodedata
from collections.abc import Iterable

from horspc.models import Platform

# Mots tolérés en plus du titre et de la plateforme (éditeurs, mentions de format standard).
FILLERS = frozenset(
    [
        "jeu",
        "jeux",
        "video",
        "standard",
        "edition",
        "fr",
        "francais",
        "francaise",
        "version",
        "vf",
        "physique",
        "neuf",
        "multilingue",
        "nintendo",
        "sony",
        "playstation",
        "microsoft",
        "xbox",
        "interactive",
        "entertainment",
    ]
)
# Mots qui signalent une autre référence que l'édition standard.
EDITION_WORDS = frozenset(
    [
        "deluxe",
        "collector",
        "collectors",
        "limited",
        "limitee",
        "ultimate",
        "gold",
        "complete",
        "premium",
        "special",
        "speciale",
        "steelbook",
        "pack",
        "bundle",
        "console",
        "amiibo",
        "guide",
        "figurine",
        "dlc",
        "season",
        "pass",
        "code",
        "telechargement",
        "download",
        "box",
        "carte",
        "goty",
        "remastered",
        "launch",
        "day",
        "one",
        "dayone",
    ]
)
EAN_RE = re.compile(r"^\d{8}$|^\d{12,14}$")

# Boîte sans disque ni cartouche, qui ne contient qu'un code de téléchargement. Dans le nom, la
# mention suffit ; dans la description, seules des formules explicites comptent (« un code de
# téléchargement » seul y décrit souvent un bonus ou un pack console : Mario Kart World).
_CODE_BOX_NAME = re.compile(
    r"\bcode\s*-?\s*in\s*-?\s*a\s*-?\s*box\b|\bcode dans (?:la|une) boite\b"
    r"|\bcode de telechargement\b|\b(?:digital|download) code\b|\bboite avec code\b"
)
_CODE_BOX_DESCRIPTION = re.compile(
    r"\bcode\s*-?\s*in\s*-?\s*a\s*-?\s*box\b|\bcode dans (?:la|une) boite\b"
    r"|ne contien(?:t|dra) qu.un code"
    r"|contien(?:t|dra) (?:uniquement|seulement) un code"
    r"|aucun (?:disque|cartouche|support physique) (?:ne sera |n.est )?inclus"
    r"|contains only a (?:download|digital) code|no disc (?:is )?included"
)


def _plain(text: str) -> str:
    """Minuscules sans accents, pour chercher des formules (« boîte » → « boite »)."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", ascii_text.lower())


def is_code_box(name: str, description: str = "") -> bool:
    """Offre « boîte avec code de téléchargement » (nom du produit, puis description)."""
    return bool(
        _CODE_BOX_NAME.search(_plain(name)) or _CODE_BOX_DESCRIPTION.search(_plain(description))
    )


def _without_code_box(name: str) -> str:
    """Nom sans la mention code-in-a-box : le format est relevé à part, pas le titre."""
    return _CODE_BOX_NAME.sub(" ", _plain(name))


def tokens(text: str) -> list[str]:
    """Mots en minuscules, sans accents ni ponctuation (« Pokémon » donne pokemon)."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.findall(r"[a-z0-9]+", ascii_text.lower())


def platforms_in(words: list[str]) -> set[Platform]:
    """Plateformes nommées dans une suite de mots (PS5, Switch, Switch 2, Xbox Series)."""
    found: set[Platform] = set()
    for i, word in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if word == "ps5" or (word == "playstation" and nxt == "5"):
            found.add("ps5")
        elif word == "switch":
            found.add("switch2" if nxt == "2" else "switch")
        elif word in ("xbs", "xsx") or (word == "xbox" and nxt == "series"):
            found.add("xbox_series")
    return found


def _platform_words(words: list[str]) -> set[int]:
    """Indices des mots qui désignent une plateforme (à retirer avant de comparer au titre)."""
    out: set[int] = set()
    for i, word in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if word in ("ps5", "xbs", "xsx"):
            out.add(i)
        elif word == "playstation" and nxt == "5":
            out |= {i, i + 1}
        elif word == "switch":
            out |= {i, i + 1} if nxt == "2" else {i}
        elif word == "xbox" and nxt == "series":
            out |= {i, i + 1}
            if i + 2 < len(words) and words[i + 2] in ("x", "s"):
                out.add(i + 2)
    return out


def title_matches(
    product_name: str, title: str, platform: Platform, extra_fillers: Iterable[str] = ()
) -> bool:
    """Nom de produit = titre + plateforme + édition standard, sans autre mot significatif."""
    words = tokens(_without_code_box(product_name))
    if platform not in platforms_in(words):
        return False
    if platform == "switch" and "switch2" in platforms_in(words):
        return False
    wanted = tokens(title)
    platform_idx = _platform_words(words)
    rest = [w for i, w in enumerate(words) if i not in platform_idx]
    if not set(wanted) <= set(rest):
        return False
    allowed = set(wanted) | FILLERS | {t for f in extra_fillers for t in tokens(f)}
    # Nombres longs (identifiants, EAN) ignorés ; un petit nombre (suite : « 2 ») compte.
    leftover = [w for w in rest if w not in allowed and not (w.isdigit() and len(w) >= 5)]
    if any(w in EDITION_WORDS for w in leftover):
        return False
    return not leftover


def is_ean(value: str) -> bool:
    return bool(EAN_RE.match(value))
