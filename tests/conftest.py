"""Shared pytest fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kgov.mock_registry import MockRegistry

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


@pytest.fixture
def registry_server():
    """A live in-process mock Schema Registry backing the disk registry fixture."""
    reg = MockRegistry(load_json(FIXTURES / "registry" / "registry.json"))
    reg.start()
    try:
        yield reg
    finally:
        reg.stop()


@pytest.fixture
def server(registry_server):
    """Alias of ``registry_server`` for tests that don't care about the name."""
    return registry_server
