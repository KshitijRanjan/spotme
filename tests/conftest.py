"""
Stubs for AWS and DB libraries injected before any app module is imported.
This prevents module-level boto3.client() and psycopg2.connect() from
making real network calls during test collection.
"""
import sys
from unittest.mock import MagicMock

import pytest

# Must run before pytest imports app.py
_boto3 = MagicMock()
_psycopg2 = MagicMock()
_psycopg2_pool = MagicMock()

sys.modules.setdefault("boto3", _boto3)
sys.modules.setdefault("psycopg2", _psycopg2)
sys.modules.setdefault("psycopg2.pool", _psycopg2_pool)


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset in-memory rate-limit counters before every test so tests are isolated."""
    import app as app_module
    app_module.limiter._storage.reset()
    yield
