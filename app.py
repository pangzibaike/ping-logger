#!/usr/bin/env python3
"""Ping Logger: bounded ICMP collection, SQLite history and a loopback web API."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import logging
from pathlib import Path
import signal
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from monitor import Settings, probe, parse_result, statistics
from storage import connect, initialize, store_results, maintenance, COLUMNS

LOG = logging.getLogger('ping_logger.collector')

INTERVAL = 60
RETENTION = 7 * 86400
MAX_TARGETS = 20
ROOT = Path(__file__).resolve().parent


def parse_ping(code, output):
    result = parse_result(code, output)
    return result.ms, result.status


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
        if conn.execute('SELECT count(*) FROM targets WHERE active=1').fetchone()[0] >= MAX_TARGETS:
            raise ValueError('最多添加 20 个目标')
        existing = conn.execute('SELECT id,active FROM targets WHERE address=?', (address,)).fetchone()
        if existing and not existing[1]:
            conn.execute('UPDATE targets SET active=1,name=? WHERE id=?', (name, existing[0]))
        else:
            conn.execute('INSERT INTO targets(address,name) VALUES(?,?)', (address, name))


def collect(db, stop, settings=None):
    settings = settings or Settings()
    with ThreadPoolExecutor(max_workers=settings.workers) as pool:
        while not stop.is_set():
            started = time.monotonic()
            ts = int(time.time())
            try:
                with connect(db) as conn:
                    targets = conn.execute('SELECT id,address FROM targets WHERE active=1').fetchall()
                def run(address):
                    return None if stop.is_set() else probe(address, settings.packets)
                results = list(pool.map(run, [t[1] for t in targets]))
                store_results(db, ts, targets, results, settings)
                maintenance(db, settings, int(time.time()))
            except Exception:
                LOG.exception('collection_cycle_failed')
            elapsed = time.monotonic() - started
            if elapsed > settings.interval:
                LOG.warning('collection_overrun elapsed=%.2f interval=%s', elapsed, settings.interval)
            stop.wait(max(1, settings.interval - elapsed))


def health(conn, settings, now):
    details = []
    # At most twenty indexed lookups; a healthy target cannot hide a stalled target.
    for target_id, address in conn.execute('SELECT id,address FROM targets WHERE active=1'):
        row = conn.execute('SELECT '+COLUMNS+' FROM samples WHERE target_id=? ORDER BY ts DESC LIMIT 1', (target_id,)).fetchone()
        state = 'pending'
        if row:
            state = 'interrupted' if now-row[0] > max(3*settings.interval, 20*6/settings.workers+10) else ('probe_error' if row[2]=='error' else 'loss' if not row[5] else 'partial_loss' if row[4]>row[5] else 'success')
        details.append({'id': target_id, 'address': address, 'latest': row[0] if row else None,
                        'state': state, 'reason': row[3] if row else None,
                        'sampling_ok': state not in ('pending','interrupted'),
                        'probe_ok': state in ('success','partial_loss','loss')})
    ok = all(t['sampling_ok'] and t['probe_ok'] for t in details)
    return {'ok': ok, 'targets': len(details), 'latest': max((t['latest'] or 0 for t in details), default=0) or None,
            'sampling_ok': all(t['sampling_ok'] for t in details), 'details': details,
            'interval': settings.interval}


def handler_for(db, settings=None):
    settings = settings or Settings()
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
                        data = health(conn, settings, int(time.time()))
                        return self.respond(200 if data['ok'] else 503, data)
                    if url.path == '/api/targets':
                        rows = conn.execute('SELECT id,address,name FROM targets WHERE active=1 ORDER BY id').fetchall()
                        return self.respond(200, [{'id': r[0], 'address': r[1], 'name': r[2]} for r in rows])
                    if url.path == '/api/data':
                        query = parse_qs(url.query)
                        target = int(query.get('target', ['0'])[0])
                        hours = int(query.get('hours', ['24'])[0])
                        if hours not in (1, 6, 24, 72, 168):
                            raise ValueError('无效时间范围')
                        rows = conn.execute('SELECT '+COLUMNS+' FROM samples WHERE target_id=? AND ts>=? ORDER BY ts',
                                            (target, int(time.time()) - hours * 3600)).fetchall()
                        state = next((t for t in health(conn, settings, int(time.time()))['details'] if t['id']==target), None)
                        latest = conn.execute('SELECT '+COLUMNS+' FROM samples WHERE target_id=? ORDER BY ts DESC LIMIT 1', (target,)).fetchone()
                        summary = statistics(rows)
                        return self.respond(200, {'samples': [r[:3] for r in rows], 'sample_details': [dict(zip(COLUMNS.split(','), r)) for r in rows],
                                                 'now': int(time.time()), 'interval': settings.interval,
                                                 'summary': summary, 'current': statistics([latest]) if latest else None,
                                                 'state': state, 'retention_days': settings.retention_days})
            except ValueError as exc:
                return self.respond(400, {'error': str(exc)})
            except sqlite3.Error:
                logging.getLogger('ping_logger.database').exception('database_http_read_failed')
                return self.respond(503, {'error': '数据库暂时不可用，请重试'})
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
                        conn.execute('UPDATE targets SET active=0 WHERE id=?', (int(data['id']),))
                else:
                    return self.respond(404, {'error': 'Not found'})
                self.respond(200, {'ok': True})
            except sqlite3.IntegrityError:
                self.respond(409, {'error': '该目标已存在'})
            except (ValueError, KeyError, TypeError) as exc:
                self.respond(400, {'error': str(exc)})
            except sqlite3.Error:
                logging.getLogger('ping_logger.database').exception('database_http_write_failed')
                self.respond(503, {'error': '数据库暂时不可用，请重试'})

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, fmt, *args):
            logging.getLogger('ping_logger.http').info('http_request peer=%s '+fmt, self.client_address[0], *args)

        def log_error(self, fmt, *args):
            category = 'http_connection_timeout' if fmt.startswith('Request timed out:') else 'http_request_error'
            logging.getLogger('ping_logger.http').warning(category+' peer=%s '+fmt, self.client_address[0], *args)

        def handle(self):
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError):
                logging.getLogger('ping_logger.http').info('http_client_disconnected peer=%s', self.client_address[0])
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default='/var/lib/ping-logger/samples.db')
    parser.add_argument('--port', type=int, default=18080)
    parser.add_argument('--config', help='JSON configuration file')
    for name in ('interval', 'packets', 'workers', 'retention-days'):
        parser.add_argument('--'+name, type=int)
    parser.add_argument('--enable-cleanup', action='store_true', default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    try:
        values = json.loads(Path(args.config).read_text()) if args.config else {}
        for key in ('interval','packets','workers','retention_days','enable_cleanup'):
            if getattr(args, key) is not None:
                values[key] = getattr(args, key)
        settings = Settings(**values)
    except (ValueError, TypeError, OSError) as exc:
        parser.error(str(exc))
    initialize(args.db)
    stop = threading.Event()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(args.db, settings))
    server.daemon_threads = True
    def shutdown(*_):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    worker = threading.Thread(target=collect, args=(args.db, stop, settings))
    worker.start()
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        worker.join()


if __name__ == '__main__':
    main()
