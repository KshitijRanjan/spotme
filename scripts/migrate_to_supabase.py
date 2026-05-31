"""
One-time migration script: copies all (face_id, drive_id) rows from the
local metadata.db SQLite file into Supabase PostgreSQL.

Run this ONCE from your local machine after setting up Supabase.
No AWS calls are made — it only reads from the local file and writes to the cloud.

Usage:
    python scripts/migrate_to_supabase.py
"""

import os, sqlite3, psycopg2
from dotenv import load_dotenv

load_dotenv()

SQLITE_PATH = os.path.join(os.path.dirname(__file__), '..', 'metadata.db')


def migrate():
    # Read from local SQLite
    print(f"Reading from {SQLITE_PATH}...")
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    rows = sqlite_conn.execute('SELECT face_id, drive_id FROM matches').fetchall()
    sqlite_conn.close()
    print(f"Found {len(rows):,} rows to migrate.")

    if not rows:
        print("Nothing to migrate. Exiting.")
        return

    # Connect to Supabase PostgreSQL
    print("Connecting to Supabase...")
    pg_conn = psycopg2.connect(os.getenv('DATABASE_URL'))
    cursor = pg_conn.cursor()

    # Ensure table and index exist
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS matches (
            face_id  TEXT,
            drive_id TEXT
        )
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_drive_id ON matches(drive_id)
    ''')

    # Bulk insert using executemany for speed
    print("Inserting rows into Supabase...")
    cursor.executemany(
        'INSERT INTO matches (face_id, drive_id) VALUES (%s, %s)',
        rows
    )
    pg_conn.commit()
    pg_conn.close()

    print(f"Done. {len(rows):,} rows successfully migrated to Supabase.")


if __name__ == "__main__":
    migrate()
