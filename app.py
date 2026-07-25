#!/usr/bin/env python3
"""
app.py - FastAPI web app do zarządzania drukowaniem zdjęć na templatce A4.
Monitoruje folder incoming/, pokazuje zdjęcia w GUI, pozwala akceptować/odrzucać.
Po zebraniu 8 slotów automatycznie generuje PDF.

Użycie:
    python app.py
    python app.py --port 8080
"""

import argparse
import asyncio
import base64
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from watchdog.events import PatternMatchingEventHandler

register_heif_opener()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
INCOMING_DIR = SCRIPT_DIR / 'incoming'
PENDING_DIR = SCRIPT_DIR / 'pending'
ARCHIVE_DIR = SCRIPT_DIR / 'archive'
REJECTED_DIR = SCRIPT_DIR / 'rejected'
BATCH_SIZE = 8
SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp', '.tiff', '.tif', '.heic', '.heif', '.avif'}
THUMBNAIL_MAX_SIZE = 300
PRINT_DPI = 300
PRINT_MM = 52
PRINT_PX = int(PRINT_MM / 25.4 * PRINT_DPI)

WEDDING_TEXT = os.getenv('WEDDING_TEXT', 'ŚLUB TOMASZA I DOMINIKI')
WEDDING_FONT_SIZE = int(os.getenv('WEDDING_FONT_SIZE', '7'))
DATE_TEXT = os.getenv('DATE_TEXT', '25 LIPIEC 2026')
DATE_FONT_SIZE = int(os.getenv('DATE_FONT_SIZE', '7'))

for d in [INCOMING_DIR, PENDING_DIR, ARCHIVE_DIR, REJECTED_DIR]:
    d.mkdir(parents=True, exist_ok=True)

app = FastAPI()
jinja_env = Environment(loader=FileSystemLoader(str(SCRIPT_DIR)))
html_template = jinja_env.get_template('template.jinja2')
index_template = jinja_env.get_template('templates/index.html')

state_lock = threading.Lock()
pending_photos: dict = {}
queue_slots: list = []
pdf_queue: list = []
used_photos: list = []
batch_history: list = []
rejected_files: list = []
connected_clients: set[WebSocket] = set()
main_event_loop = None


def square_crop_and_pad(img, size):
    img = ImageOps.exif_transpose(img)
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
    
    width, height = img.size
    if width == height:
        img_resized = img.resize((size, size), Image.LANCZOS)
        return img_resized
    
    if width > height:
        new_height = int(height * size / width)
        img_resized = img.resize((size, new_height), Image.LANCZOS)
        delta = (size - new_height) // 2
        img_padded = Image.new('RGB', (size, size), color='white')
        img_padded.paste(img_resized, (0, delta))
    else:
        new_width = int(width * size / height)
        img_resized = img.resize((new_width, size), Image.LANCZOS)
        delta = (size - new_width) // 2
        img_padded = Image.new('RGB', (size, size), color='white')
        img_padded.paste(img_resized, (delta, 0))
    
    return img_padded


def verify_and_convert(src_path):
    dest_path = src_path.with_suffix('.jpg')
    if dest_path == src_path:
        dest_path = src_path.with_name(src_path.stem + '_conv.jpg')

    try:
        if src_path.suffix.lower() in {'.heic', '.heif'}:
            img = Image.open(src_path)
            img = square_crop_and_pad(img, PRINT_PX)
            img.save(dest_path, 'JPEG', quality=90, subsampling=0)
            
            if src_path != dest_path:
                src_path.unlink(missing_ok=True)
            
            img_verify = Image.open(dest_path)
            img_verify.verify()
            return dest_path
        
        result = subprocess.run([
            'ffmpeg', '-y', '-i', str(src_path),
            '-map', '0:0',
            '-update', '1',
            '-vf', f'scale={PRINT_PX}:{PRINT_PX}:force_original_aspect_ratio=decrease,pad={PRINT_PX}:{PRINT_PX}:(ow-iw)/2:(oh-ih)/2',
            '-q:v', '2',
            str(dest_path),
        ], capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            log.error('ffmpeg błąd: %s', result.stderr[:200])
            return None

        if src_path != dest_path:
            src_path.unlink(missing_ok=True)

        img = Image.open(dest_path)
        img.verify()
        return dest_path
    except FileNotFoundError:
        log.error('ffmpeg nie znaleziony')
        return None
    except subprocess.TimeoutExpired:
        log.error('ffmpeg timeout')
        return None
    except Exception as e:
        log.error('Błąd konwersji: %s', e)
        return None


def make_thumbnail_data_uri(filepath):
    try:
        img = Image.open(filepath)
        img.thumbnail((THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE), Image.LANCZOS)
        buf = BytesIO()
        if img.mode in ('RGBA', 'LA', 'P'):
            img.save(buf, format='PNG')
            mime = 'image/png'
        else:
            img.save(buf, format='JPEG', quality=75)
            mime = 'image/jpeg'
        data = base64.b64encode(buf.getvalue()).decode('ascii')
        return f'data:{mime};base64,{data}'
    except Exception as e:
        log.error('Błąd miniatury: %s', e)
        return None


def full_image_data_uri(filepath):
    ext = filepath.suffix.lower()
    mime_map = {
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.png': 'image/png', '.bmp': 'image/bmp',
        '.gif': 'image/gif', '.webp': 'image/webp',
    }
    mime = mime_map.get(ext, 'image/jpeg')
    with open(filepath, 'rb') as f:
        data = base64.b64encode(f.read()).decode('ascii')
    return f'data:{mime};base64,{data}'


async def broadcast(message):
    dead = set()
    for ws in connected_clients.copy():
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    connected_clients.difference_update(dead)


def sync_broadcast(message):
    if main_event_loop and main_event_loop.is_running():
        asyncio.run_coroutine_threadsafe(broadcast(message), main_event_loop)


def get_state():
    return {
        'pending': [
            {'id': pid, 'filename': info['filename'], 'data_uri': info['data_uri']}
            for pid, info in pending_photos.items()
        ],
        'queue': [
            {
                'photo_id': s['photo_id'],
                'filename': s['filename'],
                'data_uri': make_thumbnail_data_uri(s['pending_path']),
            }
            for s in queue_slots
        ],
        'queue_count': len(queue_slots),
        'batch_size': BATCH_SIZE,
        'pdf_queue': [
            {
                'id': p['id'],
                'timestamp': p['timestamp'],
                'slot_count': p['slot_count'],
                'filenames': p['filenames'],
            }
            for p in pdf_queue
        ],
        'history': [
            {
                'timestamp': h['timestamp'],
                'slot_count': h['slot_count'],
                'filenames': h['filenames'],
            }
            for h in batch_history[-10:]
        ],
        'rejected': rejected_files[-50:],
    }


async def broadcast_state():
    with state_lock:
        state = get_state()
    state['type'] = 'state'
    await broadcast(state)


def sync_broadcast_state():
    if main_event_loop and main_event_loop.is_running():
        asyncio.run_coroutine_threadsafe(broadcast_state(), main_event_loop)


def generate_pdf_from_slots(slots, archive_dir):
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    batch_dir = archive_dir / f'batch_{timestamp}'
    batch_dir.mkdir(parents=True, exist_ok=True)

    seen_paths = set()
    photo_paths = []
    data_uris = []
    for slot in slots:
        path = slot['pending_path']
        data_uris.append(full_image_data_uri(path))
        if path not in seen_paths:
            seen_paths.add(path)
            photo_paths.append(path)

    html = html_template.render(
        photos=data_uris,
        wedding_text=WEDDING_TEXT,
        wedding_font_size=WEDDING_FONT_SIZE,
        date_text=DATE_TEXT,
        date_font_size=DATE_FONT_SIZE,
    )
    html_path = batch_dir / 'template.html'
    pdf_path = batch_dir / 'template.pdf'
    html_path.write_text(html, encoding='utf-8')

    try:
        from weasyprint import HTML
        HTML(filename=str(html_path)).write_pdf(str(pdf_path))
        log.info('PDF: %s', pdf_path)
    except Exception as e:
        log.error('Błąd PDF: %s', e)
        sync_broadcast({'type': 'error', 'message': f'Błąd generowania PDF: {e}'})
        return None

    for p in photo_paths:
        dest = batch_dir / p.name
        try:
            shutil.copy2(str(p), str(dest))
            p.unlink(missing_ok=True)
        except Exception as e:
            log.warning('Archiwizacja %s: %s', p.name, e)

    pdf_id = uuid.uuid4().hex[:8]
    used_photo_ids = []
    with state_lock:
        for p in photo_paths:
            dest = batch_dir / p.name
            uid = uuid.uuid4().hex[:8]
            used_photo_ids.append(uid)
            used_photos.append({
                'id': uid,
                'filename': p.name,
                'path': str(dest),
                'timestamp': timestamp,
            })

    pdf_entry = {
        'id': pdf_id,
        'timestamp': timestamp,
        'slot_count': len(slots),
        'filenames': [p.name for p in photo_paths],
        'photo_count': len(photo_paths),
        'pdf_path': str(pdf_path),
        'batch_dir': str(batch_dir),
        'used_photo_ids': used_photo_ids,
    }

    with state_lock:
        pdf_queue.append(pdf_entry)

    log.info('PDF %s: %d zdjęć, %d slotów', timestamp, len(photo_paths), len(slots))
    sync_broadcast({'type': 'pdf_generated', 'pdf': {
        'id': pdf_id, 'timestamp': timestamp, 'slot_count': len(slots),
        'filenames': [p.name for p in photo_paths],
    }})
    sync_broadcast_state()
    return pdf_entry


def check_and_process():
    with state_lock:
        if len(queue_slots) >= BATCH_SIZE:
            batch_slots = queue_slots[:BATCH_SIZE]
            queue_slots[:] = queue_slots[BATCH_SIZE:]
        else:
            return
    threading.Thread(target=generate_pdf_from_slots, args=(batch_slots, ARCHIVE_DIR), daemon=True).start()


class PhotoHandler(PatternMatchingEventHandler):
    def __init__(self):
        super().__init__(
            patterns=[f'*{ext}' for ext in SUPPORTED_EXTENSIONS],
            ignore_directories=True,
            case_sensitive=False,
        )

    def on_created(self, event):
        self._handle(event.src_path)

    def on_moved(self, event):
        dest = Path(event.dest_path)
        if dest.parent == INCOMING_DIR:
            self._handle(event.dest_path)

    def _handle(self, path_str):
        threading.Thread(target=self._process_file, args=(path_str,), daemon=True).start()

    def _process_file(self, path_str):
        time.sleep(0.5)
        src = Path(path_str)
        if not src.exists() or src.stat().st_size == 0:
            return
        if src.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return

        converted = verify_and_convert(src)
        if converted is None:
            if src.exists():
                dest = REJECTED_DIR / src.name
                try:
                    shutil.move(str(src), str(dest))
                except Exception:
                    pass
            with state_lock:
                rejected_files.append(src.name)
            log.warning('Odrzucono (nieprawidłowy obraz): %s', src.name)
            sync_broadcast_state()
            return

        photo_id = uuid.uuid4().hex[:12]
        new_name = f'{photo_id}.jpg'
        dest = PENDING_DIR / new_name

        try:
            shutil.move(str(converted), str(dest))
        except Exception as e:
            log.error('Przenoszenie %s: %s', converted.name, e)
            return

        data_uri = make_thumbnail_data_uri(dest)
        if data_uri is None:
            return

        with state_lock:
            pending_photos[photo_id] = {
                'filename': src.name,
                'data_uri': data_uri,
                'pending_path': dest,
            }

        log.info('Nowe: %s → %s', src.name, photo_id)
        sync_broadcast({'type': 'new_photo', 'photo': {
            'id': photo_id, 'filename': src.name, 'data_uri': data_uri,
        }})
        sync_broadcast_state()


# --- REST API ---

@app.get('/', response_class=HTMLResponse)
async def index():
    return HTMLResponse(index_template.render())


@app.get('/api/state')
async def api_state():
    with state_lock:
        return get_state()


@app.post('/api/accept/{photo_id}')
async def api_accept(photo_id: str, duplicates: int = 1):
    duplicates = max(1, min(duplicates, BATCH_SIZE))

    with state_lock:
        if photo_id not in pending_photos:
            return JSONResponse({'error': 'Nieznane zdjęcie'}, status_code=404)
        info = pending_photos.pop(photo_id)
        for _ in range(duplicates):
            queue_slots.append({
                'photo_id': photo_id,
                'filename': info['filename'],
                'pending_path': info['pending_path'],
            })

    log.info('Akcept: %s x%d (kolejka: %d/%d)', info['filename'], duplicates, len(queue_slots), BATCH_SIZE)
    await broadcast_state()
    threading.Thread(target=check_and_process, daemon=True).start()
    return {'status': 'ok', 'queue_count': len(queue_slots)}


@app.post('/api/reject/{photo_id}')
async def api_reject(photo_id: str):
    with state_lock:
        if photo_id not in pending_photos:
            return JSONResponse({'error': 'Nieznane zdjęcie'}, status_code=404)
        info = pending_photos.pop(photo_id)

    dest = REJECTED_DIR / info['pending_path'].name
    try:
        shutil.move(str(info['pending_path']), str(dest))
    except Exception as e:
        log.error('Odrzucanie: %s', e)

    log.info('Odrzucono: %s', info['filename'])
    await broadcast_state()
    return {'status': 'ok'}


@app.post('/api/generate-pdf')
async def api_generate_pdf():
    with state_lock:
        if not queue_slots:
            return JSONResponse({'error': 'Kolejka pusta'}, status_code=400)
        batch_slots = queue_slots[:]
        queue_slots[:] = []

    pdf_entry = generate_pdf_from_slots(batch_slots, ARCHIVE_DIR)
    await broadcast_state()
    if pdf_entry:
        return {'status': 'ok', 'pdf_id': pdf_entry['id'], 'slot_count': len(batch_slots)}
    return JSONResponse({'error': 'Błąd generowania PDF'}, status_code=500)


@app.get('/api/pdf-preview/{pdf_id}')
async def api_pdf_preview(pdf_id: str):
    with state_lock:
        entry = next((p for p in pdf_queue if p['id'] == pdf_id), None)
    if not entry:
        return JSONResponse({'error': 'Nie znaleziono PDF'}, status_code=404)

    pdf_path = Path(entry['pdf_path'])
    if not pdf_path.exists():
        return JSONResponse({'error': 'Plik PDF nie istnieje'}, status_code=404)

    with open(pdf_path, 'rb') as f:
        pdf_data = base64.b64encode(f.read()).decode('ascii')
    return {'pdf_id': pdf_id, 'data_uri': f'data:application/pdf;base64,{pdf_data}'}


@app.post('/api/mark-printed/{pdf_id}')
async def api_mark_printed(pdf_id: str):
    with state_lock:
        entry = next((p for p in pdf_queue if p['id'] == pdf_id), None)
        if not entry:
            return JSONResponse({'error': 'Nie znaleziono PDF'}, status_code=404)
        pdf_queue.remove(entry)
        batch_history.append({
            'timestamp': entry['timestamp'],
            'filenames': entry['filenames'],
            'photo_count': entry['photo_count'],
            'slot_count': entry['slot_count'],
            'pdf_path': entry['pdf_path'],
            'batch_dir': entry['batch_dir'],
            'used_photo_ids': entry.get('used_photo_ids', []),
        })
    log.info('Oznaczono jako wydrukowane: %s', pdf_id)
    await broadcast_state()
    return {'status': 'ok'}


@app.get('/api/history-detail/{timestamp}')
async def api_history_detail(timestamp: str):
    with state_lock:
        entry = next((h for h in batch_history if h['timestamp'] == timestamp), None)
    if not entry:
        return JSONResponse({'error': 'Nie znaleziono wpisu'}, status_code=404)

    pdf_path = Path(entry['pdf_path'])
    pdf_data_uri = None
    if pdf_path.exists():
        with open(pdf_path, 'rb') as f:
            pdf_data = base64.b64encode(f.read()).decode('ascii')
        pdf_data_uri = f'data:application/pdf;base64,{pdf_data}'

    batch_dir = Path(entry['batch_dir'])
    used_ids = entry.get('used_photo_ids', [])
    photos = []
    for i, fname in enumerate(entry['filenames']):
        fpath = batch_dir / fname
        if fpath.exists():
            photo_id = used_ids[i] if i < len(used_ids) else uuid.uuid4().hex[:8]
            photos.append({
                'id': photo_id,
                'filename': fname,
                'data_uri': make_thumbnail_data_uri(fpath),
                'path': str(fpath),
            })

    return {
        'timestamp': timestamp,
        'pdf_data_uri': pdf_data_uri,
        'photos': photos,
        'slot_count': entry['slot_count'],
    }


@app.post('/api/remove-pdf/{pdf_id}')
async def api_remove_pdf(pdf_id: str):
    with state_lock:
        entry = next((p for p in pdf_queue if p['id'] == pdf_id), None)
        if entry:
            pdf_queue.remove(entry)
            log.info('Usunięto PDF z kolejki: %s', pdf_id)
        else:
            return JSONResponse({'error': 'Nie znaleziono PDF'}, status_code=404)
    await broadcast_state()
    return {'status': 'ok'}


@app.post('/api/remove-slot/{index}')
async def api_remove_slot(index: int):
    with state_lock:
        if 0 <= index < len(queue_slots):
            removed = queue_slots.pop(index)
            log.info('Usunięto slot %d: %s', index, removed['filename'])
        else:
            return JSONResponse({'error': 'Nieprawidłowy indeks'}, status_code=400)
    await broadcast_state()
    return {'status': 'ok'}


@app.post('/api/clear-queue')
async def api_clear_queue():
    with state_lock:
        count = len(queue_slots)
        queue_slots[:] = []
    log.info('Wyczyszczono kolejkę (%d slotów)', count)
    await broadcast_state()
    return {'status': 'ok', 'cleared': count}


@app.post('/api/upload')
async def api_upload(file: UploadFile = File(...)):
    if not file.filename:
        return JSONResponse({'error': 'Brak pliku'}, status_code=400)

    ext = Path(file.filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return JSONResponse({'error': f'Nieobsługiwany format: {ext}'}, status_code=400)

    tmp_path = PENDING_DIR / f'_upload_{uuid.uuid4().hex[:8]}{ext}'
    content = await file.read()
    tmp_path.write_bytes(content)

    converted = verify_and_convert(tmp_path)
    if converted is None:
        tmp_path.unlink(missing_ok=True)
        return JSONResponse({'error': 'Nieprawidłowy plik obrazu'}, status_code=400)

    photo_id = uuid.uuid4().hex[:12]
    new_name = f'{photo_id}.jpg'
    dest = PENDING_DIR / new_name

    try:
        shutil.move(str(converted), str(dest))
    except Exception as e:
        log.error('Przenoszenie upload: %s', e)
        return JSONResponse({'error': 'Błąd przetwarzania'}, status_code=500)

    data_uri = make_thumbnail_data_uri(dest)
    if data_uri is None:
        dest.unlink(missing_ok=True)
        return JSONResponse({'error': 'Nie można przetworzyć obrazu'}, status_code=400)

    with state_lock:
        pending_photos[photo_id] = {
            'filename': file.filename,
            'data_uri': data_uri,
            'pending_path': dest,
        }

    log.info('Upload: %s → %s', file.filename, photo_id)
    sync_broadcast({'type': 'new_photo', 'photo': {
        'id': photo_id, 'filename': file.filename, 'data_uri': data_uri,
    }})
    await broadcast_state()
    return {'status': 'ok', 'photo_id': photo_id}


@app.get('/api/used-photo-preview/{photo_id}')
async def api_used_photo_preview(photo_id: str):
    with state_lock:
        entry = next((u for u in used_photos if u['id'] == photo_id), None)
    if not entry:
        return JSONResponse({'error': 'Nie znaleziono zdjęcia'}, status_code=404)

    path = Path(entry['path'])
    if not path.exists():
        return JSONResponse({'error': 'Plik nie istnieje'}, status_code=404)

    data_uri = make_thumbnail_data_uri(path)
    return {'photo_id': photo_id, 'filename': entry['filename'], 'data_uri': data_uri}


@app.post('/api/reuse-photo/{photo_id}')
async def api_reuse_photo(photo_id: str, duplicates: int = 1):
    duplicates = max(1, min(duplicates, BATCH_SIZE))

    with state_lock:
        entry = next((u for u in used_photos if u['id'] == photo_id), None)
        if not entry:
            return JSONResponse({'error': 'Nie znaleziono zdjęcia'}, status_code=404)

        path = Path(entry['path'])
        if not path.exists():
            return JSONResponse({'error': 'Plik nie istnieje'}, status_code=404)

        new_id = uuid.uuid4().hex[:12]
        ext = path.suffix.lower()
        new_name = f'{new_id}{ext}'
        dest = PENDING_DIR / new_name
        shutil.copy2(str(path), str(dest))

        data_uri = make_thumbnail_data_uri(dest)
        pending_photos[new_id] = {
            'filename': entry['filename'],
            'data_uri': data_uri,
            'pending_path': dest,
        }

        for _ in range(duplicates):
            queue_slots.append({
                'photo_id': new_id,
                'filename': entry['filename'],
                'pending_path': dest,
            })

    log.info('Ponowne użycie: %s x%d', entry['filename'], duplicates)
    sync_broadcast({'type': 'new_photo', 'photo': {
        'id': new_id, 'filename': entry['filename'], 'data_uri': data_uri,
    }})
    await broadcast_state()
    threading.Thread(target=check_and_process, daemon=True).start()
    return {'status': 'ok', 'queue_count': len(queue_slots)}


# --- WebSocket ---

@app.websocket('/ws')
async def websocket_endpoint(ws: WebSocket):
    global main_event_loop
    await ws.accept()
    connected_clients.add(ws)
    main_event_loop = asyncio.get_running_loop()
    await broadcast_state()
    try:
        while True:
            await ws.receive_text()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        connected_clients.discard(ws)


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description='Auto-Print Web App')
    parser.add_argument('--port', type=int, default=8080, help='Port HTTP (domyślnie: 8080)')
    parser.add_argument('--host', default='0.0.0.0', help='Host (domyślnie: 0.0.0.0)')
    args = parser.parse_args()

    log.info('=== Auto-Print Web ===')
    log.info('Incoming:  %s', INCOMING_DIR)
    log.info('Pending:   %s', PENDING_DIR)
    log.info('Archive:   %s', ARCHIVE_DIR)
    log.info('Rejected:  %s', REJECTED_DIR)
    log.info('http://%s:%d', args.host, args.port)

    handler = PhotoHandler()
    observer = PollingObserver()
    observer.schedule(handler, str(INCOMING_DIR), recursive=False)
    observer.start()
    log.info('Watchdog: %s (polling enabled)', INCOMING_DIR)

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
