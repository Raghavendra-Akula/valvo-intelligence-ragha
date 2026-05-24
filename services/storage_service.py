"""Supabase Storage Service — Upload chart images to Supabase Storage
Images persist permanently unlike local uploads/ folder on Cloud Run
"""
import os
import requests
import uuid
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv('SUPABASE_URL')  # e.g. https://xxxxx.supabase.co
SUPABASE_KEY = os.getenv('SUPABASE_KEY')  # service_role key (not anon)
BUCKET_NAME = 'charts'


def get_headers():
    return {
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'apikey': SUPABASE_KEY,
    }


def ensure_bucket_exists():
    """Create the charts bucket if it doesn't exist"""
    url = f'{SUPABASE_URL}/storage/v1/bucket'
    try:
        r = requests.get(f'{url}/{BUCKET_NAME}', headers=get_headers(), timeout=30)
        if r.status_code == 200:
            return True

        r = requests.post(url, headers={**get_headers(), 'Content-Type': 'application/json'}, json={
            'id': BUCKET_NAME,
            'name': BUCKET_NAME,
            'public': True,
        }, timeout=30)
        if r.status_code in (200, 201):
            print(f"✅ Created Supabase bucket: {BUCKET_NAME}")
            return True
        else:
            print(f"⚠️ Bucket creation response: {r.status_code} {r.text}")
            return False
    except Exception as e:
        print(f"❌ Bucket check failed: {e}")
        return False


def upload_chart_image(file_path, stock_name="chart"):
    """
    Upload a chart image to Supabase Storage
    Returns the public URL of the uploaded image
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("⚠️ SUPABASE_URL or SUPABASE_KEY not set, skipping upload")
        return None

    try:
        ensure_bucket_exists()

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_name = stock_name.replace(' ', '_').replace('.', '').lower()[:30]
        unique_id = uuid.uuid4().hex[:8]
        ext = os.path.splitext(file_path)[1] or '.png'
        filename = f"{safe_name}_{timestamp}_{unique_id}{ext}"

        with open(file_path, 'rb') as f:
            file_data = f.read()

        content_type = 'image/png'
        if ext.lower() in ('.jpg', '.jpeg'):
            content_type = 'image/jpeg'
        elif ext.lower() == '.webp':
            content_type = 'image/webp'

        url = f'{SUPABASE_URL}/storage/v1/object/{BUCKET_NAME}/{filename}'
        headers = {
            **get_headers(),
            'Content-Type': content_type,
        }
        r = requests.post(url, headers=headers, data=file_data, timeout=30)

        if r.status_code in (200, 201):
            public_url = f'{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{filename}'
            print(f"✅ Chart uploaded: {public_url}")
            return public_url
        else:
            print(f"❌ Upload failed: {r.status_code} {r.text}")
            return None

    except Exception as e:
        print(f"❌ Upload error: {e}")
        return None


def delete_chart_image(image_url):
    """Delete a chart image from Supabase Storage"""
    if not image_url or not SUPABASE_URL:
        return False
    try:
        filename = image_url.split(f'{BUCKET_NAME}/')[-1]
        url = f'{SUPABASE_URL}/storage/v1/object/{BUCKET_NAME}'
        r = requests.delete(url, headers={**get_headers(), 'Content-Type': 'application/json'}, json={
            'prefixes': [filename]
        }, timeout=30)
        return r.status_code in (200, 201)
    except Exception as e:
        print(f"❌ Delete error: {e}")
        return False