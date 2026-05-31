"""
Tests for app.py and scripts/index_photos.py covering:
  - connection pool (pool.getconn / pool.putconn) instead of raw psycopg2.connect
  - face_id index present in indexer schema SQL
  - /find-me response shape (drive_ids + links)
"""
import io
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# conftest.py has already stubbed boto3/psycopg2 in sys.modules
import app as app_module
from fastapi.testclient import TestClient

ROOT = Path(__file__).parent.parent
client = TestClient(app_module.app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rek_returning(face_ids):
    m = MagicMock()
    m.search_faces_by_image.return_value = {
        "FaceMatches": [{"Face": {"FaceId": fid}} for fid in face_ids]
    }
    return m


def _pool_returning(rows):
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value = cursor
    pool = MagicMock()
    pool.getconn.return_value = conn
    return pool, conn


def _post_selfie(extra_patches=None):
    extra_patches = extra_patches or {}
    image = io.BytesIO(b"\xff\xd8\xff" + b"\x00" * 100)  # minimal JPEG header
    return client.post(
        "/find-me",
        files={"file": ("selfie.jpg", image, "image/jpeg")},
    )


# ---------------------------------------------------------------------------
# P0-1: face_id index in index_photos.py
# ---------------------------------------------------------------------------

class TestFaceIdIndex:
    def test_index_script_creates_face_id_index(self):
        script = (ROOT / "scripts" / "index_photos.py").read_text()
        assert "idx_face_id" in script, (
            "scripts/index_photos.py must contain CREATE INDEX idx_face_id ON matches(face_id)"
        )

    def test_index_script_keeps_drive_id_index(self):
        """Adding face_id index must not remove the existing drive_id index."""
        script = (ROOT / "scripts" / "index_photos.py").read_text()
        assert "idx_drive_id" in script


# ---------------------------------------------------------------------------
# P0-2: connection pool in app.py
# ---------------------------------------------------------------------------

class TestConnectionPool:
    def test_pool_getconn_called_per_request(self):
        pool, conn = _pool_returning([("drive-abc",)])
        with patch.object(app_module, "rek", _rek_returning(["face-1"])), \
             patch.object(app_module, "pool", pool):
            _post_selfie()
        pool.getconn.assert_called_once()

    def test_pool_putconn_called_after_request(self):
        pool, conn = _pool_returning([("drive-abc",)])
        with patch.object(app_module, "rek", _rek_returning(["face-1"])), \
             patch.object(app_module, "pool", pool):
            _post_selfie()
        pool.putconn.assert_called_once_with(conn)

    def test_pool_putconn_called_even_on_db_error(self):
        """Connection must return to pool even when cursor.execute raises."""
        pool, conn = _pool_returning([])
        conn.cursor.side_effect = Exception("DB down")
        # raise_server_exceptions=False: get 500 response instead of re-raise
        safe_client = TestClient(app_module.app, raise_server_exceptions=False)
        with patch.object(app_module, "rek", _rek_returning(["face-1"])), \
             patch.object(app_module, "pool", pool):
            resp = safe_client.post(
                "/find-me",
                files={"file": ("selfie.jpg", io.BytesIO(b"\xff\xd8\xff"), "image/jpeg")},
            )
        assert resp.status_code == 500
        # Must not leave connection dangling
        pool.putconn.assert_called_once_with(conn)

    def test_no_raw_psycopg2_connect_in_request_handler(self):
        """psycopg2.connect must not be called during a request (pool replaces it)."""
        pool, conn = _pool_returning([("drive-xyz",)])
        psycopg2_mock = sys.modules["psycopg2"]
        psycopg2_mock.connect.reset_mock()

        with patch.object(app_module, "rek", _rek_returning(["face-1"])), \
             patch.object(app_module, "pool", pool):
            _post_selfie()

        psycopg2_mock.connect.assert_not_called()


# ---------------------------------------------------------------------------
# Response shape: /find-me must return drive_ids and links
# ---------------------------------------------------------------------------

class TestResponseShape:
    def test_returns_drive_ids_and_links_on_match(self):
        pool, conn = _pool_returning([("abc123",), ("def456",)])
        with patch.object(app_module, "rek", _rek_returning(["face-1"])), \
             patch.object(app_module, "pool", pool):
            resp = _post_selfie()
        data = resp.json()
        assert "drive_ids" in data, "Response must include drive_ids"
        assert "links" in data, "Response must include links"
        assert sorted(data["drive_ids"]) == ["abc123", "def456"]
        assert all("drive.google.com" in lnk for lnk in data["links"])

    def test_returns_empty_lists_when_no_face_match(self):
        with patch.object(app_module, "rek", _rek_returning([])):
            resp = _post_selfie()
        data = resp.json()
        assert data.get("drive_ids") == []
        assert data.get("links") == []


# ---------------------------------------------------------------------------
# P1-1: asyncio.to_thread — DB query must not block the event loop
# ---------------------------------------------------------------------------

class TestAsyncDBQuery:
    def test_query_drive_ids_is_a_separate_callable(self):
        """_query_drive_ids must exist as a standalone sync function so it can
        be dispatched via asyncio.to_thread without the pool reference baked in."""
        assert callable(getattr(app_module, "_query_drive_ids", None)), (
            "app.py must expose _query_drive_ids as a module-level sync function"
        )

    def test_find_me_uses_to_thread_for_db_query(self):
        """find_me handler must call asyncio.to_thread (not direct DB call)."""
        import asyncio
        import inspect
        source = inspect.getsource(app_module.find_me)
        assert "asyncio.to_thread" in source, (
            "find_me must await asyncio.to_thread(_query_drive_ids, ...) "
            "to avoid blocking the event loop with synchronous psycopg2 calls"
        )


# ---------------------------------------------------------------------------
# P1-2: Rate limiting — /find-me must return 429 after limit exceeded
# ---------------------------------------------------------------------------

class TestRateLimit:
    def test_first_request_is_allowed(self):
        pool, conn = _pool_returning([("drive-abc",)])
        with patch.object(app_module, "rek", _rek_returning(["face-1"])), \
             patch.object(app_module, "pool", pool):
            resp = _post_selfie()
        assert resp.status_code == 200

    def test_requests_beyond_limit_return_429(self):
        pool, conn = _pool_returning([("drive-abc",)])
        limit_num = int(app_module.RATE_LIMIT.split("/")[0])

        # Exhaust the limit
        for _ in range(limit_num):
            with patch.object(app_module, "rek", _rek_returning(["face-1"])), \
                 patch.object(app_module, "pool", pool):
                _post_selfie()

        # One over the limit
        with patch.object(app_module, "rek", _rek_returning(["face-1"])), \
             patch.object(app_module, "pool", pool):
            resp = _post_selfie()

        assert resp.status_code == 429


# ---------------------------------------------------------------------------
# P1-3: File validation — size and content-type guards
# ---------------------------------------------------------------------------

class TestFileValidation:
    def test_rejects_file_over_10mb(self):
        eleven_mb = io.BytesIO(b"\xff\xd8\xff" + b"\x00" * (11 * 1024 * 1024))
        resp = client.post(
            "/find-me",
            files={"file": ("big.jpg", eleven_mb, "image/jpeg")},
        )
        assert resp.status_code == 400
        assert "too large" in resp.json().get("detail", "").lower()

    def test_rejects_non_image_content_type(self):
        resp = client.post(
            "/find-me",
            files={"file": ("resume.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
        )
        assert resp.status_code == 400

    def test_accepts_jpeg(self):
        pool, conn = _pool_returning([])
        with patch.object(app_module, "rek", _rek_returning([])), \
             patch.object(app_module, "pool", pool):
            resp = client.post(
                "/find-me",
                files={"file": ("photo.jpg", io.BytesIO(b"\xff\xd8\xff" + b"\x00" * 100), "image/jpeg")},
            )
        assert resp.status_code == 200

    def test_accepts_png(self):
        pool, conn = _pool_returning([])
        with patch.object(app_module, "rek", _rek_returning([])), \
             patch.object(app_module, "pool", pool):
            resp = client.post(
                "/find-me",
                files={"file": ("photo.png", io.BytesIO(b"\x89PNG\r\n" + b"\x00" * 100), "image/png")},
            )
        assert resp.status_code == 200
