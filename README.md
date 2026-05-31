---
title: SpotMe
emoji: 📸
colorFrom: purple
colorTo: pink
sdk: streamlit
sdk_version: 1.44.0
app_file: frontend.py
pinned: false
---

# SpotMe 📸

Find yourself in wedding photos — just take a selfie.

## What it does

1. Take a selfie in the browser
2. AWS Rekognition matches your face against every photo in the wedding gallery
3. See all photos you appear in, paginated in a grid
4. Download all your photos as a single ZIP file

## Stack

| Layer | Tech |
|-------|------|
| Frontend | Streamlit |
| Backend API | FastAPI + Uvicorn |
| Face matching | AWS Rekognition |
| Photo storage | Google Drive |
| Database | Supabase (PostgreSQL) |
| Deployment | HuggingFace Spaces (frontend) + Render (backend) |

## Architecture

```
Browser (selfie)
    ↓ POST /find-me
FastAPI backend (Render)
    ↓ SearchFacesByImage
AWS Rekognition (face collection)
    ↓ face_ids
Supabase DB  →  drive_ids
    ↓
Streamlit frontend
    ↓ renders thumbnails from Google Drive
    ↓ ZIP download (server-side, all photos)
```

## Running locally

### Backend

```bash
cd "Photo Search"
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in credentials
uvicorn app:app --reload --port 8000
```

### Frontend

```bash
# in a second terminal, same venv
BACKEND_URL=http://127.0.0.1:8000 streamlit run frontend.py
```

## Environment variables

| Variable | Description |
|----------|-------------|
| `AWS_ACCESS_KEY_ID` | AWS credentials with Rekognition access |
| `AWS_SECRET_ACCESS_KEY` | AWS credentials |
| `AWS_REGION` | e.g. `eu-west-1` |
| `DATABASE_URL` | Supabase PostgreSQL connection string |
| `ALLOWED_ORIGINS` | Comma-separated frontend URLs for CORS |
| `RATE_LIMIT` | API rate limit, default `10/minute` |
| `BACKEND_URL` | Frontend uses this to reach the API |

## Database schema

```sql
CREATE TABLE matches (
    face_id  TEXT NOT NULL,
    drive_id TEXT NOT NULL
);
CREATE INDEX ON matches (face_id);
```

## Indexing photos

Scripts in `scripts/` handle ingesting photos from Google Drive into the Rekognition collection and populating the `matches` table.

## Security

- Selfies are never stored — processed in memory and discarded
- API rate-limited to prevent abuse
- CORS locked to the deployed frontend URL
- Google Drive files served via direct Drive links (no proxy)
