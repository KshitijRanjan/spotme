"""
Tests for scripts/reindex.py

Covers:
  - reset_collection / truncate_matches (destructive helpers)
  - index_and_store / compress_if_needed (per-photo logic)
  - get_indexed_ids (incremental skip logic)
  - run(prepare_only=True)  → clears only, no Drive calls
  - run(prepare_only=False) → incremental, skips already-indexed photos
"""
import io
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Import reindex with all external libs stubbed
# ---------------------------------------------------------------------------

def _load_reindex():
    """Import scripts/reindex.py with network libs mocked."""
    import importlib, importlib.util

    stubs = {
        "boto3": MagicMock(),
        "psycopg2": MagicMock(),
        "PIL": MagicMock(),
        "PIL.Image": MagicMock(),
        "googleapiclient": MagicMock(),
        "googleapiclient.discovery": MagicMock(),
        "googleapiclient.http": MagicMock(),
        "google": MagicMock(),
        "google.oauth2": MagicMock(),
        "google.oauth2.service_account": MagicMock(),
        "httplib2": MagicMock(),
        "google_auth_httplib2": MagicMock(),
        "dotenv": MagicMock(),
    }
    # Patch env vars so load_dotenv() is a no-op
    with patch.dict("sys.modules", stubs):
        with patch.dict("os.environ", {
            "REKOGNITION_COLLECTION_ID": "wedding_faces",
            "GDRIVE_FOLDER_ID": "test-folder-id",
            "AWS_REGION": "eu-central-1",
            "AWS_ACCESS_KEY_ID": "fake",
            "AWS_SECRET_ACCESS_KEY": "fake",
            "DATABASE_URL": "postgresql://fake",
            "GOOGLE_APPLICATION_CREDENTIALS": "fake.json",
        }):
            spec = importlib.util.spec_from_file_location(
                "reindex", ROOT / "scripts" / "reindex.py"
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def reindex():
    return _load_reindex()


# ---------------------------------------------------------------------------
# reset_collection
# ---------------------------------------------------------------------------

class TestResetCollection:
    def test_deletes_then_creates_collection(self, reindex):
        fake_rek = MagicMock()
        with patch.object(reindex, "rek", fake_rek), \
             patch.object(reindex, "COLLECTION_ID", "wedding_faces"):
            reindex.reset_collection()

        fake_rek.delete_collection.assert_called_once_with(CollectionId="wedding_faces")
        fake_rek.create_collection.assert_called_once_with(CollectionId="wedding_faces")

    def test_skips_delete_when_collection_missing(self, reindex):
        """ResourceNotFoundException on delete must not abort — just skip."""
        fake_rek = MagicMock()
        fake_rek.exceptions.ResourceNotFoundException = Exception
        fake_rek.delete_collection.side_effect = Exception("ResourceNotFoundException")

        with patch.object(reindex, "rek", fake_rek), \
             patch.object(reindex, "COLLECTION_ID", "wedding_faces"):
            reindex.reset_collection()  # must not raise

        fake_rek.create_collection.assert_called_once_with(CollectionId="wedding_faces")


# ---------------------------------------------------------------------------
# truncate_matches
# ---------------------------------------------------------------------------

class TestTruncateMatches:
    def test_drops_and_recreates_matches_table(self, reindex):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = lambda s: cursor
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(reindex, "db", conn):
            reindex.truncate_matches()

        all_sql = [c[0][0].strip().upper() for c in cursor.execute.call_args_list]
        assert any("DROP TABLE IF EXISTS" in s and "MATCHES" in s for s in all_sql)
        assert any("CREATE TABLE" in s and "MATCHES" in s for s in all_sql)
        assert any("IDX_FACE_ID" in s for s in all_sql)
        assert any("IDX_DRIVE_ID" in s for s in all_sql)
        conn.commit.assert_called_once()


# ---------------------------------------------------------------------------
# index_and_store
# ---------------------------------------------------------------------------

class TestIndexAndStore:
    def _make_rek_response(self, face_ids):
        return {
            "FaceRecords": [{"Face": {"FaceId": fid}} for fid in face_ids]
        }

    def test_inserts_one_row_per_detected_face(self, reindex):
        fake_rek = MagicMock()
        fake_rek.index_faces.return_value = self._make_rek_response(["face-1", "face-2"])

        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = lambda s: cursor
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(reindex, "rek", fake_rek), \
             patch.object(reindex, "db", conn), \
             patch.object(reindex, "download_photo", return_value=b"\xff\xd8\xff" + b"\x00" * 100), \
             patch.object(reindex, "compress_if_needed", side_effect=lambda b: b):
            n = reindex.index_and_store({"id": "drive-xyz", "name": "photo.jpg"})

        assert n == 2
        cursor.executemany.assert_called_once()
        rows = cursor.executemany.call_args[0][1]
        assert ("face-1", "drive-xyz") in rows
        assert ("face-2", "drive-xyz") in rows
        conn.commit.assert_called_once()

    def test_returns_zero_and_skips_insert_when_no_face_detected(self, reindex):
        fake_rek = MagicMock()
        fake_rek.index_faces.return_value = self._make_rek_response([])

        conn = MagicMock()

        with patch.object(reindex, "rek", fake_rek), \
             patch.object(reindex, "db", conn), \
             patch.object(reindex, "download_photo", return_value=b"\xff\xd8\xff"), \
             patch.object(reindex, "compress_if_needed", side_effect=lambda b: b):
            n = reindex.index_and_store({"id": "drive-xyz", "name": "photo.jpg"})

        assert n == 0
        conn.cursor.assert_not_called()
        conn.commit.assert_not_called()

    def test_drive_id_hyphen_replaced_with_underscore_for_external_id(self, reindex):
        """Rekognition ExternalImageId only allows alphanumeric + underscore."""
        fake_rek = MagicMock()
        fake_rek.index_faces.return_value = self._make_rek_response(["face-1"])

        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__ = lambda s: cursor
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(reindex, "rek", fake_rek), \
             patch.object(reindex, "db", conn), \
             patch.object(reindex, "download_photo", return_value=b"\xff\xd8\xff"), \
             patch.object(reindex, "compress_if_needed", side_effect=lambda b: b):
            reindex.index_and_store({"id": "drive-abc-123", "name": "photo.jpg"})

        call_kwargs = fake_rek.index_faces.call_args[1]
        assert call_kwargs["ExternalImageId"] == "drive_abc_123"


# ---------------------------------------------------------------------------
# compress_if_needed
# ---------------------------------------------------------------------------

class TestCompressIfNeeded:
    def test_returns_bytes_unchanged_when_under_threshold(self, reindex):
        # Anything under _COMPRESS_THRESHOLD (4 MB) is returned as-is
        small = b"\xff\xd8\xff" + b"\x00" * 100
        result = reindex.compress_if_needed(small)
        assert result == small

    def test_compresses_when_over_threshold(self, reindex):
        """Image ≥ 4 MB should be compressed to under 5 MB on first quality pass."""
        large = b"\xff\xd8\xff" + b"\x00" * (5 * 1024 * 1024)
        fake_img = MagicMock()
        fake_img.mode = "RGB"
        fake_img.size = (3000, 2000)  # under _MAX_LONG_SIDE, no resize needed

        # First save call returns bytes under 5 MB limit
        compressed = b"compressed-jpeg-data"

        def fake_save(buf, **kwargs):
            buf.write(compressed)

        fake_img.save = fake_save

        with patch.object(reindex, "Image") as mock_image_mod:
            mock_image_mod.open.return_value = fake_img
            mock_image_mod.LANCZOS = MagicMock()
            result = reindex.compress_if_needed(large)

        assert result == compressed

    def test_retries_lower_quality_until_under_limit(self, reindex):
        """If quality=80 still exceeds 5 MB, tries lower qualities until it fits."""
        large = b"\xff\xd8\xff" + b"\x00" * (5 * 1024 * 1024)
        fake_img = MagicMock()
        fake_img.mode = "RGB"
        fake_img.size = (3000, 2000)

        too_big = b"\x00" * (5 * 1024 * 1024 + 1)   # > 5 MB — will be rejected
        small_enough = b"small"

        call_count = 0

        def fake_save(buf, **kwargs):
            nonlocal call_count
            call_count += 1
            # First call (quality=80) too big, second call (quality=60) small enough
            buf.write(too_big if call_count == 1 else small_enough)

        fake_img.save = fake_save

        with patch.object(reindex, "Image") as mock_image_mod:
            mock_image_mod.open.return_value = fake_img
            mock_image_mod.LANCZOS = MagicMock()
            result = reindex.compress_if_needed(large)

        assert result == small_enough
        assert call_count == 2


# ---------------------------------------------------------------------------
# get_indexed_ids  (new — supports incremental mode)
# ---------------------------------------------------------------------------

class TestGetIndexedIds:
    def _make_conn(self, rows):
        cursor = MagicMock()
        cursor.fetchall.return_value = rows
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = lambda s: cursor
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn, cursor

    def test_returns_set_of_drive_ids_from_matches(self, reindex):
        conn, _ = self._make_conn([("id-1",), ("id-2",)])
        with patch.object(reindex, "db", conn):
            result = reindex.get_indexed_ids()
        assert result == {"id-1", "id-2"}

    def test_returns_empty_set_when_table_is_empty(self, reindex):
        conn, _ = self._make_conn([])
        with patch.object(reindex, "db", conn):
            result = reindex.get_indexed_ids()
        assert result == set()


# ---------------------------------------------------------------------------
# run(prepare_only=True)  — --prepare flag behaviour
# ---------------------------------------------------------------------------

class TestPrepareMode:
    def _make_db_conn(self):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = lambda s: cursor
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn, cursor

    def test_prepare_resets_collection_and_truncates_table(self, reindex):
        fake_rek = MagicMock()
        conn, cursor = self._make_db_conn()

        with patch.object(reindex, "rek", fake_rek), \
             patch.object(reindex, "db", conn), \
             patch.object(reindex, "confirm", lambda _: None):
            reindex.run(prepare_only=True)

        fake_rek.delete_collection.assert_called_once()
        fake_rek.create_collection.assert_called_once()
        all_sql = [c[0][0].strip().upper() for c in cursor.execute.call_args_list]
        assert any("MATCHES" in s for s in all_sql)

    def test_prepare_does_not_touch_drive(self, reindex):
        fake_rek = MagicMock()
        conn, _ = self._make_db_conn()
        fake_drive = MagicMock()

        with patch.object(reindex, "rek", fake_rek), \
             patch.object(reindex, "db", conn), \
             patch.object(reindex, "drive", fake_drive), \
             patch.object(reindex, "confirm", lambda _: None):
            reindex.run(prepare_only=True)

        fake_drive.files.assert_not_called()


# ---------------------------------------------------------------------------
# run(prepare_only=False)  — incremental indexing
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# list_all_images  — recursive Drive traversal
# ---------------------------------------------------------------------------

class TestListAllImages:
    def _make_drive_mock(self, pages_by_folder: dict):
        """
        pages_by_folder maps folder_id -> list of file dicts.
        Each call to list().execute() returns the matching folder's files.
        """
        fake_drive = MagicMock()

        def fake_execute():
            # inspect the most recent list() call's 'q' kwarg to pick the right folder
            list_call = fake_drive.files.return_value.list.call_args
            q = list_call[1].get("q", list_call[0][0] if list_call[0] else "")
            for fid, files in pages_by_folder.items():
                if fid in q:
                    return {"files": files}
            return {"files": []}

        fake_drive.files.return_value.list.return_value.execute.side_effect = fake_execute
        return fake_drive

    def test_yields_images_in_root_folder(self, reindex):
        img = {"id": "img-1", "name": "a.jpg", "mimeType": "image/jpeg"}
        fake_drive = self._make_drive_mock({"root-folder": [img]})

        with patch.object(reindex, "drive", fake_drive), \
             patch.object(reindex, "GDRIVE_FOLDER_ID", "root-folder"):
            results = list(reindex.list_all_images("root-folder"))

        assert results == [img]

    def test_recurses_into_subfolders(self, reindex):
        subfolder = {"id": "sub-1", "name": "Day1", "mimeType": "application/vnd.google-apps.folder"}
        img_root = {"id": "img-root", "name": "cover.jpg", "mimeType": "image/jpeg"}
        img_sub = {"id": "img-sub", "name": "ceremony.jpg", "mimeType": "image/jpeg"}

        fake_drive = self._make_drive_mock({
            "root-folder": [subfolder, img_root],
            "sub-1": [img_sub],
        })

        with patch.object(reindex, "drive", fake_drive):
            results = list(reindex.list_all_images("root-folder"))

        assert img_sub in results
        assert img_root in results
        assert len(results) == 2

    def test_skips_non_image_non_folder_files(self, reindex):
        pdf = {"id": "doc-1", "name": "readme.pdf", "mimeType": "application/pdf"}
        img = {"id": "img-1", "name": "a.jpg", "mimeType": "image/jpeg"}
        fake_drive = self._make_drive_mock({"root-folder": [pdf, img]})

        with patch.object(reindex, "drive", fake_drive):
            results = list(reindex.list_all_images("root-folder"))

        assert results == [img]


# ---------------------------------------------------------------------------
# run(prepare_only=False)  — incremental indexing
# ---------------------------------------------------------------------------

class TestIncrementalMode:
    def _image(self, id_, name="photo.jpg"):
        return {"id": id_, "name": name, "mimeType": "image/jpeg"}

    def _make_drive_mock(self, files):
        """Single-page Drive mock (all files in one folder, no subfolders)."""
        drive = MagicMock()
        drive.files.return_value.list.return_value.execute.return_value = {
            "files": files,
        }
        return drive

    def test_skips_already_indexed_photos(self, reindex):
        already = {"drive-old"}
        old_file = self._image("drive-old", "old.jpg")
        new_file = self._image("drive-new", "new.jpg")

        fake_drive = self._make_drive_mock([old_file, new_file])

        with patch.object(reindex, "drive", fake_drive), \
             patch.object(reindex, "get_indexed_ids", return_value=already), \
             patch.object(reindex, "index_and_store", return_value=2) as mock_idx, \
             patch.object(reindex, "time") as mock_time:
            mock_time.sleep = MagicMock()
            reindex.run(prepare_only=False)

        mock_idx.assert_called_once_with(new_file)

    def test_indexes_all_photos_when_none_indexed_yet(self, reindex):
        files = [self._image("drive-a", "a.jpg"), self._image("drive-b", "b.jpg")]
        fake_drive = self._make_drive_mock(files)

        with patch.object(reindex, "drive", fake_drive), \
             patch.object(reindex, "get_indexed_ids", return_value=set()), \
             patch.object(reindex, "index_and_store", return_value=1) as mock_idx, \
             patch.object(reindex, "time") as mock_time:
            mock_time.sleep = MagicMock()
            reindex.run(prepare_only=False)

        assert mock_idx.call_count == 2

    def test_incremental_does_not_reset_collection_or_truncate(self, reindex):
        fake_rek = MagicMock()
        fake_drive = self._make_drive_mock([])

        with patch.object(reindex, "rek", fake_rek), \
             patch.object(reindex, "drive", fake_drive), \
             patch.object(reindex, "get_indexed_ids", return_value=set()):
            reindex.run(prepare_only=False)

        fake_rek.delete_collection.assert_not_called()
        fake_rek.create_collection.assert_not_called()
