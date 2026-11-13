import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import build_opener, ProxyHandler, Request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'test.db'
        app.initialize(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ping(self):
        self.assertEqual(app.parse_ping(0, 'time=12.4 ms'), (12.4, 'ok'))
        self.assertEqual(app.parse_ping(0, 'time<1 ms'), (1, 'ok'))
        self.assertEqual(app.parse_ping(1, '100% packet loss'), (None, 'loss'))
        self.assertEqual(app.parse_ping(1, 'Destination Host Unreachable'), (None, 'loss'))
        self.assertEqual(app.parse_ping(2, 'Network is unreachable'), (None, 'error'))

    def test_target_validation(self):
        for address in ('--help', '1.1.1.1; echo hi', 'localhost', '127.0.0.1', '::1', 'ff02::1', '0.0.0.0', 'fe80::1%eth0'):
            with self.subTest(address=address), self.assertRaises(ValueError):
                app.add_target(self.db, address, '')
        app.add_target(self.db, '2606:4700:4700::1111', 'IPv6')
        with patch.object(app, 'MAX_TARGETS', 1), self.assertRaises(ValueError):
            app.add_target(self.db, '1.1.1.1', '')

    def test_collector_retention_and_deleted_target_race(self):
        app.add_target(self.db, '1.1.1.1', 'test')
        with app.connect(self.db) as conn:
            conn.execute('INSERT INTO samples VALUES(1,?,?,?)', (int(time.time())-app.RETENTION-1, 1, 'ok'))
        stop = threading.Event()
        def fake_probe(_):
            stop.set()
            return 22, 'ok'
        with patch.object(app, 'probe', fake_probe):
            app.collect(self.db, stop)
        with app.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT ms,status FROM samples').fetchall(), [(22, 'ok')])
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        stop.clear()
        def delete_during_probe(_):
            with app.connect(self.db) as conn:
                conn.execute('DELETE FROM targets')
            stop.set()
            return 1, 'ok'
        with patch.object(app, 'probe', delete_during_probe):
            app.collect(self.db, stop)
        with app.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM samples').fetchone()[0], 0)

    def test_http(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), app.handler_for(self.db))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        client = build_opener(ProxyHandler({}))
        base = 'http://127.0.0.1:' + str(server.server_port)
        def post(path, data, **headers):
            return client.open(Request(base+path, data=json.dumps(data).encode(), headers={'Content-Type':'application/json', **headers}))
        try:
            self.assertTrue(json.load(client.open(base+'/api/health'))['ok'])
            post('/api/targets', {'address':'1.1.1.1','name':'test'})
            with self.assertRaises(HTTPError) as caught:
                client.open(base+'/api/health')
            self.assertEqual(caught.exception.code, 503)
            with self.assertRaises(HTTPError) as caught:
                post('/api/targets', {'address':'8.8.8.8'}, Origin='https://evil.example')
            self.assertEqual(caught.exception.code, 403)
            with self.assertRaises(HTTPError) as caught:
                post('/api/targets', {'address':'1.1.1.1'})
            self.assertEqual(caught.exception.code, 409)
            targets = json.load(client.open(base+'/api/targets'))
            self.assertEqual(targets[0]['address'], '1.1.1.1')
            with app.connect(self.db) as conn:
                conn.executemany('INSERT INTO samples VALUES(?,?,?,?)', [(1,int(time.time())-i,ms,status) for i,ms,status in [(0,10,'ok'),(1,None,'loss'),(2,None,'error')]])
            rows = json.load(client.open(base+'/api/data?target=1&hours=1'))['samples']
            self.assertEqual(len(rows), 3)
            for path in ('/', '/app.js', '/style.css', '/chart.js'):
                self.assertEqual(client.open(base+path).status, 200)
            for path in ('/api/data?hours=-1', '/../../etc/passwd'):
                with self.assertRaises(HTTPError):
                    client.open(base+path)
            post('/api/delete', {'id':1})
            self.assertEqual(json.load(client.open(base+'/api/targets')), [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
