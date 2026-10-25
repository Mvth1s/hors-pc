"""`horspc stale` : jeux sans offre ou dont l'offre retenue est trop ancienne."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from horspc.models import MERCHANTS, Catalog, GameEntry, PriceObservation
from horspc.prices.selection import RECENT_DAYS, latest_per_offer, select_best

FORMAT_LABELS = {"physical": "physique", "code_box": "boîte avec code", "digital": "démat"}


@dataclass(frozen=True)
class StaleGame:
    game: GameEntry
    last: PriceObservation | None  # relevé le plus récent, tous marchands confondus
    per_merchant: list[PriceObservation]  # dernier relevé par (marchand, format)


def stale_games(catalog: Catalog, today: date) -> list[StaleGame]:
    """Jeux à relever, du plus ancien dernier relevé au plus récent (sans relevé en tête)."""
    result = []
    for game in catalog.games:
        observations = [o for o in catalog.observations if o.slug == game.slug]
        selection = select_best(observations, today)
        if selection is not None and not selection.stale:
            continue
        per_merchant = sorted(
            latest_per_offer(observations), key=lambda o: (-o.observed_at.toordinal(), o.merchant)
        )
        last = per_merchant[0] if per_merchant else None
        result.append(StaleGame(game, last, per_merchant))
    return sorted(
        result,
        key=lambda s: (s.last is not None, s.last.observed_at if s.last else date.min),
    )


def format_report(games: list[StaleGame], today: date) -> str:
    """Rapport lisible en terminal."""
    if not games:
        return f"Tous les jeux ont un relevé de moins de {RECENT_DAYS} jours."
    lines = [
        f"{len(games)} jeu(x) à relever (aucun relevé, ou dernier relevé retenu de "
        f"{RECENT_DAYS} jours ou plus) au {_fr(today)} :",
    ]
    for item in games:
        lines += ["", f"{item.game.title}  [{item.game.slug}]"]
        if item.last is None:
            lines.append("  aucun relevé")
            continue
        age = (today - item.last.observed_at).days
        lines.append(f"  dernier relevé : {_fr(item.last.observed_at)} (il y a {age} j)")
        lines.append(f"    {item.last.url}")
        lines.append("  derniers relevés par marchand :")
        width = max(len(_merchant(o)) for o in item.per_merchant)
        for obs in item.per_merchant:
            lines.append(
                f"    {_merchant(obs):<{width}}  {_fr(obs.observed_at)}  "
                f"{_euros(obs.price):>9}  {obs.url}"
            )
    return "\n".join(lines)


def _merchant(obs: PriceObservation) -> str:
    return f"{MERCHANTS[obs.merchant].name} ({FORMAT_LABELS[obs.format]})"


def _fr(day: date) -> str:
    return day.strftime("%d/%m/%Y")


def _euros(amount: Decimal) -> str:
    return f"{amount:.2f} €".replace(".", ",")
