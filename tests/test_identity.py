"""Garde-fou d'identité : seul le pseudo Mvth1s apparaît, jamais l'identité civile de l'éditeur
ni ses liens personnels, dans aucun fichier suivi par git."""

import codecs
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Chaînes interdites, encodées en ROT13 pour que ce fichier ne les contienne pas lui-même
# (`git grep` doit rester vide) : nom de famille, nom du site personnel, profil LinkedIn.
FORBIDDEN = [codecs.decode(s, "rot13") for s in ("nthnqb", "znguvfnthnqb", "yvaxrqva.pbz/va/")]


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout
    return [ROOT / name for name in out.decode().split("\0") if name]


def offenders(paths: list[Path], root: Path) -> list[str]:
    """« fichier : chaîne » pour chaque chaîne interdite trouvée, sans tenir compte de la casse."""
    found = []
    for path in paths:
        if not path.is_file():  # supprimé du disque mais encore dans l'index
            continue
        text = path.read_bytes().decode("utf-8", "ignore").lower()
        found += [f"{path.relative_to(root)} : {w}" for w in FORBIDDEN if w.lower() in text]
    return found


def test_aucune_identite_civile_dans_les_fichiers_suivis() -> None:
    assert offenders(tracked_files(), ROOT) == [], (
        "identité civile ou lien personnel : utiliser le pseudo Mvth1s"
    )


def test_garde_fou_insensible_a_la_casse(tmp_path: Path) -> None:
    page = tmp_path / "page.html"
    page.write_text(f"<p>{FORBIDDEN[0].upper()}</p><a href='https://{FORBIDDEN[2]}x'>", "utf-8")
    clean = tmp_path / "ok.html"
    clean.write_text("<p>Mvth1s</p>", encoding="utf-8")
    assert offenders([page, clean], tmp_path) == [
        f"page.html : {FORBIDDEN[0]}",
        f"page.html : {FORBIDDEN[2]}",
    ]
