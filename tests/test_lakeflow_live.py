import base64
from copy import deepcopy
import json
from pathlib import Path
import time
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from automation import AutomationService
from databricks_settings import DatabricksSettings
from lakeflow_live import LakeflowLive
from monitoring import Conflict
import test_lakeflow_results as binding_fixture

class LiveTests(unittest.TestCase):
    def setUp(self):
        self.fixture=binding_fixture.BindingTests();self.fixture.setUp();self.runs=self.fixture.runs;self.rid=self.fixture.rid;self.lakeflow=self.fixture.service;self.fixture.bundle()
        with self.runs.db() as db:db.execute("UPDATE sources SET status='converted',review='approved' WHERE run_id=?",(self.rid,))
        self.calls=[];self.notebooks={};self.jobs={};self.pipelines={};self.failed=False;self.drift=False;self.query_value='4';self.running=False
        self.settings=DatabricksSettings(self.runs,self.transport);self.settings.save({'workspace_url':'https://dbc-fixture.cloud.databricks.com','auth_mode':'session_token','warehouse_id':'warehouse','catalog':'main','schema':'migration','revision':0,'token':'fixture-secret-token'})
        self.automation=AutomationService(self.runs,self.settings);self.live=LakeflowLive(self.runs,self.settings,self.lakeflow,self.automation,sleep=lambda n:time.sleep(.001))
        self.case=self.runs.save_case(self.rid,{'title':'Four products','source':'ProductPackage.dtsx','target_sql':'SELECT COUNT(*) AS ProductCnt FROM dbo.Product','columns':[{'name':'ProductCnt','type':'integer'}]});self.automation.baseline(self.rid,{'case_id':self.case['id'],'case_version':1,'version':0,'expected':{'format':'json','content':'[{"ProductCnt":4}]'},'evidence':'Synthetic four-product fixture'})
    def tearDown(self):self.running=False;self.live.close();self.automation.close();self.fixture.tearDown()
    def transport(self,host,token,method,path,payload=None):
        self.calls.append((method,path,deepcopy(payload)))
        if path.endswith('/mkdirs'):return {}
        if path.endswith('/import'):self.notebooks[payload['path']]=payload['content'];return {}
        if '/workspace/export?' in path:
            from urllib.parse import parse_qs,urlsplit
            p=parse_qs(urlsplit(path).query)['path'][0];return {'content':base64.b64encode(b'changed').decode() if self.drift else self.notebooks[p]}
        if path=='/api/2.1/jobs/create':self.jobs['101']=deepcopy(payload);return {'job_id':101}
        if path=='/api/2.0/pipelines':self.pipelines['pid-1']=deepcopy(payload);return {'pipeline_id':'pid-1'}
        if path=='/api/2.0/pipelines/pid-1':return {'spec':self.pipelines['pid-1']}
        if '/jobs/get?' in path:return {'settings':self.jobs['101']}
        if path.endswith('/run-now'):return {'run_id':202}
        if path.endswith('/runs/cancel'):self.running=False;self.failed=True;return {}
        if '/runs/get?run_id=' in path:
            result='FAILED' if self.failed else 'SUCCESS';state={'life_cycle_state':'RUNNING'} if self.running else {'life_cycle_state':'TERMINATED','result_state':result}
            return {'state':state,'tasks':[{'task_key':t['task_key'],'run_id':300+i,'state':state} for i,t in enumerate(self.jobs['101']['tasks'])]}
        if '/get-output?' in path:return {'notebook_output':{'result':json.dumps({'crosslake_task_values':{'ssis_User_ProductCount':4}})}}
        if path=='/api/2.0/sql/statements':return {'statement_id':'query-id','status':{'state':'SUCCEEDED'},'manifest':{'total_row_count':1,'total_chunk_count':1,'schema':{'columns':[{'name':'ProductCnt'}]}},'result':{'data_array':[[self.query_value]]}}
        raise AssertionError((method,path))
    def wait(self,op):
        for _ in range(1000):
            op=self.live.status(self.rid,op['id'])
            if op['status'] not in ('queued','deploying','running','validating','cancelling'):return op
            time.sleep(.005)
        raise AssertionError('Operation never finished: '+json.dumps(op))
    def deploy(self):
        p=self.live.preview(self.rid);return self.wait(self.live.deploy(self.rid,{'revision':1,'workspace_url':p['workspace_url'],'bundle_sha256':p['bundle_sha256'],'allow_deploy':True,'reviewed_code':True}))
    def execute_job(self,dep,**changes):return self.live.run(self.rid,{'revision':1,'deployment_id':dep['id'],'job_resource':next(iter(dep['report']['jobs'])),'allow_run':True,**changes})
    def test_deploy_isolated_paused_and_does_not_execute(self):
        dep=self.deploy();self.assertEqual(dep['status'],'deployed');self.assertEqual(len(dep['report']['notebooks']),3);self.assertTrue(all('/Shared/Crosslake/'+self.rid+'/' in n for n in self.notebooks));self.assertTrue(all(not p['overwrite'] for m,path,p in self.calls if path.endswith('/import')));self.assertFalse(any(path.endswith('/run-now') for m,path,p in self.calls));job=self.jobs['101'];self.assertEqual(job['schedule']['pause_status'],'PAUSED');self.assertTrue(all(t['notebook_task']['base_parameters']['migration_reviewed']=='true' for t in job['tasks']))
    def test_complete_run_and_linked_validation(self):
        dep=self.deploy();op=self.wait(self.execute_job(dep,validate_after=True,allow_validation=True,case_ids=[self.case['id']],fixture_ref='Synthetic product input v1'));self.assertEqual(op['status'],'validated');r=op['report'];self.assertEqual(r['remote_run_id'],'202');self.assertEqual(r['validation_status'],'passed');self.assertEqual(r['validation_report']['parent_execution']['remote_run_id'],'202');self.assertEqual(r['tasks'][-1]['task_values']['ssis_User_ProductCount'],4)
        raw=Path(self.fixture.tmp.name,'db').read_bytes();self.assertNotIn(b'fixture-secret-token',raw)
    def test_validation_mismatch_is_distinct_from_job_success(self):
        self.query_value='5';op=self.wait(self.execute_job(self.deploy(),validate_after=True,allow_validation=True,case_ids=[self.case['id']],fixture_ref='fixture'));self.assertEqual(op['status'],'validation_failed');self.assertEqual(op['report']['state']['result_state'],'SUCCESS')
    def test_failed_job_never_starts_validation(self):
        self.failed=True;op=self.wait(self.execute_job(self.deploy(),validate_after=True,allow_validation=True,case_ids=[self.case['id']],fixture_ref='fixture'));self.assertEqual(op['status'],'failed');self.assertNotIn('suite_id',op['report']);self.assertFalse(any('/sql/statements' in path for m,path,p in self.calls))
    def test_drift_prevents_remote_execution(self):
        dep=self.deploy();self.drift=True;op=self.wait(self.execute_job(dep));self.assertEqual(op['status'],'error');self.assertFalse(any(path.endswith('/run-now') for m,path,p in self.calls))
    def test_job_setting_drift_prevents_execution(self):
        dep=self.deploy();self.jobs['101']['tasks'][0]['max_retries']=9;self.assertEqual(self.wait(self.execute_job(dep))['status'],'error')
    def test_consents_and_workspace_pin(self):
        p=self.live.preview(self.rid)
        with self.assertRaises(ValueError):self.live.deploy(self.rid,{'revision':1})
        with self.assertRaises(Conflict):self.live.deploy(self.rid,{'revision':1,'allow_deploy':True,'reviewed_code':True,'workspace_url':'https://other.cloud.databricks.com','bundle_sha256':p['bundle_sha256']})
        self.assertEqual(self.calls,[])
        dep=self.deploy()
        with self.assertRaises(ValueError):self.execute_job(dep,allow_run=False)
        with self.assertRaises(ValueError):self.execute_job(dep,validate_after=True,allow_validation=False)
    def test_source_review_required(self):
        with self.runs.db() as db:db.execute("UPDATE sources SET review='pending' WHERE run_id=?",(self.rid,))
        with self.assertRaises(Conflict):self.live.preview(self.rid)
    def test_cancel_owned_run_and_delete_guard(self):
        dep=self.deploy();self.running=True;op=self.execute_job(dep)
        for _ in range(100):
            current=self.live.status(self.rid,op['id'])
            if current['report'].get('remote_run_id'):break
            time.sleep(.005)
        with self.assertRaises(Conflict):self.runs.delete(self.rid)
        self.live.cancel(self.rid,{'id':op['id']});self.assertEqual(self.wait(op)['status'],'cancelled');self.assertTrue(any(p=={'run_id':202} for m,path,p in self.calls if path.endswith('/runs/cancel')))
    def test_pipeline_resources_resolve_to_notebook_libraries(self):
        dep=self.live.preview(self.rid);body={'kind':'deployment','proof':'fixture','paths':{'pipelines/copy.py':'# Databricks notebook source\nprint(1)'},'namespace':{'catalog':'main','schema':'migration'},'resources':{'pipelines':{'copy':{'name':'copy','configuration':{'crosslake.migration_reviewed':'false'},'libraries':[{'file':{'path':'${workspace.file_path}/pipelines/copy.py'}}]}},'jobs':{'copy_job':{'name':'copy_job','tasks':[{'task_key':'copy','pipeline_task':{'pipeline_id':'${resources.pipelines.copy.id}'}}]}}}}
        config,token,_=self.settings._credentials({'revision':1});out=self.wait(self.live._queue(self.rid,'deployment',body,self.live._deploy,config,token));self.assertEqual(out['status'],'deployed');self.assertEqual(self.jobs['101']['tasks'][0]['pipeline_task']['pipeline_id'],'pid-1');self.assertIn('notebook',self.pipelines['pid-1']['libraries'][0]);self.assertEqual(self.pipelines['pid-1']['configuration']['crosslake.migration_reviewed'],'true')

    def test_post_run_validation_refuses_baseline_changes(self):
        dep=self.deploy();self.running=True;op=self.execute_job(dep,validate_after=True,allow_validation=True,case_ids=[self.case['id']],fixture_ref='fixture')
        for _ in range(200):
            if self.live.status(self.rid,op['id'])['report'].get('remote_run_id'):break
            time.sleep(.005)
        self.automation.baseline(self.rid,{'case_id':self.case['id'],'case_version':1,'version':1,'expected':{'format':'json','content':'[{"ProductCnt":5}]'},'evidence':'Changed during execution'})
        self.running=False;out=self.wait(op);self.assertEqual(out['status'],'validation_error');self.assertNotIn('suite_id',out['report']);self.assertFalse(any('/sql/statements' in path for m,path,p in self.calls))
    def test_failed_partial_deployment_keeps_recorded_ids(self):
        original=self.settings.transport
        def fail(host,token,method,path,payload=None):
            if path.endswith('/jobs/create'):raise RuntimeError('provider-private-error')
            return original(host,token,method,path,payload)
        self.settings.transport=fail;out=self.deploy();self.assertEqual(out['status'],'error');self.assertEqual(len(out['report']['notebooks']),3);self.assertNotIn('provider-private-error',json.dumps(out))
    def test_read_only_reconciliation_does_not_resubmit(self):
        dep=self.deploy();op=self.wait(self.execute_job(dep));before=sum(path.endswith('/run-now') for m,path,p in self.calls)
        out=self.live.refresh_remote(self.rid,{'id':op['id'],'revision':1});self.assertEqual(out['report']['reconciled_remote_state']['result_state'],'SUCCESS');self.assertEqual(before,sum(path.endswith('/run-now') for m,path,p in self.calls))
    def test_http_routes_and_request_guards(self):
        from server import AppServer,Handler,ROOT
        from functools import partial
        import threading
        from urllib.request import Request,urlopen
        from urllib.error import HTTPError
        server=AppServer(('127.0.0.1',0),partial(Handler,directory=str(ROOT/'static')),self.runs);server.lakeflow_live.close();server.lakeflow_live=self.live
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base='http://127.0.0.1:'+str(server.server_port)+'/api/runs/'+self.rid+'/lakeflow/live'
        try:
            self.assertEqual(json.load(urlopen(base))['operations'],[])
            req=Request(base+'/preview',data=b'{}',headers={'Content-Type':'application/json','X-Accelerator-Request':'1'});self.assertIn('bundle_sha256',json.load(urlopen(req)))
            with self.assertRaises(HTTPError) as e:urlopen(Request(base+'/deploy',data=b'{}',headers={'Content-Type':'application/json'}))
            self.assertEqual(e.exception.code,415)
        finally:server.shutdown();server.server_close();thread.join()
