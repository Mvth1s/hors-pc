"""Fixtures communes : horloge figée, client HTTP sans attente, réponses IGDB figées."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from horspc.http import HttpClient

FIXTURES = Path(__file__).parent / "fixtures"

NOW = datetime(2026, 10, 26, 6, 17, 42, tzinfo=UTC)
TODAY = date(2026, 10, 26)


def load_fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def http(sleeps: list[float]):
    """Client HTTP dont les attentes sont enregistrées au lieu d'être subies."""
    with HttpClient(sleep=sleeps.append, clock=lambda: 0.0) as client:
        yield client
