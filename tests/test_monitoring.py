from functools import partial
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from monitoring import RunService,Conflict
from server import AppServer,Handler,ROOT

FILES={'files':[{'name':'a.sql','sql':'SELECT 1;'}]}

def wait_for(service,run_id,states,timeout=10):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        run=service.get(run_id)
        if run['status'] in states:return run
        time.sleep(.02)
    raise AssertionError('Timed out waiting for run: '+str(service.get(run_id,False)))

def generated(files):
    return {'engine':'test-engine','files':[{'name':files[0]['name'],'sql':'SELECT 1;'}],'conversion_errors':'','diagnostics':''}

class RunTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'migration.sqlite3';self.service=RunService(self.path)
    def tearDown(self):self.service.close();self.temp.cleanup()
    def assessed(self,payload=FILES):
        run=self.service.create(payload);return wait_for(self.service,run['id'],{'assessed','failed'})
    def converted(self):
        run=self.assessed()
        with patch('monitoring.convert',side_effect=generated):
            self.service.start_conversion(run['id']);return wait_for(self.service,run['id'],{'completed','partial','failed'})

    def test_durable_sources_artifacts_and_review_with_manual_evidence(self):
        run=self.converted();self.assertEqual(run['status'],'completed')
        payload={'source':'a.sql','review':'approved','validation':'passed','review_note':'Code reviewed','validation_note':'Manual: sample data test, workspace X, row count 1','version':0}
        self.service.review(run['id'],payload)
        self.service.close();self.service=RunService(self.path)
        restored=self.service.get(run['id'])
        self.assertEqual(restored['files'][0]['sql'],'SELECT 1;')
        self.assertEqual(restored['conversion']['files'][0]['sql'],'SELECT 1;')
        self.assertFalse(restored['conversion']['validated_on_databricks'])
        self.assertEqual(restored['sources'][0]['validation'],'passed')
        self.assertTrue(any('manual validation: passed' in e['message'] for e in restored['events']))
        self.assertEqual(self.service.listing()['stats']['manual_passed'],1)

    def test_partial_failure_preserves_good_artifacts_and_blocks_approval(self):
        run=self.assessed({'files':[{'name':'a.sql','sql':'SELECT 1;'},{'name':'b.sql','sql':'SELECT 2;'}]})
        def convert(files):
            if files[0]['name']=='b.sql':raise RuntimeError('Unsupported construct')
            return generated(files)
        with patch('monitoring.convert',side_effect=convert):
            self.service.start_conversion(run['id']);result=wait_for(self.service,run['id'],{'partial','failed','completed'})
        self.assertEqual(result['status'],'partial');self.assertEqual(result['progress'],{'total':2,'finished':2,'converted':1,'blocked':1})
        self.assertTrue(result['conversion']['files'])
        with self.assertRaises(Conflict):self.service.review(run['id'],{'source':'b.sql','review':'approved','validation':'not_run','version':0})
        self.assertEqual(self.service.listing()['stats']['blocked'],1)

    def test_engine_errors_are_blocked_but_output_kept(self):
        run=self.assessed()
        with patch('monitoring.convert',return_value={**generated(FILES['files']),'conversion_errors':'known error'}):
            self.service.start_conversion(run['id']);result=wait_for(self.service,run['id'],{'failed'})
        self.assertEqual(result['sources'][0]['status'],'blocked')
        self.assertTrue(result['conversion']['files'])

    def test_duplicate_start_and_review_version_conflict(self):
        run=self.converted()
        with self.assertRaises(Conflict):self.service.start_conversion(run['id'])
        payload={'source':'a.sql','review':'in_review','validation':'not_run','version':0}
        self.service.review(run['id'],payload)
        with self.assertRaises(Conflict):self.service.review(run['id'],payload)
        with self.assertRaises(ValueError):self.service.review(run['id'],{**payload,'review':'approved','validation':'passed','version':1})

    def test_interrupted_work_is_not_replayed(self):
        run=self.assessed()
        with self.service.db() as db:
            db.execute("UPDATE runs SET status='converting' WHERE id=?",(run['id'],))
            db.execute("UPDATE sources SET status='converting' WHERE run_id=?",(run['id'],))
        self.service.close();self.service=RunService(self.path)
        result=self.service.get(run['id']);self.assertEqual(result['status'],'interrupted')
        self.assertEqual(result['sources'][0]['status'],'blocked')
        self.assertIn('restarted',result['error'])

    def test_active_run_survives_client_disconnection_and_cannot_be_deleted(self):
        gate=threading.Event();run=self.assessed()
        def convert(files):gate.wait(5);return generated(files)
        with patch('monitoring.convert',side_effect=convert):
            self.service.start_conversion(run['id']);wait_for(self.service,run['id'],{'converting'})
            try:
                with self.assertRaises(Conflict):self.service.delete(run['id'])
                with self.assertRaises(Conflict):self.service.start_conversion(run['id'])
            finally:gate.set()
            self.assertEqual(wait_for(self.service,run['id'],{'completed'})['status'],'completed')

    def test_search_pagination_and_delete(self):
        first=self.assessed({**FILES,'label':'Alpha'});self.assessed({**FILES,'label':'Beta'})
        page=self.service.listing(limit=1,query='Alpha');self.assertEqual(page['total'],1);self.assertEqual(page['runs'][0]['id'],first['id'])
        self.assertEqual(len(self.service.listing(limit=1,offset=1)['runs']),1)
        self.service.delete(first['id'])
        with self.assertRaises(KeyError):self.service.get(first['id'])
        with self.service.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM sources WHERE run_id=?',(first['id'],)).fetchone()[0],0)

class RunAPITests(unittest.TestCase):
    def test_async_run_history_review_and_refresh_api(self):
        with tempfile.TemporaryDirectory() as directory:
            service=RunService(Path(directory)/'runs.db')
            server=AppServer(('127.0.0.1',0),partial(Handler,directory=str(ROOT/'static')),run_service=service)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            def post(path,payload):
                return urlopen(Request(base+path,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Accelerator-Request':'1'}))
            try:
                with post('/api/runs',FILES) as response:
                    self.assertEqual(response.status,202);run=json.load(response)
                wait_for(service,run['id'],{'assessed'})
                with urlopen(base+'/api/runs/'+run['id']+'?summary=1') as response:
                    summary=json.load(response);self.assertNotIn('files',summary);self.assertEqual(summary['status'],'assessed')
                with urlopen(base+'/api/runs?q=Migration') as response:self.assertEqual(json.load(response)['total'],1)
                with patch('monitoring.convert',side_effect=generated):
                    with post('/api/runs/'+run['id']+'/convert',{}) as response:self.assertEqual(response.status,202)
                    wait_for(service,run['id'],{'completed'})
                with post('/api/runs/'+run['id']+'/review',{'source':'a.sql','review':'in_review','validation':'not_run','version':0}) as response:
                    self.assertEqual(json.load(response)['sources'][0]['review'],'in_review')
                with self.assertRaises(HTTPError) as failure:post('/api/runs/'+run['id']+'/convert',{})
                self.assertEqual(failure.exception.code,409)
                with self.assertRaises(HTTPError) as hidden:urlopen(base+'/.data/migration.sqlite3')
                self.assertEqual(hidden.exception.code,404)
            finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
