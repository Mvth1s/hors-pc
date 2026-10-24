"""Vérifie, par événement, quels jobs et étapes de update.yml s'exécutent."""

import ast
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "update.yml"
FULL_EVENTS = ["schedule", "workflow_dispatch"]


def runs(condition: str | None, event: str) -> bool:
    """Évalue une condition `if:` limitée à github.event_name, ==, ||, && et always()."""
    if condition is None:
        return True
    expr = condition.replace("always()", "True").replace("||", " or ").replace("&&", " and ")
    expr = expr.replace("github.event_name", repr(event))
    return _evaluate(ast.parse(expr, mode="eval").body, condition)


def _evaluate(node: ast.expr, condition: str) -> bool:
    """Évalue sans eval() : seuls les booléens, == entre constantes, and/or sont admis."""
    match node:
        case ast.Constant(value=bool() as value):
            return value
        case ast.BoolOp(op=ast.Or(), values=values):
            return any(_evaluate(v, condition) for v in values)
        case ast.BoolOp(op=ast.And(), values=values):
            return all(_evaluate(v, condition) for v in values)
        case ast.Compare(
            left=ast.Constant(value=left), ops=[ast.Eq()], comparators=[ast.Constant(value=right)]
        ):
            return left == right
    raise ValueError(f"condition non gérée par le test : {condition}")


def workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def executed_steps(event: str) -> list[dict[str, Any]]:
    steps = []
    for job in workflow()["jobs"].values():
        if runs(job.get("if"), event):
            steps += [s for s in job["steps"] if runs(s.get("if"), event)]
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
    assert builds == ["uv run python -m horspc build"]
    assert runs(workflow()["jobs"]["freshness"].get("if"), event)


@pytest.mark.parametrize("event", ["push", *FULL_EVENTS])
def test_lint_et_tests_avant_le_build(event: str) -> None:
    script = commands(executed_steps(event)).splitlines()
    position = {cmd: i for i, cmd in enumerate(script)}
    build = next(i for i, line in enumerate(script) if "horspc build" in line)
    for cmd in ("uv run ruff check .", "uv run ruff format --check .", "uv run pytest"):
        assert cmd in position, cmd
        assert position[cmd] < build
