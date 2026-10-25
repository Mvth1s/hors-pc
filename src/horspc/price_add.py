"""`horspc price add` : saisie interactive d'un relevé dans prices.manual.yaml."""

import io
import os
from collections.abc import Callable
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from horspc.models import MERCHANTS, Catalog, PriceObservation

Ask = Callable[[str], str]
Say = Callable[[str], None]

FORMAT_LABELS = {"physical": "physique", "code_box": "boîte avec code", "digital": "démat"}


class Aborted(Exception):
    """Saisie interrompue : rien n'est écrit."""


def price_add(
    games_path: Path,
    prices_path: Path,
    today: date,
    ask: Ask = input,
    say: Say = print,
) -> int:
    """Demande un relevé, le valide puis l'ajoute en fin de fichier. Renvoie le code de sortie."""
    games = yaml.safe_load(games_path.read_text(encoding="utf-8")) or []
    existing = yaml.safe_load(prices_path.read_text(encoding="utf-8")) or []
    try:
        entry = _prompt(games, ask, say, today)
        catalog = Catalog.model_validate(
            {"games": games, "observations": [*existing, entry]}, context={"today": today}
        )
        _append(prices_path, catalog.observations[-1], games_path, today)
    except Aborted as exc:
        say(f"Abandon, rien n'est écrit : {exc}")
        return 1
    except ValidationError as exc:
        say(f"Relevé refusé, rien n'est écrit :\n{exc}")
        return 1
    say(f"Relevé ajouté à {prices_path}.")
    return 0


def _prompt(games: list[dict[str, Any]], ask: Ask, say: Say, today: date) -> dict[str, Any]:
    slugs = [g["slug"] for g in games]
    _enable_completion(slugs)
    say("Jeux :")
    for i, game in enumerate(games, 1):
        say(f"  {i:>2}. {game['slug']}  ({game['title']})")
    slug = _choose(ask, "Jeu (numéro ou slug, Tab pour compléter) : ", slugs)
    game = next(g for g in games if g["slug"] == slug)
    platforms = [e["platform"] for e in game.get("editions", [])]
    platform = None  # jeu à une seule édition : déduite au build
    if len(platforms) > 1:
        platform = _choose(ask, f"Édition ({' / '.join(platforms)}) : ", platforms)

    merchant_ids = list(MERCHANTS)
    say("Marchands admis :")
    for i, merchant_id in enumerate(merchant_ids, 1):
        merchant = MERCHANTS[merchant_id]
        formats = ", ".join(FORMAT_LABELS[f] for f in sorted(merchant.formats))
        say(f"  {i:>2}. {merchant_id}  ({merchant.name}, {formats})")
    merchant_id = _choose(ask, "Marchand (numéro ou identifiant) : ", merchant_ids)

    formats = sorted(MERCHANTS[merchant_id].formats)
    if len(formats) == 1:
        offer_format = formats[0]
        say(f"Format : {FORMAT_LABELS[offer_format]} (seul format admis pour {merchant_id})")
    else:
        offer_format = _choose(ask, f"Format ({' / '.join(formats)}) : ", formats)

    price = _amount(ask, "Prix TTC en € (ex. 53,30) : ", allow_empty=False)
    shipping = _amount(ask, "Frais de port en € (0 = offerts, vide = inconnus) : ", True)

    sold = ask("Vendu par l'enseigne elle-même, pas par un vendeur tiers ? [o/n] : ")
    if sold.strip().lower() not in ("o", "oui", "y", "yes"):
        raise Aborted("seules les offres vendues par l'enseigne elle-même sont acceptées")

    url = ask("URL de la page produit : ").strip()
    return {
        "slug": slug,
        **({"platform": platform} if platform else {}),
        "price": price,
        "shipping": shipping,
        "format": offer_format,
        "condition": "new",
        "merchant": merchant_id,
        "sold_by_merchant": True,
        "url": url,
        "observed_at": today,
    }


def _choose(ask: Ask, prompt: str, choices: list[str]) -> str:
    for _ in range(3):
        answer = ask(prompt).strip()
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        if answer in choices:
            return answer
    raise Aborted("choix invalide")


def _amount(ask: Ask, prompt: str, allow_empty: bool) -> Decimal | None:
    for _ in range(3):
        answer = ask(prompt).strip().replace(",", ".").replace("€", "").strip()
        if not answer and allow_empty:
            return None
        try:
            value = Decimal(answer)
        except InvalidOperation:
            continue
        if value.is_finite():
            return value
    raise Aborted("montant invalide")


def _enable_completion(words: list[str]) -> None:
    try:
        import readline
    except ImportError:  # pas de readline (Windows) : la liste numérotée suffit
        return

    def complete(text: str, state: int) -> str | None:
        matches = [w for w in words if w.startswith(text)]
        return matches[state] if state < len(matches) else None

    readline.set_completer(complete)
    readline.parse_and_bind("tab: complete")


def _represent_null(representer: Any, _: None) -> Any:
    return representer.represent_scalar("tag:yaml.org,2002:null", "null")


def _represent_euros(representer: Any, amount: Decimal) -> Any:
    return representer.represent_scalar("tag:yaml.org,2002:float", f"{amount:.2f}")


def _append(prices_path: Path, obs: PriceObservation, games_path: Path, today: date) -> None:
    """Ajoute le relevé en fin de liste, commentaires et ordre préservés (écriture atomique)."""
    rt = YAML()
    rt.indent(mapping=2, sequence=2, offset=0)  # même style que les exemples du fichier
    rt.width = 4096
    rt.representer.add_representer(type(None), _represent_null)
    rt.representer.add_representer(Decimal, _represent_euros)
    document = rt.load(prices_path.read_text(encoding="utf-8"))
    if document is None:
        document = CommentedSeq()
    if isinstance(document, CommentedSeq) and not document:
        document.fa.set_block_style()  # remplace un `[]` vide par une liste en bloc
    entry = CommentedMap()
    entry["slug"] = obs.slug
    if obs.platform is not None:
        entry["platform"] = obs.platform
    entry["price"] = obs.price
    entry["shipping"] = obs.shipping
    entry["format"] = obs.format
    entry["condition"] = obs.condition
    entry["merchant"] = obs.merchant
    entry["sold_by_merchant"] = obs.sold_by_merchant
    entry["url"] = str(obs.url)
    entry["observed_at"] = obs.observed_at
    document.append(entry)

    buffer = io.StringIO()
    rt.dump(document, buffer)
    text = buffer.getvalue()
    # Garde-fou : le fichier produit doit relire exactement le même catalogue valide.
    Catalog.model_validate(
        {
            "games": yaml.safe_load(games_path.read_text(encoding="utf-8")) or [],
            "observations": yaml.safe_load(text) or [],
        },
        context={"today": today},
    )
    tmp = prices_path.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, prices_path)
