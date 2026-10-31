"""Jaquettes et captures IGDB : téléchargées en WebP dans site/, jamais servies depuis IGDB."""

import logging
import os
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from horspc.http import HttpClient

log = logging.getLogger(__name__)

# IGDB sert directement le WebP quand on demande l'extension .webp : aucune conversion locale.
IMAGE_URL = "https://images.igdb.com/igdb/image/upload/t_cover_big/{image_id}.webp"
COVERS_DIR = "covers"
SCREEN_URL = "https://images.igdb.com/igdb/image/upload/t_screenshot_big/{image_id}.webp"
SCREENS_DIR = "screens"


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
    content = _download(http, IMAGE_URL.format(image_id=image_id), f"Jaquette de {slug}")
    if content is None:
        return keep_local(site_dir, slug, previous_image_id)
    return CoverUpdate(slug, image_id, content=content)


def _download(http: HttpClient, url: str, label: str) -> bytes | None:
    """Télécharge une image WebP IGDB ; None en cas d'échec (nouvel essai au run suivant)."""
    try:
        content = http.request("GET", url).content
        if content[:4] != b"RIFF" or content[8:12] != b"WEBP":
            raise ValueError("la réponse n'est pas une image WebP")
    except (httpx.HTTPError, ValueError) as exc:
        log.info("%s non téléchargée (%s) : %s", label, url.rsplit("/", 1)[-1], exc)
        return None
    return content


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


# --- Captures d'écran -----------------------------------------------------------------


@dataclass(frozen=True)
class ScreensUpdate:
    """Captures d'un jeu après le build : image_id présents sur disque et fichiers à écrire."""

    slug: str
    image_ids: tuple[str, ...]
    contents: dict[str, bytes] = field(default_factory=dict)


def screen_url(slug: str, image_id: str) -> str:
    """Chemin relatif d'une capture (le nom de fichier est l'image_id IGDB)."""
    return f"{SCREENS_DIR}/{slug}/{image_id}.webp"


def keep_local_screens(site_dir: Path, slug: str, image_ids: Iterable[str]) -> ScreensUpdate:
    """Réutilise les captures déjà présentes (mode hors ligne ou IGDB indisponible)."""
    ids = tuple(i for i in image_ids if (site_dir / screen_url(slug, i)).exists())
    return ScreensUpdate(slug, ids)


def fetch_screens(
    http: HttpClient, site_dir: Path, slug: str, image_ids: Iterable[str]
) -> ScreensUpdate:
    """Télécharge les captures absentes ; un fichier existant (même image_id) est conservé."""
    kept: list[str] = []
    contents: dict[str, bytes] = {}
    for image_id in image_ids:
        if (site_dir / screen_url(slug, image_id)).exists():
            kept.append(image_id)
            continue
        content = _download(http, SCREEN_URL.format(image_id=image_id), f"Capture de {slug}")
        if content is not None:
            kept.append(image_id)
            contents[image_id] = content
    return ScreensUpdate(slug, tuple(kept), contents)


def apply_screens(site_dir: Path, updates: Iterable[ScreensUpdate]) -> None:
    """Écrit les nouvelles captures et supprime celles qui ne sont plus utilisées."""
    root = site_dir / SCREENS_DIR
    slugs = set()
    for update in updates:
        slugs.add(update.slug)
        directory = root / update.slug
        for image_id, content in update.contents.items():
            directory.mkdir(parents=True, exist_ok=True)
            path = site_dir / screen_url(update.slug, image_id)
            tmp = path.with_suffix(".webp.tmp")
            tmp.write_bytes(content)
            os.replace(tmp, path)
        if directory.is_dir():
            for path in directory.glob("*.webp"):
                if path.stem not in update.image_ids:
                    path.unlink()
            if not any(directory.iterdir()):
                directory.rmdir()
    if root.is_dir():
        for directory in root.iterdir():
            if directory.is_dir() and directory.name not in slugs:
                shutil.rmtree(directory)
