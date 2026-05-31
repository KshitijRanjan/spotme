# SpotMe — CLAUDE.md

## Project overview

Wedding photo finder. User takes a selfie → backend matches face via AWS Rekognition → returns Google Drive IDs of matching photos → frontend shows grid + ZIP download.

## Stack

- `frontend.py` — Streamlit UI (HuggingFace Spaces)
- `app.py` — FastAPI backend (Render)
- AWS Rekognition — face collection + search
- Supabase (PostgreSQL) — maps `face_id` → `drive_id`
- Google Drive — photo storage

## Commands

```bash
# Backend
uvicorn app:app --reload --port 8000

# Frontend
BACKEND_URL=http://127.0.0.1:8000 streamlit run frontend.py
```

## Key files

| File | Purpose |
|------|---------|
| `app.py` | FastAPI `/find-me` endpoint; Rekognition search; DB query |
| `frontend.py` | Streamlit UI; selfie capture; results grid; ZIP download |
| `requirements.txt` | All dependencies (backend + frontend combined) |
| `Dockerfile` | HuggingFace Spaces container config |
| `scripts/` | One-off scripts to index photos into Rekognition + DB |

## Architecture notes

- `frontend.py` calls `BACKEND_URL/find-me` (POST, multipart selfie)
- Backend returns `{"drive_ids": [...]}` — list of Google Drive file IDs
- Frontend fetches thumbnails via `https://drive.google.com/thumbnail?id={id}&sz=w400`
- ZIP download: frontend fetches each Drive file via `requests.get`, zips in-memory with `zipfile`, serves via `st.download_button`
- Selfies never stored — processed in memory only

## Environment variables (required)

```
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
AWS_REGION
DATABASE_URL          # Supabase postgres URL (sslmode=require added automatically)
ALLOWED_ORIGINS       # comma-separated CORS origins
RATE_LIMIT            # default: 10/minute
BACKEND_URL           # frontend → backend URL
```

## Deployment

- **Frontend**: HuggingFace Spaces (`ranjaksi/spotme`) — push to `hf` remote
- **Backend**: Render — auto-deploys from GitHub `main`
- **GitHub remote**: `origin` → `KshitijRanjan/spotme`
- **HuggingFace remote**: `hf` → `https://huggingface.co/spaces/ranjaksi/spotme`

To deploy frontend changes:
```bash
git push origin main   # GitHub
git push hf main       # HuggingFace (triggers Space rebuild)
```

## DB schema

```sql
CREATE TABLE matches (
    face_id  TEXT NOT NULL,
    drive_id TEXT NOT NULL
);
CREATE INDEX ON matches (face_id);
```

## Constraints

- `app.py` is the sole backend file — keep it that way unless asked to split
- Max upload: 10 MB, allowed types: jpeg/png/webp/heic/heif
- DB pool: min 1, max 10 (Supabase free tier cap ~60 connections)
- Pagination: 24 photos per page
