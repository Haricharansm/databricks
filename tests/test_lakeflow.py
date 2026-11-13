import ast
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lakeflow import LakeflowService,package_graph,pipeline_code,static_sql,key
from monitoring import RunService,Conflict
from ssis import inventory,package_key
from bladebridge import GUARD
from test_validation import wait

ROOT=Path(__file__).resolve().parents[1]
SQL_XML=(ROOT/'samples/ssis/ReadOnlySalesSmokeTest/ReadOnlySalesSmokeTest.dtsx').read_text()
COPY_XML=(ROOT/'samples/lakeflow/DirectCopy.dtsx').read_text()

class LakeflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=RunService(Path(self.tmp.name)/'db');self.service=LakeflowService(self.store)
        self.files=[{'name':'Sales.dtsx','sql':SQL_XML},{'name':'Copy.dtsx','sql':COPY_XML}]
        self.rid=wait(self.store,self.store.create({'files':self.files})['id'])['id']
        cells=[]
        for i,t in enumerate(inventory(self.files[0])['tasks']):cells.append('# Processing node '+t['id']+', type EXECUTE_SQL\nquery = '+repr('SELECT '+str(i)+' AS value')+'\nspark.sql(query)\n')
        self.notebook=GUARD+('\n# COMMAND ----------\n'.join(cells))
        with self.store.db() as db:db.execute('UPDATE runs SET conversion_json=? WHERE id=?',(json.dumps({'files':[{'name':'notebooks/'+package_key('Sales.dtsx')+'/Sales.py','kind':'notebook','sql':self.notebook}]}),self.rid))
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def save(self,edit=None):
        current=self.service.get(self.rid);p=current['profile']
        for m in p['task_mappings']:
            if m['source']=='Copy.dtsx':m.update(kind='pipeline',pipeline={'source_kind':'table','source':'main.source.customers','target_table':'customers','mode':'batch'})
        if edit:edit(p)
        return self.service.save(self.rid,{'version':current['version'],'fingerprint':current['fingerprint'],'profile':p})
    def generate(self,edit=None):self.save(edit);return self.service.generate(self.rid)
    def test_jobs_and_pipelines_preserve_exact_success_dependencies(self):
        result=self.generate();r=result['report'];self.assertEqual(r['jobs'],2);self.assertEqual(r['pipelines'],1);self.assertFalse(r['deployable']);self.assertFalse(r['executed_by_app'])
        resource=json.loads(next(f['sql'] for f in result['files'] if f['name']=='resources/lakeflow.json'))['resources']
        sqljob=resource['jobs'][key('Sales.dtsx')];tasks=inventory(self.files[0])['tasks']
        self.assertEqual([t['task_key'] for t in sqljob['tasks']],[key(t['id']) for t in tasks])
        self.assertNotIn('depends_on',sqljob['tasks'][0]);self.assertEqual(sqljob['tasks'][1]['depends_on'],[{'task_key':key(tasks[0]['id'])}]);self.assertEqual(sqljob['tasks'][2]['depends_on'],[{'task_key':key(tasks[1]['id'])}])
        self.assertTrue(all(t['notebook_task']['base_parameters']['migration_reviewed']=='false' for t in sqljob['tasks']))
        pipeline=next(iter(resource['pipelines'].values()));self.assertEqual(pipeline['configuration']['crosslake.migration_reviewed'],'false');self.assertFalse(pipeline['continuous'])
        self.assertIn('pipeline_task',resource['jobs'][key('Copy.dtsx')]['tasks'][0])
        for f in result['files']:
            if f['name'].endswith('.py'):ast.parse(f['sql'])
            if f['name'].startswith('notebooks/'):
                self.assertIn('USE CATALOG `main`',f['sql']);self.assertIn('USE SCHEMA `migration`',f['sql']);self.assertLess(f['sql'].index('raise RuntimeError'),f['sql'].index('USE CATALOG'))
    def test_schedule_always_paused_and_retries_default_zero(self):
        result=self.generate(lambda p:p.update(schedule={'quartz_cron_expression':'0 0 2 * * ?','timezone_id':'America/Chicago','pause_status':'UNPAUSED'},max_retries=2))
        resources=json.loads(next(f['sql'] for f in result['files'] if f['name']=='resources/lakeflow.json'))['resources']
        self.assertTrue(all(j['schedule']['pause_status']=='PAUSED' for j in resources['jobs'].values()));self.assertTrue(all(t['max_retries']==2 for j in resources['jobs'].values() for t in j['tasks']))
    def test_profiles_persist_and_optimistic_versions_reject_lost_updates(self):
        original=self.service.get(self.rid);self.save();self.assertEqual(self.service.get(self.rid)['version'],1)
        with self.assertRaises(Conflict):self.service.save(self.rid,{'version':0,'fingerprint':original['fingerprint'],'profile':original['profile']})
        self.assertEqual(LakeflowService(self.store).get(self.rid)['version'],1)
    def test_stale_conversion_invalidates_generation(self):
        self.save()
        with self.store.db() as db:db.execute('UPDATE runs SET conversion_json=? WHERE id=?',(json.dumps({'files':[]}),self.rid))
        self.assertTrue(self.service.get(self.rid)['stale'])
        with self.assertRaises(Conflict):self.service.generate(self.rid)
    def test_failure_edges_or_cycles_never_emit_a_partial_package_job(self):
        for xml in [SQL_XML.replace('DTS:Value="0"','DTS:Value="1"',1),SQL_XML.replace('</DTS:PrecedenceConstraints>','<DTS:PrecedenceConstraint DTS:From="Package\\Verify_Active_Totals" DTS:To="Package\\Preview_Source_Rows"/></DTS:PrecedenceConstraints>')]:
            with self.assertRaises(ValueError):package_graph({'name':'x.dtsx','sql':xml})
    def test_missing_sql_and_unsupported_component_block_complete_job(self):
        with self.store.db() as db:db.execute('UPDATE runs SET conversion_json=? WHERE id=?',('{"files":[]}',self.rid))
        result=self.generate();self.assertEqual(result['report']['jobs'],1);self.assertEqual(result['report']['status'],'partial');sales=next(p for p in result['report']['packages'] if p['source']=='Sales.dtsx');self.assertNotIn('job_resource',sales);self.assertEqual(len(sales['blockers']),3)
    def test_static_sql_rejects_dynamic_session_state_and_unknown_calls(self):
        for cell in ['sql=f"SELECT {value}"\nspark.sql(sql)','sql="CREATE TEMP VIEW x AS SELECT 1"\nspark.sql(sql)','sql="SELECT 1"\nprint(sql)','raise RuntimeError("no")','sql="SELECT @param"\nspark.sql(sql)']:
            with self.subTest(cell=cell):
                with self.assertRaises(ValueError):static_sql(cell)
        self.assertEqual(static_sql('sql=f"SELECT 1"\nspark.sql(sql)'),['SELECT 1'])
        self.assertEqual(static_sql('sql="UPDATE main.raw.t SET amount=1"\nspark.sql(sql)'),['UPDATE main.raw.t SET amount=1'])
    def test_streaming_file_pipeline_has_autoloader_schema_and_review_gate(self):
        code,notes=pipeline_code({'source_kind':'files','source':'/Volumes/main/raw/files/customers','format':'csv','schema_ddl':'id BIGINT, name STRING','target_table':'customers','mode':'streaming'})
        self.assertIn('@dp.table',code);self.assertIn('cloudFiles',code);self.assertIn('.schema(',code);self.assertIn('migration_reviewed',code);ast.parse(code)
        batch,_=pipeline_code({'source_kind':'table','source':'main.raw.customers','target_table':'customers','mode':'batch'});self.assertIn('@dp.materialized_view',batch);self.assertNotIn('readStream',batch)
    def test_invalid_pipeline_sources_and_injected_names_are_refused(self):
        for config in [dict(source_kind='files',source='D:\\data\\file.csv'),dict(source_kind='files',source='/Volumes/main/raw/files/../bad',format='csv',schema_ddl='id INT'),dict(source_kind='table',source='dbo.Customers'),dict(source_kind='table',source='main.raw.customers',target_table='x);evil()')]:
            with self.subTest(config=config):
                with self.assertRaises(ValueError):pipeline_code({'target_table':'customers','mode':'batch',**config})
    def test_unknown_task_mapping_and_invalid_retries_rejected(self):
        for edit in [lambda p:p.update(max_retries=-1),lambda p:p['task_mappings'].append({'source':'other','task_id':'fake','kind':'auto_sql'})]:
            with self.assertRaises(ValueError):self.save(edit)
    def test_modified_task_lineage_is_not_silently_accepted(self):
        run=self.store.get(self.rid);run['files'][1]['sql']=COPY_XML.replace('Source.Outputs','Other.Outputs')
        with self.store.db() as db:db.execute('UPDATE runs SET source_json=? WHERE id=?',(json.dumps(run['files']),self.rid))
        result=self.generate();copy=next(p for p in result['report']['packages'] if p['source']=='Copy.dtsx');self.assertNotIn('job_resource',copy);self.assertIn('lineage',copy['blockers'][0])
    def test_delete_cascades_saved_profile(self):
        self.save();self.store.delete(self.rid)
        with self.store.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM lakeflow_profiles').fetchone()[0],0)


class LakeflowHTTPTests(unittest.TestCase):
    def setUp(self):
        from functools import partial
        import threading
        from server import AppServer,Handler
        self.fixture=LakeflowTests();self.fixture.setUp()
        self.server=AppServer(('127.0.0.1',0),partial(Handler,directory=str(ROOT/'static')),self.fixture.store)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_address[1]);self.path='/api/runs/'+self.fixture.rid+'/lakeflow'
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.fixture.tearDown()
    def request(self,path,payload=None,header=True):
        from urllib.request import Request,urlopen
        headers={'Content-Type':'application/json'}
        if header:headers['X-Accelerator-Request']='1'
        request=Request(self.url+path,data=json.dumps(payload).encode() if payload is not None else None,headers=headers)
        with urlopen(request,timeout=10) as response:return response.read()
    def test_routes_save_generate_and_download_bundle(self):
        import io,zipfile
        profile=json.loads(self.request(self.path));saved=json.loads(self.request(self.path,{'version':0,'fingerprint':profile['fingerprint'],'profile':profile['profile']}));self.assertEqual(saved['version'],1)
        bundle=json.loads(self.request(self.path+'/bundle'));self.assertEqual(bundle['report']['jobs'],1);self.assertEqual(bundle['report']['status'],'partial')
        archive=zipfile.ZipFile(io.BytesIO(self.request('/api/export',bundle)));self.assertIn('lakeflow-plan.json',archive.namelist());self.assertIn('resources/lakeflow.json',archive.namelist());self.assertIn('databricks.yml',archive.namelist())
    def test_profile_endpoint_rejects_stale_version_and_cross_origin_style_request(self):
        from urllib.error import HTTPError
        p=json.loads(self.request(self.path));payload={'version':0,'fingerprint':p['fingerprint'],'profile':p['profile']};self.request(self.path,payload)
        with self.assertRaises(HTTPError) as exc:self.request(self.path,payload)
        self.assertEqual(exc.exception.code,409)
        with self.assertRaises(HTTPError) as exc:self.request(self.path,payload,False)
        self.assertEqual(exc.exception.code,415)

if __name__=='__main__':unittest.main()
