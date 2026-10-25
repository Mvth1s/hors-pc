"""Audit mensuel des sources : réponses figées, aucune requête réseau réelle."""

from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from horspc import audit
from horspc.http import HttpClient
from horspc.prices.fetch import SERVER_ERRORS
from tests.conftest import FIXTURES

TODAY = date(2026, 11, 1)
LECLERC_FP = "https://www.e.leclerc/fp/mario-kart-world-nintendo-switch-2-0045496312336"
CGV = "https://www.e.leclerc/i/conditions-generales-de-vente"
REGISTRY: dict[str, dict[str, Any]] = {
    "leclerc": {
        "name": "E.Leclerc",
        "status": "retenue",
        "active": "leclerc",
        "home": "https://www.e.leclerc/",
        "product": LECLERC_FP,
        "paths": ["/cat/jeux-ps5", "/recherche?q=x"],
        "terms": [CGV],
    },
    "fnac": {
        "name": "Fnac",
        "status": "exclue",
        "home": "https://www.fnac.com/",
        "product": "https://www.fnac.com/a21590928/Ghost-of-Yotei-PS5",
    },
}
ROBOTS = "User-agent: *\nDisallow: /recherche\n"
CGV_PAGE = (
    "<html><body><h1>CGV</h1><p>Article 16. Toute reproduction des textes et images du site est"
    " interdite.</p><p>Livraison sous 3 jours.</p></body></html>"
)
FICHE = (FIXTURES / "sources" / "leclerc.html").read_text(encoding="utf-8")


def site(**over: Any) -> dict[str, Any]:
    """Réponses par défaut : Leclerc accessible, Fnac en 403 (Akamai)."""
    return {
        "leclerc_robots": httpx.Response(200, text=ROBOTS),
        "leclerc_home": httpx.Response(200, text="<html><head></head><body>Accueil</body></html>"),
        "leclerc_fp": httpx.Response(200, text=FICHE),
        "leclerc_cgv": httpx.Response(200, text=CGV_PAGE),
        "fnac_robots": httpx.Response(200, text="User-agent: *\nDisallow: /panier\n"),
        "fnac_home": httpx.Response(403, text="FNAC DARTY - Maintenance"),
        "fnac_fp": httpx.Response(200, text="<html>fiche</html>"),
    } | over


def mock(router: respx.MockRouter, responses: dict[str, httpx.Response]) -> dict[str, respx.Route]:
    routes = {
        "leclerc_robots": router.get("https://www.e.leclerc/robots.txt"),
        "leclerc_tdm": router.get("https://www.e.leclerc/.well-known/tdmrep.json"),
        "leclerc_home": router.get("https://www.e.leclerc/"),
        "leclerc_fp": router.get(LECLERC_FP),
        "leclerc_cgv": router.get(CGV),
        "fnac_robots": router.get("https://www.fnac.com/robots.txt"),
        "fnac_tdm": router.get("https://www.fnac.com/.well-known/tdmrep.json"),
        "fnac_home": router.get("https://www.fnac.com/"),
        "fnac_fp": router.get(REGISTRY["fnac"]["product"]),
    }
    for name, route in routes.items():
        route.mock(return_value=responses.get(name, httpx.Response(404)))
    return routes


def run_audit(
    responses: dict[str, httpx.Response],
) -> tuple[dict[str, Any], dict[str, respx.Route]]:
    with respx.mock(assert_all_called=False) as router:
        routes = mock(router, responses)
        http = HttpClient(sleep=lambda _: None, clock=lambda: 0.0, retry_statuses=SERVER_ERRORS)
        auditor = audit.Auditor(http)
        try:
            state = audit.run(REGISTRY, auditor, TODAY)
        finally:
            auditor.close()
        return state, routes


def test_rien_de_change() -> None:
    first, _ = run_audit(site())
    second, _ = run_audit(site())
    assert audit.compare(None, first) == {}  # état initial : rien à signaler
    assert audit.compare(first, second) == {}
    leclerc = first["sources"]["leclerc"]
    assert leclerc["jsonld"] == "lisible"
    assert leclerc["robots"]["verdicts"] == {
        "/cat/jeux-ps5": "autorisé",
        "/recherche?q=x": "interdit (Disallow: /recherche)",
    }
    assert leclerc["terms"][CGV]["clauses"] == 1  # seule la clause « reproduction »


def test_robots_txt_modifie() -> None:
    first, _ = run_audit(site())
    robots = ROBOTS + "Disallow: /cat/\n"
    second, _ = run_audit(site(leclerc_robots=httpx.Response(200, text=robots)))
    changes = audit.compare(first, second)["leclerc"]
    assert any(c.startswith("robots.txt : règles qui s'appliquent à HorsPC") for c in changes)
    assert (
        "robots.txt : verdict pour `/cat/jeux-ps5` : autorisé → interdit (Disallow: /cat/)"
        in changes
    )


def test_cgu_modifiees_a_relire() -> None:
    first, _ = run_audit(site())
    page = CGV_PAGE.replace("est interdite", "et toute extraction automatisée sont interdites")
    second, _ = run_audit(site(leclerc_cgv=httpx.Response(200, text=page)))
    (change,) = audit.compare(first, second)["leclerc"]
    assert change.startswith(f"CGU <{CGV}> : clauses modifiées") and change.endswith("à relire")


def test_cgu_hors_clauses_ignorees() -> None:
    first, _ = run_audit(site())
    page = CGV_PAGE.replace("3 jours", "5 jours")  # paragraphe sans mot suivi
    second, _ = run_audit(site(leclerc_cgv=httpx.Response(200, text=page)))
    assert audit.compare(first, second) == {}


def test_enseigne_debloquee() -> None:
    first, routes = run_audit(site())
    assert first["sources"]["fnac"]["home"] == {"status": 403, "blocked": True}
    # Bloquée : la fiche n'est plus demandée pendant l'audit.
    assert not routes["fnac_fp"].called
    assert first["sources"]["fnac"]["product"]["status"] == "non demandé (bloqué)"
    second, _ = run_audit(site(fnac_home=httpx.Response(200, text="<html>Fnac</html>")))
    changes = audit.compare(first, second)["fnac"]
    assert "accueil : blocage : blocage levé" in changes
    assert "fiche : statut HTTP : non demandé (bloqué) → 200" in changes


def test_source_active_bloquee() -> None:
    first, _ = run_audit(site())
    second, _ = run_audit(site(leclerc_fp=httpx.Response(403, text="Forbidden")))
    changes = audit.compare(first, second)["leclerc"]
    assert "fiche : blocage : blocage apparu" in changes
    assert "fiche : JSON-LD : lisible → non lu (403)" in changes


def test_json_ld_casse() -> None:
    first, _ = run_audit(site())
    broken = httpx.Response(200, text="<html><body>Nouvelle maquette</body></html>")
    second, _ = run_audit(site(leclerc_fp=broken))
    assert (
        "fiche : JSON-LD : lisible → cassé : JSON-LD Product absent"
        in audit.compare(first, second)["leclerc"]
    )


def test_tdm_reservation_detectee() -> None:
    home = httpx.Response(
        200,
        headers={"tdm-reservation": "1"},
        text='<html><head><meta name="tdm-reservation" content="1"></head></html>',
    )
    tdmrep = httpx.Response(200, text='[{"location": "/", "tdm-reservation": 1}]')
    first, _ = run_audit(site())
    second, _ = run_audit(site(leclerc_home=home, leclerc_tdm=tdmrep))
    changes = audit.compare(first, second)["leclerc"]
    assert "TDMRep : en-tête tdm-reservation : None → 1" in changes
    assert "TDMRep : meta tdm-reservation : None → 1" in changes
    assert any(c.startswith("TDMRep (/.well-known/tdmrep.json) : 404 → 200") for c in changes)


def test_robots_non_verifiable_enseigne_non_sollicitee() -> None:
    state, routes = run_audit(site(fnac_robots=httpx.Response(403)))
    fnac = state["sources"]["fnac"]
    assert fnac["robots"]["status"] == 403
    assert not routes["fnac_home"].called and not routes["fnac_fp"].called
    assert fnac["home"]["status"] == "non demandé (bloqué)"


def test_chemin_interdit_jamais_demande() -> None:
    registry = {"leclerc": REGISTRY["leclerc"] | {"terms": ["https://www.e.leclerc/recherche"]}}
    with respx.mock(assert_all_called=False) as router:
        routes = mock(router, site())
        search = router.get("https://www.e.leclerc/recherche").respond(200)
        http = HttpClient(sleep=lambda _: None, clock=lambda: 0.0)
        state = audit.run(registry, audit.Auditor(http), TODAY)
    assert not search.called and routes["leclerc_home"].called
    assert state["sources"]["leclerc"]["terms"]["https://www.e.leclerc/recherche"]["status"] == (
        "non demandé (interdit)"
    )


def test_rapports() -> None:
    first, _ = run_audit(site())
    second, _ = run_audit(site(leclerc_fp=httpx.Response(403)))
    changes = audit.compare(first, second)
    body = audit.changes_markdown(changes, REGISTRY, TODAY)
    assert "### E.Leclerc (retenue)" in body and "aucune source n'a été activée" in body
    summary = audit.summary_markdown(second, changes, REGISTRY, initial=False)
    assert "**1 enseigne(s) avec des changements à relire**" in summary
    assert "| Fnac | exclue | 200 | 403 (bloqué) |" in summary
    assert "État initial" in audit.summary_markdown(first, {}, REGISTRY, initial=True)


def test_audit_ne_touche_pas_a_la_configuration() -> None:
    code = Path(audit.__file__).read_text(encoding="utf-8")
    assert "sources.config.yaml n'est pas modifié" in code  # seule mention, dans la docstring
    assert code.count("sources.config") == 1


REGISTRY_PATH = Path(__file__).parents[1] / "data" / "sources.audit.yaml"


@pytest.mark.parametrize("sid", list(audit.load_registry(REGISTRY_PATH)))
def test_registre_complet(sid: str) -> None:
    cfg = audit.load_registry(REGISTRY_PATH)[sid]
    assert cfg["status"] in ("retenue", "désactivée", "exclue")
    assert cfg["home"].startswith("https://") and cfg["name"]
    if cfg.get("active"):
        assert cfg.get("product"), "une source active a une fiche pour vérifier le JSON-LD"


def audit_pair(first_site: dict, second_site: dict) -> tuple[dict, dict[str, list[str]]]:
    """Deux audits successifs avec report des CGU illisibles, comme la commande."""
    states = []
    previous = None
    for responses in (first_site, second_site):
        with respx.mock(assert_all_called=False) as router:
            mock(router, responses)
            http = HttpClient(sleep=lambda _: None, clock=lambda: 0.0, retry_statuses=SERVER_ERRORS)
            current, changes = audit.audit_and_compare(
                REGISTRY, audit.Auditor(http), previous, TODAY
            )
        states.append(current)
        previous = current
    return states[-1], changes


def test_cgu_illisible_ce_mois_ci_garde_son_empreinte() -> None:
    state, changes = audit_pair(site(), site(leclerc_cgv=httpx.Response(403)))
    assert changes == {}  # un aléa d'accès n'est pas un changement de clauses
    term = state["sources"]["leclerc"]["terms"][CGV]
    assert term["hash"] is not None and term["unread"] == TODAY.isoformat()


def test_tdmrep_page_html_n_est_pas_une_declaration() -> None:
    challenge = httpx.Response(200, text="<html><head><META NAME='ROBOTS'></head>défi</html>")
    other = httpx.Response(200, text="<html>autre jeton</html>")
    state, changes = audit_pair(site(leclerc_tdm=challenge), site(leclerc_tdm=other))
    assert changes == {}
    assert state["sources"]["leclerc"]["tdm"]["tdmrep"] == "200, pas un fichier TDMRep"
