import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from accelerator import Toolkit,read_query
from databricks_settings import DatabricksSettings
from monitoring import RunService
from test_validation import wait

class ToolkitTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=RunService(Path(self.tmp.name)/'runs.db');self.calls=[]
        self.result={'status':{'state':'SUCCEEDED'},'manifest':{'total_row_count':3,'schema':{'columns':[{'name':'id'},{'name':'amount'}]}},'result':{'data_array':[['1','10.25'],['1','10.25'],['2',None]]}}
        def transport(host,token,method,path,payload=None):
            self.calls.append((method,path,payload))
            if method=='POST':return {'statement_id':'abc-123','status':{'state':'PENDING'}}
            if '/jobs/' in path:return {'state':{'life_cycle_state':'TERMINATED','result_state':'SUCCESS'},'tasks':[{'task_key':'load','state':{'result_state':'SUCCESS'}}]}
            return self.result
        self.settings=DatabricksSettings(self.store,transport)
        self.settings.save({'workspace_url':'https://dbc-test.cloud.databricks.com','auth_mode':'session_token','warehouse_id':'abc123','catalog':'','schema':'','cli_profile':'','revision':0,'token':'fixture-token-value'})
        self.tk=Toolkit(self.store,self.settings)
        self.run=wait(self.store,self.store.create({'files':[{'name':'a.sql','sql':'SELECT id, amount FROM dbo.Sales;'}]})['id']);self.rid=self.run['id']
        self.case=self.store.save_case(self.rid,{'title':'Amounts','source':'a.sql','target_sql':'SELECT id, amount FROM sales','columns':[{'name':'id','type':'integer'},{'name':'amount','type':'decimal'}]})
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def payload(self):return {'case_id':self.case['id'],'version':1,'revision':1,'allow_compute':True,'expected_ref':'SQL Server fixture export','expected':{'format':'csv','content':'id,amount\n1,10.25\n1,10.25\n2,\\N\n'}}
    def test_query_executes_and_saves_real_target_evidence(self):
        op=self.tk.query(self.rid,self.payload());self.assertEqual(op['comparison_status'],'passed')
        report=self.store.validation_attempt(self.rid,op['attempt_id'])['report'];self.assertTrue(report['executed_by_app']);self.assertEqual(report['target_execution']['remote_id'],'abc-123');self.assertFalse(report['validated_on_databricks']);self.assertFalse(report['provenance']['verified_by_app'])
        count=len(self.store.validation_case(self.rid,self.case['id'])['attempts']);self.tk.refresh(self.rid,{'id':op['id'],'revision':1});self.assertEqual(count,len(self.store.validation_case(self.rid,self.case['id'])['attempts']))
        self.assertNotIn('fixture-token-value',Path(self.tmp.name,'runs.db').read_bytes().decode('latin1'))
    def test_refuses_compute_without_consent_or_stale_case(self):
        for change in ({'allow_compute':False},{'version':2}):
            with self.assertRaises(ValueError):self.tk.query(self.rid,{**self.payload(),**change})
        self.assertFalse(self.calls)
    def test_refuses_truncated_chunked_or_incomplete_results(self):
        for change in ('truncated','chunk','count'):
            with self.subTest(change=change):
                self.result={'status':{'state':'SUCCEEDED'},'manifest':{'total_row_count':0,'schema':{'columns':[{'name':'id'}]}},'result':{'data_array':[]}}
                if change=='truncated':self.result['manifest']['truncated']=True
                elif change=='chunk':self.result['result']['next_chunk_index']=1
                else:self.result['manifest']['total_row_count']=3
                with self.assertRaises(ValueError):self.tk.query(self.rid,self.payload())
        self.assertFalse(self.store.validation_case(self.rid,self.case['id'])['attempts'])
    def test_wrong_values_fail_and_failed_statement_never_compares(self):
        self.result['result']['data_array'][0][1]='99'
        self.assertEqual(self.tk.query(self.rid,self.payload())['comparison_status'],'failed')
        self.result={'status':{'state':'FAILED','error':{'message':'sensitive-provider-text'}}}
        op=self.tk.query(self.rid,self.payload());self.assertNotIn('attempt_id',op);self.assertNotIn('sensitive-provider-text',json.dumps(op))
    def test_query_guard(self):
        for sql in ['DROP TABLE x','SELECT 1; SELECT 2','WITH x AS (SELECT 1) INSERT INTO y SELECT * FROM x','SELECT * INTO x FROM y']:
            with self.assertRaises(ValueError):read_query(sql)
        self.assertEqual(read_query("SELECT ';DROP' AS text;"),"SELECT ';DROP' AS text;")
    def test_mapping_persists_and_unknown_reference_rejected(self):
        refs=self.tk.dependencies(self.rid)['references'];self.assertEqual(refs[0]['name'],'dbo.Sales')
        m={**refs[0],'target':'main.sales.orders','notes':'reviewed'}
        self.tk.dependencies(self.rid,{'mappings':[m]});self.assertEqual(self.tk.dependencies(self.rid)['mappings'][0]['target'],'main.sales.orders')
        with self.assertRaises(ValueError):self.tk.dependencies(self.rid,{'mappings':[{'source':'bad','name':'x','kind':'connection'}]})
    def test_local_checks_are_scoped_and_pass(self):
        result=self.tk.checks();self.assertTrue(result['passed']);self.assertEqual(len(result['checks']),8);self.assertIn('Local inventory',result['scope'])

    def test_suite_has_ssis_procedure_and_blocker(self):
        suite=self.tk.suite();run=wait(self.store,self.store.create({'files':suite['files']})['id']);self.assertEqual(len(run['files']),6)
        from ssis import inventory
        script=next(f for f in suite['files'] if f['name']=='UnsupportedScript.dtsx')
        self.assertIn('ssis-script',[f['code'] for f in inventory(script)['findings']])
    def test_live_job_status_and_workspace_binding(self):
        op=self.tk.attach(self.rid,{'revision':1,'remote_id':'12345'});self.assertEqual(op['state']['result_state'],'SUCCESS');self.assertEqual(op['tasks'][0]['task_key'],'load')
        self.settings.save({'workspace_url':'https://dbc-other.cloud.databricks.com','auth_mode':'session_token','warehouse_id':'abc123','catalog':'','schema':'','cli_profile':'','revision':1,'token':'fixture-token-value'})
        with self.assertRaises(ValueError):self.tk.refresh(self.rid,{'id':op['id'],'revision':2})
    def test_review_package_hashes_and_no_credentials(self):
        with self.store.db() as db:db.execute('UPDATE runs SET conversion_json=? WHERE id=?',(json.dumps({'files':[{'name':'converted/a.sql','sql':'SELECT 1;'},{'name':'notebooks/a.py','sql':'# Databricks notebook source\nprint(1)'}]}),self.rid))
        package=self.tk.package(self.rid);self.assertIn('databricks.yml',[f['name'] for f in package['files']]);self.assertNotIn('fixture-token-value',json.dumps(package));manifest=json.loads(next(f['sql'] for f in package['files'] if f['name']=='migration-manifest.json'));self.assertEqual(len(manifest['artifacts'][0]['sha256']),64);self.assertFalse(manifest['deployable']);resources=json.loads(next(f['sql'] for f in package['files'] if f['name']=='resources/notebooks.json'));job=next(iter(resources['resources']['jobs'].values()));self.assertEqual(job['tasks'][0]['notebook_task']['base_parameters']['migration_reviewed'],'false');self.assertNotIn('schedule',job)

if __name__=='__main__':unittest.main()
