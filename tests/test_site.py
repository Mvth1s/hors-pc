"""Contrôles statiques du site : pages légales, pied de page, aucune ressource externe."""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

SITE = Path(__file__).parents[1] / "site"
PAGES = sorted(SITE.glob("*.html"))
LEGAL_LINKS = {"methodologie.html", "mentions-legales.html", "confidentialite.html"}


class Resources(HTMLParser):
    """Collecte les URL chargées automatiquement (pas les liens <a> de navigation)."""

    def __init__(self) -> None:
        super().__init__()
        self.loaded: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "a":
            self.links.append(a.get("href", ""))
        elif tag in ("script", "img", "iframe", "source", "audio", "video", "embed"):
            self.loaded += [a[k] for k in ("src", "srcset") if a.get(k)]
        elif tag == "link":
            self.loaded.append(a.get("href", ""))
        if "style" in a:
            self.loaded += re.findall(r"url\(([^)]+)\)", a["style"])


def parse(page: Path) -> Resources:
    parser = Resources()
    parser.feed(page.read_text(encoding="utf-8"))
    return parser


def is_external(url: str) -> bool:
    url = url.strip("'\" ")
    return bool(re.match(r"^(https?:)?//", url, re.I))


def test_pages_attendues() -> None:
    assert {p.name for p in PAGES} == {"index.html", *LEGAL_LINKS}


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_aucune_ressource_externe_dans_le_html(page: Path) -> None:
    external = [u for u in parse(page).loaded if is_external(u)]
    assert external == []


def test_aucune_ressource_externe_dans_css_et_js() -> None:
    css = (SITE / "style.css").read_text(encoding="utf-8")
    assert [u for u in re.findall(r"url\(([^)]+)\)", css) if is_external(u)] == []
    assert "@import" not in css
    for script in SITE.glob("*.js"):
        code = script.read_text(encoding="utf-8")
        assert not re.search(r"(fetch|import)\s*\(\s*['\"`]https?:", code), script.name


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_pied_de_page_lie_les_pages_legales(page: Path) -> None:
    links = set(parse(page).links)
    assert links >= LEGAL_LINKS
    assert "./" in links


@pytest.mark.parametrize("name", sorted(LEGAL_LINKS))
def test_pages_legales_datees(name: str) -> None:
    text = (SITE / name).read_text(encoding="utf-8")
    assert re.search(r"Dernière mise à jour : \d{2}/\d{2}/\d{4}", text)


def test_hebergeur_sans_donnee_inventee() -> None:
    text = (SITE / "mentions-legales.html").read_text(encoding="utf-8")
    assert "GitHub, Inc." in text and "88 Colin P. Kelly Jr. St." in text
    assert "docs.github.com/en/site-policy/privacy-policies" in text  # source citée


def test_adresse_de_contact_renseignee() -> None:
    """TODO bloquant : échoue tant que l'adresse de contact de l'éditeur manque."""
    text = (SITE / "mentions-legales.html").read_text(encoding="utf-8")
    assert "TODO-CONTACT" not in text, "adresse de contact à renseigner dans mentions-legales.html"
    assert "[adresse de contact à renseigner]" not in text
