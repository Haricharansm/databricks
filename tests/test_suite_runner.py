import copy
import json
import unittest
from pathlib import Path
from suite_runner import run_suite, DatabricksEndpoint, Cancelled
from ci_read_query import read_query

ROOT=Path(__file__).resolve().parents[1]
class Endpoint:
    def __init__(self,data,events,label):self.data=data;self.events=events;self.label=label
    def query(self,sql,cancel):self.events.append(self.label+' query');return self.data,{'executed_by_app':True}
    def run_job(self,job,cancel):self.events.append(self.label+' job');return {'job_id':job}
class SuiteTests(unittest.TestCase):
    def setUp(self):self.manifest=json.loads((ROOT/'samples/golden-suite/suite.json').read_text())
    def test_offline_and_drift(self):
        self.assertEqual(run_suite(self.manifest,fixture_mode=True)['status'],'passed')
        m=copy.deepcopy(self.manifest);m['cases'][0]['actual']['content']='[{"id":9,"amount":"99"}]'
        self.assertNotEqual(run_suite(m,fixture_mode=True)['status'],'passed')
    def test_fixture_rejects_remote(self):
        events=[];e=Endpoint({},events,'target')
        self.assertEqual(run_suite(self.manifest,e,fixture_mode=True)['status'],'error');self.assertEqual(events,[])
    def test_source_precedes_target_and_drift_blocks_job(self):
        events=[];data=self.manifest['cases'][0]['expected'];source=Endpoint(data,events,'source');target=Endpoint(data,events,'target')
        result=run_suite(self.manifest,target,source,source_job='ssis',target_job='123')
        self.assertEqual(result['status'],'passed');self.assertEqual(events,['source job','source query','target job','target query'])
        events.clear();source.data={'format':'json','content':'[{"id":9,"amount":"99"}]'}
        self.assertEqual(run_suite(self.manifest,target,source,target_job='123')['status'],'failed');self.assertEqual(events,['source query'])
    def test_preflight_before_job(self):
        events=[];e=Endpoint({},events,'target');self.manifest['cases'][0]['definition']['target_sql']='DELETE FROM x'
        self.assertEqual(run_suite(self.manifest,e,target_job='123')['status'],'error');self.assertEqual(events,[])
    def test_cancelled(self):self.assertEqual(run_suite(self.manifest,fixture_mode=True,cancel=lambda:True)['status'],'cancelled')
    def test_query_guard(self):
        for sql in ['DELETE FROM x','SELECT 1; DROP TABLE x','WITH x AS (DELETE FROM y) SELECT * FROM x']:
            with self.assertRaises(ValueError):read_query(sql)
        self.assertTrue(read_query("SELECT 'DROP TABLE x;' AS label"))
    def response(self):return {'statement_id':'abc-123','status':{'state':'SUCCEEDED'},'manifest':{'total_row_count':1,'total_chunk_count':1,'schema':{'columns':[{'name':'id'}]}},'result':{'data_array':[['1']]}}
    def test_target_complete_and_incomplete(self):
        for change in ['ok','truncated','chunks','missing','duplicate']:
            r=self.response()
            if change=='truncated':r['manifest']['truncated']=True
            if change=='chunks':r['manifest']['total_chunk_count']=2
            if change=='missing':r['manifest']['total_row_count']=2
            if change=='duplicate':r['manifest']['schema']['columns']=[{'name':'id'},{'name':'id'}];r['result']['data_array']=[['1','1']]
            e=DatabricksEndpoint({'workspace_url':'https://test.cloud.databricks.com','warehouse_id':'w'},'secret',lambda *args:r)
            if change=='ok':self.assertEqual(json.loads(e.query('SELECT 1',lambda:False)[0]['content'])['rows'],[{'id':'1'}])
            else:
                with self.assertRaises(ValueError):e.query('SELECT 1',lambda:False)
    def test_cancel_own_statement(self):
        calls=[]
        def transport(host,token,method,path,body):calls.append(path);return {'statement_id':'abc-123','status':{'state':'RUNNING'}}
        e=DatabricksEndpoint({'workspace_url':'https://test.cloud.databricks.com','warehouse_id':'w'},'secret',transport)
        with self.assertRaises(Cancelled):e.query('SELECT 1',lambda:True)
        self.assertEqual(calls[-1],'/api/2.0/sql/statements/abc-123/cancel')
if __name__=='__main__':unittest.main()

class SourceAdapterTests(unittest.TestCase):
    def adapter(self,*,running=False,changed=False,failed=False):
        from suite_runner import SqlServerEndpoint
        from datetime import datetime,timedelta
        now=datetime(2026,1,1,12,0,0,500000);calls=[]
        class Cursor:
            def execute(self,sql,*args):self.sql=sql;calls.append(sql);return self
            def fetchone(self):
                if 'version_number' in self.sql:return ('job-id',1,now)
                if 'MAX(session_id)' in self.sql:return (1,)
                if 'job_history_id' in self.sql:return (now.replace(microsecond=0),now+timedelta(seconds=1),42)
                if 'start_execution_date' in self.sql:return (now-timedelta(seconds=10),None) if running else (None,None)
                if 'run_status' in self.sql:return (0 if failed else 1,0)
                raise AssertionError(self.sql)
        class Conn:
            def cursor(self):return Cursor()
            def close(self):pass
        return SqlServerEndpoint('sensitive-credential',allowed_jobs=['fixture'],connect=lambda _:Conn(),sleep=lambda _:None),calls
    def test_agent_success_handles_second_precision(self):
        e,calls=self.adapter();self.assertEqual(e.run_job('fixture',lambda:False)['history_instance_id'],42)
        self.assertEqual(sum('sp_start_job' in x for x in calls),1)
    def test_agent_running_rejected_without_start(self):
        e,calls=self.adapter(running=True)
        with self.assertRaises(ValueError):e.run_job('fixture',lambda:False)
        self.assertFalse(any('sp_start_job' in x for x in calls))
    def test_agent_failure_and_allowlist(self):
        e,calls=self.adapter(failed=True)
        with self.assertRaises(RuntimeError):e.run_job('fixture',lambda:False)
        calls.clear()
        with self.assertRaises(ValueError):e.run_job('other',lambda:False)
        self.assertEqual(calls,[])
