from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from costagent.budget import RunBudget
from costagent.config import Settings
from costagent.indexer import Index
from costagent.store import Store

ROOT = Path(__file__).resolve().parent.parent
NOW = dt.datetime(2026, 3, 2, 9, 0, tzinfo=dt.UTC)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, db_path=str(tmp_path / "agent.db"))


@pytest.fixture
def store(settings):
    s = Store(settings.db_path)
    yield s
    s.close()


@pytest.fixture
def index(store, settings) -> Index:
    idx = Index(store, settings.chunk_lines)
    idx.build(ROOT / "sample_corpus", settings.max_file_bytes)
    return idx


@pytest.fixture
def budget(store, settings) -> RunBudget:
    return RunBudget(store, Decimal("1.00"), Decimal("5.00"), settings.min_output_tokens, clock=lambda: NOW)
