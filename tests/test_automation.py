import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from automation import AutomationService
from monitoring import RunService,Conflict
from databricks_settings import DatabricksSettings
from test_validation import wait

class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.runs=RunService(self.root/'runs.db');self.settings=DatabricksSettings(self.runs)
        self.service=AutomationService(self.runs,self.settings)
        self.run=wait(self.runs,self.runs.create({'files':[{'name':'a.sql','sql':'SELECT 1 AS id'}]})['id']);self.rid=self.run['id']
        self.case=self.runs.save_case(self.rid,{'title':'Golden','source':'a.sql','target_sql':'SELECT 1 AS id','columns':[{'name':'id','type':'integer'}]})
        self.data={'format':'json','content':'[{"id":1}]'}
        self.service.baseline(self.rid,{'case_id':self.case['id'],'case_version':1,'version':0,'expected':self.data,'evidence':'Synthetic fixture v1'})
        self.attempt=self.runs.evaluate_case(self.rid,self.case['id'],{'version':1,'expected':self.data,'actual':self.data,'expected_ref':'synthetic','actual_ref':'synthetic','evidence':'Offline fixture'})
    def tearDown(self):self.service.close();self.runs.close();self.tmp.cleanup()
    def payload(self):return {'case_ids':[self.case['id']],'mode':'fixtures','fixture_ref':'synthetic v1','fixture_attempts':{self.case['id']:self.attempt['id']},'include_baselines':True}
    def test_version_conflict(self):
        with self.assertRaises(Conflict):self.service.baseline(self.rid,{'case_id':self.case['id'],'case_version':1,'version':0,'expected':self.data,'evidence':'v2'})
    def test_durable_suite_and_stale_baseline(self):
        status=self.service.start(self.rid,self.payload())
        for _ in range(200):
            status=self.service.status(self.rid,status['id'])
            if status['status'] not in ('queued','running'):break
            time.sleep(.01)
        self.assertEqual(status['status'],'passed');self.assertFalse(status['report']['executed_by_app'])
        self.service.baseline(self.rid,{'case_id':self.case['id'],'case_version':1,'version':1,'expected':self.data,'evidence':'v2'})
        self.assertTrue(self.service.get(self.rid)['history'][0]['stale'])
    def test_export_is_standalone_and_detects_tampering(self):
        files=self.service.export(self.rid,self.payload())['files'];out=self.root/'export';out.mkdir()
        for f in files:
            p=out/f['name'];p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(f['sql'].encode())
        result=subprocess.run([sys.executable,'ci_runner.py','crosslake-suite.json','--output','results'],cwd=out,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        report=json.loads((out/'results/suite-report.json').read_text());self.assertNotIn('target_sql',report['cases'][0]['definition'])
        manifest=json.loads((out/'crosslake-suite.json').read_text());(out/manifest['cases'][0]['expected_file']).write_text('[{"id":99}]')
        result=subprocess.run([sys.executable,'ci_runner.py','crosslake-suite.json','--output','bad'],cwd=out,capture_output=True,text=True)
        self.assertEqual(result.returncode,2);self.assertIn('failure',(out/'bad/junit.xml').read_text())
    def test_export_consent_and_live_consent(self):
        p=self.payload();p['include_baselines']=False
        with self.assertRaises(ValueError):self.service.export(self.rid,p)
        p=self.payload();p['mode']='databricks'
        with self.assertRaises(ValueError):self.service.start(self.rid,p)
if __name__=='__main__':unittest.main()

class AutomationHttpTests(unittest.TestCase):
    def test_export_paths_and_automation_endpoint(self):
        import threading
        import urllib.request
        from urllib.error import HTTPError
        from functools import partial
        from server import AppServer,Handler,ROOT
        with tempfile.TemporaryDirectory() as d:
            runs=RunService(Path(d)/'runs.db');server=AppServer(('127.0.0.1',0),partial(Handler,directory=str(ROOT/'static')),runs);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                base='http://127.0.0.1:'+str(server.server_port)
                def post(path,payload):
                    req=urllib.request.Request(base+path,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Accelerator-Request':'1'})
                    return urllib.request.urlopen(req)
                for name in ['.gitattributes','.github/workflows/crosslake-suite.yml']:
                    self.assertEqual(post('/api/export',{'files':[{'name':name,'sql':'fixture'}]}).status,200)
                for name in ['.github/workflows/other.yml','.env','../crosslake-suite.yml']:
                    with self.assertRaises(HTTPError) as e:post('/api/export',{'files':[{'name':name,'sql':'fixture'}]})
                    self.assertEqual(e.exception.code,400)
                run=wait(runs,runs.create({'files':[{'name':'a.sql','sql':'SELECT 1'}]})['id'])
                self.assertEqual(json.load(urllib.request.urlopen(base+'/api/runs/'+run['id']+'/automation'))['cases'],[])
                with self.assertRaises(HTTPError) as e:post('/api/runs/'+run['id']+'/automation/start',{'mode':'fixtures'})
                self.assertEqual(e.exception.code,400)
            finally:server.shutdown();server.server_close();thread.join()
