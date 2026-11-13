"""Isolated real iputils and process-lifecycle checks, never use the live database."""
import json
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import build_opener, ProxyHandler

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
import monitor


@unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('ping'), 'Linux iputils required')
class Runtime(unittest.TestCase):
    def test_real_ipv4_ipv6_packets(self):
        for address in ('127.0.0.1','::1'):
            for packets in (1,3,5):
                with self.subTest(address=address,packets=packets):
                    result=monitor.probe(address,packets)
                    self.assertEqual(result.status,'ok')
                    self.assertEqual((result.sent,result.received),(packets,packets))

    def test_actual_process_sigterm_flush(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'runtime.db'
            app.initialize(db)
            # Loopback targets are fixture-only, inserted without exposing them in UI.
            with app.connect(db) as conn:
                conn.execute('INSERT INTO targets(address,name) VALUES(?,?)',('127.0.0.1','fixture'))
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            process=subprocess.Popen([sys.executable,str(app.ROOT/'app.py'),'--db',str(db),'--port',str(port),'--interval','10','--packets','3'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            client=build_opener(ProxyHandler({}))
            try:
                deadline=time.monotonic()+10
                while True:
                    try:
                        data=json.load(client.open(f'http://127.0.0.1:{port}/api/data?target=1&hours=1',timeout=1))
                        if data['samples']: break
                    except OSError:
                        pass
                    if time.monotonic()>deadline: self.fail('No sample from isolated process')
                    time.sleep(.1)
                self.assertEqual(data['current']['successful_packets'],3)
                self.assertEqual(data['state']['state'],'success')
                self.assertEqual(json.load(client.open(f'http://127.0.0.1:{port}/api/health'))['details'][0]['state'],'success')
                process.send_signal(signal.SIGTERM)
                stdout,stderr=process.communicate(timeout=10)
                self.assertEqual(process.returncode,0,stderr)
                with app.connect(db) as conn:
                    self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                    self.assertEqual(conn.execute('SELECT received FROM samples').fetchone()[0],3)
            finally:
                if process.poll() is None:
                    process.kill();process.communicate()
