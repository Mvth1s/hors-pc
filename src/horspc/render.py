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

from horspc.models import DataFile, EditionOut, GameOut, OfferOut
from horspc.prices.selection import RECENT_DAYS, edition_offer_key

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

CONSOLES = {
    "ps5": ("PS5", "ps"),
    "switch": ("Switch", "nin"),
    "switch2": ("Switch 2", "nin"),
    "xbox_series": ("Xbox Series", "xb"),
}
SCHEMA_PLATFORMS = {
    "ps5": "PlayStation 5",
    "switch": "Nintendo Switch",
    "switch2": "Nintendo Switch 2",
    "xbox_series": "Xbox Series X|S",
}
STATUSES = {
    "none": ("Exclusif console", "status ex"),
    "announced": ("Portage PC annoncé", "status port"),
}
FORMATS = {
    "physical": "Physique",
    "code_box": "Boîte avec code de téléchargement",
    "digital": "Démat",
}
# Libellés courts des cartes (la page de jeu garde le libellé complet).
FORMATS_SHORT = FORMATS | {"code_box": "Code dans la boîte"}
# Filtres console du catalogue, comme PLATFORM_FILTERS dans app.js.
FILTER_GROUPS: dict[str, tuple[str, ...]] = {
    "ps5": ("ps5",),
    "switch2": ("switch", "switch2"),
    "xbox_series": ("xbox_series",),
}
STOPWORDS = {"the", "of", "a", "an", "le", "la", "les", "de", "du", "des", "et"}
ARROW = (
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M7 17 17 7M8 7h9v9"></path></svg>'
)


def render_site(data: DataFile, site_dir: Path, redirects: dict[str, str] | None = None) -> None:
    """Écrit le catalogue statique, les pages de jeux, les métadonnées SEO et le sitemap.

    `redirects` : ancien slug → slug actuel (jeux fusionnés) ; l'ancienne page devient une
    redirection, hors sitemap.
    """
    redirects = {
        old: new for old, new in (redirects or {}).items() if new in {g.slug for g in data.games}
    }
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
            cards = (_card(g, i, generated) for i, g in enumerate(games))
            text = _replace(text, "grid", "\n".join(cards))
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
    for old, new in redirects.items():
        path = games_root / old / "index.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_redirect_page(f"{SITE_URL}{GAMES_DIR}/{new}/"), encoding="utf-8")
    slugs = {g.slug for g in games} | set(redirects)
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
                "name": "Jeux console absents du PC",
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


# Libellés courts des badges sur les cartes mobiles (le nom complet reste lu à l'écran).
CONSOLES_SHORT = {"xbox_series": "Xbox"}


def _consoles(game: GameOut) -> str:
    badges = []
    for p in game.platforms:
        name, cls = CONSOLES[p]
        short = CONSOLES_SHORT.get(p)
        label = (
            f'<span class="c-long">{name}</span><span class="c-short" aria-hidden="true">'
            f"{short}</span>"
            if short
            else name
        )
        badges.append(f'<span class="console {cls}">{label}</span>')
    return "".join(badges)


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


def _price_block(game: GameOut, with_cta: bool, offer: OfferOut | None = None) -> str:
    """Bloc prix d'une offre (par défaut la meilleure toutes éditions).

    Pour un jeu à plusieurs éditions, la console de l'offre est précisée (« sur PS5 »).
    """
    offer = offer if offer is not None else game.best_offer
    if offer is None:
        return _no_price_block(game.editions)
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
    where = f"{_e(merchant_label(offer))}, relevé le {_fr(offer.observed_at)}{shipping}"
    if len(game.editions) > 1:
        where = f"Sur {CONSOLES[offer.platform][0]} · {where}"
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


def no_price_text(editions: list[EditionOut]) -> str:
    """« En rupture chez E.Leclerc, constaté le JJ/MM/AAAA » si une fiche est en rupture, sinon
    « Pas encore de prix relevé » (constat le plus récent parmi ces éditions)."""
    ruptures = [e.out_of_stock for e in editions if e.out_of_stock is not None]
    if not ruptures:
        return "Pas encore de prix relevé"
    latest = max(ruptures, key=lambda r: r.observed_at)
    return f"En rupture chez {_e(latest.merchant)}, constaté le {_fr(latest.observed_at)}"


def _no_price_block(editions: list[EditionOut]) -> str:
    cls = "no-price out" if any(e.out_of_stock for e in editions) else "no-price"
    return f'<div class="price-block"><p class="{cls}">{no_price_text(editions)}.</p></div>'


def merchant_label(offer: OfferOut) -> str:
    """« Boulanger », ou « ShopVite via Pixmania » pour un vendeur pro d'une place de marché."""
    return offer.merchant if offer.seller is None else f"{offer.seller} via {offer.merchant}"


def best_edition_offer(offers: list[OfferOut]) -> OfferOut | None:
    """Meilleure offre parmi des éditions (même ordre que best_offer au build)."""
    return min(
        offers,
        key=lambda o: edition_offer_key(
            o.stale,
            o.format,
            o.price_cents,
            o.shipping_cents,
            o.observed_at,
            o.merchant,
            o.platform,
        ),
        default=None,
    )


def filter_views(game: GameOut) -> dict[str, OfferOut | None]:
    """Offre affichée par filtre console, pour un jeu à plusieurs éditions (sinon vide)."""
    if len(game.editions) < 2:
        return {}
    views: dict[str, OfferOut | None] = {}
    for key, platforms in FILTER_GROUPS.items():
        editions = [e for e in game.editions if e.platform in platforms]
        if editions:
            views[key] = best_edition_offer([e.offer for e in editions if e.offer])
    return views


def _sort_values(offer: OfferOut | None) -> dict[str, Any]:
    return {
        "price": None if offer is None else offer.price_cents + (offer.shipping_cents or 0),
        "discount": None if offer is None else offer.discount_pct,
        "format": "" if offer is None else FORMATS_SHORT[offer.format],
    }


def _sort_attrs(game: GameOut) -> str:
    best = _sort_values(game.best_offer)
    price = "" if best["price"] is None else str(best["price"])
    discount = "" if best["discount"] is None else str(best["discount"])
    release = game.release_date.isoformat() if game.release_date else ""
    views = filter_views(game)
    # Prix, réduction et format par filtre console : app.js les utilise pour l'affichage et le tri.
    data_views = (
        f" data-views='{_a(json.dumps({k: _sort_values(o) for k, o in views.items()}))}'"
        if views
        else ""
    )
    return (
        f'data-platforms="{" ".join(game.platforms)}" data-status="{game.pc_status}" '
        f'data-price="{price}" data-discount="{discount}" data-release="{release}" '
        f'data-title="{_a(game.title)}"{data_views}'
    )


def both_formats(game: GameOut) -> bool:
    """« Physique et numérique » seulement si les deux preuves existent ; aucun autre badge.

    Par édition : numérique = store officiel ou Amazon Digital (IGDB) ; physique = offre
    physique neuve relevée, ou entrée Physical (IGDB) non contredite par les relevés récents.
    """
    return any(e.sold_digital and _physical_proof(e) for e in game.editions)


def _physical_proof(edition: EditionOut) -> bool:
    """Offre physique relevée, ou entrée Physical IGDB que les relevés ne contredisent pas.

    Si la meilleure offre récente (moins de 30 jours) est une boîte avec code, aucun relevé
    récent n'est physique (le physique passe avant à la sélection) : l'entrée IGDB est ignorée.
    """
    offer = edition.offer
    if offer is not None and offer.format == "physical":
        return True
    contradicted = offer is not None and not offer.stale and offer.format == "code_box"
    return edition.listed_physical and not contradicted


def is_upcoming(game: GameOut, today: date) -> bool:
    """« À venir » : date de sortie future (s'ajoute au statut PC, ne le remplace pas)."""
    return game.release_date is not None and game.release_date > today


def _badges(game: GameOut, today: date) -> str:
    """Statut PC, plus « Sortie le JJ/MM/AAAA » pour un jeu à venir.

    `data-release` permet à app.js de retirer le badge une fois la date passée, entre deux builds.
    """
    label, cls = STATUSES[game.pc_status]
    badges = f'<span class="{cls}">{label}</span>'
    if both_formats(game):
        badges += '<span class="status fmt">Physique et numérique</span>'
    if is_upcoming(game, today):
        badges += (
            f'<span class="status soon" data-release="{game.release_date.isoformat()}">'
            f'<span class="visually-hidden">À venir : </span>'
            f"Sortie le {_fr(game.release_date)}</span>"
        )
    return f'<div class="badges">{badges}</div>'


def _card(game: GameOut, index: int, today: date) -> str:
    if game.release_date is None:
        release = "Date de sortie inconnue"
    elif is_upcoming(game, today):
        release = "À venir"
    else:
        release = f"Sortie le {_fr(game.release_date)}"
    fmt = FORMATS_SHORT[game.best_offer.format] if game.best_offer else ""
    # Une vue de prix par filtre console (jeux à plusieurs éditions) ; sans JS, seule la
    # meilleure offre toutes éditions est visible.
    prices = f'<div class="price-view" data-view="all">{_price_block(game, True)}</div>'
    for key, offer in filter_views(game).items():
        group = [e for e in game.editions if e.platform in FILTER_GROUPS[key]]
        block = _price_block(game, True, offer) if offer else _no_price_block(group)
        prices += f'<div class="price-view" data-view="{key}" hidden>{block}</div>'
    return (
        f'<article class="card" {_sort_attrs(game)}>'
        f"{_cover(game, index, '', lazy=index >= 4)}"
        f'<div class="body">{_badges(game, today)}'
        f'<h2 class="title"><a href="{GAMES_DIR}/{game.slug}/">{_e(game.title)}</a></h2>'
        f'<p class="sub"><span>{release}</span><span class="fmt">{fmt}</span></p>'
        f"{prices}</div></article>"
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
    label = STATUSES[game.pc_status][0]
    platforms = ", ".join(CONSOLES[p][0] for p in game.platforms)
    title = f"{game.title} ({platforms}) : prix et statut PC · HorsPC"
    description = f"{game.title} sur {platforms}, " + (
        "pas encore sorti sur PC (portage annoncé)"
        if game.pc_status == "announced"
        else "jamais sorti sur PC"
    )
    if game.best_offer:
        on = f" sur {CONSOLES[game.best_offer.platform][0]}" if len(game.editions) > 1 else ""
        description += (
            f". Meilleur prix relevé{on} : {_euros(game.best_offer.price_cents)} chez "
            f"{merchant_label(game.best_offer)} le {_fr(game.best_offer.observed_at)}."
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
        + "\n"
        + _jsonld(_breadcrumb(game, url))
        + '\n<script src="../../game.js" defer></script>'
    )
    facts = [
        ("Plateformes", platforms),
        ("Sortie", _release_fact(game, generated)),
        ("Statut PC", label),
    ]
    if game.best_offer:
        facts.append(("Format de l'offre", FORMATS[game.best_offer.format]))
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
    # game.js remplace le lien par history.back() si le visiteur vient du catalogue (filtres
    # et position conservés).
    main = f"""<main class="wrap game-page">
<p class="game-top"><a class="back-btn" href="../../" data-back>← Retour au catalogue</a></p>
<article class="game">
  {_cover(game, 0, "../../", lazy=False)}
  <div class="game-body">
    {_badges(game, generated)}
    <h1>{_e(game.title)}</h1>
    <h2>Offre retenue</h2>
    {_price_block(game, with_cta=True)}
  </div>
  <section class="game-facts" aria-labelledby="informations">
    <h2 id="informations">Informations</h2>
    <dl class="facts">{dl}</dl>
  </section>
</article>
{_editions_section(game)}{_summary_section(game)}{_trailer_section(game)}{_gallery_section(game)}</main>"""
    page = template.replace("<!--horspc:game-head-->", head)
    page = page.replace("<!--horspc:game-main-->", main)
    return _set_footer_date(page, generated)


def _editions_section(game: GameOut) -> str:
    """Tableau « Prix par édition » (jeux à plusieurs éditions) : une ligne par console."""
    if len(game.editions) < 2:
        return ""
    rows = []
    for edition in game.editions:
        console = f'<th scope="row">{CONSOLES[edition.platform][0]}</th>'
        offer = edition.offer
        if offer is None:
            text = no_price_text([edition])
            rows.append(f'<tr>{console}<td class="no-offer" colspan="5">{text}</td></tr>')
            continue
        price = _euros(offer.price_cents)
        if offer.shipping_cents:
            price += f" <small>+ {_euros(offer.shipping_cents)} de port</small>"
        stale = ' <span class="old-tag">Prix à vérifier</span>' if offer.stale else ""
        link = (
            f'<a class="cta small" href="{_a(str(offer.url))}" rel="noopener nofollow" '
            f'target="_blank">Voir l\'offre{ARROW}</a>'
        )
        rows.append(
            f"<tr>{console}"
            f'<td data-label="Prix">{price}</td>'
            f'<td data-label="Format">{FORMATS[offer.format]}</td>'
            f'<td data-label="Marchand">{_e(merchant_label(offer))}</td>'
            f'<td data-label="Relevé le">{_fr(offer.observed_at)}{stale}</td>'
            f"<td>{link}</td></tr>"
        )
    return (
        '<section class="game-section" aria-labelledby="editions">'
        '<h2 id="editions">Prix par édition</h2>'
        '<table class="editions"><thead><tr><th scope="col">Console</th>'
        '<th scope="col">Prix</th><th scope="col">Format</th><th scope="col">Marchand</th>'
        '<th scope="col">Relevé le</th><th scope="col"><span class="visually-hidden">Lien'
        "</span></th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></section>\n"
    )


def _breadcrumb(game: GameOut, url: str) -> dict[str, Any]:
    """Fil d'Ariane pour les moteurs (la page affiche un bouton de retour à la place)."""
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Catalogue", "item": SITE_URL},
            {"@type": "ListItem", "position": 2, "name": game.title, "item": url},
        ],
    }


def _redirect_page(target: str) -> str:
    """Page d'un ancien slug : redirection immédiate, canonical vers la page fusionnée."""
    return (
        '<!doctype html>\n<html lang="fr">\n<head>\n<meta charset="utf-8">\n'
        f'<title>Page déplacée · HorsPC</title>\n<link rel="canonical" href="{_a(target)}">\n'
        f'<meta http-equiv="refresh" content="0; url={_a(target)}">\n'
        '<meta name="robots" content="noindex">\n</head>\n<body>\n'
        f'<p>Cette page a été déplacée : <a href="{_a(target)}">{_e(target)}</a>.</p>\n'
        "</body>\n</html>\n"
    )


def _release_fact(game: GameOut, today: date) -> str:
    if game.release_date is None:
        return "inconnue"
    return _fr(game.release_date) + (" (à venir)" if is_upcoming(game, today) else "")


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
    # Une offre non vérifiée depuis 30 jours n'est plus publiée : son prix n'est plus garanti.
    fresh = [e.offer for e in game.editions if e.offer is not None and not e.offer.stale]
    if len(fresh) > 1:
        prices = [o.price_cents for o in fresh]
        data["offers"] = {
            "@type": "AggregateOffer",
            "lowPrice": f"{min(prices) / 100:.2f}",
            "highPrice": f"{max(prices) / 100:.2f}",
            "offerCount": len(fresh),
            "priceCurrency": "EUR",
        }
    elif fresh:
        offer = fresh[0]
        data["offers"] = {
            "@type": "Offer",
            "price": f"{offer.price_cents / 100:.2f}",
            "priceCurrency": "EUR",
            "priceValidUntil": (offer.observed_at + timedelta(days=RECENT_DAYS)).isoformat(),
            # Offre disponible au relevé ; le stock n'est pas suivi au-delà.
            "availability": "https://schema.org/InStock",
            "itemCondition": "https://schema.org/NewCondition",
            "url": str(offer.url),
            "seller": {"@type": "Organization", "name": offer.seller or offer.merchant},
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
