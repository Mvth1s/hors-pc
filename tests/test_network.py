"""Garde-fou réseau : une requête vers un nouvel hôte passe par le Fetcher (robots.txt vérifié
avant la requête), jamais par httpx ou HttpClient appelé directement."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = sorted([*(ROOT / "src").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")])

# Client bas niveau : seul http.py l'instancie.
DIRECT = re.compile(
    r"\bhttpx\.(?:get|post|put|patch|delete|head|request|stream|Client|AsyncClient)\b"
    r"|\burllib\.request\b|\bimport requests\b|\bhttp\.client\b"
    r"|\bsocket\.(?:socket|create_connection)\b"
)
# Appels directs d'un HttpClient (`http.request(...)`, `self._http.post(...)`).
CALL = re.compile(r"\bhttp\.(?:request|post)\(")
# Modules autorisés à appeler HttpClient sans Fetcher, et pourquoi.
ALLOWED_CALLERS = {
    "src/horspc/http.py": "le client lui-même",
    "src/horspc/robots.py": "lecture de robots.txt, préalable à toute requête du Fetcher",
    "src/horspc/prices/fetch.py": "le Fetcher",
    "src/horspc/igdb.py": "API IGDB et jeton Twitch (api.igdb.com, id.twitch.tv)",
    "src/horspc/covers.py": "images IGDB (images.igdb.com)",
    "src/horspc/audit.py": "audit mensuel : robots.txt, TDMRep et CGU des enseignes",
}


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_seul_http_py_utilise_un_client_bas_niveau() -> None:
    offenders = [
        f"{_rel(p)}:{n}"
        for p in SOURCES
        if _rel(p) != "src/horspc/http.py"
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if DIRECT.search(line)
    ]
    assert offenders == [], "requête réseau hors de HttpClient : passer par le Fetcher"


def test_nouvel_hote_par_le_fetcher() -> None:
    offenders = sorted(
        _rel(p)
        for p in SOURCES
        if _rel(p) not in ALLOWED_CALLERS and CALL.search(p.read_text(encoding="utf-8"))
    )
    assert offenders == [], (
        "HttpClient appelé directement : un nouvel hôte passe par Fetcher.get (robots.txt "
        "vérifié avant la requête) ; sinon justifier l'exception dans ALLOWED_CALLERS"
    )
