import asyncio
import os
import psycopg2
import psycopg2.pool
import boto3
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from dotenv import load_dotenv

load_dotenv()

MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
RATE_LIMIT = os.getenv("RATE_LIMIT", "10/minute")

limiter = Limiter(key_func=get_remote_address)
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS — only allow requests from the deployed Streamlit frontend
# Set ALLOWED_ORIGINS to your Streamlit app URL in the Render dashboard
allowed_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# AWS Rekognition client
rek = boto3.client(
    'rekognition',
    region_name=os.getenv('AWS_REGION'),
    aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
    aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
)

# Connection pool: min 1, max 10 connections.
# Supabase free tier allows ~60 concurrent connections; 10 leaves headroom.
_db_url = os.getenv('DATABASE_URL', '')
# Supabase requires SSL; append sslmode if not already present
if _db_url and 'sslmode' not in _db_url:
    _db_url += '?sslmode=require'
print(f"Connecting to DB (url length={len(_db_url)})...", flush=True)
try:
    pool = psycopg2.pool.ThreadedConnectionPool(1, 10, _db_url)
    print("DB pool created OK.", flush=True)
except Exception as _e:
    print(f"FATAL: DB pool init failed: {_e}", flush=True)
    raise


def _query_drive_ids(face_ids: list) -> list:
    """Synchronous DB query dispatched via asyncio.to_thread to avoid blocking the event loop."""
    conn = pool.getconn()
    try:
        cursor = conn.cursor()
        placeholders = ','.join(['%s'] * len(face_ids))
        cursor.execute(
            f"SELECT DISTINCT drive_id FROM matches WHERE face_id IN ({placeholders})",
            face_ids,
        )
        return [row[0] for row in cursor.fetchall()]
    finally:
        pool.putconn(conn)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/find-me")
@limiter.limit(RATE_LIMIT)
async def find_me(request: Request, file: UploadFile = File(...)):
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Only JPEG, PNG, WebP, and HEIC images are accepted. Got: {file.content_type}",
        )

    contents = await file.read()

    if len(contents) > MAX_FILE_BYTES:
        raise HTTPException(status_code=400, detail="Image too large. Maximum size is 10 MB.")

    try:
        # MaxFaces=2000 avoids API timeouts while capturing all matches.
        # FaceMatchThreshold=70 is lowered for better wedding day matching.
        response = rek.search_faces_by_image(
            CollectionId=os.getenv('REKOGNITION_COLLECTION_ID'),
            Image={'Bytes': contents},
            MaxFaces=2000,
            FaceMatchThreshold=70,
        )
    except Exception as e:
        print(f"AWS Error: {e}")
        return {"error": "Search failed. AWS could not process the image."}

    face_ids = [match['Face']['FaceId'] for match in response['FaceMatches']]

    if not face_ids:
        return {"links": [], "drive_ids": []}

    drive_ids = sorted(await asyncio.to_thread(_query_drive_ids, face_ids))
    links = [f"https://drive.google.com/file/d/{d_id}/view" for d_id in drive_ids]
    return {"links": links, "drive_ids": drive_ids}
