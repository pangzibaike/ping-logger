import concurrent.futures
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import monitor
import storage


class Reliability(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name)/'test.db'
        app.initialize(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, address='1.1.1.1'):
        app.add_target(self.db, address, '')

    def test_ipv4_ipv6_command_and_packets(self):
        for address in ('1.1.1.1', '2606:4700:4700::1111'):
            for count in (1,3,5):
                output='\n'.join('64 bytes time=%s ms' % (i+1) for i in range(count))
                output+=f'\n{count} packets transmitted, {count} received'
                with patch('monitor.subprocess.run', return_value=subprocess.CompletedProcess([],0,output,'')) as run:
                    result=monitor.probe(address,count)
                self.assertEqual((result.sent,result.received), (count,count))
                self.assertEqual(result.ms,(count+1)/2)
                self.assertEqual(run.call_args.args[0][-1],address)
                self.assertIn(str(count),run.call_args.args[0])
                self.assertNotIn('-w',run.call_args.args[0])
                self.assertEqual(run.call_args.kwargs['timeout'],6)
                self.assertEqual(result.rtts,list(range(1,count+1)))

    def test_slow_single_response_is_not_rejected(self):
        output='64 bytes time=204 ms\n1 packets transmitted, 1 received, 0% packet loss'
        with patch('monitor.subprocess.run',return_value=subprocess.CompletedProcess([],0,output,'')) as run:
            result=monitor.probe('23.94.147.121',1)
        self.assertNotIn('-w',run.call_args.args[0])
        self.assertEqual((result.sent,result.received,result.ms,result.status),(1,1,204,'ok'))

    def test_failure_categories(self):
        for code,text,reason,status in [(1,'100% packet loss','timeout','loss'),(1,'Destination Host Unreachable','unreachable','loss'),
                (2,'Operation not permitted','permission','error'),(2,'Network is unreachable','no_route','error'),(2,'weird output','system_error','error')]:
            with self.subTest(reason=reason):
                result=monitor.parse_result(code,text)
                self.assertEqual((result.reason,result.status),(reason,status))
        for exception,reason in [(FileNotFoundError(),'command_missing'),(PermissionError(),'permission'),(OSError(),'system_error'),(subprocess.TimeoutExpired('ping',6),'process_timeout')]:
            with patch('monitor.subprocess.run',side_effect=exception), self.assertLogs('ping_logger.probe'):
                result=monitor.probe('1.1.1.1')
            self.assertEqual(result.reason,reason)
            self.assertEqual(result.sent,0)

    def test_partial_loss(self):
        for count in (3,5):
            result=monitor.parse_result(1,f'time=10 ms\ntime=20 ms\n{count} packets transmitted, 2 received',count)
            self.assertEqual((result.sent,result.received,result.ms),(count,2,15))

    def test_statistics_and_empty(self):
        rows=[(i, float(i), 'ok','success',1,1,float(i),float(i),json.dumps([i]),60) for i in range(1,101)]
        stats=monitor.statistics(rows)
        self.assertEqual(stats['avg_ms'],50.5)
        self.assertEqual(stats['p50_ms'],50.5)
        self.assertAlmostEqual(stats['p95_ms'],95.05)
        self.assertAlmostEqual(stats['p99_ms'],99.01)
        self.assertEqual(stats['loss_pct'],0)
        empty=monitor.statistics([])
        for key in ('avg_ms','p50_ms','p95_ms','p99_ms','loss_pct'):
            self.assertIsNone(empty[key])
        self.assertEqual(monitor.statistics(rows[:1])['p99_ms'],1)

    def test_packet_weighting_loss_and_streak(self):
        rows=[(60,15,'ok','timeout',3,2,10,20,'[10,20]',60),
              (120,None,'error','permission',0,0,None,None,'[]',60),
              (180,None,'loss','timeout',5,0,None,None,'[]',60),
              (240,None,'loss','timeout',1,0,None,None,'[]',60)]
        s=monitor.statistics(rows)
        self.assertAlmostEqual(s['loss_pct'],700/9)
        self.assertEqual(s['consecutive_loss_cycles'],2)
        self.assertEqual((s['last_success'],s['last_failure']),(60,240))
        rows.append((1000,None,'loss','timeout',1,0,None,None,'[]',60))
        self.assertEqual(monitor.statistics(rows)['consecutive_loss_cycles'],1)

    def test_health_each_target(self):
        self.add(); self.add('8.8.8.8')
        now=int(time.time())
        storage.store_results(self.db,now,[(1,'a')],[monitor.parse_result(0,'time=1 ms')],app.Settings())
        with app.connect(self.db) as c:
            h=app.health(c,app.Settings(),now)
        self.assertFalse(h['ok'])
        self.assertEqual([d['state'] for d in h['details']],['success','pending'])
        for reason,state in [('timeout','loss'),('permission','probe_error')]:
            r=monitor.Result(None,'loss' if state=='loss' else 'error',reason,1 if state=='loss' else 0,0,[])
            storage.store_results(self.db,now,[(2,'b')],[r],app.Settings())
            with app.connect(self.db) as c: h=app.health(c,app.Settings(),now)
            self.assertEqual(h['details'][1]['state'],state)
            self.assertEqual(h['ok'],state=='loss')
        with app.connect(self.db) as c: h=app.health(c,app.Settings(),now+1000)
        self.assertEqual(h['details'][0]['state'],'interrupted')

    def test_migration_preserves_legacy_and_idempotent(self):
        old=Path(self.tmp.name)/'old.db'
        with sqlite3.connect(old) as c:
            c.executescript('CREATE TABLE targets(id INTEGER PRIMARY KEY AUTOINCREMENT,address TEXT UNIQUE,name TEXT); CREATE TABLE samples(target_id INTEGER,ts INTEGER,ms REAL,status TEXT,PRIMARY KEY(target_id,ts)) WITHOUT ROWID; INSERT INTO targets VALUES(9,"1.1.1.1","old"); INSERT INTO samples VALUES(9,100,12,"ok"); INSERT INTO samples VALUES(9,160,NULL,"loss");')
        app.initialize(old); app.initialize(old)
        with app.connect(old) as c:
            self.assertEqual(c.execute('SELECT id,address,name FROM targets').fetchone(),(9,'1.1.1.1','old'))
            rows=c.execute('SELECT '+storage.COLUMNS+' FROM samples ORDER BY ts').fetchall()
            self.assertEqual(len(rows),2)
            self.assertEqual(monitor.statistics(rows)['loss_pct'],50)
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        self.assertEqual(len(list(old.parent.glob('old.db.pre-v2-*.sqlite'))),1)

    def test_transaction_rolls_back(self):
        self.add()
        with self.assertRaises(RuntimeError):
            with app.connect(self.db) as c:
                c.execute('UPDATE targets SET name="oops"')
                raise RuntimeError()
        with app.connect(self.db) as c:
            self.assertEqual(c.execute('SELECT name FROM targets').fetchone()[0],'1.1.1.1')

    def test_busy_retries_and_recovers(self):
        self.add()
        original = storage.connect
        calls = 0
        def busy_once(db):
            nonlocal calls
            calls += 1
            if calls == 1: raise sqlite3.OperationalError('database is locked')
            return original(db)
        with patch.object(storage,'connect',busy_once), self.assertLogs('ping_logger.database'):
            storage.store_results(self.db,100,[(1,'a')],[monitor.parse_result(0,'time=1 ms')],app.Settings())
        self.assertEqual(calls,2)
        with app.connect(self.db) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM samples').fetchone()[0],1)

    def test_wal_concurrent_reader_writer(self):
        self.add()
        with app.connect(self.db) as reader:
            self.assertEqual(reader.execute('PRAGMA journal_mode').fetchone()[0],'wal')
            reader.execute('BEGIN')
            reader.execute('SELECT * FROM targets').fetchall()
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                pool.submit(storage.store_results,self.db,100,[(1,'a')],[monitor.parse_result(0,'time=1 ms')],app.Settings()).result(3)

    def test_retention_requires_opt_in_and_days(self):
        self.add()
        now=int(time.time())
        for ts in (now-31*86400,now-8*86400,now):
            storage.store_results(self.db,ts,[(1,'a')],[monitor.parse_result(0,'time=1 ms')],app.Settings())
        storage.maintenance(self.db,app.Settings(),now)
        with app.connect(self.db) as c: self.assertEqual(c.execute('SELECT count(*) FROM samples').fetchone()[0],3)
        storage.maintenance(self.db,app.Settings(retention_days=30,enable_cleanup=True),now)
        with app.connect(self.db) as c: self.assertEqual(c.execute('SELECT count(*) FROM samples').fetchone()[0],2)
        storage.maintenance(self.db,app.Settings(enable_cleanup=True),now)
        with app.connect(self.db) as c: self.assertEqual(c.execute('SELECT count(*) FROM samples').fetchone()[0],1)

    def test_soft_delete_keeps_history_and_reactivation(self):
        self.add()
        storage.store_results(self.db,100,[(1,'a')],[monitor.parse_result(0,'time=1 ms')],app.Settings())
        with app.connect(self.db) as c: c.execute('UPDATE targets SET active=0')
        self.add()
        with app.connect(self.db) as c:
            self.assertEqual(c.execute('SELECT id,active FROM targets').fetchone(),(1,1))
            self.assertEqual(c.execute('SELECT count(*) FROM samples').fetchone()[0],1)

    def test_concurrency_and_stop(self):
        for i in range(1,7): self.add(f'1.1.1.{i}')
        stop=threading.Event(); lock=threading.Lock(); active=peak=completed=0
        def fake(*_):
            nonlocal active,peak,completed
            with lock: active+=1; peak=max(peak,active)
            time.sleep(.02)
            with lock:
                active-=1; completed+=1
                if completed==6: stop.set()
            return monitor.parse_result(0,'time=1 ms')
        with patch.object(app,'probe',fake):
            thread=threading.Thread(target=app.collect,args=(self.db,stop,app.Settings(workers=2)))
            thread.start();thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(peak,2)
        self.assertEqual(completed,6)

    def test_settings_validation(self):
        for key,value in [('interval',1),('packets',2),('workers',0),('retention_days',8),('enable_cleanup','yes')]:
            with self.assertRaises(ValueError): monitor.Settings(**{key:value})

    def test_controls_precede_target_buttons(self):
        from html.parser import HTMLParser
        class IDs(HTMLParser):
            def __init__(self): super().__init__(); self.values=[]
            def handle_starttag(self,tag,attrs):
                for name,value in attrs:
                    if name=='id': self.values.append(value)
        parser=IDs()
        parser.feed((app.ROOT/'web'/'index.html').read_text())
        self.assertEqual(len(parser.values),len(set(parser.values)))
        for control in ('filter','hours','management'):
            self.assertLess(parser.values.index(control),parser.values.index('targets'))

    def test_http_timeout_classification(self):
        handler=app.handler_for(self.db)
        server=app.ThreadingHTTPServer(('127.0.0.1',0),handler)
        original=handler.setup
        def setup(obj):
            original(obj);obj.connection.settimeout(.05)
        with patch.object(handler,'setup',setup), self.assertLogs('ping_logger.http',level='WARNING') as logs:
            thread=threading.Thread(target=server.serve_forever);thread.start()
            try:
                with socket.create_connection(server.server_address) as sock:
                    sock.sendall(b'GET / HTTP/1.1')
                    time.sleep(.15)
            finally:
                server.shutdown();server.server_close();thread.join()
        self.assertTrue(any('http_connection_timeout' in line for line in logs.output))


if __name__=='__main__': unittest.main()
