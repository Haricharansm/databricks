"""Versioned golden baselines, durable suite jobs and review-bundle export."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import threading
import uuid
from monitoring import ACTIVE,Conflict,now
from validation import dataset,read_dataset,compare
from suite_runner import run_suite,validate_manifest,DatabricksEndpoint,SqlServerEndpoint,sha

ROOT=Path(__file__).parent
TERMINAL={'passed','failed','error','cancelled','interrupted'}

def artifact_fingerprint(run):
    return sha({'source':[{'name':f['name'],'sha256':hashlib.sha256(f['sql'].encode()).hexdigest()} for f in run['files']],'artifacts':[{'name':f['name'],'sha256':hashlib.sha256(f['sql'].encode()).hexdigest()} for f in (run.get('conversion') or {}).get('files',[])]})

class AutomationService:
    def __init__(self,runs,settings,source_factory=SqlServerEndpoint,target_factory=DatabricksEndpoint,packager=None):
        self.packager=packager;self.runs=runs;self.settings=settings;self.source_factory=source_factory;self.target_factory=target_factory;self.lock=threading.RLock();self.flags={};self.pins={};self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='golden-suite')
        with runs.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS golden_baselines(case_id TEXT PRIMARY KEY REFERENCES validation_cases(id) ON DELETE CASCADE,run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,case_version INTEGER NOT NULL,version INTEGER NOT NULL,expected_json TEXT NOT NULL,evidence TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS golden_suites(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,status TEXT NOT NULL,manifest_json TEXT NOT NULL,report_json TEXT,events_json TEXT NOT NULL,created_at TEXT NOT NULL,finished_at TEXT,cancel_requested INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS golden_suite_run ON golden_suites(run_id,created_at);''')
            db.execute("UPDATE golden_suites SET status='interrupted',finished_at=? WHERE status IN ('queued','running')",(now(),))
    def _run(self,run_id):
        run=self.runs.get(run_id)
        if run['status'] in ACTIVE:raise Conflict('Wait for the migration run to finish before configuring or executing a suite.')
        return run
    def get(self,run_id):
        run=self._run(run_id);cases=self.runs.validation_cases(run_id)['cases'];fingerprint=artifact_fingerprint(run)
        with self.runs.db() as db:
            baseline_rows={r['case_id']:dict(r) for r in db.execute('SELECT * FROM golden_baselines WHERE run_id=?',(run_id,))}
            history=[]
            for row in db.execute('SELECT id,status,created_at,finished_at,manifest_json,report_json FROM golden_suites WHERE run_id=? ORDER BY rowid DESC LIMIT 50',(run_id,)):
                manifest=json.loads(row['manifest_json']);report=json.loads(row['report_json']) if row['report_json'] else None
                stale=manifest['artifact_fingerprint']!=fingerprint or any(not any(c['id']==m['case_id'] and c['version']==m['case_version'] and baseline_rows.get(c['id'],{}).get('version')==m['baseline_version'] for c in cases) for m in manifest['cases'])
                history.append({'id':row['id'],'status':row['status'],'created_at':row['created_at'],'finished_at':row['finished_at'],'mode':report.get('mode') if report else None,'summary':report.get('summary') if report else None,'stale':stale})
        result=[]
        for case in cases:
            b=baseline_rows.get(case['id']);expected=json.loads(b['expected_json']) if b else None
            result.append({'id':case['id'],'version':case['version'],'title':case['definition']['title'],'source':case['source'],'fixture_attempt':case.get('latest_attempt',{}).get('id') if case.get('latest_attempt') and not case['latest_attempt']['stale'] else None,'baseline':{'version':b['version'],'case_version':b['case_version'],'stale':b['case_version']!=case['version'],'sha256':hashlib.sha256(expected['content'].encode()).hexdigest(),'evidence':b['evidence'],'updated_at':b['updated_at']} if b else None})
        return {'run_id':run_id,'artifact_fingerprint':fingerprint,'cases':result,'history':history,'source_odbc_configured':bool(os.environ.get('CROSSLAKE_SQLSERVER_CONNECTION_STRING')),'pyodbc_available':importlib.util.find_spec('pyodbc') is not None,'limits':{'max_rows':10000,'max_result_bytes':2*1024*1024},'fixture_alignment_verified':False}
    def baseline(self,run_id,payload):
        self._run(run_id)
        if not isinstance(payload,dict):raise ValueError('Invalid baseline request.')
        case=self.runs.validation_case(run_id,payload.get('case_id',''))
        if payload.get('case_version')!=case['version']:raise Conflict('Test definition changed. Reload it before saving a golden baseline.')
        expected=dataset(payload.get('expected'));read_dataset(expected)
        if compare(case['definition'],expected,expected)['status']!='passed':raise ValueError('Baseline does not satisfy its test definition. Check column names/types, NULL rules and keyed uniqueness.')
        evidence=payload.get('evidence')
        if not isinstance(evidence,str) or not evidence.strip() or len(evidence)>2000:raise ValueError('Record the source export reference/provenance (up to 2000 characters).')
        with self.runs.db() as db:
            current=db.execute('SELECT version FROM golden_baselines WHERE case_id=? AND run_id=?',(case['id'],run_id)).fetchone();version=current['version'] if current else 0
            fresh=db.execute('SELECT version FROM validation_cases WHERE id=? AND run_id=?',(case['id'],run_id)).fetchone()
            if not fresh or fresh['version']!=case['version'] or payload.get('version')!=version:raise Conflict('Baseline/test definition changed in another tab. Reload before saving.')
            db.execute('INSERT OR REPLACE INTO golden_baselines VALUES(?,?,?,?,?,?,?)',(case['id'],run_id,case['version'],version+1,json.dumps(expected),evidence.strip(),now()))
        return self.get(run_id)
    def manifest(self,run_id,payload):
        run=self._run(run_id);ids=payload.get('case_ids')
        if not isinstance(ids,list) or not 1<=len(ids)<=100 or any(not isinstance(i,str) for i in ids) or len(set(ids))!=len(ids):raise ValueError('Select 1–100 unique cases.')
        cases=[]
        for ident in ids:
            case=self.runs.validation_case(run_id,ident)
            with self.runs.db() as db:b=db.execute('SELECT * FROM golden_baselines WHERE case_id=? AND run_id=?',(ident,run_id)).fetchone()
            if not b or b['case_version']!=case['version']:raise Conflict('Every selected case needs a golden baseline for its current definition version.')
            expected=json.loads(b['expected_json']);row={'case_id':ident,'case_version':case['version'],'baseline_version':b['version'],'definition':case['definition'],'expected':expected,'expected_sha256':hashlib.sha256(expected['content'].encode()).hexdigest(),'expected_ref':b['evidence']}
            if payload.get('mode')=='fixtures':
                # Only an explicitly chosen historic target result can be replayed offline.
                attempt_id=(payload.get('fixture_attempts') or {}).get(ident)
                if not isinstance(attempt_id,str):raise ValueError('Fixture mode requires a recorded comparison attempt for every selected case.')
                attempt=self.runs.validation_attempt(run_id,attempt_id,True)
                if attempt['report']['case_id']!=ident or attempt['stale']:raise Conflict('Recorded fixture attempt does not match the current case definition.')
                current_artifacts=[{'name':f['name'],'sha256':hashlib.sha256(f['sql'].encode()).hexdigest()} for f in (run.get('conversion') or {}).get('files',[])]
                if attempt['report']['source_snapshot']['run_artifacts']!=current_artifacts:raise Conflict('Recorded fixture result belongs to different conversion artifacts.')
                row['actual']=attempt['recorded_inputs']['actual'];row['actual_ref']='Recorded comparison '+attempt_id
            cases.append(row)
        manifest={'schema_version':1,'run_id':run_id,'fixture_ref':payload.get('fixture_ref'),'artifact_fingerprint':artifact_fingerprint(run),'cases':cases,'created_at':now()}
        return validate_manifest(manifest)
    def start(self,run_id,payload):
        if not isinstance(payload,dict):raise ValueError('Invalid suite request.')
        mode=payload.get('mode','databricks')
        if mode not in ('databricks','sqlserver','fixtures'):raise ValueError('Suite mode must be databricks, sqlserver or fixtures.')
        manifest=self.manifest(run_id,payload);target=None;source=None
        if mode=='fixtures' and (payload.get('source_job') or payload.get('target_job')):raise ValueError('Fixture replay cannot start jobs.')
        if mode!='fixtures':
            if payload.get('allow_compute') is not True:raise ValueError('Confirm warehouse query execution and compute cost.')
            config,token,revision=self.settings._credentials(payload)
            if not config['warehouse_id']:raise ValueError('Save a target SQL warehouse first.')
            target=self.target_factory(config,token,self.settings.transport)
            manifest['target_settings_revision']=revision;manifest['target_namespace']={k:config.get(k) for k in ('workspace_url','warehouse_id','catalog','schema')}
        if mode=='sqlserver':
            if payload.get('allow_source_queries') is not True:raise ValueError('Confirm SQL Server source read-query execution.')
            source=self.source_factory()
        source_job=payload.get('source_job') or None;target_job=payload.get('target_job') or None
        if source_job and (source is None or payload.get('allow_source_job') is not True):raise ValueError('Starting a source SSIS/SQL Agent job needs SQL Server mode and separate consent.')
        if target_job and (target is None or payload.get('allow_target_job') is not True):raise ValueError('Starting a target job needs separate consent.')
        manifest['execution_plan']={'mode':mode,'source_job':source_job,'target_job':target_job}
        ident=uuid.uuid4().hex
        with self.lock,self.runs.db() as db:
            if db.execute("SELECT COUNT(*) FROM golden_suites WHERE status IN ('queued','running')").fetchone()[0]>=1:raise Conflict('Another golden suite is queued or running. Finish or cancel it first.')
            # Pin definitions/baselines and artifacts immediately before queueing.
            for case in manifest['cases']:
                current=db.execute('SELECT c.version AS cv,b.version AS bv,b.case_version AS bc FROM validation_cases c JOIN golden_baselines b ON b.case_id=c.id WHERE c.id=? AND c.run_id=?',(case['case_id'],run_id)).fetchone()
                if not current or current['cv']!=case['case_version'] or current['bv']!=case['baseline_version'] or current['bc']!=case['case_version']:raise Conflict('Case or baseline changed before execution; reload the suite.')
            if payload.get('artifact_fingerprint',manifest['artifact_fingerprint'])!=manifest['artifact_fingerprint']:raise Conflict('Migration artifacts changed since the UI was loaded.')
            if artifact_fingerprint(self._run(run_id))!=manifest['artifact_fingerprint']:raise Conflict('Migration artifacts changed; reload before execution.')
            self.flags[ident]=threading.Event();db.execute('INSERT INTO golden_suites(id,run_id,status,manifest_json,events_json,created_at) VALUES(?,?,?,?,?,?)',(ident,run_id,'queued',json.dumps(manifest),'[]',now()))
        self.executor.submit(self._execute,ident,manifest,target,source,mode,source_job,target_job)
        return self.status(run_id,ident)
    def _execute(self,ident,manifest,target,source,mode,source_job,target_job):
        def event(value):
            with self.runs.db() as db:
                row=db.execute('SELECT events_json FROM golden_suites WHERE id=?',(ident,)).fetchone()
                if row:events=json.loads(row['events_json']);events.append(value);db.execute('UPDATE golden_suites SET events_json=? WHERE id=?',(json.dumps(events[-250:]),ident))
        try:
            with self.runs.db() as db:db.execute("UPDATE golden_suites SET status='running' WHERE id=?",(ident,))
            report=run_suite(manifest,target,source,fixture_mode=mode=='fixtures',source_job=source_job,target_job=target_job,cancel=self.flags[ident].is_set,progress=event)
            with self.runs.db() as db:
                db.execute('UPDATE golden_suites SET status=?,report_json=?,finished_at=? WHERE id=?',(report['status'],json.dumps(report),now(),ident));self.runs.event(db,manifest['run_id'],'Golden suite '+report['status']+' ('+report['mode']+'). Results do not automatically approve deployment.')
        except Exception:
            with self.runs.db() as db:db.execute("UPDATE golden_suites SET status='error',report_json=?,finished_at=? WHERE id=?",(json.dumps({'status':'error','error':'Suite worker failed; no successful evidence was recorded.'}),now(),ident))
        finally:
            with self.lock:self.flags.pop(ident,None)
    def status(self,run_id,ident):
        self.runs.get(run_id,False)
        with self.runs.db() as db:row=db.execute('SELECT * FROM golden_suites WHERE id=? AND run_id=?',(ident,run_id)).fetchone()
        if not row:raise KeyError('Golden suite not found.')
        return {'id':row['id'],'run_id':run_id,'status':row['status'],'created_at':row['created_at'],'finished_at':row['finished_at'],'events':json.loads(row['events_json']),'report':json.loads(row['report_json']) if row['report_json'] else None,'cancel_requested':bool(row['cancel_requested'])}
    def cancel(self,run_id,payload):
        ident=payload.get('id');current=self.status(run_id,ident)
        if current['status'] not in TERMINAL:
            with self.lock:
                if ident in self.flags:self.flags[ident].set()
            with self.runs.db() as db:db.execute('UPDATE golden_suites SET cancel_requested=1 WHERE id=? AND run_id=?',(ident,run_id))
        return self.status(run_id,ident)
    def export(self,run_id,payload):
        if payload.get('include_baselines') is not True:raise ValueError('Confirm that the CI export may include the selected golden datasets and query text. Do not commit sensitive data to a public repository.')
        manifest=self.manifest(run_id,payload);files=[]
        if payload.get('include_deployment'):
            if self.packager is None:raise ValueError('Deployment packager is unavailable.')
            package=self.packager(run_id)
            enabled=payload.get('enable_reviewed_candidate') is True
            if enabled and any(s['status']!='converted' or s['review']!='approved' for s in self._run(run_id)['sources']):raise Conflict('Approve every converted source before enabling sandbox notebook execution in the CI package.')
            candidates=[]
            for f in package['files']:
                content=f['sql']
                if enabled and f['name'].startswith('resources/') and f['name'].endswith('.json'):
                    resource=json.loads(content)
                    for job in resource.get('resources',{}).get('jobs',{}).values():
                        for task in job.get('tasks',[]):
                            if 'notebook_task' in task:task['notebook_task'].setdefault('base_parameters',{})['migration_reviewed']='true'
                    content=json.dumps(resource,indent=2)
                name='migration/'+f['name'];files.append({'name':name,'sql':content});candidates.append({'path':name,'sha256':hashlib.sha256(content.encode()).hexdigest()})
            manifest['candidate_artifacts']=candidates;manifest['sandbox_execution_enabled']=enabled
        for case in manifest['cases']:
            for side in ('expected','actual'):
                if side not in case:continue
                data=case.pop(side);name='golden/'+case['case_id']+'-'+side+'.'+data['format'];case[side+'_file']=name;case[side+'_format']=data['format'];files.append({'name':name,'sql':data['content']})
        files.extend([{'name':'crosslake-suite.json','sql':json.dumps(manifest,indent=2)},{'name':'validation.py','sql':(ROOT/'validation.py').read_text()},{'name':'suite_runner.py','sql':(ROOT/'suite_runner.py').read_text()},{'name':'accelerator.py','sql':(ROOT/'accelerator.py').read_text()},{'name':'ci_runner.py','sql':(ROOT/'ci_runner.py').read_text()}])
        files=[f for f in files if f['name']!='accelerator.py']
        files.append({'name':'ci_read_query.py','sql':(ROOT/'ci_read_query.py').read_text()})
        files.append({'name':'.gitattributes','sql':'golden/** -text\n'})
        files.extend([{'name':'databricks_settings.py','sql':(ROOT/'databricks_settings.py').read_text()},{'name':'.github/workflows/crosslake-suite.yml','sql':(ROOT/'ci_templates/crosslake-suite.yml').read_text()},{'name':'README.md','sql':(ROOT/'ci_templates/README.md').read_text()}])
        if len(files)>190 or sum(len(f['sql'].encode()) for f in files)>8*1024*1024:raise ValueError('CI bundle exceeds 8 MB; select fewer/smaller cases.')
        return {'files':files,'report':{'run_id':run_id,'fixture_ref':manifest['fixture_ref'],'artifact_fingerprint':manifest['artifact_fingerprint'],'contains_golden_data':True,'deployment_approved':False}}
    def close(self):
        with self.lock:
            for flag in self.flags.values():flag.set()
        self.executor.shutdown(wait=True)
