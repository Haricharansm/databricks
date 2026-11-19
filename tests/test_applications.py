from copy import deepcopy
import json
from pathlib import Path
import unittest
from applications import ApplicationService,dag
from monitoring import Conflict
from ssis import package_key
from test_validation import wait
import test_lakeflow_results as fixtures

class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.BindingTests();self.f.setUp();self.f.bundle();self.runs=self.f.runs;self.lakeflow=self.f.service;self.a=ApplicationService(self.runs,self.lakeflow);self.rid=self.f.rid
        self._approve(self.rid)
        self.app=self.a.create({'name':'DailyFinanceClose'})
    def tearDown(self):self.f.tearDown()
    def _approve(self,rid):
        with self.runs.db() as db:db.execute("UPDATE sources SET status='converted',review='approved' WHERE run_id=?",(rid,))
        p=self.lakeflow.get(rid);p['profile'].update(environment_version='4',schedule=None);self.lakeflow.save(rid,{'version':p['version'],'fingerprint':p['fingerprint'],'profile':p['profile']})
    def member(self):
        rid=wait(self.runs,self.runs.create({'files':[{'name':'SecondPackage.dtsx','sql':fixtures.XML}]})['id'])['id'];conversion=deepcopy(self.runs.get(self.rid)['conversion'])
        for f in conversion['files']:f['name']=f['name'].replace(package_key('ProductPackage.dtsx'),package_key('SecondPackage.dtsx'))
        with self.runs.db() as db:db.execute('UPDATE runs SET conversion_json=? WHERE id=?',(json.dumps(conversion),rid))
        self._approve(rid);return rid
    def save(self,ids=None,edit=None):
        p=deepcopy(self.app['profile']);p['run_ids']=ids or [self.rid];p['labels']={i:'FinanceAsset'+str(n+1) for n,i in enumerate(p['run_ids'])};p['release_reviewed']=True;p['review_note']='Reviewed sandbox orchestration and data contract.'
        if edit:edit(p)
        self.app=self.a.save(self.app['id'],{'version':self.app['version'],'profile':p});return self.app
    def export(self):return self.a.export(self.app['id'],{'version':self.app['version'],'fingerprint':self.app['fingerprint'],'include_code':True})
    def test_application_master_dependencies_naming_and_no_legacy_client(self):
        second=self.member();self.save([self.rid,second],lambda p:p.update(dependencies=[{'from':self.rid,'to':second,'reviewed':True}],prefix='saxon',environment='sit'))
        out=self.export();self.assertEqual(out['report']['status'],'review_required');r=json.loads(next(f['sql'] for f in out['files'] if f['name']=='resources/application.json'))['resources'];parent=next(j for k,j in r['jobs'].items() if k.startswith('application_'));self.assertEqual(len(parent['tasks']),2);self.assertEqual(parent['tasks'][1]['depends_on'],[{'task_key':parent['tasks'][0]['task_key']}]);self.assertIn('saxon_DailyFinanceClose_master_sit',parent['name']);self.assertNotIn('"client"',json.dumps(r));self.assertTrue(all(j['environments'][0]['spec']['environment_version']=='4' for k,j in r['jobs'].items() if not k.startswith('application_')))
    def test_duplicate_candidates_are_reports_not_automatic_refactors(self):
        second=self.member();self.save([self.rid,second]);self.assertTrue(self.app['duplicates']);out=self.export();self.assertTrue(any(f['name'].startswith('refactoring/candidate_') for f in out['files']));self.assertTrue(all('review_required' in d for d in self.app['duplicates']))
    def test_review_and_unresolved_architecture_block_executable_release(self):
        self.save(edit=lambda p:p.update(release_reviewed=False,architecture=[{'run_id':self.rid,'source':'SourceTable','target':'','decision':'unresolved','notes':'Needs customer mapping'}]));out=self.export();self.assertEqual(out['report']['status'],'blocked');r=json.loads(next(f['sql'] for f in out['files'] if f['name']=='resources/application.json'));self.assertEqual(r['resources']['jobs'],{});self.assertFalse(any(f['name'].startswith('assets/') for f in out['files']))
    def test_profile_and_artifact_staleness(self):
        self.save();old=deepcopy(self.app)
        with self.runs.db() as db:db.execute('UPDATE runs SET conversion_json=? WHERE id=?',('{"files":[]}',self.rid))
        with self.assertRaises(Conflict):self.a.export(self.app['id'],{'version':old['version'],'fingerprint':old['fingerprint'],'include_code':True})
    def test_member_delete_guard_and_optimistic_updates(self):
        self.save()
        with self.assertRaises(Conflict):self.runs.delete(self.rid)
        with self.assertRaises(Conflict):self.a.save(self.app['id'],{'version':1,'profile':self.app['profile']})
    def test_cycles_duplicate_names_unknown_runs_and_bad_imports(self):
        second=self.member()
        for edit in [lambda p:p.update(dependencies=[{'from':self.rid,'to':second,'reviewed':True},{'from':second,'to':self.rid,'reviewed':True}]),lambda p:p.update(labels={self.rid:'same',second:'SAME'}),lambda p:p.update(run_ids=[{}])]:
            with self.assertRaises(ValueError):self.save([self.rid,second],edit)
    def test_scheduler_import_paused_and_distinct_patterns_block(self):
        second=self.member();entry={'run_id':self.rid,'source_scheduler':'SQLAgent','source_reference':'Finance_Daily_Close','quartz_cron_expression':'0 0 2 * * ?','timezone_id':'America/Chicago','reviewed':True}
        self.save([self.rid,second],lambda p:p.update(schedules=[entry]));out=self.export();r=json.loads(next(f['sql'] for f in out['files'] if f['name']=='resources/application.json'))['resources'];parent=next(v for k,v in r['jobs'].items() if k.startswith('application_'));self.assertEqual(parent['schedule']['pause_status'],'PAUSED')
        self.save([self.rid,second],lambda p:p.update(schedules=[entry,{**entry,'run_id':second,'quartz_cron_expression':'0 0 3 * * ?'}]));self.assertEqual(self.export()['report']['status'],'blocked')
    def test_source_profile_schedule_cannot_be_silently_removed(self):
        self.f.bundle();self.save();out=self.export();self.assertEqual(out['report']['status'],'blocked')

    def test_application_http_create_save_export_and_origin_guards(self):
        from server import AppServer,Handler,ROOT
        from functools import partial
        import threading
        from urllib.request import Request,urlopen
        from urllib.error import HTTPError
        server=AppServer(('127.0.0.1',0),partial(Handler,directory=str(ROOT/'static')),self.runs);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base='http://127.0.0.1:'+str(server.server_port)
        def post(path,payload,origin=None):
            headers={'Content-Type':'application/json','X-Accelerator-Request':'1'}
            if origin:headers['Origin']=origin
            return urlopen(Request(base+path,data=json.dumps(payload).encode(),headers=headers))
        try:
            created=json.load(post('/api/applications',{'name':'MonthlyClose'}));self.assertEqual(created['profile']['name'],'MonthlyClose')
            self.assertTrue(json.load(urlopen(base+'/api/applications'))['applications'])
            with self.assertRaises(HTTPError) as e:post('/api/applications',{'name':'Other'},'https://untrusted.test')
            self.assertEqual(e.exception.code,403)
            result=json.load(post('/api/applications/'+created['id']+'/export',{'version':created['version'],'fingerprint':created['fingerprint'],'include_code':True}));self.assertEqual(result['report']['status'],'blocked');self.assertTrue(any(f['name']=='application-report.html' for f in result['files']))
        finally:server.shutdown();server.server_close();thread.join()

    def test_lakeflow_mapping_changes_invalidate_application_export(self):
        self.save();old=deepcopy(self.app);p=self.lakeflow.get(self.rid);p['profile']['environment_version']='5';self.lakeflow.save(self.rid,{'version':p['version'],'fingerprint':p['fingerprint'],'profile':p['profile']})
        with self.assertRaises(Conflict):self.a.export(old['id'],{'version':old['version'],'fingerprint':old['fingerprint'],'include_code':True})

    def test_bundle_state_identity_is_unique_and_survives_display_rename(self):
        self.save();first=next(f['sql'] for f in self.export()['files'] if f['name']=='databricks.yml');p=deepcopy(self.app['profile']);p['name']='RenamedFinance';self.app=self.a.save(self.app['id'],{'version':self.app['version'],'profile':p});second=next(f['sql'] for f in self.export()['files'] if f['name']=='databricks.yml');self.assertEqual(first,second);self.assertIn(self.app['id'],second)
