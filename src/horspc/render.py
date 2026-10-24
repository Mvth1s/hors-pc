"""Rendu statique du site : fiches dans index.html, une page par jeu, SEO et sitemap.xml.

index.html et les pages racine restent des gabarits écrits à la main : le build ne remplace
que les zones balisées <!--horspc:nom--> ... <!--/horspc:nom-->.
"""

import html
import json
import re
import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from horspc.models import DataFile, GameOut
from horspc.prices.selection import RECENT_DAYS

SITE_URL = "https://mvth1s.github.io/hors-pc/"
OG_IMAGE = "og-image.png"  # 1200 x 630, image de partage générique
OG_IMAGE_SIZE = (1200, 630)
COVER_SIZE = (264, 352)  # t_cover_big IGDB
SCREEN_SIZE = (940, 529)  # t_screenshot_big IGDB (dimensions constatées des fichiers servis)
PLAY_ICON = (
    '<svg width="28" height="28" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
    '<path d="M8 5.5v13a1 1 0 0 0 1.5.86l10.5-6.5a1 1 0 0 0 0-1.72L9.5 4.64A1 1 0 0 0 8 5.5z"/>'
    "</svg>"
)
GAMES_DIR = "jeux"
AUTHOR = {
    "@type": "Person",
    "name": "Mvth1s",
    "alternateName": "Mathis Aguado",
    "url": "https://mathisaguado.vercel.app",
    "sameAs": [
        "https://www.linkedin.com/in/mathis-aguado",
        "https://github.com/Mvth1s",
    ],
}

CONSOLES = {"ps5": ("PS5", "ps"), "switch": ("Switch", "nin"), "switch2": ("Switch 2", "nin")}
SCHEMA_PLATFORMS = {
    "ps5": "PlayStation 5",
    "switch": "Nintendo Switch",
    "switch2": "Nintendo Switch 2",
}
STATUSES = {
    "none": ("Exclusif console", "status ex"),
    "announced": ("Portage PC annoncé", "status port"),
}
FORMATS = {"physical": "Physique", "digital": "Démat"}
STOPWORDS = {"the", "of", "a", "an", "le", "la", "les", "de", "du", "des", "et"}
ARROW = (
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M7 17 17 7M8 7h9v9"></path></svg>'
)


def render_site(data: DataFile, site_dir: Path) -> None:
    """Écrit le catalogue statique, les pages de jeux, les métadonnées SEO et le sitemap."""
    games = [g for g in data.games if g.pc_status != "released"]
    generated = data.generated_at.date()
    previous_lastmod = _read_sitemap(site_dir / "sitemap.xml")
    lastmod: dict[str, date] = {SITE_URL: generated}

    for page in sorted(site_dir.glob("*.html")):
        text = page.read_text(encoding="utf-8")
        text = _set_footer_date(text, generated)
        url = SITE_URL if page.name == "index.html" else SITE_URL + page.name
        if page.name == "index.html":
            text = _replace(text, "head", _index_head(text, games))
            text = _replace(text, "updated", _updated_line(data))
            text = _replace(text, "count", _count(len(games)))
            text = _replace(text, "grid", "\n".join(_card(g, i) for i, g in enumerate(games)))
        else:
            text = _replace(text, "head", _page_head(text, url))
            lastmod[url] = _visible_update(text) or generated
        page.write_text(external_links(text), encoding="utf-8")

    template = _game_template(site_dir)
    games_root = site_dir / GAMES_DIR
    for game in games:
        url = f"{SITE_URL}{GAMES_DIR}/{game.slug}/"
        path = games_root / game.slug / "index.html"
        page = external_links(_game_page(template, game, url, generated))
        unchanged = path.exists() and _main(path.read_text(encoding="utf-8")) == _main(page)
        lastmod[url] = previous_lastmod.get(url, generated) if unchanged else generated
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(page, encoding="utf-8")
    slugs = {g.slug for g in games}
    if games_root.is_dir():
        for directory in games_root.iterdir():
            if directory.is_dir() and directory.name not in slugs:
                shutil.rmtree(directory)

    (site_dir / "sitemap.xml").write_text(_sitemap(lastmod), encoding="utf-8")


# --- Liens externes ---------------------------------------------------------------

NEW_TAB = '<span class="visually-hidden"> (nouvel onglet)</span>'
EXT_ICON = '<span class="ext-icon" aria-hidden="true">\u2197</span>'
_LINK = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S)


def external_links(text: str) -> str:
    """Liens http(s) : nouvel onglet, rel noopener noreferrer, icône ↗ et mention pour lecteurs
    d'écran. Idempotent ; les liens mailto: et internes ne sont pas modifiés."""

    def fix(match: re.Match[str]) -> str:
        attrs, inner = match.group(1), match.group(2)
        href = re.search(r'href="([^"]*)"', attrs)
        if not href or not re.match(r"https?://", href.group(1)):
            return match.group(0)
        rel = re.search(r'rel="([^"]*)"', attrs)
        tokens = [
            t for t in (rel.group(1).split() if rel else []) if t not in ("noopener", "noreferrer")
        ]
        attrs = re.sub(r'\s(?:target|rel)="[^"]*"', "", attrs)
        attrs += f' target="_blank" rel="{" ".join([*tokens, "noopener", "noreferrer"])}"'
        label = re.search(r'aria-label="([^"]*)"', attrs)
        if label:  # lien-icône : la mention passe dans l'aria-label
            if not label.group(1).endswith("(nouvel onglet)"):
                attrs = attrs.replace(
                    label.group(0), f'aria-label="{label.group(1)} (nouvel onglet)"'
                )
            return f"<a{attrs}>{inner}</a>"
        if NEW_TAB not in inner:
            inner = re.sub(r"\s*\u2197\s*$", "", inner)  # flèche texte remplacée par l'icône
            has_icon = "<svg" in inner or EXT_ICON in inner
            inner = inner + ("" if has_icon else EXT_ICON) + NEW_TAB
        return f"<a{attrs}>{inner}</a>"

    return _LINK.sub(fix, text)


# --- Zones balisées -------------------------------------------------------------


def _replace(text: str, name: str, content: str) -> str:
    pattern = re.compile(rf"(<!--horspc:{name}-->).*?(<!--/horspc:{name}-->)", re.S)
    if not pattern.search(text):
        raise ValueError(f"zone <!--horspc:{name}--> absente du gabarit")
    return pattern.sub(lambda m: f"{m.group(1)}\n{content}\n{m.group(2)}", text, count=1)


def _set_footer_date(text: str, day: date) -> str:
    """Date du catalogue dans le pied de page commun (identique sur toutes les pages)."""
    return re.sub(
        r"<span data-foot-updated[^>]*>.*?</span></span>",
        f"<span data-foot-updated> · catalogue généré le <span data-foot-date>{_fr(day)}"
        "</span></span>",
        text,
        count=1,
        flags=re.S,
    )


# --- En-têtes SEO -----------------------------------------------------------------


def _meta(text: str) -> tuple[str, str]:
    title = html.unescape(re.search(r"<title>(.*?)</title>", text, re.S).group(1))
    description = html.unescape(
        re.search(r'<meta name="description" content="([^"]*)">', text).group(1)
    )
    return title, description


def _social(title: str, description: str, url: str, image: str | None, kind: str) -> str:
    image_url = SITE_URL + (image or OG_IMAGE)
    width, height = COVER_SIZE if image else OG_IMAGE_SIZE
    card = "summary" if image else "summary_large_image"
    tags = [
        f'<link rel="canonical" href="{_a(url)}">',
        f'<meta property="og:type" content="{kind}">',
        '<meta property="og:site_name" content="HorsPC">',
        '<meta property="og:locale" content="fr_FR">',
        f'<meta property="og:title" content="{_a(title)}">',
        f'<meta property="og:description" content="{_a(description)}">',
        f'<meta property="og:url" content="{_a(url)}">',
        f'<meta property="og:image" content="{_a(image_url)}">',
        f'<meta property="og:image:width" content="{width}">',
        f'<meta property="og:image:height" content="{height}">',
        f'<meta name="twitter:card" content="{card}">',
        f'<meta name="twitter:title" content="{_a(title)}">',
        f'<meta name="twitter:description" content="{_a(description)}">',
        f'<meta name="twitter:image" content="{_a(image_url)}">',
    ]
    return "\n".join(tags)


def _jsonld(data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, indent=2).replace("</", "<\\/")
    return f'<script type="application/ld+json">\n{payload}\n</script>'


def _page_head(text: str, url: str) -> str:
    title, description = _meta(text)
    return _social(title, description, url, None, "website")


def _index_head(text: str, games: list[GameOut]) -> str:
    title, description = _meta(text)
    graph = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebSite",
                "@id": SITE_URL + "#site",
                "name": "HorsPC",
                "url": SITE_URL,
                "description": description,
                "inLanguage": "fr-FR",
                "publisher": {"@id": SITE_URL + "#editeur"},
            },
            {**AUTHOR, "@id": SITE_URL + "#editeur"},
            {
                "@type": "ItemList",
                "name": "Exclusivités console absentes sur PC",
                "numberOfItems": len(games),
                "itemListElement": [
                    {
                        "@type": "ListItem",
                        "position": i,
                        "url": f"{SITE_URL}{GAMES_DIR}/{g.slug}/",
                        "name": g.title,
                    }
                    for i, g in enumerate(games, 1)
                ],
            },
        ],
    }
    return _social(title, description, SITE_URL, None, "website") + "\n" + _jsonld(graph)


# --- Catalogue ----------------------------------------------------------------------


def _updated_line(data: DataFile) -> str:
    stamp = data.generated_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f'<p class="updated" id="updated" data-generated-at="{stamp}">'
        '<span class="live" id="live"></span><span>Catalogue mis à jour le '
        f'<strong id="updated-date">{_fr(data.generated_at.date())}</strong></span></p>'
    )


def _count(n: int) -> str:
    return f'<strong id="count">{n} {"jeux" if n > 1 else "jeu"}</strong>'


def _initials(title: str) -> str:
    main = title.split(":")[-1] if ":" in title else title
    words = [w for w in re.split(r"[\s\-]+", main) if w and w.lower() not in STOPWORDS]
    if len(words) == 1:
        return words[0][:2].upper()
    return "".join(w[0] for w in words[:2]).upper()


def _consoles(game: GameOut) -> str:
    return "".join(
        f'<span class="console {CONSOLES[p][1]}">{CONSOLES[p][0]}</span>' for p in game.platforms
    )


def _cover(game: GameOut, color: int, prefix: str, lazy: bool) -> str:
    if game.cover_url:
        loading = ' loading="lazy"' if lazy else ""
        return (
            '<div class="cover has-img">'
            f'<img src="{prefix}{_a(game.cover_url)}" width="{COVER_SIZE[0]}" '
            f'height="{COVER_SIZE[1]}"{loading} decoding="async" '
            f'alt="Jaquette de {_a(game.title)}">'
            f'<span class="covers">{_consoles(game)}</span></div>'
        )
    return (
        f'<div class="cover cv-{color % 8}" role="img" aria-label="Visuel provisoire de '
        f'{_a(game.title)}"><span class="covers">{_consoles(game)}</span>'
        f'<span class="initials" aria-hidden="true">{_e(_initials(game.title))}</span>'
        '<span class="cover-tag">Visuel provisoire</span></div>'
    )


def _price_block(game: GameOut, with_cta: bool) -> str:
    offer = game.offer
    if offer is None:
        return '<div class="price-block"><p class="no-price">Pas encore de prix relevé.</p></div>'
    launch = (
        f'<span class="was">Lancement <s>{_euros(game.launch_price_cents)}</s></span>'
        if game.launch_price_cents
        else ""
    )
    discount = (
        f'<span class="discount">-{offer.discount_pct}&nbsp;%</span>'
        if offer.discount_pct and offer.discount_pct > 0
        else ""
    )
    if offer.shipping_cents is None:
        shipping = ""
    elif offer.shipping_cents == 0:
        shipping = " · port offert"
    else:
        shipping = f" · + {_euros(offer.shipping_cents)} de port"
    where = f"{_e(offer.merchant)}, relevé le {_fr(offer.observed_at)}{shipping}"
    source = (
        f'<p class="source old"><span class="warn-dot" aria-hidden="true"></span>'
        f"<span>{where}. Prix à vérifier.</span></p>"
        if offer.stale
        else f'<p class="source">{where}</p>'
    )
    cta = (
        f'<a class="cta" href="{_a(str(offer.url))}" rel="noopener nofollow" '
        f'target="_blank">Voir l\'offre{ARROW}</a>'
        if with_cta
        else ""
    )
    whole, cents = divmod(offer.price_cents, 100)
    big = f"{_thousands(whole)}<small>,{cents:02d}&nbsp;€</small>"
    return (
        f'<div class="price-block">{launch}<div class="price-row">'
        f'<span class="price">{big}</span>{discount}</div></div>{source}{cta}'
    )


def _sort_attrs(game: GameOut) -> str:
    offer = game.offer
    price = "" if offer is None else str(offer.price_cents + (offer.shipping_cents or 0))
    discount = "" if offer is None or offer.discount_pct is None else str(offer.discount_pct)
    release = game.release_date.isoformat() if game.release_date else ""
    return (
        f'data-platforms="{" ".join(game.platforms)}" data-status="{game.pc_status}" '
        f'data-price="{price}" data-discount="{discount}" data-release="{release}" '
        f'data-title="{_a(game.title)}"'
    )


def _card(game: GameOut, index: int) -> str:
    label, cls = STATUSES[game.pc_status]
    release = (
        f"Sortie le {_fr(game.release_date)}" if game.release_date else "Date de sortie inconnue"
    )
    fmt = FORMATS[game.offer.format] if game.offer else ""
    return (
        f'<article class="card" {_sort_attrs(game)}>'
        f"{_cover(game, index, '', lazy=index >= 4)}"
        f'<div class="body"><span class="{cls}">{label}</span>'
        f'<h2 class="title"><a href="{GAMES_DIR}/{game.slug}/">{_e(game.title)}</a></h2>'
        f'<p class="sub"><span>{release}</span><span>{fmt}</span></p>'
        f"{_price_block(game, with_cta=True)}</div></article>"
    )


# --- Pages de jeux ---------------------------------------------------------------------


def _game_template(site_dir: Path) -> str:
    """Gabarit d'une page de jeu : celui des pages légales, liens relatifs remontés de 2 niveaux."""
    text = (site_dir / "methodologie.html").read_text(encoding="utf-8")
    text = re.sub(r"<main\b.*?</main>", "<!--horspc:game-main-->", text, count=1, flags=re.S)
    text = re.sub(r"<title>.*?</title>\n", "", text, count=1, flags=re.S)
    text = re.sub(r'<meta name="description" content="[^"]*">\n', "", text, count=1)
    text = _replace(text, "head", "<!--horspc:game-head-->")

    def up(match: re.Match[str]) -> str:
        attr, value = match.group(1), match.group(2)
        if re.match(r"^(https?:|mailto:|#|&#)", value):
            return match.group(0)
        return f'{attr}="../../{value}"'

    return re.sub(r'\b(href|src)="([^"]*)"', up, text)


def _game_page(template: str, game: GameOut, url: str, generated: date) -> str:
    label, cls = STATUSES[game.pc_status]
    platforms = ", ".join(CONSOLES[p][0] for p in game.platforms)
    title = f"{game.title} ({platforms}) : prix et statut PC · HorsPC"
    description = f"{game.title} sur {platforms}, " + (
        "pas encore sorti sur PC (portage annoncé)"
        if game.pc_status == "announced"
        else "jamais sorti sur PC"
    )
    if game.offer:
        description += (
            f". Meilleur prix relevé : {_euros(game.offer.price_cents)} chez "
            f"{game.offer.merchant} le {_fr(game.offer.observed_at)}."
        )
    else:
        description += ". Prix pas encore relevé."
    cover = game.cover_url
    head = (
        f"<title>{_e(title)}</title>\n"
        f'<meta name="description" content="{_a(description)}">\n'
        + _social(title, description, url, cover, "website")
        + "\n"
        + _jsonld(_videogame(game, url))
        + '\n<script src="../../game.js" defer></script>'
    )
    facts = [
        ("Plateformes", platforms),
        ("Sortie", _fr(game.release_date) if game.release_date else "inconnue"),
        ("Statut PC", label),
    ]
    if game.developers:
        facts.append(
            ("Studio" + ("s" if len(game.developers) > 1 else ""), _e(", ".join(game.developers)))
        )
    if game.publishers:
        facts.append(
            ("Éditeur" + ("s" if len(game.publishers) > 1 else ""), _e(", ".join(game.publishers)))
        )
    if game.launch_price_cents:
        facts.append(("Prix de lancement", _euros(game.launch_price_cents)))
    if game.igdb_url:
        facts.append(
            ("Fiche IGDB", f'<a href="{_a(str(game.igdb_url))}" rel="noopener">IGDB.com</a>')
        )
    dl = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in facts)
    main = f"""<main class="wrap game-page">
<nav class="breadcrumb" aria-label="Fil d'Ariane">
  <a href="../../">Catalogue</a> \u203a {_e(game.title)}
</nav>
<article class="game">
  {_cover(game, 0, "../../", lazy=False)}
  <div class="game-body">
    <span class="{cls}">{label}</span>
    <h1>{_e(game.title)}</h1>
    <h2>Offre retenue</h2>
    {_price_block(game, with_cta=True)}
    <h2>Informations</h2>
    <dl class="facts">{dl}</dl>
    <p><a class="back" href="../../">← Retour au catalogue</a></p>
  </div>
</article>
{_summary_section(game)}{_trailer_section(game)}{_gallery_section(game)}</main>"""
    page = template.replace("<!--horspc:game-head-->", head)
    page = page.replace("<!--horspc:game-main-->", main)
    return _set_footer_date(page, generated)


def _summary_section(game: GameOut) -> str:
    if not game.summary:
        return ""
    paragraphs = "".join(f"<p>{_e(p.strip())}</p>" for p in game.summary.split("\n") if p.strip())
    if game.summary_source == "igdb":
        body = f'<div lang="en">{paragraphs}</div><p class="game-note">Résumé IGDB (en anglais)</p>'
    else:
        body = paragraphs
    return (
        '<section class="game-section" aria-labelledby="resume">'
        f'<h2 id="resume">Résumé</h2>{body}</section>\n'
    )


def _trailer_section(game: GameOut) -> str:
    """Façade : image et bouton ; l'iframe youtube-nocookie.com n'est créée qu'au clic."""
    if not game.trailer_youtube_id:
        return ""
    if game.screenshots:
        poster = f"../../{game.screenshots[0].url}"
        width, height = SCREEN_SIZE
    elif game.cover_url:
        poster = f"../../{game.cover_url}"
        width, height = COVER_SIZE
    else:
        poster = None
    image = (
        f'<img src="{_a(poster)}" width="{width}" height="{height}" loading="lazy" '
        'decoding="async" alt="">'
        if poster
        else ""
    )
    video = game.trailer_youtube_id
    return (
        '<section class="game-section" aria-labelledby="bande-annonce">'
        '<h2 id="bande-annonce">Bande-annonce</h2>'
        f'<div class="trailer" data-youtube-id="{_a(video)}" data-title="{_a(game.title)}">'
        f"{image}"
        f'<button type="button" class="trailer-play" aria-label="Lire la bande-annonce de '
        f'{_a(game.title)} (charge YouTube)">{PLAY_ICON}</button>'
        "</div>"
        '<p class="game-note">La lecture charge YouTube (Google), qui peut déposer des cookies.</p>'
        f'<noscript><p><a href="https://www.youtube.com/watch?v={_a(video)}">'
        "Voir la bande-annonce sur YouTube</a></p></noscript>"
        "</section>\n"
    )


def _gallery_section(game: GameOut) -> str:
    if not game.screenshots:
        return ""
    width, height = SCREEN_SIZE
    figures = "".join(
        f'<figure><img src="../../{_a(shot.url)}" width="{width}" height="{height}" '
        f'loading="lazy" decoding="async" alt="Capture de {_a(game.title)} ({i})"></figure>'
        for i, shot in enumerate(game.screenshots, 1)
    )
    return (
        '<section class="game-section" aria-labelledby="captures">'
        f'<h2 id="captures">Captures</h2><div class="gallery">{figures}</div>'
        '<p class="game-note">Captures officielles via IGDB.com.</p></section>\n'
    )


def _videogame(game: GameOut, url: str) -> dict[str, Any]:
    data: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "VideoGame",
        "name": game.title,
        "url": url,
        "image": SITE_URL + (game.cover_url or OG_IMAGE),
        "gamePlatform": [SCHEMA_PLATFORMS[p] for p in game.platforms],
        "inLanguage": "fr-FR",
    }
    if game.release_date:
        data["datePublished"] = game.release_date.isoformat()
    if game.igdb_url:
        data["sameAs"] = str(game.igdb_url)
    if game.summary:
        data["description"] = game.summary
    if game.developers:
        data["author"] = [{"@type": "Organization", "name": n} for n in game.developers]
    if game.publishers:
        data["publisher"] = [{"@type": "Organization", "name": n} for n in game.publishers]
    offer = game.offer
    # Une offre non vérifiée depuis 30 jours n'est plus publiée : son prix n'est plus garanti.
    if offer and not offer.stale:
        data["offers"] = {
            "@type": "Offer",
            "price": f"{offer.price_cents / 100:.2f}",
            "priceCurrency": "EUR",
            "priceValidUntil": (offer.observed_at + timedelta(days=RECENT_DAYS)).isoformat(),
            # Relevé manuel d'une offre disponible ; le stock n'est pas suivi au-delà.
            "availability": "https://schema.org/InStock",
            "itemCondition": "https://schema.org/NewCondition",
            "url": str(offer.url),
            "seller": {"@type": "Organization", "name": offer.merchant},
        }
    return data


# --- Sitemap -------------------------------------------------------------------------------


def _main(text: str) -> str:
    match = re.search(r"<main\b.*?</main>", text, re.S)
    return match.group(0) if match else text


def _visible_update(text: str) -> date | None:
    match = re.search(r"Dernière mise à jour : (\d{2})/(\d{2})/(\d{4})", text)
    return date(int(match[3]), int(match[2]), int(match[1])) if match else None


def _read_sitemap(path: Path) -> dict[str, date]:
    if not path.exists():
        return {}
    entries = re.findall(
        r"<loc>(.*?)</loc>\s*<lastmod>(\d{4}-\d{2}-\d{2})</lastmod>", path.read_text("utf-8")
    )
    return {html.unescape(loc): date.fromisoformat(day) for loc, day in entries}


def _sitemap(lastmod: dict[str, date]) -> str:
    urls = "".join(
        f"  <url>\n    <loc>{xml_escape(url)}</loc>\n    <lastmod>{day.isoformat()}</lastmod>\n"
        "  </url>\n"
        for url, day in sorted(lastmod.items())
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}</urlset>\n"
    )


# --- Utilitaires -----------------------------------------------------------------------------


def _e(value: object) -> str:
    return html.escape(str(value), quote=False)


def _a(value: object) -> str:
    return html.escape(str(value), quote=True)


def _fr(day: date) -> str:
    return day.strftime("%d/%m/%Y")


def _thousands(n: int) -> str:
    return f"{n:,}".replace(",", "\u202f")  # espace fine insécable


def _euros(cents: int) -> str:
    whole, rest = divmod(cents, 100)
    return f"{_thousands(whole)},{rest:02d}\u00a0€"
