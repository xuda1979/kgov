"""Shared pytest fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_json(path: Path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def avro_series():
    return load_json(FIXTURES / "schema_evolution" / "avro_series.json")


@pytest.fixture
def json_series():
    return load_json(FIXTURES / "schema_evolution" / "json_series.json")
