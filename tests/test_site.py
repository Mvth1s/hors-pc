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
    assert len(summaries) == 11
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


def test_pages_de_jeux_sans_iframe_ni_ressource_youtube() -> None:
    pages = [
        p
        for p in sorted(SITE.glob("jeux/*/index.html"))
        if 'http-equiv="refresh"' not in p.read_text(encoding="utf-8")
    ]
    assert pages
    for page in pages:
        text = page.read_text(encoding="utf-8")
        assert "<iframe" not in text, page.parent.name
        external = [u for u in parse(page).loaded if is_external(u)]
        assert external == [], page.parent.name
        assert '<script src="../../game.js" defer></script>' in text


# --- Fichiers locaux référencés ---------------------------------------------------------------

SITE_URL = "https://mvth1s.github.io/hors-pc/"


def local_references(page: Path) -> set[str]:
    """Cibles locales d'une page : src, href, og:image et twitter:image (URL du site ramenée à
    un chemin local), sans les liens externes, ancres, mailto ni paramètres."""
    text = page.read_text(encoding="utf-8")
    refs = re.findall(r'\b(?:src|href)="([^"]+)"', text)
    refs += re.findall(r'<meta (?:property|name)="(?:og|twitter):image" content="([^"]+)"', text)
    found = set()
    for ref in map(html.unescape, refs):
        if ref.startswith(SITE_URL):
            target = SITE / ref.removeprefix(SITE_URL)
        elif re.match(r"^(?:[a-z]+:|//|#)", ref):
            continue  # externe, mailto, ancre
        else:
            target = page.parent / ref
        path = target.as_posix().split("#")[0].split("?")[0]
        if path.endswith("/") or Path(path).is_dir():
            path = path.rstrip("/") + "/index.html"
        found.add(path)
    return found


ALL_PAGES = sorted(SITE.glob("*.html")) + sorted(SITE.glob("jeux/*/index.html"))


@pytest.mark.parametrize("page", ALL_PAGES, ids=lambda p: str(p.relative_to(SITE)))
def test_fichiers_references_presents(page: Path) -> None:
    missing = [ref for ref in sorted(local_references(page)) if not Path(ref).is_file()]
    assert missing == [], missing


def test_og_image_et_polices_presentes() -> None:
    from horspc.render import OG_IMAGE

    assert (SITE / OG_IMAGE).is_file()
    index = (SITE / "index.html").read_text(encoding="utf-8")
    assert f'<meta property="og:image" content="{SITE_URL}{OG_IMAGE}">' in index
    css = (SITE / "style.css").read_text(encoding="utf-8")
    fonts = re.findall(r"url\(([^)]+)\)", css)
    assert fonts and all((SITE / f.strip("'\"")).is_file() for f in fonts)


def test_attribution_igdb_dans_le_pied_de_page_de_chaque_page() -> None:
    pages = [
        p
        for p in sorted(SITE.glob("*.html")) + sorted(SITE.glob("jeux/*/index.html"))
        if 'http-equiv="refresh"' not in p.read_text(encoding="utf-8")
    ]
    for page in pages:
        foot = footer_of(page)
        assert "Données de jeux et jaquettes via" in foot, page
        assert '<a href="https://www.igdb.com" target="_blank" rel="noopener noreferrer">' in foot
