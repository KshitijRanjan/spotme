"""
scripts/reindex.py — Index edited photos into the face-recognition pipeline.

Two modes:

  --prepare   DESTRUCTIVE. Deletes the Rekognition collection and truncates
              the matches table. Run once before your first incremental pass.
              No photos are indexed.

  (default)   INCREMENTAL. Indexes photos from GDRIVE_FOLDER_ID that are not
              yet in the matches table. Safe to re-run as Drive upload
              progresses. Will not clear any existing data.

Typical workflow while uploading edited photos to Drive:

    # Run once now — clears old raw-photo data
    python scripts/reindex.py --prepare

    # Run repeatedly as photos land on Drive
    python scripts/reindex.py

    # Final run after upload is 100% complete
    python scripts/reindex.py
"""

import io
import os
import sys
import time

import boto3
import httplib2
import google_auth_httplib2
import psycopg2
from PIL import Image
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

load_dotenv()

COLLECTION_ID = os.getenv('REKOGNITION_COLLECTION_ID')
GDRIVE_FOLDER_ID = os.getenv('GDRIVE_FOLDER_ID')

rek = boto3.client(
    'rekognition',
    region_name=os.getenv('AWS_REGION'),
    aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
    aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
)

creds = service_account.Credentials.from_service_account_file(
    os.getenv('GOOGLE_APPLICATION_CREDENTIALS', 'service_account.json'),
    scopes=['https://www.googleapis.com/auth/drive.readonly'],
)
# Build Drive client with explicit 5-minute HTTP timeout.
# Default httplib2 transport has a short socket timeout — large edited JPEGs fail.
_http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=300))
drive = build('drive', 'v3', http=_http)

db = psycopg2.connect(os.getenv('DATABASE_URL'))
db.autocommit = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def confirm(prompt: str) -> None:
    answer = input(prompt + " [yes/N]: ").strip().lower()
    if answer != "yes":
        print("Aborted.")
        sys.exit(0)


def reset_collection() -> None:
    print(f"\nDeleting Rekognition collection '{COLLECTION_ID}'...")
    try:
        rek.delete_collection(CollectionId=COLLECTION_ID)
        print("  ✓ Deleted.")
    except rek.exceptions.ResourceNotFoundException:
        print("  Collection did not exist — skipping delete.")

    print(f"Creating fresh collection '{COLLECTION_ID}'...")
    rek.create_collection(CollectionId=COLLECTION_ID)
    print("  ✓ Created.")


def truncate_matches() -> None:
    print("\nSetting up matches table...")
    with db.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS matches")
        cur.execute("CREATE TABLE matches (face_id TEXT, drive_id TEXT)")
        cur.execute("CREATE INDEX idx_face_id ON matches(face_id)")
        cur.execute("CREATE INDEX idx_drive_id ON matches(drive_id)")
    db.commit()
    print("  ✓ Done.")


def get_indexed_ids() -> set:
    """Return drive_ids already present in the matches table."""
    with db.cursor() as cur:
        cur.execute("SELECT DISTINCT drive_id FROM matches")
        return {row[0] for row in cur.fetchall()}


def download_photo(file_id: str) -> bytes:
    """Download with 3 retries and 50 MB chunks to handle large edited JPEGs."""
    for attempt in range(3):
        try:
            request = drive.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request, chunksize=50 * 1024 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk(num_retries=3)
            return fh.getvalue()
        except Exception as e:
            if attempt == 2:
                raise
            wait = 2 ** attempt  # 1s, 2s
            print(f" (retry {attempt + 1}/3 after {wait}s)", end="", flush=True)
            time.sleep(wait)


_REK_MAX_BYTES = 5 * 1024 * 1024          # Rekognition hard limit: 5 MB
_COMPRESS_THRESHOLD = 4 * 1024 * 1024    # compress any image ≥ 4 MB for safety margin
_MAX_LONG_SIDE = 4096                    # resize to this before quality loop

# Disable PIL decompression-bomb guard — wedding photos can be very large
Image.MAX_IMAGE_PIXELS = None


def compress_if_needed(image_bytes: bytes) -> bytes:
    """Return image bytes guaranteed to be under Rekognition's 5 MB limit.

    Steps:
      1. If already < 4 MB, return as-is.
      2. Open with PIL; resize if longer side > 4096 px.
      3. Iterative quality reduction (80 → 60 → 40 → 20).
      4. Last resort: halve dimensions again and save at quality=20.
    """
    if len(image_bytes) < _COMPRESS_THRESHOLD:
        return image_bytes

    img = Image.open(io.BytesIO(image_bytes))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    # Resize mega images so quality loop is more effective
    w, h = img.size
    long_side = max(w, h)
    if long_side > _MAX_LONG_SIDE:
        scale = _MAX_LONG_SIDE / long_side
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    for quality in (80, 60, 40, 20):
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        result = out.getvalue()
        if len(result) < _REK_MAX_BYTES:
            return result

    # Still too big — halve dimensions and retry at quality=20
    w, h = img.size
    img = img.resize((w // 2, h // 2), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=20, optimize=True)
    return out.getvalue()


def list_all_images(folder_id: str):
    """Recursively yield all image files under folder_id (depth-first, all pages)."""
    page_token = None
    while True:
        results = drive.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, mimeType)",
            pageSize=100,
            pageToken=page_token,
        ).execute()

        for item in results.get('files', []):
            if item['mimeType'] == 'application/vnd.google-apps.folder':
                print(f"  [folder] {item['name']}/")
                yield from list_all_images(item['id'])
            elif 'image/' in item.get('mimeType', ''):
                yield item

        page_token = results.get('nextPageToken')
        if not page_token:
            break


def index_and_store(file: dict) -> int:
    """Download, index, and store a single photo. Returns faces detected."""
    image_bytes = download_photo(file['id'])
    image_bytes = compress_if_needed(image_bytes)

    response = rek.index_faces(
        CollectionId=COLLECTION_ID,
        Image={'Bytes': image_bytes},
        ExternalImageId=file['id'].replace('-', '_'),
    )

    face_records = response.get('FaceRecords', [])
    if not face_records:
        return 0

    with db.cursor() as cur:
        cur.executemany(
            'INSERT INTO matches (face_id, drive_id) VALUES (%s, %s)',
            [(r['Face']['FaceId'], file['id']) for r in face_records],
        )
    db.commit()
    return len(face_records)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run(prepare_only: bool = False) -> None:
    if prepare_only:
        print("=" * 60)
        print("  PREPARE — clear old data for fresh reindex")
        print("=" * 60)
        print(f"\n  Collection  : {COLLECTION_ID}")
        print("\n  ⚠️  Deletes Rekognition collection + truncates matches table.")
        print("     Run 'python scripts/reindex.py' afterwards to index photos.\n")
        confirm("  Proceed?")
        reset_collection()
        truncate_matches()
        print("\nDone. Upload your photos to Drive, then run:")
        print("    python scripts/reindex.py")
        return

    # Incremental indexing — safe to re-run at any time
    already_indexed = get_indexed_ids()
    print(f"Resuming... {len(already_indexed)} photo(s) already indexed.\n")

    total_new = 0
    total_skipped_indexed = 0
    total_faces = 0
    total_errors = 0

    for file in list_all_images(GDRIVE_FOLDER_ID):
        if file['id'] in already_indexed:
            total_skipped_indexed += 1
            continue

        total_new += 1
        print(f"[+{total_new}] {file['name']}...", end=" ", flush=True)
        try:
            n_faces = index_and_store(file)
            total_faces += n_faces
            print(f"{n_faces} face(s)")
        except Exception as e:
            total_errors += 1
            print(f"ERROR — {e}")

        time.sleep(0.1)

    print(f"\nDone.")
    print(f"  New photos indexed : {total_new}")
    print(f"  Faces stored       : {total_faces}")
    print(f"  Already indexed    : {total_skipped_indexed}")
    print(f"  Errors             : {total_errors}")


if __name__ == "__main__":
    run(prepare_only="--prepare" in sys.argv)
