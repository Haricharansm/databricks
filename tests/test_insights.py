import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from insights import InsightsService,query_hash,sql_details,scope_plan,workload_input,numeric
from monitoring import RunService,Conflict
from test_validation import wait

ROOT=Path(__file__).resolve().parents[1]
FILES=[{'name':'tables.sql','sql':'CREATE TABLE dbo.Customers (id INT, name VARCHAR(50)); CREATE VIEW dbo.ActiveCustomers AS SELECT * FROM dbo.Customers;'},
 {'name':'load.sql','sql':'INSERT INTO dbo.Customers (id,name) SELECT id,name FROM dbo.Stage; SELECT * FROM dbo.ActiveCustomers;'},
 {'name':'proc.sql','sql':'CREATE PROCEDURE dbo.ReadCustomers AS BEGIN IF 1=1 SELECT * FROM dbo.Customers; DECLARE c CURSOR FOR SELECT id FROM dbo.Customers; WHILE 1=0 BEGIN SELECT 1; END; END;'},
 {'name':'caller.sql','sql':'EXEC dbo.ReadCustomers;'},
 {'name':'extra.sql','sql':"SELECT 'FROM dbo.Secret WHERE CURSOR' AS text; -- FROM dbo.Hidden\n"}]

def record(text='SELECT * FROM dbo.ActiveCustomers',**changes):return {'query_text':text,'execution_count':10,'interval_start':'2026-11-13T10:00:00-06:00','interval_end':'2026-11-13T11:00:00-06:00','total_duration_ms':'100.5','total_cpu_ms':'40.25','logical_reads':80,'application':'ETL','user':'fixture',**changes}
def payload(rows,version=0):return {'version':version,'evidence':'Synthetic Query Store export; one observed interval, not full usage proof.','data':{'format':'json','content':json.dumps(rows)}}

class InsightsTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=RunService(Path(self.tmp.name)/'db');self.rid=wait(self.store,self.store.create({'label':'Assessment fixture','files':FILES})['id'])['id'];self.service=InsightsService(self.store)
 def tearDown(self):self.store.close();self.tmp.cleanup()
 def test_highlights_have_measured_static_counts_and_unknown_usage(self):
  r=self.service.get(self.rid);self.assertEqual(r['summary']['files'],5);self.assertEqual(r['summary']['declared_tables'],1);self.assertEqual(r['summary']['declared_views'],1);self.assertEqual(r['summary']['declared_procedures'],1);self.assertFalse(r['workload']['available']);self.assertTrue(all(q['workload_status']=='unknown' for q in r['queries']));self.assertEqual(r['summary']['metrics']['cursors'],1);self.assertEqual(r['summary']['metrics']['loops'],1)
 def test_comments_literals_and_ctes_do_not_create_external_objects(self):
  detail=sql_details("WITH x AS (SELECT * FROM dbo.Customers WHERE name='FROM dbo.Fake') SELECT * FROM x JOIN dbo.Orders o ON 1=1 -- FROM dbo.Hidden")
  self.assertEqual({r['name'] for r in detail['references']},{'dbo.Customers','x','dbo.Orders'});self.assertTrue(next(r for r in detail['references'] if r['name']=='x')['local_cte'])
 def test_quoted_identifiers_alias_update_and_comma_sources(self):
  d=sql_details('UPDATE c SET name=\'x\' FROM [dbo].[Customer]]Name] AS c JOIN dbo.Other o ON 1=1; SELECT * FROM dbo.A a, dbo.B b;')
  target=next(r for r in d['references'] if r['usage']=='UPDATE');self.assertEqual(target['parts'],('dbo','customer]name'));self.assertIn('dbo.B',{r['name'] for r in d['references']})
 def test_insert_top_does_not_invent_table_definition(self):
  d=sql_details('INSERT TOP(10) INTO dbo.T SELECT * FROM dbo.S;');self.assertFalse(d['definitions']);self.assertIn(('dbo.T','INSERT'),{(r['name'],r['usage']) for r in d['references']})
 def test_reference_only_is_not_missing_and_fake_names_ignored(self):
  r=self.service.get(self.rid);entities={e['name']:e for e in r['entities']};self.assertEqual(entities['dbo.Stage']['resolution'],'reference_only');self.assertNotIn('dbo.Secret',entities);self.assertNotIn('dbo.Hidden',entities);self.assertTrue(any(e['kind']=='reads' for e in r['lineage']['edges']));self.assertFalse(any('missing' in e['resolution'] for e in entities.values()))
 def test_query_hash_preserves_literals_and_token_boundaries(self):
  self.assertEqual(query_hash('SELECT * FROM dbo.T; -- hello'),query_hash(' select  *\nfrom dbo.t'))
  self.assertNotEqual(query_hash("SELECT 'A'"),query_hash("SELECT 'a'"));self.assertNotEqual(query_hash('SELECT 1 + 23'),query_hash('SELECT 12 + 3'))
 def test_scope_reachability_includes_procedure_and_table_providers(self):
  r=self.service.save(self.rid,{'version':0,'entrypoints':['caller.sql']});states={f['source']:f['status'] for f in r['scope']['files']};self.assertEqual(states['proc.sql'],'referenced_by_entrypoint');self.assertEqual(states['tables.sql'],'referenced_by_entrypoint');self.assertEqual(states['extra.sql'],'not_reached_in_uploaded_graph');self.assertGreaterEqual(len(r['scope']['waves']),3)
 def test_scope_cycles_are_explicit_planning_groups(self):
  p=scope_plan(['a','b','c'],[{'consumer':'a','provider':'b'},{'consumer':'b','provider':'a'}],['a']);self.assertTrue(p['waves'][0]['groups'][0]['cycle']);self.assertEqual(len(p['waves'][0]['groups'][0]['files']),2)
 def test_import_cpu_io_metrics_and_observed_text_without_unused_claim(self):
  r=self.service.save(self.rid,payload([record()]),True);self.assertEqual(r['workload']['totals']['total_cpu_ms'],'40.25');self.assertEqual(r['workload']['totals']['logical_reads'],'80');self.assertEqual(r['workload']['matched_uploaded_unique_texts'],1);self.assertTrue(any(q['workload_status']=='observed_text' for q in r['queries']));self.assertTrue(any(q['workload_status']=='not_observed_in_import' for q in r['queries']));self.assertFalse(r['workload']['verified_by_app']);self.assertEqual(len(r['workload']['timeline']),1)
 def test_duplicate_sql_does_not_double_count_or_claim_file_attribution(self):
  files=[{'name':'a.sql','sql':'SELECT 1'},{'name':'b.sql','sql':'SELECT 1'}];rid=wait(self.store,self.store.create({'files':files})['id'])['id'];r=self.service.save(rid,payload([record('SELECT 1')]),True)
  self.assertEqual(r['workload']['totals']['execution_count'],'10');self.assertEqual(r['workload']['rankings']['file'][0]['label'],'(ambiguous duplicate SQL across sources)');self.assertTrue(all(q['workload_status']=='observed_text_ambiguous_source' for q in r['queries']));self.assertTrue(all(f['unambiguous_observed_texts']==0 for f in r['files']))
 def test_workload_replacement_preserves_scope_and_clear_preserves_static_report(self):
  self.service.save(self.rid,{'version':0,'entrypoints':['load.sql']});r=self.service.save(self.rid,payload([record()],1),True);r=self.service.save(self.rid,payload([record(execution_count=2)],r['version']),True);self.assertEqual(r['workload']['totals']['execution_count'],'2');self.assertEqual(r['scope']['entrypoints'],['load.sql']);r=self.service.clear_workload(self.rid,{'version':r['version']});self.assertFalse(r['workload']['available']);self.assertEqual(r['summary']['files'],5)
 def test_profiles_persist_and_versions_reject_stale_imports(self):
  self.service.save(self.rid,payload([record()]),True);self.assertTrue(InsightsService(self.store).get(self.rid)['workload']['available'])
  with self.assertRaises(Conflict):self.service.save(self.rid,payload([record()]),True)
 def test_invalid_workload_values_and_timezone_are_rejected(self):
  for changes in [{'execution_count':-1},{'execution_count':True},{'total_cpu_ms':'NaN'},{'total_cpu_ms':'1e-10000'},{'logical_reads':'1.5'},{'interval_start':'2026-11-13T10:00:00'},{'interval_end':'2020-01-01T00:00:00Z'},{'execution_count':0}]:
   with self.subTest(changes=changes):
    with self.assertRaises(ValueError):workload_input(payload([record(**changes)]))
 def test_missing_cpu_and_reads_remain_unknown(self):
  r=self.service.save(self.rid,payload([record(total_cpu_ms=None,logical_reads=None)]),True);self.assertIsNone(r['workload']['totals']['total_cpu_ms']);self.assertIsNone(r['workload']['rankings']['file'][0]['logical_reads'])
 def test_html_exports_escape_source_objects_and_include_method(self):
  rid=wait(self.store,self.store.create({'files':[{'name':'x.sql','sql':'CREATE TABLE [<script>alert(1)</script>] (id INT);'}]})['id'])['id'];bundle=self.service.export(rid);html=next(f['sql'] for f in bundle['files'] if f['name']=='assessment.html');self.assertNotIn('<script>',html);self.assertIn('&lt;script&gt;',html);self.assertIn('not grammar-parser certification',html);self.assertIn('queries.csv',[f['name'] for f in bundle['files']])
 def test_go_batches_and_procedure_bodies_remain_deterministic(self):
  rid=wait(self.store,self.store.create({'files':[{'name':'b.sql','sql':'SELECT 1\nGO\nSELECT 2\nGO\n'}]})['id'])['id'];r=self.service.get(rid);self.assertEqual(r['summary']['statement_units'],2);self.assertEqual([q['line'] for q in r['queries']],[1,3])
 def test_temporary_tables_are_scoped_to_source_file(self):
  rid=wait(self.store,self.store.create({'files':[{'name':'a.sql','sql':'CREATE TABLE #x (id INT); SELECT * FROM #x;'},{'name':'b.sql','sql':'SELECT * FROM #x;'}]})['id'])['id'];r=self.service.get(rid);temps=[e for e in r['entities'] if e['temporary']];self.assertEqual(len(temps),2);self.assertFalse(r['scope']['dependencies'])
 def test_ssis_control_and_data_paths_are_reported_without_passwords(self):
  xml=(ROOT/'samples/lakeflow/DirectCopy.dtsx').read_text().replace('<DTS:Executables>','<DTS:ConnectionManager DTS:ObjectName="DB" DTS:DTSID="conn" DTS:CreationName="OLEDB" DTS:ConnectionString="Password=fixture-secret"/><DTS:Executables>',1)
  rid=wait(self.store,self.store.create({'files':[{'name':'copy.dtsx','sql':xml}]})['id'])['id'];r=self.service.get(rid);self.assertEqual(r['summary']['ssis_tasks'],1);self.assertTrue(any(e['kind']=='flows' for e in r['lineage']['edges']));self.assertNotIn('fixture-secret',json.dumps(r))
 def test_flat_file_paths_are_metadata_endpoints_not_connection_credentials(self):
  xml=(ROOT/'samples/lakeflow/DirectCopy.dtsx').read_text().replace('<DTS:Executables>','<DTS:ConnectionManager DTS:ObjectName="InputFile" DTS:DTSID="flat" DTS:CreationName="FLATFILE"><DTS:ObjectData><DTS:ConnectionManager DTS:ConnectionString="D:\\fixture\\customers.csv"/></DTS:ObjectData></DTS:ConnectionManager><DTS:Executables>',1)
  rid=wait(self.store,self.store.create({'files':[{'name':'file.dtsx','sql':xml}]})['id'])['id'];r=self.service.get(rid);self.assertTrue(any(n['kind']=='file_endpoint' and 'customers.csv' in n['label'] for n in r['lineage']['nodes']))
 def test_unclosed_sql_is_reported_as_lexical_issue(self):
  d=sql_details("SELECT 'unclosed");self.assertTrue(d['issues'])
 def test_exec_return_binding_and_dynamic_sql_are_not_fake_entities(self):
  d=sql_details('EXEC @status = dbo.Child @id=1; EXEC(@sql);')
  self.assertEqual({r['name'] for r in d['references']},{'dbo.Child'});self.assertEqual(d['metrics']['dynamic_exec'],1)
 def test_cte_parentheses_in_literals_and_call_edge_direction(self):
  d=sql_details("WITH a AS (SELECT '(' AS marker FROM dbo.T) SELECT * FROM a")
  self.assertEqual(d['cte_count'],1);self.assertFalse(d['issues']);r=self.service.get(self.rid);query=next(q for q in r['queries'] if q['source']=='caller.sql');proc=next(e for e in r['entities'] if e['kind']=='procedure');self.assertTrue(any(e['kind']=='calls' and e['from']==query['id'] and e['to']==proc['id'] for e in r['lineage']['edges']))
 def test_deleting_run_cascades_evidence(self):
  self.service.save(self.rid,payload([record()]),True);self.store.delete(self.rid)
  with self.store.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM assessment_evidence').fetchone()[0],0)

class InsightsHTTPTests(unittest.TestCase):
 def setUp(self):
  import threading
  from functools import partial
  from server import AppServer,Handler
  self.fixture=InsightsTests();self.fixture.setUp();self.server=AppServer(('127.0.0.1',0),partial(Handler,directory=str(ROOT/'static')),self.fixture.store);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.url='http://127.0.0.1:'+str(self.server.server_address[1]);self.path='/api/runs/'+self.fixture.rid+'/insights'
 def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.fixture.tearDown()
 def request(self,path,p=None):
  from urllib.request import Request,urlopen
  with urlopen(Request(self.url+path,data=json.dumps(p).encode() if p is not None else None,headers={'Content-Type':'application/json','X-Accelerator-Request':'1'}),timeout=20) as r:return r.read()
 def test_get_save_and_zip_report_endpoints(self):
  r=json.loads(self.request(self.path));self.assertFalse(r['workload']['available']);r=json.loads(self.request(self.path+'/scope',{'version':0,'entrypoints':['caller.sql']}));self.assertEqual(r['version'],1);bundle=json.loads(self.request(self.path+'/export'));z=zipfile.ZipFile(io.BytesIO(self.request('/api/export',bundle)));self.assertIn('assessment.html',z.namelist());self.assertIn('entities.csv',z.namelist())
 def test_bad_workload_does_not_persist_and_stale_scope_is_conflict(self):
  from urllib.error import HTTPError
  with self.assertRaises(HTTPError) as e:self.request(self.path+'/workload',payload([record(total_cpu_ms='NaN')]))
  self.assertEqual(e.exception.code,400);self.request(self.path+'/scope',{'version':0,'entrypoints':[]})
  with self.assertRaises(HTTPError) as e:self.request(self.path+'/scope',{'version':0,'entrypoints':[]})
  self.assertEqual(e.exception.code,409)

if __name__=='__main__':unittest.main()
