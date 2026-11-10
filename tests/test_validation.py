import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from validation import definition,compare,dataset
from monitoring import RunService,Conflict

COLUMNS=[{'name':'id','type':'integer','nullable':False},{'name':'amount','type':'decimal'},{'name':'note','type':'string'}]

def case(**kwargs):return definition({'title':'Test','source':'a.sql','columns':COLUMNS,**kwargs})
def data(content,format='json'):return dataset({'format':format,'content':content if isinstance(content,str) else json.dumps(content)})
def evaluate(expected,actual,**kwargs):return compare(case(**kwargs),data(expected),data(actual))

def wait(service,run_id):
    for _ in range(500):
        result=service.get(run_id)
        if result['status'] not in ('uploaded','assessing'):return result
        time.sleep(.01)
    raise AssertionError('Assessment did not finish')

class ComparisonTests(unittest.TestCase):
    def test_cross_format_unordered_duplicates_nulls_and_exact_decimals(self):
        expected=data('id,amount,note\n1,10.00,paid\n2,20.50,\\N\n2,20.50,\\N\n','csv')
        actual=data([{'id':2,'amount':'20.50','note':None},{'id':1,'amount':'10.0','note':'paid'},{'id':2,'amount':'20.50','note':None}])
        result=compare(case(),expected,actual)
        self.assertEqual(result['status'],'passed');self.assertEqual(result['duplicate_rows']['actual'],1)
        self.assertFalse(result['executed_by_app'])

    def test_same_count_different_duplicate_distribution_fails(self):
        result=evaluate([{'id':1,'amount':1,'note':'a'}]*2+[{'id':2,'amount':1,'note':'b'}],
                        [{'id':1,'amount':1,'note':'a'}]+[{'id':2,'amount':1,'note':'b'}]*2)
        self.assertEqual(result['status'],'failed');self.assertEqual(result['mismatch_count'],2)

    def test_large_decimal_precision_is_not_rounded_to_float(self):
        result=evaluate('[{"id":1,"amount":9007199254740993.1234,"note":"x"}]','[{"id":1,"amount":9007199254740993.1235,"note":"x"}]')
        self.assertEqual(result['status'],'failed')

    def test_empty_string_is_distinct_from_null(self):
        result=compare(case(),data('id,amount,note\n1,1,\n','csv'),data([{'id':1,'amount':1,'note':None}]))
        self.assertEqual(result['status'],'failed')
        self.assertFalse(next(c for c in result['checks'] if c['name']=='null_counts')['passed'])

    def test_ordered_comparison_detects_order(self):
        rows=[{'id':1,'amount':1,'note':'a'},{'id':2,'amount':2,'note':'b'}]
        self.assertEqual(evaluate(rows,list(reversed(rows)),mode='ordered')['status'],'failed')
        self.assertEqual(evaluate(rows,list(reversed(rows)))['status'],'passed')

    def test_keyed_tolerance_and_unique_nonnull_keys(self):
        columns=[{'name':'id','type':'integer'},{'name':'amount','type':'decimal','tolerance':'0.01'}]
        expected=[{'id':1,'amount':'10.000'},{'id':2,'amount':'20.000'}]
        actual=[{'id':2,'amount':'20.009'},{'id':1,'amount':'10.001'}]
        self.assertEqual(evaluate(expected,actual,columns=columns,mode='keyed',key_columns=['id'])['status'],'passed')
        self.assertEqual(evaluate(expected,[{'id':1,'amount':10},{'id':1,'amount':10}],columns=columns,mode='keyed',key_columns=['id'])['status'],'failed')
        self.assertEqual(evaluate([{'id':None,'amount':10}],[{'id':None,'amount':10}],columns=columns,mode='keyed',key_columns=['id'])['status'],'failed')

    def test_tolerance_is_not_supported_for_unordered_or_key_columns(self):
        for options in [dict(columns=[{'name':'a','type':'decimal','tolerance':'0.1'}]),dict(columns=[{'name':'id','type':'decimal','tolerance':'0.1'}],mode='keyed',key_columns=['id'])]:
            with self.assertRaises(ValueError):case(**options)

    def test_timezone_equivalence_naive_and_submicrosecond_rejection(self):
        columns=[{'name':'time','type':'timestamp'}]
        self.assertEqual(evaluate([{'time':'2026-11-10T10:00:00-06:00'}],[{'time':'2026-11-10T16:00:00Z'}],columns=columns)['status'],'passed')
        for value in ['2026-11-10T16:00:00','2026-11-10T16:00:00.1234567Z']:
            self.assertEqual(evaluate([{'time':value}],[{'time':value}],columns=columns)['status'],'failed')

    def test_schema_empty_results_and_duplicate_json_fields(self):
        self.assertEqual(evaluate([{'wrong':1}],[{'wrong':1}])['status'],'failed')
        empty={'columns':['id','amount','note'],'rows':[]}
        self.assertEqual(evaluate(empty,empty)['status'],'passed')
        self.assertEqual(evaluate([],[])['status'],'error')
        self.assertEqual(evaluate('[{"id":1,"id":2,"amount":1,"note":"x"}]',[])['status'],'error')

    def test_invalid_types_boolean_numeric_and_nonfinite_numbers(self):
        rows=[{'id':True,'amount':1,'note':'x'}]
        self.assertEqual(evaluate(rows,rows)['status'],'failed')
        self.assertEqual(evaluate('[{"id":1,"amount":NaN,"note":"x"}]',[])['status'],'error')
        rows=[{'id':1,'amount':'Infinity','note':'x'}]
        self.assertEqual(evaluate(rows,rows)['status'],'failed')
        self.assertEqual(evaluate([{'id':1,'amount':1,'note':3}],[{'id':1,'amount':1,'note':3}])['status'],'failed')

    def test_malformed_csv_and_bounded_mismatch_samples(self):
        self.assertEqual(compare(case(),data('id,amount,note\n1,1\n','csv'),data([]))['status'],'error')
        expected=[{'id':i,'amount':1,'note':'x'} for i in range(50)]
        actual=[{'id':i+100,'amount':1,'note':'x'} for i in range(50)]
        result=evaluate(expected,actual)
        self.assertEqual(result['mismatch_count'],100);self.assertEqual(len(result['mismatches']),20)
        with self.assertRaises(ValueError):dataset({'format':'json','content':'x'*(2*1024*1024+1)})

    def test_large_integer_difference_survives_decimal_context(self):
        value='9'*100
        columns=[{'name':'n','type':'decimal','tolerance':'0'}]
        self.assertEqual(evaluate([{'n':value}],[{'n':str(int(value)-1)}],columns=columns,mode='ordered')['status'],'failed')

class ValidationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'runs.db';self.service=RunService(self.path)
        run=self.service.create({'files':[{'name':'a.sql','sql':'SELECT 1;'}]});self.run=wait(self.service,run['id']);self.run_id=run['id']
    def tearDown(self):self.service.close();self.temp.cleanup()
    def saved(self):return self.service.save_case(self.run_id,case())
    def request(self,version=1):return {'version':version,'expected':data([{'id':1,'amount':1,'note':None}]),'actual':data([{'id':1,'amount':'1.0','note':None}]),'expected_ref':'Synthetic SQL Server export','actual_ref':'Synthetic Databricks export','evidence':'Fixture-only unit test; no SQL execution.'}

    def test_case_attempt_and_evidence_survive_restart_without_promoting_validation(self):
        saved=self.saved();report=self.service.evaluate_case(self.run_id,saved['id'],self.request())
        self.assertEqual(report['status'],'passed');self.assertFalse(report['validated_on_databricks']);self.assertFalse(report['provenance']['verified_by_app'])
        self.assertEqual(report['inputs']['expected']['sha256'],hashlib.sha256(self.request()['expected']['content'].encode()).hexdigest())
        self.service.close();self.service=RunService(self.path)
        restored=self.service.validation_attempt(self.run_id,report['id'],True)
        self.assertEqual(restored['recorded_inputs']['expected'],self.request()['expected'])
        source=self.service.get(self.run_id)['sources'][0]
        self.assertEqual(source['validation'],'not_run');self.assertEqual(source['review'],'pending')
        self.assertEqual(self.service.validation_cases(self.run_id)['summary']['passed'],1)

    def test_editing_case_preserves_immutable_report_and_marks_latest_stale(self):
        saved=self.saved();report=self.service.evaluate_case(self.run_id,saved['id'],self.request())
        self.service.save_case(self.run_id,{**case(),'title':'Changed definition','version':1},saved['id'])
        historical=self.service.validation_attempt(self.run_id,report['id'])
        self.assertTrue(historical['stale']);self.assertEqual(historical['report']['definition']['title'],'Test')
        self.assertEqual(self.service.validation_cases(self.run_id)['summary']['not_compared'],1)
        with self.assertRaises(Conflict):self.service.evaluate_case(self.run_id,saved['id'],self.request())
        with self.assertRaises(Conflict):self.service.save_case(self.run_id,{**case(),'version':1},saved['id'])

    def test_provenance_required_and_run_scope_enforced(self):
        saved=self.saved()
        with self.assertRaises(ValueError):self.service.evaluate_case(self.run_id,saved['id'],{**self.request(),'evidence':''})
        other=wait(self.service,self.service.create({'files':[{'name':'a.sql','sql':'SELECT 2;'}]})['id'])
        with self.assertRaises(KeyError):self.service.validation_case(other['id'],saved['id'])
        with self.assertRaises(KeyError):self.service.evaluate_case(other['id'],saved['id'],self.request())

    def test_invalid_input_is_saved_as_error_not_pass(self):
        saved=self.saved();report=self.service.evaluate_case(self.run_id,saved['id'],{**self.request(),'actual':data('{bad json')})
        self.assertEqual(report['status'],'error');self.assertEqual(self.service.validation_cases(self.run_id)['summary']['error'],1)

    def test_deleting_run_cascades_validation_records(self):
        saved=self.saved();self.service.evaluate_case(self.run_id,saved['id'],self.request());self.service.delete(self.run_id)
        with self.service.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM validation_cases').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM validation_attempts').fetchone()[0],0)


class ValidationAPITests(unittest.TestCase):
    def test_case_and_comparison_endpoints(self):
        from functools import partial
        import threading
        from urllib.request import Request,urlopen
        from urllib.error import HTTPError
        from server import AppServer,Handler,ROOT
        with tempfile.TemporaryDirectory() as directory:
            service=RunService(Path(directory)/'runs.db')
            server=AppServer(('127.0.0.1',0),partial(Handler,directory=str(ROOT/'static')),run_service=service)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base=f'http://127.0.0.1:{server.server_port}'
            def post(path,payload):return urlopen(Request(base+path,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Accelerator-Request':'1'}))
            try:
                run=wait(service,service.create({'files':[{'name':'a.sql','sql':'SELECT 1;'}]})['id'])
                prefix='/api/runs/'+run['id']+'/validation'
                with post(prefix+'/cases',case()) as response:self.assertEqual(response.status,201);saved=json.load(response)
                request={'version':1,'expected':data([{'id':1,'amount':1,'note':'x'}]),'actual':data([{'id':1,'amount':1,'note':'x'}]),'expected_ref':'Synthetic source','actual_ref':'Synthetic target','evidence':'Fixture only; no execution.'}
                with post(prefix+'/cases/'+saved['id']+'/compare',request) as response:report=json.load(response)
                self.assertEqual(report['status'],'passed');self.assertIn('source_sha256',report['source_snapshot'])
                with urlopen(base+prefix) as response:self.assertEqual(json.load(response)['summary']['passed'],1)
                with urlopen(base+prefix+'/cases/'+saved['id']) as response:self.assertEqual(len(json.load(response)['attempts']),1)
                with urlopen(base+prefix+'/attempts/'+report['id']+'?inputs=1') as response:self.assertIn('recorded_inputs',json.load(response))
                with self.assertRaises(HTTPError) as failure:post(prefix+'/cases/'+saved['id'],{**case(),'version':0})
                self.assertEqual(failure.exception.code,409)
            finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
