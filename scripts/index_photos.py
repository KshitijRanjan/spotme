import os, psycopg2, boto3, io, time
from PIL import Image
from dotenv import load_dotenv
from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.http import MediaIoBaseDownload

load_dotenv()

# 1. AWS Configuration
aws_kwargs = {
    'region_name': os.getenv('AWS_REGION'),
    'aws_access_key_id': os.getenv('AWS_ACCESS_KEY_ID'),
    'aws_secret_access_key': os.getenv('AWS_SECRET_ACCESS_KEY')
}
rek = boto3.client('rekognition', **aws_kwargs)

# 2. Google Drive Configuration
creds = service_account.Credentials.from_service_account_file(
    os.getenv('GOOGLE_APPLICATION_CREDENTIALS', 'service_account.json'),
    scopes=['https://www.googleapis.com/auth/drive.readonly']
)
drive = build('drive', 'v3', credentials=creds)

# 3. Supabase PostgreSQL connection
db = psycopg2.connect(os.getenv('DATABASE_URL'))
db.autocommit = False
with db.cursor() as cur:
    cur.execute('CREATE TABLE IF NOT EXISTS matches (face_id TEXT, drive_id TEXT)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_face_id ON matches(face_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_drive_id ON matches(drive_id)')
db.commit()

def get_indexed_ids():
    with db.cursor() as cursor:
        cursor.execute('SELECT DISTINCT drive_id FROM matches')
        return {row[0] for row in cursor.fetchall()}

def run_indexing():
    COLLECTION_ID = os.getenv('REKOGNITION_COLLECTION_ID')
    indexed_ids = get_indexed_ids()
    print(f"🔄 Resuming... {len(indexed_ids)} files already in database.")

    try:
        rek.create_collection(CollectionId=COLLECTION_ID)
    except rek.exceptions.ResourceAlreadyExistsException:
        pass

    # --- PAGINATION LOGIC START ---
    page_token = None
    processed_count = 0
    
    while True:
        query = f"'{os.getenv('GDRIVE_FOLDER_ID')}' in parents and mimeType contains 'image/'"
        results = drive.files().list(
            q=query, 
            fields="nextPageToken, files(id, name)",
            pageSize=100, # Smaller pages reduce memory spikes
            pageToken=page_token
        ).execute()

        files = results.get('files', [])
        
        for file in files:
            if file['id'] in indexed_ids:
                continue
            
            processed_count += 1
            print(f"[{processed_count}] Indexing: {file['name']}...")
            
            try:
                # Download from Drive
                request = drive.files().get_media(fileId=file['id'])
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                
                image_bytes = fh.getvalue()

                # AWS Byte Limit Check (5MB)
                if len(image_bytes) > 5242880:
                    img = Image.open(io.BytesIO(image_bytes))
                    if img.mode in ("RGBA", "P"): img = img.convert("RGB")
                    out_fh = io.BytesIO()
                    img.save(out_fh, format="JPEG", quality=80, optimize=True)
                    image_bytes = out_fh.getvalue()

                # Index in Rekognition
                response = rek.index_faces(
                    CollectionId=COLLECTION_ID,
                    Image={'Bytes': image_bytes},
                    ExternalImageId=file['id'].replace('-', '_')
                )
                
                # Store mappings in Supabase PostgreSQL
                with db.cursor() as cur:
                    for record in response['FaceRecords']:
                        cur.execute(
                            'INSERT INTO matches (face_id, drive_id) VALUES (%s, %s)',
                            (record['Face']['FaceId'], file['id'])
                        )
                db.commit()
                
                # Prevent API rate-limiting
                time.sleep(0.1)

            except Exception as e:
                print(f"  ❌ Error on {file['name']}: {e}")

        # Check if there is another page of files
        page_token = results.get('nextPageToken')
        if not page_token:
            break
    # --- PAGINATION LOGIC END ---

    db.close()
    print("\nAll pages processed. Gallery is fully indexed.")

if __name__ == "__main__":
    run_indexing()