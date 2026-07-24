#!/usr/bin/env python3
"""
watch.py - Monitoruje folder incoming/ na nowe zdjęcia.
Po zebraniu 8 zdjęć wypełnia template, generuje PDF i wysyła do druku przez CUPS.

Użycie:
    python watch.py                          # domyślna drukarka systemowa
    python watch.py --printer Canon_Selphy   # konkretna drukarka
    python watch.py --dry-run                # test bez drukowania
"""

import argparse
import base64
import logging
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from watchdog.events import PatternMatchingEventHandler
from jinja2 import Environment, FileSystemLoader

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
BATCH_SIZE = 8
SUPPORTED_PATTERNS = ['*.jpg', '*.jpeg', '*.png', '*.bmp', '*.gif', '*.webp']


class PhotoQueue:
    def __init__(self, batch_size=BATCH_SIZE):
        self.batch_size = batch_size
        self.queue = []
        self.lock = threading.Lock()
        self.processing = False

    def add(self, path):
        with self.lock:
            if path in self.queue:
                return False
            self.queue.append(path)
            log.info('Kolejka: %d/%d  (+ %s)', len(self.queue), self.batch_size, path.name)
            if len(self.queue) >= self.batch_size and not self.processing:
                self.processing = True
                return True
        return False

    def take_batch(self):
        with self.lock:
            batch = self.queue[:self.batch_size]
            self.queue = self.queue[self.batch_size:]
            return batch

    def mark_done(self):
        with self.lock:
            self.processing = False
            if len(self.queue) >= self.batch_size:
                self.processing = True
                return True
        return False


class BatchProcessor:
    def __init__(self, printer=None, dry_run=False):
        self.printer = printer
        self.dry_run = dry_run
        self.jinja_env = Environment(loader=FileSystemLoader(str(SCRIPT_DIR)))
        self.template = self.jinja_env.get_template('template.jinja2')

    def image_to_data_uri(self, path):
        ext = path.suffix.lower()
        mime_map = {
            '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.png': 'image/png', '.bmp': 'image/bmp',
            '.gif': 'image/gif', '.webp': 'image/webp',
        }
        mime = mime_map.get(ext, 'image/jpeg')
        with open(path, 'rb') as f:
            data = base64.b64encode(f.read()).decode('ascii')
        return f'data:{mime};base64,{data}'

    def process(self, photo_paths, archive_dir):
        log.info('=== Przetwarzanie partii %d zdjęć ===', len(photo_paths))
        data_uris = [self.image_to_data_uri(p) for p in photo_paths]
        html = self.template.render(photos=data_uris)

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        batch_dir = archive_dir / f'batch_{timestamp}'
        batch_dir.mkdir(parents=True, exist_ok=True)

        html_path = batch_dir / 'template.html'
        pdf_path = batch_dir / 'template.pdf'

        html_path.write_text(html, encoding='utf-8')

        try:
            from weasyprint import HTML
            HTML(filename=str(html_path)).write_pdf(str(pdf_path))
            log.info('PDF wygenerowany: %s', pdf_path)
        except ImportError:
            log.error('weasyprint nie jest zainstalowany. Zainstaluj: pip install weasyprint')
            return None
        except Exception as e:
            log.error('Błąd generowania PDF: %s', e)
            return None

        self.print_pdf(pdf_path)

        archived = 0
        for p in photo_paths:
            dest = batch_dir / p.name
            try:
                shutil.copy2(str(p), str(dest))
                p.unlink(missing_ok=True)
                archived += 1
            except FileNotFoundError:
                if dest.exists():
                    archived += 1
            except OSError as e:
                log.warning('Błąd archiwizacji %s: %s', p.name, e)
        log.info('Zarchiwizowano %d/%d zdjęć do: %s', archived, len(photo_paths), batch_dir)

        return batch_dir

    def print_pdf(self, pdf_path):
        if self.dry_run:
            log.info('[DRY RUN] Pomijam drukowanie: %s', pdf_path)
            return

        cmd = ['lp']
        if self.printer:
            cmd.extend(['-d', self.printer])
        cmd.append(str(pdf_path))

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                log.info('Wysłano do druku: %s', result.stdout.strip())
            else:
                log.error('Błąd lp: %s', result.stderr.strip())
        except FileNotFoundError:
            log.error('Polecenie "lp" nie znalezione. Zainstaluj CUPS.')
        except subprocess.TimeoutExpired:
            log.error('Timeout drukowania (30s)')
        except Exception as e:
            log.error('Błąd drukowania: %s', e)


class PhotoHandler(PatternMatchingEventHandler):
    def __init__(self, queue, processor, archive_dir, incoming_dir):
        super().__init__(patterns=SUPPORTED_PATTERNS, ignore_directories=True)
        self.queue = queue
        self.processor = processor
        self.archive_dir = archive_dir
        self.incoming_dir = incoming_dir

    def on_created(self, event):
        self._handle(event.src_path)

    def on_moved(self, event):
        dest = Path(event.dest_path)
        if dest.parent == self.incoming_dir:
            self._handle(event.dest_path)

    def _handle(self, path_str):
        path = Path(path_str)
        threading.Thread(target=self._delayed_add, args=(path,), daemon=True).start()

    def _delayed_add(self, path):
        time.sleep(0.5)
        if not path.exists() or path.stat().st_size == 0:
            return

        should_process = self.queue.add(path)
        if should_process:
            threading.Thread(target=self._process_batch, daemon=True).start()

    def _process_batch(self):
        batch = self.queue.take_batch()
        if len(batch) < BATCH_SIZE:
            for p in batch:
                self.queue.add(p)
            self.queue.mark_done()
            return

        success = self.processor.process(batch, self.archive_dir)
        if not success:
            log.warning('Przetwarzanie nie powiodło się – zdjęcia wracają do kolejki')
            for p in batch:
                if p.exists():
                    self.queue.add(p)

        again = self.queue.mark_done()
        if again:
            threading.Thread(target=self._process_batch, daemon=True).start()


def main():
    parser = argparse.ArgumentParser(
        description='Auto-print – monitoruje folder i drukuje templatki A4 z 8 zdjęciami'
    )
    parser.add_argument('--printer', '-p', help='Nazwa drukarki CUPS (domyślnie: systemowa)')
    parser.add_argument('--incoming', default=str(SCRIPT_DIR / 'incoming'),
                        help='Folder do monitorowania (domyślnie: ./incoming)')
    parser.add_argument('--archive', default=str(SCRIPT_DIR / 'archive'),
                        help='Folder na wydrukowane zdjęcia (domyślnie: ./archive)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Generuj PDF ale nie wysyłaj do drukarki')
    args = parser.parse_args()

    incoming_dir = Path(args.incoming)
    archive_dir = Path(args.archive)
    incoming_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)

    log.info('=== Auto-Print ===')
    log.info('Incoming:  %s', incoming_dir)
    log.info('Archive:   %s', archive_dir)
    log.info('Drukarka:  %s', args.printer or 'domyślna systemowa')
    log.info('Partia:    %d zdjęć', BATCH_SIZE)
    if args.dry_run:
        log.info('Tryb:      DRY RUN (bez drukowania)')
    log.info('Nasłuchiwanie... Wrzuć zdjęcia do folderu incoming/')
    log.info('Naciśnij Ctrl+C aby zatrzymać')

    queue = PhotoQueue()
    processor = BatchProcessor(printer=args.printer, dry_run=args.dry_run)
    handler = PhotoHandler(queue, processor, archive_dir, incoming_dir)

    observer = PollingObserver()
    observer.schedule(handler, str(incoming_dir), recursive=False)
    observer.start()

    try:
        while observer.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        log.info('Zatrzymywanie...')
    finally:
        observer.stop()
        observer.join()
        log.info('Zatrzymano.')


if __name__ == '__main__':
    main()
