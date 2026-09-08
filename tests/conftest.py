"""Shared pytest fixtures.

The critical job here is isolation: tests must never write to the real database
and never call a real provider.
"""

import os
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

# MODELMUX_DB_PATH must be set BEFORE app.main is imported, because main.py
# loads config at import time (deliberately -- a broken config should stop the
# process immediately). Each run gets its own file.
_TEST_DB = Path(os.environ.get("TEMP", "/tmp")) / f"modelmux-test-{uuid.uuid4().hex}.db"
os.environ["MODELMUX_DB_PATH"] = str(_TEST_DB)

# Tests import the app package from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.providers.mock import MockProvider  # noqa: E402


@pytest.fixture
def db_path() -> Path:
    return _TEST_DB


@pytest.fixture
def mock_provider():
    """A mock that succeeds. Override attributes per test."""
    return MockProvider(text="mocked answer", tokens_in=10, tokens_out=5)


@pytest.fixture
def client(mock_provider):
    """TestClient with EVERY provider swapped for the same mock.

    Entering the TestClient context manager runs the app's lifespan, so the
    database is initialised exactly as it is in production. We then replace the
    registered providers -- the config still names groq/google/anthropic, so
    the substitution is invisible to the endpoint. That is the payoff of the
    adapter boundary: the handler cannot tell the difference.

    EVERY provider, not just the small tier's. Stage 2 made routing real, so a
    long prompt now resolves to `google` and a longer one to `anthropic`. When
    this fixture only replaced "groq", such a test escaped the mock and made a
    real network call -- which then failed on a missing GOOGLE_API_KEY and
    surfaced as a confusing KeyError.

    A single shared mock instance is used deliberately: `mock.calls` then
    counts provider calls across all tiers, so "was any provider called?" stays
    a single assertion.
    """
    with TestClient(app) as test_client:
        for name in list(test_client.app.state.providers):
            test_client.app.state.providers[name] = mock_provider
        yield test_client


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_db():
    """Delete this run's temp database when the session ends.

    A session-scoped autouse fixture rather than a pytest_sessionfinish hook:
    pytest only honours initialisation hooks from the ROOTDIR conftest, and
    this conftest lives in tests/. The hook silently never fired.

    Each run creates a uuid-named file so runs never share state; without this
    they accumulate in TEMP forever (eight had piled up before anyone looked).
    WAL means three files, not one.
    """
    yield
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(_TEST_DB) + suffix).unlink(missing_ok=True)
        except OSError:
            # Never fail a run over cleanup.
            pass


def rows(db: Path) -> list[sqlite3.Row]:
    """Every logged row, oldest first."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM requests ORDER BY timestamp").fetchall()
    finally:
        conn.close()
