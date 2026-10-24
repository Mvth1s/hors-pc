"""Jaquettes IGDB : téléchargées en WebP dans site/covers/, jamais servies depuis IGDB."""

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import httpx

from horspc.http import HttpClient

log = logging.getLogger(__name__)

# IGDB sert directement le WebP quand on demande l'extension .webp : aucune conversion locale.
IMAGE_URL = "https://images.igdb.com/igdb/image/upload/t_cover_big/{image_id}.webp"
COVERS_DIR = "covers"


@dataclass(frozen=True)
class CoverUpdate:
    """Jaquette d'un jeu après le build : image_id présent sur disque et écriture à faire."""

    slug: str
    image_id: str | None
    content: bytes | None = None  # nouveau fichier à écrire
    delete: bool = False  # IGDB n'a plus de jaquette : supprimer l'ancien fichier


def cover_url(slug: str) -> str:
    """Chemin relatif de la jaquette, tel qu'écrit dans data.json et lu par le front."""
    return f"{COVERS_DIR}/{slug}.webp"


def keep_local(site_dir: Path, slug: str, image_id: str | None) -> CoverUpdate:
    """Réutilise le fichier déjà présent (mode hors ligne ou IGDB indisponible)."""
    exists = image_id is not None and (site_dir / cover_url(slug)).exists()
    return CoverUpdate(slug, image_id if exists else None)


def fetch_cover(
    http: HttpClient,
    site_dir: Path,
    slug: str,
    image_id: str | None,
    previous_image_id: str | None,
) -> CoverUpdate:
    """Télécharge la jaquette si son image_id a changé ou si le fichier manque.

    En cas d'échec, l'ancien fichier est conservé s'il existe.
    """
    if image_id is None:
        return CoverUpdate(slug, None, delete=True)
    if image_id == previous_image_id and (site_dir / cover_url(slug)).exists():
        return CoverUpdate(slug, image_id)
    try:
        content = http.request("GET", IMAGE_URL.format(image_id=image_id)).content
        if content[:4] != b"RIFF" or content[8:12] != b"WEBP":
            raise ValueError("la réponse n'est pas une image WebP")
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Jaquette de %s non téléchargée (%s) : %s", slug, image_id, exc)
        return keep_local(site_dir, slug, previous_image_id)
    return CoverUpdate(slug, image_id, content=content)


def apply_updates(site_dir: Path, updates: Iterable[CoverUpdate]) -> None:
    """Écrit les nouvelles jaquettes, supprime les retirées et les orphelines."""
    directory = site_dir / COVERS_DIR
    slugs = set()
    for update in updates:
        slugs.add(update.slug)
        path = site_dir / cover_url(update.slug)
        if update.delete:
            path.unlink(missing_ok=True)
        elif update.content is not None:
            directory.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".webp.tmp")
            tmp.write_bytes(update.content)
            os.replace(tmp, path)
            log.info("Jaquette de %s mise à jour (%s)", update.slug, update.image_id)
    if directory.is_dir():
        for path in directory.glob("*.webp"):
            if path.stem not in slugs:
                path.unlink()
                log.info("Jaquette orpheline supprimée : %s", path.name)
