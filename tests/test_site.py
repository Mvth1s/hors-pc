"""Contrôles statiques du site : pages légales, pied de page, aucune ressource externe."""

import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

SITE = Path(__file__).parents[1] / "site"
PAGES = sorted(SITE.glob("*.html"))
# rel de <link> qui déclenchent un téléchargement (canonical, alternate… n'en déclenchent pas).
LOADED_RELS = {"stylesheet", "preload", "icon", "apple-touch-icon", "modulepreload", "manifest"}
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
        elif tag == "link" and set(a.get("rel", "").split()) & LOADED_RELS:
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
    assert {p.name for p in PAGES} == {"index.html", "faq.html", *LEGAL_LINKS}


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


def test_contact_encode_en_entites() -> None:
    text = (SITE / "mentions-legales.html").read_text(encoding="utf-8")
    assert "mag.d3v+github@pm.me" not in text  # pas en clair dans la source
    assert "mag.d3v+github@pm.me" in html.unescape(text)
    assert "mailto:mag.d3v+github@pm.me" in html.unescape(text)
    assert "https://support.github.com/contact" in text


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_favicons_lies_et_presents(page: Path) -> None:
    text = page.read_text(encoding="utf-8")
    for name in ("favicon.svg", "favicon-32.png", "apple-touch-icon.png"):
        assert f'href="{name}"' in text
        assert (SITE / name).is_file()


def _footer(page: Path) -> str:
    blocks = re.findall(r"<footer\b.*?</footer>", page.read_text(encoding="utf-8"), re.S)
    assert len(blocks) == 1, f"{page.name} : {len(blocks)} bloc(s) <footer>"
    return blocks[0]


def test_footer_identique_dans_toutes_les_pages() -> None:
    reference = _footer(SITE / "index.html")
    divergent = [p.name for p in PAGES if _footer(p) != reference]
    assert not divergent, f"pied de page différent de index.html dans : {', '.join(divergent)}"


def test_aucune_page_ne_charge_data_json() -> None:
    """Le catalogue et les dates sont rendus au build : data.json n'est plus lu par le site."""
    for script in SITE.glob("*.js"):
        assert "data.json" not in script.read_text(encoding="utf-8"), script.name
    assert not (SITE / "footer.js").exists()


def test_faq_jsonld_coherent_avec_les_questions() -> None:
    text = (SITE / "faq.html").read_text(encoding="utf-8")
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', text, re.S)
    assert len(blocks) == 1
    data = json.loads(blocks[0])
    assert data["@context"] == "https://schema.org" and data["@type"] == "FAQPage"
    summaries = [html.unescape(q) for q in re.findall(r"<summary>(.*?)</summary>", text)]
    assert [q["name"] for q in data["mainEntity"]] == summaries
    assert len(summaries) == 9
    for question in data["mainEntity"]:
        assert question["@type"] == "Question"
        assert question["acceptedAnswer"]["@type"] == "Answer"
        assert question["acceptedAnswer"]["text"].strip()


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_consentement_sur_chaque_page_sans_snippet_google(page: Path) -> None:
    text = page.read_text(encoding="utf-8")
    assert '<script src="consent.js" defer></script>' in text
    # Le snippet standard charge gtag.js au chargement : interdit, seul consent.js l'injecte.
    assert "googletagmanager" not in text and "gtag(" not in text
    assert "Gérer les cookies" in footer_of(page)


def footer_of(page: Path) -> str:
    return re.search(r"<footer\b.*?</footer>", page.read_text(encoding="utf-8"), re.S).group(0)
