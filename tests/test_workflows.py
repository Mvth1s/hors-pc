"""Vérifie, par événement, quels jobs et étapes de update.yml s'exécutent."""

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "update.yml"
FULL_EVENTS = ["schedule", "workflow_dispatch"]


def runs(
    condition: str | None, event: str, test_notification: bool = False, dry_run: bool = False
) -> bool:
    """Évalue une condition `if:` limitée à github.event_name, ==, !, ||, && et always()."""
    if condition is None:
        return True
    # Chemin nominal : aucune étape précédente en échec. Les inputs n'existent que sur dispatch.
    dispatch = event == "workflow_dispatch"
    expr = condition.strip().removeprefix("${{").removesuffix("}}").strip()
    expr = (
        expr.replace("always()", "True")
        .replace("failure()", "False")
        .replace("||", " or ")
        .replace("&&", " and ")
        .replace("!=", "≠")
        .replace("!", " not ")
        .replace("≠", "!=")
    )
    expr = expr.replace("github.event_name", repr(event))
    expr = expr.replace("inputs.test_notification", repr(dispatch and test_notification))
    expr = expr.replace("inputs.scrape_dry_run", repr(dispatch and dry_run))
    return _evaluate(ast.parse(expr.strip(), mode="eval").body, condition)


def _evaluate(node: ast.expr, condition: str) -> bool:
    """Évalue sans eval() : seuls les booléens, == entre constantes, and/or sont admis."""
    match node:
        case ast.Constant(value=bool() as value):
            return value
        case ast.BoolOp(op=ast.Or(), values=values):
            return any(_evaluate(v, condition) for v in values)
        case ast.BoolOp(op=ast.And(), values=values):
            return all(_evaluate(v, condition) for v in values)
        case ast.UnaryOp(op=ast.Not(), operand=operand):
            return not _evaluate(operand, condition)
        case ast.Compare(
            left=ast.Constant(value=left), ops=[ast.Eq()], comparators=[ast.Constant(value=right)]
        ):
            return left == right
    raise ValueError(f"condition non gérée par le test : {condition}")


def workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def executed_steps(
    event: str, test_notification: bool = False, dry_run: bool = False
) -> list[dict[str, Any]]:
    steps = []
    ran: set[str] = set()
    for name, job in workflow()["jobs"].items():
        needs = job.get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        # Un job dont une dépendance est sautée l'est aussi, sauf condition avec always().
        if not set(needs) <= ran and "always()" not in str(job.get("if")):
            continue
        if runs(job.get("if"), event, test_notification, dry_run):
            ran.add(name)
            steps += [
                s for s in job["steps"] if runs(s.get("if"), event, test_notification, dry_run)
            ]
    return steps


def commands(steps: list[dict[str, Any]]) -> str:
    return "\n".join(s.get("run", "") for s in steps)


def test_push_ne_commite_rien_et_build_hors_ligne() -> None:
    steps = executed_steps("push")
    script = commands(steps)
    assert "git commit" not in script
    assert "git push" not in script
    builds = [line for line in script.splitlines() if "horspc build" in line]
    assert builds == ["uv run python -m horspc build --offline"]
    assert not any("secrets." in str(s.get("env", {})) for s in steps)
    assert any(s.get("uses", "").startswith("actions/deploy-pages") for s in steps)


def test_push_ne_lance_pas_freshness() -> None:
    jobs = workflow()["jobs"]
    assert not runs(jobs["freshness"].get("if"), "push")


@pytest.mark.parametrize("event", FULL_EVENTS)
def test_build_complet_et_commit_hors_push(event: str) -> None:
    script = commands(executed_steps(event))
    assert "git commit" in script
    builds = [line for line in script.splitlines() if "horspc build" in line]
    # Build complet : relevé automatique des prix (pas de --no-scrape) et bilan écrit.
    assert builds == [
        'uv run python -m horspc build --scrape-report "$RUNNER_TEMP/scrape-report.json"'
    ]
    assert "data/prices.scraped.jsonl" in script and "data/discovered.json" in script
    # Catalogue découvert et cache d'identification : versionnés comme les relevés.
    assert "data/games.discovered.yaml" in script and "data/discovery.cache.json" in script
    assert runs(workflow()["jobs"]["freshness"].get("if"), event)


def test_build_borne_dans_le_temps() -> None:
    assert workflow()["jobs"]["build"]["timeout-minutes"] == 90


@pytest.mark.parametrize("event", ["push", *FULL_EVENTS])
def test_lint_et_tests_avant_le_build(event: str) -> None:
    script = commands(executed_steps(event)).splitlines()
    position = {cmd: i for i, cmd in enumerate(script)}
    build = next(i for i, line in enumerate(script) if "horspc build" in line)
    for cmd in ("uv run ruff check .", "uv run ruff format --check .", "uv run pytest"):
        assert cmd in position, cmd
        assert position[cmd] < build


@pytest.mark.parametrize("event", ["push", *FULL_EVENTS])
def test_notification_discord_seulement_en_cas_d_echec(event: str) -> None:
    job = workflow()["jobs"]["notify"]
    condition = job["if"]
    assert not runs(condition, event)  # chemin nominal : pas de notification
    on_failure = runs(condition.replace("failure()", "always()"), event)
    assert on_failure == (event in FULL_EVENTS)
    step = job["steps"][0]
    assert step["env"]["DISCORD_WEBHOOK_URL"] == "${{ secrets.DISCORD_WEBHOOK_URL }}"
    assert 'if [ -z "$DISCORD_WEBHOOK_URL" ]' in step["run"]  # secret absent : rien
    for var in ("WORKFLOW", "EVENT", "RUN_URL"):
        assert f"${var}" in step["run"]


# --- discord.yml -------------------------------------------------------------------------

DISCORD = WORKFLOW.parent / "discord.yml"


def discord() -> dict[str, Any]:
    return yaml.safe_load(DISCORD.read_text(encoding="utf-8"))


def discord_steps() -> list[dict[str, Any]]:
    return [s for job in discord()["jobs"].values() for s in job["steps"]]


def test_discord_evenements() -> None:
    on = discord()[True]  # PyYAML lit la clé « on » comme le booléen True
    assert on == {
        "issues": {"types": ["opened", "closed", "reopened"]},
        "issue_comment": {"types": ["created"]},
        "pull_request_target": {"types": ["opened", "closed", "reopened"]},
        "release": {"types": ["published"]},
    }


def test_discord_permissions_vides_et_sans_checkout() -> None:
    wf = discord()
    assert wf["permissions"] == {}
    for job in wf["jobs"].values():
        assert job.get("permissions", {}) == {}
    assert not any("checkout" in s.get("uses", "") for s in discord_steps())
    assert "actions/checkout" not in DISCORD.read_text(encoding="utf-8")


def test_discord_aucune_expression_dans_les_run() -> None:
    for step in discord_steps():
        assert "${{" not in step.get("run", ""), step.get("name")
    assert "${{ github.event" not in "".join(s.get("run", "") for s in discord_steps())


def test_discord_sans_mention_ni_username_et_secret_facultatif() -> None:
    (step,) = discord_steps()
    script = step["run"]
    assert "allowed_mentions: {parse: []}" in script
    assert "username" not in script
    assert step["env"] == {"DISCORD_WEBHOOK_URL": "${{ secrets.DISCORD_WEBHOOK_URL }}"}
    assert 'if [ -z "${DISCORD_WEBHOOK_URL:-}" ]' in script
    assert "trunc(256)" in script and "trunc(300)" in script


def test_discord_ignore_le_bot() -> None:
    condition = discord()["jobs"]["notify"]["if"]
    assert "github.event.issue.user.login == 'github-actions[bot]'" in condition
    assert "github.event.comment.user.login == 'github-actions[bot]'" in condition


def test_update_notify_sans_mention() -> None:
    script = workflow()["jobs"]["notify"]["steps"][0]["run"]
    assert "allowed_mentions: {parse: []}" in script
    assert "${{" not in script


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq absent")
@pytest.mark.parametrize(
    ("event", "payload", "expected_title", "color"),
    [
        (
            "issues",
            {
                "action": "opened",
                "issue": {
                    "number": 12,
                    "title": "T" * 400,
                    "html_url": "https://github.com/i/12",
                    "user": {"login": "alice"},
                    "body": "@everyone " + "x" * 400,
                },
            },
            "[Issue ouverte] #12 TTT",
            3066993,
        ),
        (
            "pull_request_target",
            {
                "action": "closed",
                "pull_request": {
                    "number": 7,
                    "title": "feat",
                    "html_url": "https://github.com/p/7",
                    "user": {"login": "carol"},
                    "body": None,
                    "merged": True,
                },
            },
            "[PR fusionnée] #7 feat",
            9001445,
        ),
    ],
)
def test_discord_payload(
    tmp_path: Path, event: str, payload: dict[str, Any], expected_title: str, color: int
) -> None:
    (step,) = discord_steps()
    (tmp_path / "event.json").write_text(json.dumps(payload), encoding="utf-8")
    env = {
        "PATH": "/usr/bin:/bin",
        "GITHUB_EVENT_NAME": event,
        "GITHUB_REPOSITORY": "Mvth1s/hors-pc",
        "GITHUB_EVENT_PATH": str(tmp_path / "event.json"),
    }  # pas de DISCORD_WEBHOOK_URL : rien n'est envoyé
    result = subprocess.run(
        ["bash", "-c", step["run"]], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "rien n'est envoyé" in result.stdout
    data = json.loads((tmp_path / "payload.json").read_text(encoding="utf-8"))
    assert data["allowed_mentions"] == {"parse": []}
    (embed,) = data["embeds"]
    assert embed["title"].startswith(expected_title)
    assert len(embed["title"]) <= 256 and len(embed["description"]) <= 300
    assert embed["color"] == color
    assert "username" not in data


FAILURE_STEP = "Échec volontaire pour tester la notification"


def test_option_test_notification() -> None:
    option = workflow()[True]["workflow_dispatch"]["inputs"]["test_notification"]
    assert option["type"] == "boolean"
    assert option["default"] is False


@pytest.mark.parametrize(
    ("event", "option", "expected"),
    [
        ("workflow_dispatch", True, True),
        ("workflow_dispatch", False, False),
        ("schedule", True, False),
        ("schedule", False, False),
        ("push", True, False),
        ("push", False, False),
    ],
)
def test_echec_volontaire_seulement_sur_dispatch_avec_option(
    event: str, option: bool, expected: bool
) -> None:
    names = [s.get("name") for s in executed_steps(event, test_notification=option)]
    assert (FAILURE_STEP in names) is expected


def test_echec_volontaire_avant_build_commit_et_deploiement() -> None:
    build_steps = workflow()["jobs"]["build"]["steps"]
    names = [s.get("name") for s in build_steps]
    assert names[0] == FAILURE_STEP
    step = build_steps[0]
    assert "exit 1" in step["run"]
    assert "${{" not in step["run"]
    # deploy et notify dépendent du job build : un échec ici bloque tout déploiement.
    jobs = workflow()["jobs"]
    assert "build" in jobs["deploy"]["needs"]
    assert "build" in jobs["notify"]["needs"]


# --- Relevé des prix : alerte non bloquante et diagnostic scrape_dry_run -------------------

ALERT_STEP = "Alerte Discord si une source est bloquée ou cassée"


def build_step(name: str) -> dict[str, Any]:
    return next(s for s in workflow()["jobs"]["build"]["steps"] if s.get("name") == name)


def test_option_scrape_dry_run() -> None:
    option = workflow()[True]["workflow_dispatch"]["inputs"]["scrape_dry_run"]
    assert option["type"] == "boolean" and option["default"] is False


def test_dry_run_ni_build_ni_commit_ni_deploiement() -> None:
    steps = executed_steps("workflow_dispatch", dry_run=True)
    script = commands(steps)
    assert "horspc build" not in script and "git commit" not in script
    assert not any(s.get("uses", "").startswith("actions/deploy-pages") for s in steps)
    assert 'uv run python -m horspc scrape-probe >> "$GITHUB_STEP_SUMMARY"' in script
    jobs = workflow()["jobs"]
    assert jobs["probe"]["permissions"] == {"contents": "read"}
    assert not runs(jobs["freshness"]["if"], "workflow_dispatch", dry_run=True)


@pytest.mark.parametrize("event", ["push", *FULL_EVENTS])
def test_pas_de_diagnostic_hors_dry_run(event: str) -> None:
    assert not runs(workflow()["jobs"]["probe"]["if"], event)
    assert "scrape-probe" not in commands(executed_steps(event))


@pytest.mark.parametrize("event", FULL_EVENTS)
def test_alerte_releve_apres_build_avant_commit_sans_echec(event: str) -> None:
    names = [s.get("name") for s in executed_steps(event)]
    assert ALERT_STEP in names and "Bilan du relevé des prix" in names
    build = next(i for i, n in enumerate(names) if n and n.startswith("Génère site/data.json"))
    commit = names.index("Commite le site et les relevés s'ils ont changé")
    assert build < names.index(ALERT_STEP) < commit
    step = build_step(ALERT_STEP)
    assert step["continue-on-error"] is True
    assert "${{" not in step["run"] and "allowed_mentions: {parse: []}" in step["run"]
    assert step["env"]["DISCORD_WEBHOOK_URL"] == "${{ secrets.DISCORD_WEBHOOK_URL }}"


def test_pas_d_alerte_releve_sur_push() -> None:
    assert ALERT_STEP not in [s.get("name") for s in executed_steps("push")]


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq absent")
@pytest.mark.parametrize(
    ("blocked", "expected"),
    [({"leclerc": "HTTP 403"}, "::warning::⚠️ HorsPC"), ({}, "Relevé complet : pas d'alerte")],
)
def test_script_alerte_releve(tmp_path: Path, blocked: dict[str, str], expected: str) -> None:
    import sys

    report = {"ok": 3, "blocked": blocked, "broken": [], "counts": {}, "events": []}
    (tmp_path / "scrape-report.json").write_text(json.dumps(report), encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # « uv run python … » → l'interpréteur des tests.
    (bin_dir / "uv").write_text(f'#!/bin/sh\nshift 2\nexec {sys.executable} "$@"\n')
    (bin_dir / "uv").chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "RUNNER_TEMP": str(tmp_path),
        "RUN_URL": "https://github.com/Mvth1s/hors-pc/actions/runs/1",
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
    }  # pas de DISCORD_WEBHOOK_URL : rien n'est envoyé
    result = subprocess.run(
        ["bash", "-c", build_step(ALERT_STEP)["run"]], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout
    if blocked:
        assert (
            "leclerc (HTTP 403)" in result.stdout and "Secret DISCORD_WEBHOOK_URL" in result.stdout
        )


# --- Supply chain ------------------------------------------------------------------------------

PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+(\.\d+)*$")


@pytest.mark.parametrize("path", sorted(WORKFLOW.parent.glob("*.yml")), ids=lambda p: p.name)
def test_toutes_les_actions_epinglees_par_sha(path: Path) -> None:
    lines = [
        line.split("uses:", 1)[1].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if re.match(r"^\s*(-\s+)?uses:", line)
    ]
    loose = [u for u in lines if not PINNED.match(u)]
    assert loose == [], loose  # SHA de commit complet + version en commentaire


def test_dependabot_actions_et_uv_hebdomadaire() -> None:
    config = yaml.safe_load((WORKFLOW.parents[1] / "dependabot.yml").read_text(encoding="utf-8"))
    by_ecosystem = {u["package-ecosystem"]: u for u in config["updates"]}
    assert set(by_ecosystem) == {"github-actions", "uv"}
    for update in by_ecosystem.values():
        assert update["schedule"]["interval"] == "weekly"
        (group,) = update["groups"].values()
        assert group["update-types"] == ["minor", "patch"]


# --- Audit mensuel des sources --------------------------------------------------------------

AUDIT = WORKFLOW.parent / "sources-audit.yml"


def audit_workflow() -> dict[str, Any]:
    return yaml.safe_load(AUDIT.read_text(encoding="utf-8"))


def test_audit_mensuel_et_manuel() -> None:
    wf = audit_workflow()
    assert wf[True] == {"schedule": [{"cron": "23 5 1 * *"}], "workflow_dispatch": None}


def test_audit_permissions_minimales() -> None:
    wf = audit_workflow()
    assert wf["permissions"] == {}
    (job,) = wf["jobs"].values()
    assert job["permissions"] == {"contents": "write", "issues": "write"}


def test_audit_ne_commite_que_l_etat_et_ne_touche_pas_a_la_config() -> None:
    steps = audit_workflow()["jobs"]["audit"]["steps"]
    script = "\n".join(s.get("run", "") for s in steps)
    assert "git add data/sources.audit.json" in script
    assert script.count("git add") == 1
    assert "sources.config" not in script
    for step in steps:
        assert "${{" not in step.get("run", ""), step.get("name")


def test_audit_une_seule_issue_ouverte_et_discord_non_bloquant() -> None:
    steps = {s.get("name"): s for s in audit_workflow()["jobs"]["audit"]["steps"]}
    issue = steps["Issue « changements à relire »"]["run"]
    assert "gh issue list --label sources-audit --state open" in issue
    assert "gh issue comment" in issue and "gh issue create" in issue
    discord = steps["Notification Discord"]
    assert discord["continue-on-error"] is True
    assert "allowed_mentions: {parse: []}" in discord["run"]
