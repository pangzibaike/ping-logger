#!/usr/bin/env python3
"""Ping Logger: bounded ICMP collection, SQLite history and a loopback web API."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import ipaddress
import json
import logging
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

INTERVAL = 60
RETENTION = 7 * 86400
MAX_TARGETS = 20
ROOT = Path(__file__).resolve().parent


def parse_ping(code, output):
    match = re.search(r'time([=<])\s*([\d.]+)\s*ms', output)
    if code == 0 and match:
        return float(match[2]), 'ok'
    if code == 2 or any(s in output.lower() for s in ('not permitted', 'permission denied', 'network is unreachable')):
        return None, 'error'
    return None, 'loss'


def probe(address):
    try:
        result = subprocess.run(['ping', '-n', '-c', '1', '-W', '2', address],
                                capture_output=True, text=True, timeout=4,
                                env=dict(os.environ, LC_ALL='C'))
        return parse_ping(result.returncode, result.stdout + result.stderr)
    except subprocess.TimeoutExpired:
        return None, 'loss'
    except OSError:
        return None, 'error'


@contextmanager
def connect(db):
    conn = sqlite3.connect(str(db), timeout=10)
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def initialize(db):
    with connect(db) as conn:
        conn.execute('PRAGMA auto_vacuum=INCREMENTAL')
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript('''
CREATE TABLE IF NOT EXISTS targets (
 id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL UNIQUE, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS samples (
 target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
 ts INTEGER NOT NULL, ms REAL, status TEXT NOT NULL,
 PRIMARY KEY(target_id,ts)) WITHOUT ROWID;
''')


def add_target(db, address, name):
    address = str(ipaddress.ip_address(address.strip()))
    ip = ipaddress.ip_address(address)
    if ip.is_unspecified or ip.is_multicast or ip.is_loopback or ip.is_link_local or '%' in address:
        raise ValueError('不支持回环、链路本地、组播或未指定地址')
    name = name.strip() or address
    if len(name) > 80:
        raise ValueError('名称最长 80 字符')
    with connect(db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT count(*) FROM targets').fetchone()[0] >= MAX_TARGETS:
            raise ValueError('最多添加 20 个目标')
        conn.execute('INSERT INTO targets(address,name) VALUES(?,?)', (address, name))


def collect(db, stop):
    with ThreadPoolExecutor(max_workers=4) as pool:
        while not stop.is_set():
            started = time.monotonic()
            try:
                with connect(db) as conn:
                    targets = conn.execute('SELECT id,address FROM targets').fetchall()
                results = list(pool.map(probe, [t[1] for t in targets]))
                ts = int(time.time())
                with connect(db) as conn:
                    for (target_id, _), (ms, status) in zip(targets, results):
                        conn.execute('INSERT OR REPLACE INTO samples SELECT ?,?,?,? WHERE EXISTS (SELECT 1 FROM targets WHERE id=?)',
                                     (target_id, ts, ms, status, target_id))
                    conn.execute('DELETE FROM samples WHERE ts < ?', (ts - RETENTION,))
                    conn.execute('PRAGMA incremental_vacuum(64)')
            except Exception:
                logging.exception('Collection failed')
            stop.wait(max(1, INTERVAL - (time.monotonic() - started)))


def handler_for(db):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, payload, mime='application/json; charset=utf-8'):
            body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            assets = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript'),
                      '/style.css': ('style.css', 'text/css'), '/chart.js': ('chart.js', 'text/javascript')}
            if url.path in assets:
                name, mime = assets[url.path]
                return self.respond(200, (ROOT / 'web' / name).read_bytes(), mime)
            try:
                with connect(db) as conn:
                    if url.path == '/api/health':
                        latest = conn.execute('SELECT max(ts) FROM samples').fetchone()[0]
                        count = conn.execute('SELECT count(*) FROM targets').fetchone()[0]
                        stale = bool(count and (latest is None or time.time() - latest > 180))
                        return self.respond(503 if stale else 200, {'ok': not stale, 'targets': count, 'latest': latest})
                    if url.path == '/api/targets':
                        rows = conn.execute('SELECT id,address,name FROM targets ORDER BY id').fetchall()
                        return self.respond(200, [{'id': r[0], 'address': r[1], 'name': r[2]} for r in rows])
                    if url.path == '/api/data':
                        query = parse_qs(url.query)
                        target = int(query.get('target', ['0'])[0])
                        hours = int(query.get('hours', ['24'])[0])
                        if hours not in (1, 6, 24, 72, 168):
                            raise ValueError('无效时间范围')
                        rows = conn.execute('SELECT ts,ms,status FROM samples WHERE target_id=? AND ts>=? ORDER BY ts',
                                            (target, int(time.time()) - hours * 3600)).fetchall()
                        return self.respond(200, {'samples': rows, 'now': int(time.time()), 'interval': INTERVAL})
            except ValueError as exc:
                return self.respond(400, {'error': str(exc)})
            self.respond(404, {'error': 'Not found'})

        def do_POST(self):
            # Browser writes must be same-origin JSON, including when Basic auth is cached.
            origin = self.headers.get('Origin')
            if origin and origin not in ('https://' + self.headers.get('Host', ''), 'http://' + self.headers.get('Host', '')):
                return self.respond(403, {'error': 'Origin rejected'})
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.respond(415, {'error': 'JSON required'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 4096:
                    raise ValueError('无效请求大小')
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError('无效请求')
                path = urlparse(self.path).path
                if path == '/api/targets':
                    if not isinstance(data.get('address'), str) or not isinstance(data.get('name', ''), str):
                        raise ValueError('请输入 IP 和名称')
                    add_target(db, data['address'], data.get('name', ''))
                elif path == '/api/delete':
                    with connect(db) as conn:
                        conn.execute('DELETE FROM targets WHERE id=?', (int(data['id']),))
                else:
                    return self.respond(404, {'error': 'Not found'})
                self.respond(200, {'ok': True})
            except sqlite3.IntegrityError:
                self.respond(409, {'error': '该目标已存在'})
            except (ValueError, KeyError, TypeError) as exc:
                self.respond(400, {'error': str(exc)})

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, fmt, *args):
            logging.info(fmt, *args)
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default='/var/lib/ping-logger/samples.db')
    parser.add_argument('--port', type=int, default=18080)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    initialize(args.db)
    stop = threading.Event()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(args.db))
    server.daemon_threads = True
    def shutdown(*_):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    worker = threading.Thread(target=collect, args=(args.db, stop))
    worker.start()
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        worker.join()


if __name__ == '__main__':
    main()
