#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Улучшенная версия video_web_server.py — многопоточность, статус скана, исправления багов."""

import argparse
import base64
import html
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ========== КОНФИГУРАЦИЯ ==========
VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm'}
BASE_DIR = os.environ.get('BASE_DIR', '/media/320-sata/tor')
DB_PATH = os.environ.get('DB_PATH', '/var/cache/video_server/metadata.db')
CACHE_TTL_SEC = int(os.environ.get('CACHE_TTL_SEC', '3600'))
SCAN_INTERVAL_MINUTES = int(os.environ.get('SCAN_INTERVAL_MINUTES', '180'))
SCAN_WORKERS = int(os.environ.get('SCAN_WORKERS', '2'))
URL_PREFIX = os.environ.get('URL_PREFIX', '').rstrip('/')
AUTH_USER = os.environ.get('AUTH_USER', '')
AUTH_PASS = os.environ.get('AUTH_PASS', '')
REQUIRE_AUTH = bool(AUTH_USER and AUTH_PASS)
# Нагрузка CPU: 1 транскод одновременно, ultrafast, меньше потоков x264
MAX_TRANSCODE_JOBS = int(os.environ.get('MAX_TRANSCODE_JOBS', '1'))
TRANSCODE_QUEUE_SEC = int(os.environ.get('TRANSCODE_QUEUE_SEC', '300'))
FFMPEG_PRESET = os.environ.get('FFMPEG_PRESET', 'ultrafast')
FFMPEG_CRF = os.environ.get('FFMPEG_CRF', '28')
FFMPEG_THREADS = int(os.environ.get('FFMPEG_THREADS', '2'))
FFMPEG_NICE = int(os.environ.get('FFMPEG_NICE', '10'))
FFMPEG_VIDEO_ENCODER = os.environ.get('FFMPEG_VIDEO_ENCODER', 'libx264')

SKIP_DIR_NAMES = {'.git', '@eaDir', '$RECYCLE.BIN', '.Trash', '__pycache__'}

MIME_MAP = {
    '.mp4': 'video/mp4',
    '.mkv': 'video/x-matroska',
    '.avi': 'video/x-msvideo',
    '.mov': 'video/quicktime',
    '.wmv': 'video/x-ms-wmv',
    '.flv': 'video/x-flv',
    '.webm': 'video/webm',
}

scan_state = {
    'running': False,
    'done': 0,
    'total': 0,
    'started_at': None,
    'error': None,
    'message': '',
}
scan_lock = threading.Lock()
video_dirs_cache = set()
video_dirs_lock = threading.Lock()
transcode_semaphore = threading.BoundedSemaphore(MAX_TRANSCODE_JOBS)


def ffmpeg_prefix():
    """Понижает приоритет ffmpeg, чтобы не душить transmission и систему."""
    if os.name != 'posix':
        return []
    prefix = []
    if shutil.which('nice'):
        prefix += ['nice', '-n', str(FFMPEG_NICE)]
    if shutil.which('ionice'):
        prefix += ['ionice', '-c', '3']
    return prefix


def build_transcode_cmd(real_file, transcode_audio, transcode_video):
    cmd = ffmpeg_prefix() + ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', real_file]
    if transcode_video:
        cmd += ['-c:v', FFMPEG_VIDEO_ENCODER, '-preset', FFMPEG_PRESET, '-crf', FFMPEG_CRF]
        if FFMPEG_VIDEO_ENCODER == 'libx264':
            cmd += ['-threads', str(FFMPEG_THREADS)]
    else:
        cmd += ['-c:v', 'copy']
    if transcode_audio:
        cmd += ['-c:a', 'aac', '-b:a', '128k']
    else:
        cmd += ['-c:a', 'copy']
    cmd += ['-movflags', 'frag_keyframe+empty_moov', '-f', 'mp4', '-']
    return cmd


def make_url(path):
    if URL_PREFIX:
        return f'{URL_PREFIX}{path}'
    return path


def mime_for_path(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    return MIME_MAP.get(ext, 'application/octet-stream')


def format_size(num_bytes):
    if num_bytes < 1024:
        return f'{num_bytes} B'
    if num_bytes < 1024 ** 2:
        return f'{num_bytes / 1024:.1f} KB'
    if num_bytes < 1024 ** 3:
        return f'{num_bytes / 1024 ** 2:.1f} MB'
    return f'{num_bytes / 1024 ** 3:.2f} GB'


def title_from_filename(filepath):
    return os.path.splitext(os.path.basename(filepath))[0].replace('_', ' ').replace('.', ' ')


# ---------- БАЗА ДАННЫХ И МЕТАДАННЫЕ ----------
def get_db_conn():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn


def init_db():
    with get_db_conn() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS video_cache (
                path TEXT PRIMARY KEY,
                filename TEXT,
                title TEXT,
                audio_tracks INTEGER DEFAULT 0,
                audio_codecs TEXT,
                video_codec TEXT,
                mtime REAL,
                last_checked REAL,
                last_seen REAL
            )
        ''')
        for col, ddl in (
            ('video_codec', 'ALTER TABLE video_cache ADD COLUMN video_codec TEXT'),
            ('audio_codecs', 'ALTER TABLE video_cache ADD COLUMN audio_codecs TEXT'),
            ('last_seen', 'ALTER TABLE video_cache ADD COLUMN last_seen REAL'),
        ):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass


def get_video_metadata(filepath):
    try:
        cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', '-show_streams', filepath,
        ]
        output = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=120)
        data = json.loads(output.decode('utf-8'))
        fmt = data.get('format', {})
        tags = fmt.get('tags') or {}
        title = tags.get('title') or tags.get('TITLE') or tags.get('Title')
        streams = data.get('streams', [])
        video_codec = None
        audio_streams = []
        for stream in streams:
            if stream.get('codec_type') == 'video' and video_codec is None:
                video_codec = stream.get('codec_name', '').lower()
            elif stream.get('codec_type') == 'audio':
                audio_streams.append(stream)
        audio_tracks = len(audio_streams)
        audio_codecs = ', '.join(
            sorted({s.get('codec_name', 'unknown') for s in audio_streams})
        ) if audio_streams else ''
        return title, audio_tracks, audio_codecs, video_codec
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None, 0, '', None
    except OSError as exc:
        print(f'Ошибка обработки {filepath}: {exc}')
        return None, 0, '', None


def collect_video_files():
    video_files = []
    for root, dirs, files in os.walk(BASE_DIR):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES and not d.startswith('.')]
        for name in files:
            if name.startswith('.'):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in VIDEO_EXTENSIONS:
                video_files.append(os.path.join(root, name))
    return video_files


def _scan_one(fpath, refresh, now):
    try:
        mtime = os.path.getmtime(fpath)
    except OSError:
        return None, 'skip'
    if not refresh:
        with get_db_conn() as conn:
            row = conn.execute(
                'SELECT mtime, last_checked FROM video_cache WHERE path=?', (fpath,)
            ).fetchone()
            if row and row[0] == mtime and (now - row[1]) < CACHE_TTL_SEC:
                conn.execute('UPDATE video_cache SET last_seen=? WHERE path=?', (now, fpath))
                conn.commit()
                return None, 'cached'
    title, audio_tracks, audio_codecs, video_codec = get_video_metadata(fpath)
    if not title:
        title = title_from_filename(fpath)
    return (
        fpath,
        os.path.basename(fpath),
        title,
        audio_tracks,
        audio_codecs,
        video_codec,
        mtime,
        now,
        now,
    ), 'processed'


def scan_directory(refresh=False):
    with scan_lock:
        if scan_state['running']:
            scan_state['message'] = 'Сканирование уже выполняется'
            return False
        scan_state.update({
            'running': True,
            'done': 0,
            'total': 0,
            'started_at': time.time(),
            'error': None,
            'message': 'Сканирование запущено',
        })

    print('Сканирование видео (анализ кодеков)...')
    start = time.time()
    now = time.time()

    try:
        video_files = collect_video_files()
        total = len(video_files)
        known_paths = set(video_files)

        # --- НОВОЕ: обновляем кеш директорий (добавляем все родительские папки) ---
        global video_dirs_cache
        with video_dirs_lock:
            video_dirs_cache = set()
            for fpath in video_files:
                # Идём от родительской папки файла вверх до BASE_DIR
                dir_path = os.path.dirname(fpath)
                while dir_path != BASE_DIR and dir_path.startswith(BASE_DIR + os.sep):
                    video_dirs_cache.add(dir_path)
                    dir_path = os.path.dirname(dir_path)
                # Добавляем сам BASE_DIR, если файл в нём или в подпапке
                if dir_path == BASE_DIR:
                    video_dirs_cache.add(BASE_DIR)
            # Всегда показываем корневую папку (даже если в ней нет видео, но в подпапках есть)
            video_dirs_cache.add(BASE_DIR)
        # -------------------------------------------------------------------------

        with scan_lock:
            scan_state['total'] = total
        print(f'Найдено {total} файлов')

        batch = []
        batch_lock = threading.Lock()
        BATCH_SIZE = 100

        def flush_batch(conn, rows):
            if not rows:
                return
            conn.executemany('''
                INSERT OR REPLACE INTO video_cache
                (path, filename, title, audio_tracks, audio_codecs, video_codec,
                 mtime, last_checked, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', rows)
            conn.commit()

        with get_db_conn() as conn:
            with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
                futures = {pool.submit(_scan_one, fpath, refresh, now): fpath for fpath in video_files}
                for future in as_completed(futures):
                    result, status = future.result()
                    with scan_lock:
                        scan_state['done'] += 1
                        if scan_state['done'] % 50 == 0 or scan_state['done'] == total:
                            print(f"Обработано {scan_state['done']}/{total}")
                    if result is None:
                        continue
                    with batch_lock:
                        batch.append(result)
                        if len(batch) >= BATCH_SIZE:
                            flush_batch(conn, batch)
                            batch = []
            if batch:
                flush_batch(conn, batch)

            stale = conn.execute('SELECT path FROM video_cache').fetchall()
            to_delete = [row[0] for row in stale if row[0] not in known_paths]
            if to_delete:
                conn.executemany('DELETE FROM video_cache WHERE path=?', [(p,) for p in to_delete])
                conn.commit()
                print(f'Удалено устаревших записей: {len(to_delete)}')

        elapsed = time.time() - start
        msg = f'Готово за {elapsed:.1f} с'
        print(msg)
        with scan_lock:
            scan_state['message'] = msg
        return True
    except Exception as exc:
        print(f'Ошибка сканирования: {exc}')
        with scan_lock:
            scan_state['error'] = str(exc)
            scan_state['message'] = f'Ошибка: {exc}'
        return False
    finally:
        with scan_lock:
            scan_state['running'] = False

def start_scan_async(refresh=False):
    def runner():
        scan_directory(refresh=refresh)

    thread = threading.Thread(target=runner, daemon=True, name='video-scan')
    thread.start()
    return thread


def background_scanner(stop_event):
    print(f'[Фон] Сканер запущен, интервал {SCAN_INTERVAL_MINUTES} мин.')
    while not stop_event.is_set():
        if stop_event.wait(SCAN_INTERVAL_MINUTES * 60):
            break
        print('[Фон] Начинаю фоновое обновление кэша...')
        try:
            scan_directory(refresh=True)
        except Exception as exc:
            print(f'[Фон] Ошибка сканирования: {exc}')
    print('[Фон] Сканер остановлен.')


def get_cached_video_info(filepath):
    with get_db_conn() as conn:
        row = conn.execute(
            'SELECT title, audio_tracks, audio_codecs, video_codec FROM video_cache WHERE path=?',
            (filepath,),
        ).fetchone()
        if row:
            return row[0], row[1] or 0, row[2] or '', row[3]
    return None, 0, '', None


def ensure_cache_video(filepath):
    with get_db_conn() as conn:
        row = conn.execute(
            'SELECT title, audio_tracks, audio_codecs, video_codec FROM video_cache WHERE path=?',
            (filepath,),
        ).fetchone()
        if row is None:
            title, tracks, audio_codecs, video_codec = get_video_metadata(filepath)
            if not title:
                title = title_from_filename(filepath)
            try:
                mtime = os.path.getmtime(filepath)
            except OSError:
                mtime = time.time()
            now = time.time()
            conn.execute('''
                INSERT OR REPLACE INTO video_cache
                (path, filename, title, audio_tracks, audio_codecs, video_codec,
                 mtime, last_checked, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                filepath, os.path.basename(filepath), title, tracks,
                audio_codecs, video_codec, mtime, now, now,
            ))
            conn.commit()
            return title, tracks, audio_codecs, video_codec
        title, tracks, audio_codecs, video_codec = row
        if not title:
            title = title_from_filename(filepath)
        return title, tracks, audio_codecs, video_codec


def need_audio_transcoding(audio_codecs):
    if not audio_codecs:
        return False
    unsupported = {'dts', 'ac3', 'eac3', 'truehd', 'dts-hd'}
    return any(codec.strip() in unsupported for codec in audio_codecs.lower().split(','))


def need_video_transcoding(video_codec):
    if not video_codec:
        return False
    unsupported = {
        'mpeg4', 'xvid', 'divx', 'msmpeg4', 'msmpeg4v2', 'msmpeg4v3',
        'wmv1', 'wmv2', 'wmv3', 'vc1',
    }
    return video_codec in unsupported


# ---------- БЕЗОПАСНОСТЬ И НАВИГАЦИЯ ----------
def is_safe_path(relative_path):
    if not relative_path:
        return True
    norm = os.path.normpath(relative_path)
    return not (norm.startswith('..') or os.path.isabs(norm))


def get_full_path(relative_path):
    if not relative_path:
        return BASE_DIR
    return os.path.join(BASE_DIR, relative_path)


def resolve_video_path(relative_path):
    """Проверяет путь и возвращает абсолютный путь к файлу или None."""
    if not is_safe_path(relative_path):
        return None
    real_base = os.path.realpath(BASE_DIR)
    candidate = os.path.join(BASE_DIR, relative_path)
    real_file = os.path.realpath(candidate)
    if not real_file.startswith(real_base + os.sep) and real_file != real_base:
        return None
    if not os.path.isfile(real_file):
        return None
    ext = os.path.splitext(real_file)[1].lower()
    if ext not in VIDEO_EXTENSIONS:
        return None
    return real_file


def list_directory(relative_path, sort_by='name', order='asc', search_query=''):
    full = get_full_path(relative_path)
    if not os.path.isdir(full):
        return None
    items = []
    search = search_query.strip().lower()
    try:
        for name in sorted(os.listdir(full)):
            item_path = os.path.join(full, name)
            rel = os.path.relpath(item_path, BASE_DIR) if item_path != BASE_DIR else ''
            try:
                mtime = os.path.getmtime(item_path)
            except OSError:
                mtime = 0
            if os.path.isdir(item_path):
                # --- НОВОЕ: пропускаем папки без видео ---
                with video_dirs_lock:
                    if item_path not in video_dirs_cache:
                        continue
                # -----------------------------------------
                items.append({
                    'type': 'dir',
                    'name': name,
                    'full_path': rel,
                    'mtime': mtime,
                })
            else:
                ext = os.path.splitext(name)[1].lower()
                if ext not in VIDEO_EXTENSIONS:
                    continue
                if search and search not in name.lower():
                    continue
                title, tracks, audio_codecs, video_codec = ensure_cache_video(item_path)
                try:
                    size = os.path.getsize(item_path)
                except OSError:
                    size = 0
                items.append({
                    'type': 'file',
                    'name': name,
                    'full_path': rel,
                    'title': title,
                    'audio_icon': '✅' if tracks > 0 else '❌',
                    'audio_tracks': tracks,
                    'audio_codecs': audio_codecs,
                    'video_codec': video_codec or '',
                    'needs_transcode': need_audio_transcoding(audio_codecs) or need_video_transcoding(video_codec),
                    'mtime': mtime,
                    'size': size,
                })
    except OSError:
        return None

    reverse = order == 'desc'
    if sort_by == 'name':
        items.sort(key=lambda x: x['name'].lower(), reverse=reverse)
    elif sort_by == 'mtime':
        items.sort(key=lambda x: x.get('mtime', 0), reverse=reverse)
    elif sort_by == 'size':
        items.sort(key=lambda x: x.get('size', 0), reverse=reverse)
    items.sort(key=lambda x: 0 if x['type'] == 'dir' else 1)
    return items


def search_recursive(relative_path, search_query, sort_by='name', order='asc'):
    """Рекурсивно ищет видеофайлы по имени во всех подпапках."""
    full = get_full_path(relative_path)
    if not os.path.isdir(full):
        return None

    results = []
    search = search_query.strip().lower()
    for root, dirs, files in os.walk(full):
        # Пропускаем системные папки
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES and not d.startswith('.')]
        for name in files:
            if name.startswith('.'):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in VIDEO_EXTENSIONS:
                continue
            if search and search not in name.lower():
                continue
            item_path = os.path.join(root, name)
            rel = os.path.relpath(item_path, BASE_DIR) if item_path != BASE_DIR else ''
            try:
                mtime = os.path.getmtime(item_path)
            except OSError:
                mtime = 0
            title, tracks, audio_codecs, video_codec = ensure_cache_video(item_path)
            try:
                size = os.path.getsize(item_path)
            except OSError:
                size = 0
            results.append({
                'type': 'file',
                'name': name,
                'full_path': rel,
                'title': title,
                'audio_icon': '✅' if tracks > 0 else '❌',
                'audio_tracks': tracks,
                'audio_codecs': audio_codecs,
                'video_codec': video_codec or '',
                'needs_transcode': need_audio_transcoding(audio_codecs) or need_video_transcoding(video_codec),
                'mtime': mtime,
                'size': size,
            })

    # Сортировка
    reverse = order == 'desc'
    if sort_by == 'name':
        results.sort(key=lambda x: x['name'].lower(), reverse=reverse)
    elif sort_by == 'mtime':
        results.sort(key=lambda x: x.get('mtime', 0), reverse=reverse)
    elif sort_by == 'size':
        results.sort(key=lambda x: x.get('size', 0), reverse=reverse)

    return results

# ---------- HTTP ОБРАБОТЧИК ----------
class VideoHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def authenticate(self):
        auth = self.headers.get('Authorization')
        if not auth:
            return False
        try:
            auth_type, creds = auth.split(' ', 1)
            if auth_type.lower() != 'basic':
                return False
            decoded = base64.b64decode(creds).decode('utf-8')
            user, pwd = decoded.split(':', 1)
            return (
                secrets.compare_digest(user, AUTH_USER)
                and secrets.compare_digest(pwd, AUTH_PASS)
            )
        except (ValueError, UnicodeDecodeError):
            return False

    def send_auth_request(self):
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="Video Server"')
        self.end_headers()
        try:
            self.wfile.write(b'<html><body><h1>401 Unauthorized</h1></body></html>')
        except (BrokenPipeError, ConnectionResetError):
            pass

    def require_auth_or_send(self):
        if REQUIRE_AUTH and not self.authenticate():
            self.send_auth_request()
            return False
        return True

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.require_auth_or_send():
            return

        try:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)

            if parsed.path == '/health':
                self.send_json({'status': 'ok', 'base_dir': BASE_DIR})
                return

            if parsed.path == '/status':
                with scan_lock:
                    payload = dict(scan_state)
                self.send_json(payload)
                return

            if parsed.path in ('/', '/index.html'):
                self.handle_index(query)
                return

            if parsed.path == '/watch':
                self.handle_watch(query)
                return

            if parsed.path == '/stream':
                self.handle_stream(query)
                return

            if parsed.path == '/playlist.m3u':
                self.handle_playlist(query)
                return

            self.send_error(404)

        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle_index(self, query):
        dir_param = query.get('dir', [''])[0]
        if not is_safe_path(dir_param):
            self.send_error(403, 'Access denied')
            return

        if query.get('refresh_cache', [''])[0] == '1':
            start_scan_async(refresh=True)

        sort_by = query.get('sort', ['name'])[0]
        order = query.get('order', ['asc'])[0]
        search_query = query.get('q', [''])[0]
        if sort_by not in ('name', 'mtime', 'size'):
            sort_by = 'name'
        if order not in ('asc', 'desc'):
            order = 'asc'

        if search_query:
            items = search_recursive(dir_param, search_query, sort_by, order)
        else:
            items = list_directory(dir_param, sort_by, order, '')
        if items is None:
            self.send_error(404, 'Directory not found')
            return

        breadcrumbs = self.get_breadcrumbs(dir_param)
        page = self.render_browser(items, dir_param, breadcrumbs, sort_by, order, search_query)
        body = page.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_watch(self, query):
        paths = query.get('path', [])
        if not paths:
            self.send_error(400, 'Missing path')
            return

        relative = paths[0]
        real_file = resolve_video_path(relative)
        if not real_file:
            self.send_error(404, 'File not found')
            return

        title, audio_tracks, audio_codecs, video_codec = ensure_cache_video(real_file)
        warning = ''
        transcode_audio = need_audio_transcoding(audio_codecs)
        transcode_video = need_video_transcoding(video_codec)
        if transcode_audio:
            warning += f'⚠️ Аудиокодек "{audio_codecs}" не поддерживается. Будет перекодирован в AAC. '
        if transcode_video:
            warning += f'⚠️ Видеокодек "{video_codec}" не поддерживается. Будет перекодирован в H.264.'
        if not warning and audio_tracks == 0:
            warning = '❌ В файле нет аудиодорожек.'

        parent_rel = os.path.dirname(relative).replace('\\', '/')
        all_items = list_directory(parent_rel, sort_by='name', order='asc')
        video_files = [item for item in (all_items or []) if item['type'] == 'file']
        current_index = next(
            (i for i, vf in enumerate(video_files) if vf['full_path'] == relative),
            None,
        )
        prev_path = video_files[current_index - 1]['full_path'] if current_index and current_index > 0 else None
        next_path = (
            video_files[current_index + 1]['full_path']
            if current_index is not None and current_index + 1 < len(video_files)
            else None
        )

        stream_mime = 'video/mp4' if (transcode_audio or transcode_video) else mime_for_path(real_file)
        page = self.render_watch_page(
            relative, title, audio_tracks, audio_codecs, video_codec, warning,
            prev_path=prev_path, next_path=next_path,
            current_index=current_index, total_files=len(video_files),
            parent_rel=parent_rel, stream_mime=stream_mime,
        )
        body = page.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_stream(self, query):
        paths = query.get('path', [])
        if not paths:
            self.send_error(400)
            return

        real_file = resolve_video_path(paths[0])
        if not real_file:
            self.send_error(404)
            return

        _, _, audio_codecs, video_codec = ensure_cache_video(real_file)
        transcode_audio = need_audio_transcoding(audio_codecs)
        transcode_video = need_video_transcoding(video_codec)

        if not transcode_audio and not transcode_video:
            self.stream_file_direct(real_file)
        else:
            self.stream_file_transcoded(real_file, transcode_audio, transcode_video)

    def stream_file_direct(self, real_file):
        mime = mime_for_path(real_file)
        file_size = os.path.getsize(real_file)
        range_header = self.headers.get('Range')
        start = 0
        end = file_size - 1

        if range_header:
            match = re.search(r'bytes=(\d+)-(\d*)', range_header)
            if not match:
                self.send_error(416)
                return
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else file_size - 1
            if start >= file_size or start > end:
                self.send_error(416)
                return
            end = min(end, file_size - 1)
            length = end - start + 1
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
            self.send_header('Content-Length', str(length))
            self.send_header('Content-Type', mime)
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()
            with open(real_file, 'rb') as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            return

        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(file_size))
        self.send_header('Accept-Ranges', 'bytes')
        self.end_headers()
        with open(real_file, 'rb') as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def stream_file_transcoded(self, real_file, transcode_audio, transcode_video):
        acquired = transcode_semaphore.acquire(timeout=TRANSCODE_QUEUE_SEC)
        if not acquired:
            self.send_response(503)
            self.send_header('Retry-After', '30')
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(
                'Сервер занят другим транскодом. Подождите и обновите страницу.'.encode('utf-8')
            )
            return

        cmd = build_transcode_cmd(real_file, transcode_audio, transcode_video)
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            while True:
                data = proc.stdout.read(1024 * 1024)
                if not data:
                    break
                self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(500, 'ffmpeg not found')
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if proc and proc.poll() is None:
                proc.kill()
                proc.wait()
            transcode_semaphore.release()

    def handle_playlist(self, query):
        paths = query.get('path', [])
        if not paths:
            self.send_error(400, 'Missing path')
            return

        relative = paths[0]
        real_file = resolve_video_path(relative)
        if not real_file:
            self.send_error(404, 'File not found')
            return

        host = self.headers.get('Host', 'localhost:8001')
        scheme = 'https' if self.headers.get('X-Forwarded-Proto') == 'https' else 'http'
        stream_path = f'/stream?path={urllib.parse.quote(relative)}'
        if URL_PREFIX:
            stream_path = URL_PREFIX + stream_path
        full_stream_url = f'{scheme}://{host}{stream_path}'
        if REQUIRE_AUTH:
            full_stream_url = f'{scheme}://{urllib.parse.quote(AUTH_USER)}:{urllib.parse.quote(AUTH_PASS)}@{host}{stream_path}'

        filename = os.path.basename(real_file)
        m3u_content = f'#EXTM3U\n#EXTINF:0,{filename}\n{full_stream_url}\n'
        body = m3u_content.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'audio/x-mpegurl')
        self.send_header('Content-Disposition', f'attachment; filename="{filename}.m3u"')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        if not self.require_auth_or_send():
            return
        try:
            parsed = urlparse(self.path)
            if parsed.path == '/stream':
                self.send_response(200)
                self.send_header('Accept-Ranges', 'none')
                self.end_headers()
            elif parsed.path == '/health':
                self.send_response(200)
                self.end_headers()
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def get_breadcrumbs(self, current_dir):
        parts = current_dir.split('/') if current_dir else []
        crumbs = [{'name': '🏠 Корень', 'url': make_url('/')}]
        path = ''
        for part in parts:
            if not part:
                continue
            path += part + '/'
            crumbs.append({
                'name': part,
                'url': make_url(f'/?dir={urllib.parse.quote(path.rstrip("/"))}'),
            })
        return crumbs

    def render_browser(self, items, current_dir, breadcrumbs, sort_by='name', order='asc', search_query=''):
        rows = []
        if current_dir:
            parent = os.path.dirname(current_dir)
            parent_url = (
                make_url(f'/?dir={urllib.parse.quote(parent)}&sort={sort_by}&order={order}&q={urllib.parse.quote(search_query)}')
                if parent else
                make_url(f'/?sort={sort_by}&order={order}&q={urllib.parse.quote(search_query)}')
            )
            rows.append(f'''
            <tr style="background-color:#2a2a2a;">
                <td colspan="5"><a href="{parent_url}">📁 .. (Наверх)</a></td>
            </tr>
            ''')

        for item in items:
            mtime_str = time.strftime('%Y-%m-%d %H:%M', time.localtime(item['mtime'])) if item['mtime'] else ''
            if item['type'] == 'dir':
                dir_url = make_url(
                    f'/?dir={urllib.parse.quote(item["full_path"])}&sort={sort_by}&order={order}&q={urllib.parse.quote(search_query)}'
                )
                rows.append(f'''
                <tr>
                    <td><a href="{dir_url}">📁 {self.escape_html(item["name"])}</a></td>
                    <td></td>
                    <td></td>
                    <td>{mtime_str}</td>
                    <td></td>
                </tr>
                ''')
            else:
                watch_url = make_url(f'/watch?path={urllib.parse.quote(item["full_path"])}')
                download_url = make_url(f'/stream?path={urllib.parse.quote(item["full_path"])}')
                m3u_url = make_url(f'/playlist.m3u?path={urllib.parse.quote(item["full_path"])}')
                codec_hint = item['video_codec'].upper() if item['video_codec'] else ''
                transcode_mark = ' ⚠️' if item.get('needs_transcode') else ''
                rows.append(f'''
                <tr>
                    <td><a href="{watch_url}" target="_blank">{self.escape_html(item["name"])}</a> <a href="{download_url}" download style="font-size:0.8rem;">📥</a></td>
                    <td style="text-align:center">{item["audio_icon"]}</td>
                    <td>{self.escape_html(codec_hint)}{transcode_mark}</td>
                    <td>{format_size(item.get("size", 0))}<br><span style="color:#888;font-size:0.85rem;">{mtime_str}</span></td>
                    <td><a href="{m3u_url}" download style="font-size:0.8rem;">📁 M3U</a></td>
                </tr>
                ''')

        bread_html = ' / '.join(
            f'<a href="{c["url"]}">{self.escape_html(c["name"])}</a>' for c in breadcrumbs
        )

        def sort_link(field, label):
            new_order = 'desc' if (sort_by == field and order == 'asc') else 'asc'
            arrow = ' ▲' if (sort_by == field and order == 'asc') else ' ▼' if (sort_by == field and order == 'desc') else ''
            url = make_url(
                f'/?dir={urllib.parse.quote(current_dir)}&sort={field}&order={new_order}&q={urllib.parse.quote(search_query)}'
            )
            return f'<a href="{url}">{label}{arrow}</a>'

        search_action = make_url(f'/?dir={urllib.parse.quote(current_dir)}&sort={sort_by}&order={order}')

        with scan_lock:
            scan_msg = scan_state.get('message') or ''
            scanning = scan_state.get('running', False)
            scan_progress = f'{scan_state.get("done", 0)}/{scan_state.get("total", 0)}'

        scan_banner = ''
        if scanning:
            scan_banner = f'<div class="scan-status running">⟳ Сканирование: {scan_progress}</div>'
        elif scan_msg:
            scan_banner = f'<div class="scan-status">{self.escape_html(scan_msg)}</div>'

        return f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="color-scheme" content="dark">
    <title>Видеотека — файловый менеджер</title>
    <style>
        body {{ background-color: #121212; color: #e0e0e0; font-family: system-ui; margin: 2rem; }}
        h1 {{ color: #ffffff; border-left: 4px solid #bb86fc; padding-left: 1rem; }}
        .breadcrumbs {{ margin: 1rem 0; font-size: 1.1rem; background: #1e1e1e; padding: 0.5rem; border-radius: 8px; }}
        table {{ border-collapse: collapse; width: 100%; background-color: #1e1e1e; border-radius: 8px; overflow: hidden; }}
        th, td {{ border: 1px solid #333; padding: 10px 12px; text-align: left; vertical-align: top; }}
        th {{ background-color: #2c2c2c; color: #bb86fc; }}
        tr:nth-child(even) {{ background-color: #252525; }}
        tr:hover {{ background-color: #2a2a2a; }}
        a {{ color: #8ab4f8; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        .toolbar {{ margin-bottom: 1rem; display: flex; gap: 0.75rem; flex-wrap: wrap; align-items: center; }}
        .toolbar a, .toolbar button {{ background-color: #2c2c2c; padding: 6px 12px; border-radius: 20px; font-size:0.9rem; border: none; color: #8ab4f8; cursor: pointer; }}
        .search {{ display: flex; gap: 0.5rem; }}
        .search input {{ background: #1e1e1e; border: 1px solid #444; color: #e0e0e0; border-radius: 20px; padding: 6px 12px; min-width: 220px; }}
        .scan-status {{ margin-bottom: 1rem; background: #1e2a1e; padding: 8px 12px; border-radius: 8px; }}
        .scan-status.running {{ background: #2a241e; color: #ffcc80; }}
        footer {{ margin-top: 2rem; text-align: center; color: #555; font-size: 0.8rem; }}
    </style>
</head>
<body>
    <h1>📂 Видеотека — навигация по папкам</h1>
    <div class="breadcrumbs">📍 {bread_html}</div>
    {scan_banner}
    <div class="toolbar">
        <a href="{make_url('/?refresh_cache=1&dir=' + urllib.parse.quote(current_dir))}">⟳ Обновить кеш</a>
        <form class="search" method="get" action="{search_action}">
            <input type="hidden" name="dir" value="{self.escape_html(current_dir)}">
            <input type="hidden" name="sort" value="{sort_by}">
            <input type="hidden" name="order" value="{order}">
            <input type="search" name="q" value="{self.escape_html(search_query)}" placeholder="Поиск по имени...">
            <button type="submit">Найти</button>
        </form>
    </div>
    <table>
        <thead>
            <tr>
                <th>{sort_link('name', 'Имя')}</th>
                <th>🔊</th>
                <th>🎬 Кодек</th>
                <th>{sort_link('size', 'Размер / Дата')}</th>
                <th>M3U</th>
            </tr>
        </thead>
        <tbody>{''.join(rows)}</tbody>
    </table>
    <footer>Клик по названию → просмотр | 📥 → скачать | 📁 M3U → плейлист для VLC</footer>
    <script>
        (function() {{
            var scanning = {'true' if scanning else 'false'};
            if (scanning) {{
                setInterval(function() {{
                    fetch('{make_url("/status")}')
                        .then(function(r) {{ return r.json(); }})
                        .then(function(data) {{
                            var el = document.querySelector('.scan-status');
                            if (!el) return;
                            if (data.running) {{
                                el.textContent = '⟳ Сканирование: ' + data.done + '/' + data.total;
                                el.className = 'scan-status running';
                            }} else {{
                                el.textContent = data.message || 'Готово';
                                el.className = 'scan-status';
                                if (!data.running) scanning = false;
                            }}
                        }})
                        .catch(function() {{}});
                }}, 3000);
            }}
        }})();
    </script>
</body>
</html>'''

    def render_watch_page(
        self, relative_path, title, audio_tracks, audio_codecs, video_codec, warning,
        prev_path=None, next_path=None, current_index=None, total_files=None,
        parent_rel='', stream_mime='video/mp4',
    ):
        encoded_path = urllib.parse.quote(relative_path)
        stream_url = make_url(f'/stream?path={encoded_path}')
        download_link = make_url(f'/stream?path={encoded_path}')
        m3u_link = make_url(f'/playlist.m3u?path={encoded_path}')
        back_link = make_url(f'/?dir={urllib.parse.quote(parent_rel)}') if parent_rel else make_url('/')

        audio_info = f'🔊 Аудиодорожек: {audio_tracks}'
        if audio_codecs:
            audio_info += f', кодеки: {audio_codecs}'
        if video_codec:
            audio_info += f' | 🎬 Видео: {video_codec.upper()}'

        storage_key = 'video_pos_' + base64.urlsafe_b64encode(relative_path.encode('utf-8')).decode('ascii')

        nav_buttons = ''
        if current_index is not None and total_files:
            nav_buttons += f'<div style="margin-top:1rem; font-size:0.9rem;">Серия {current_index + 1} из {total_files}</div>'
        nav_buttons += '<div style="margin: 1rem 0;">'
        if prev_path:
            prev_url = make_url(f'/watch?path={urllib.parse.quote(prev_path)}')
            nav_buttons += f'<a href="{prev_url}" class="nav-btn" id="prevBtn">⬅ Предыдущий</a> '
        else:
            nav_buttons += '<span class="nav-btn disabled">⬅ Предыдущий</span> '
        nav_buttons += '<button id="playPauseBtn" class="nav-btn" onclick="togglePlayPause()">⏯ Пауза / Воспроизведение</button>'
        if next_path:
            next_url = make_url(f'/watch?path={urllib.parse.quote(next_path)}')
            nav_buttons += f' <a href="{next_url}" class="nav-btn" id="nextBtn">Следующий ➡</a>'
        else:
            nav_buttons += ' <span class="nav-btn disabled">Следующий ➡</span>'
        nav_buttons += '</div>'

        return f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="color-scheme" content="dark">
    <title>{self.escape_html(title)} — просмотр</title>
    <style>
        body {{ background-color: #121212; color: #e0e0e0; font-family: system-ui; margin: 2rem; text-align: center; }}
        .container {{ max-width: 90%; margin: auto; }}
        video {{ max-width: 100%; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.5); }}
        .info {{ margin-top: 1rem; background: #1e1e1e; display: inline-block; padding: 0.5rem 1rem; border-radius: 30px; }}
        .warning {{ color: #ffaa66; margin-top: 1rem; background: #2a1e1e; padding: 10px; border-radius: 12px; }}
        a {{ color: #8ab4f8; text-decoration: none; }}
        .back, .download {{ margin-top: 2rem; display: inline-block; background: #2c2c2c; padding: 6px 12px; border-radius: 20px; margin-right: 10px; }}
        .nav-btn {{
            display: inline-block; background: #2c2c2c; color: #8ab4f8; padding: 8px 16px;
            border-radius: 20px; text-decoration: none; font-size: 1rem; margin: 0 5px; border: none; cursor: pointer;
        }}
        .nav-btn.disabled {{ color: #555; pointer-events: none; background: #1a1a1a; }}
        .nav-btn:hover:not(.disabled) {{ background: #3a3a3a; }}
    </style>
</head>
<body>
    <div class="container">
        <h2>🎥 Сейчас воспроизводится:</h2>
        <h1>{self.escape_html(title)}</h1>
        <div class="info">{self.escape_html(audio_info)}</div>
        {f'<div class="warning">{self.escape_html(warning)}</div>' if warning else ''}
        <br><br>
        <video id="videoPlayer" controls autoplay>
            <source src="{stream_url}" type="{self.escape_html(stream_mime)}">
            Ваш браузер не поддерживает видео.
        </video>
        <br>
        {nav_buttons}
        <br>
        <a href="{download_link}" download class="download">📥 Скачать оригинал</a>
        <a href="{m3u_link}" download class="download">📁 M3U плейлист</a>
        <a href="{back_link}" class="back">← Назад к списку</a>
    </div>
    <script>
        (function() {{
            var video = document.getElementById('videoPlayer');
            var storageKey = '{storage_key}';
            video.addEventListener('loadedmetadata', function() {{
                var saved = localStorage.getItem(storageKey);
                if (saved && !isNaN(parseFloat(saved))) {{
                    var pos = parseFloat(saved);
                    if (pos > 0.5 && pos < video.duration) {{
                        video.currentTime = pos;
                    }}
                }}
            }});
            function savePos() {{
                if (video && video.currentTime) {{
                    localStorage.setItem(storageKey, video.currentTime);
                }}
            }}
            video.addEventListener('pause', savePos);
            window.addEventListener('beforeunload', savePos);
            window.togglePlayPause = function() {{
                if (video.paused) video.play(); else video.pause();
            }};
            document.addEventListener('keydown', function(e) {{
                if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA')) return;
                if (e.code === 'Space') {{ e.preventDefault(); togglePlayPause(); }}
                if (e.code === 'ArrowLeft' && document.getElementById('prevBtn')) document.getElementById('prevBtn').click();
                if (e.code === 'ArrowRight' && document.getElementById('nextBtn')) document.getElementById('nextBtn').click();
            }});
        }})();
    </script>
</body>
</html>'''

    @staticmethod
    def escape_html(text):
        return html.escape(str(text), quote=True)

    def log_message(self, format, *args):
        print(f'[{self.address_string()}] {format % args}')


def run_server(port=8001):
    init_db()

    # --- ИНИЦИАЛИЗАЦИЯ КЕША ДИРЕКТОРИЙ (чтобы папки отображались сразу) ---
    global video_dirs_cache
    with video_dirs_lock:
        video_dirs_cache = set()
        for root, dirs, files in os.walk(BASE_DIR):
            # Пропускаем системные папки
            dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES and not d.startswith('.')]
            for name in files:
                ext = os.path.splitext(name)[1].lower()
                if ext in VIDEO_EXTENSIONS:
                    # Добавляем текущую папку и все родительские
                    dir_path = root
                    while dir_path != BASE_DIR and dir_path.startswith(BASE_DIR + os.sep):
                        video_dirs_cache.add(dir_path)
                        dir_path = os.path.dirname(dir_path)
                    if dir_path == BASE_DIR:
                        video_dirs_cache.add(BASE_DIR)
                    break  # достаточно одного файла в папке
        video_dirs_cache.add(BASE_DIR)
    # ---------------------------------------------------------------------

    try:
        subprocess.run(
            ['ffprobe', '-version'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        print('ОШИБКА: ffprobe не найден. Установите ffmpeg: sudo apt install ffmpeg -y')
        return

    with get_db_conn() as conn:
        cnt = conn.execute('SELECT COUNT(*) FROM video_cache').fetchone()[0]
        if cnt == 0:
            print('Кэш пуст, выполняю первичное сканирование...')
            scan_directory(refresh=False)

    stop_event = threading.Event()
    scanner_thread = threading.Thread(
        target=background_scanner, args=(stop_event,), daemon=True, name='video-scanner',
    )
    scanner_thread.start()

    if URL_PREFIX:
        print(f'Сервер запущен на http://0.0.0.0:{port} с префиксом URL: {URL_PREFIX}')
    else:
        print(f'Сервер запущен на http://0.0.0.0:{port}')
    print(f'BASE_DIR={BASE_DIR}')
    print(f'DB_PATH={DB_PATH}')
    print(f'Транскод: max {MAX_TRANSCODE_JOBS} одновременно, preset={FFMPEG_PRESET}, crf={FFMPEG_CRF}, threads={FFMPEG_THREADS}')
    if REQUIRE_AUTH:
        print(f'Аутентификация включена (логин: {AUTH_USER})')
    else:
        print('Аутентификация отключена (задайте AUTH_USER и AUTH_PASS)')

    server = ThreadingHTTPServer(('0.0.0.0', port), VideoHandler)
    server.request_queue_size = 256
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nОстановка сервера...')
        stop_event.set()
        server.shutdown()
        scanner_thread.join(timeout=5)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Локальный видеосервер с кешем метаданных')
    parser.add_argument(
        '--port', type=int,
        default=int(os.environ.get('PORT', '8001')),
        help='Порт HTTP-сервера (по умолчанию 8001 или $PORT)',
    )
    args = parser.parse_args()
    run_server(port=args.port)
