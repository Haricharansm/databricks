"""Explicit Lakeflow deployment, owned Job execution and linked golden validation."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import base64
import hashlib
import json
import re
import threading
import time
import uuid
from urllib.parse import quote
from monitoring import Conflict,now
from ci_read_query import read_query

ACTIVE={'queued','deploying','running','validating','cancelling'}
TERMINAL={'TERMINATED','SKIPPED','INTERNAL_ERROR'}

def fingerprint(value):return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
def source_hash(text):return hashlib.sha256(text.replace('\r\n','\n').strip().encode()).hexdigest()
def remote_id(value):
    text=str(value)
    if not re.fullmatch(r'[A-Za-z0-9-]{1,100}',text):raise RuntimeError('Workspace returned an invalid resource ID.')
    return text

def subset(expected,actual):
    if isinstance(expected,dict):return isinstance(actual,dict) and all(k in actual and subset(v,actual[k]) for k,v in expected.items())
    if isinstance(expected,list):
        if not isinstance(actual,list) or len(expected)!=len(actual):return False
        for identity in ('task_key','environment_key'):
            if expected and all(isinstance(x,dict) and identity in x for x in expected+actual):
                by_id={x[identity]:x for x in actual}
                return len(by_id)==len(actual) and all(x[identity] in by_id and subset(x,by_id[x[identity]]) for x in expected)
        return all(subset(a,b) for a,b in zip(expected,actual))
    return expected==actual

class LakeflowLive:
    def __init__(self,runs,settings,lakeflow,automation,*,sleep=time.sleep,clock=time.monotonic,timeout=7200):
        self.runs=runs;self.settings=settings;self.lakeflow=lakeflow;self.automation=automation;self.sleep=sleep;self.clock=clock;self.timeout=timeout;self.lock=threading.RLock();self.flags={};self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='lakeflow-live')
        with runs.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS lakeflow_live(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,kind TEXT NOT NULL,status TEXT NOT NULL,body_json TEXT NOT NULL,report_json TEXT NOT NULL,created_at TEXT NOT NULL,finished_at TEXT)')
            db.execute("UPDATE lakeflow_live SET status='interrupted',finished_at=? WHERE status IN ('queued','deploying','running','validating','cancelling')",(now(),))
    def _save(self,ident,status,report):
        with self.runs.db() as db:db.execute('UPDATE lakeflow_live SET status=?,report_json=?,finished_at=? WHERE id=?',(status,json.dumps(report),None if status in ACTIVE else now(),ident))
    def status(self,run_id,ident):
        with self.runs.db() as db:row=db.execute('SELECT * FROM lakeflow_live WHERE id=? AND run_id=?',(ident,run_id)).fetchone()
        if row is None:raise KeyError('Lakeflow deployment/execution not found.')
        return {k:row[k] for k in ('id','run_id','kind','status','created_at','finished_at')}|{'report':json.loads(row['report_json'])}
    def history(self,run_id):
        self.runs.get(run_id,False)
        with self.runs.db() as db:ids=[r[0] for r in db.execute('SELECT id FROM lakeflow_live WHERE run_id=? ORDER BY rowid DESC LIMIT 50',(run_id,))]
        return {'operations':[self.status(run_id,i) for i in ids]}
    def _body(self,ident):
        with self.runs.db() as db:return json.loads(db.execute('SELECT body_json FROM lakeflow_live WHERE id=?',(ident,)).fetchone()[0])
    def refresh_remote(self,run_id,payload):
        op=self.status(run_id,payload.get('id',''))
        if op['kind']!='execution' or not op['report'].get('remote_run_id'):raise ValueError('Select an execution with a recorded remote run ID.')
        if op['status'] in ACTIVE:return op
        config,token,_=self.settings._credentials(payload)
        if config['workspace_url']!=op['report']['workspace_url']:raise Conflict('Saved workspace differs from this execution.')
        response=self.settings.transport(config['workspace_url'],token,'GET','/api/2.1/jobs/runs/get?run_id='+remote_id(op['report']['remote_run_id']),None)
        if response.get('has_more') or response.get('next_page_token'):raise ValueError('Remote task evidence is incomplete.')
        report=op['report'];report['reconciled_remote_state']={k:response.get('state',{}).get(k) for k in ('life_cycle_state','result_state')};report['reconciled_at']=now();report['reconciliation_note']='Read-only remote status refresh. No Job was restarted and no validation was automatically replayed.'
        self._save(op['id'],op['status'],report);return self.status(run_id,op['id'])
    def _ready(self,run_id):
        bundle=self.lakeflow.generate(run_id);r=bundle['report'];run=self.runs.get(run_id)
        if not r['jobs'] or any(p['blockers'] for p in r['packages']):raise ValueError('Every package must generate a complete Job before live deployment.')
        if any(s['status']!='converted' or s['review']!='approved' for s in run['sources']):raise Conflict('Approve every converted source in Runs & reviews before live deployment or execution.')
        resources=json.loads(next(f['sql'] for f in bundle['files'] if f['name']=='resources/lakeflow.json'))['resources']
        paths={f['name']:f['sql'] for f in bundle['files'] if f['name'].endswith('.py')}
        for job in resources['jobs'].values():
            if len(job['tasks'])>100:raise ValueError('Live monitoring supports at most 100 tasks per Job; use the exported bundle for larger graphs.')
            for task in job['tasks']:
                if 'notebook_task' in task and not task['notebook_task']['notebook_path'].startswith('${workspace.file_path}/notebooks/'):raise ValueError('Live deployment currently requires bundled notebooks; existing workspace notebook mappings stay export-only.')
        if any(len(code.encode())>400000 for code in paths.values()):raise ValueError('A notebook exceeds the 400 KB live verification limit; use the exported bundle.')
        proof=fingerprint({'profile':r['profile_sha256'],'fingerprint':r['fingerprint'],'artifacts':r['artifacts']})
        return bundle,resources,paths,proof
    def preview(self,run_id):
        bundle,resources,paths,proof=self._ready(run_id);config=self.settings.public()
        return {'bundle_sha256':proof,'workspace_url':config.get('workspace_url'),'revision':config['revision'],'jobs':list(resources['jobs']),'pipelines':list(resources['pipelines']),'notebooks':list(paths),'profile_version':bundle['report']['profile_version'],'namespace':{k:bundle['report']['profile'][k] for k in ('catalog','schema')},'schedules_paused':True,'creates_new_resources':True}
    def _queue(self,run_id,kind,body,worker,config,token):
        ident=uuid.uuid4().hex;report={'workspace_url':config['workspace_url'],'events':[],'jobs':{},'pipelines':{},'notebooks':[]}
        with self.lock,self.runs.db() as db:
            if db.execute("SELECT 1 FROM lakeflow_live WHERE status IN ('queued','deploying','running','validating','cancelling')").fetchone():raise Conflict('Another Lakeflow live operation is active; finish it first.')
            self.flags[ident]=threading.Event();db.execute('INSERT INTO lakeflow_live VALUES(?,?,?,?,?,?,?,NULL)',(ident,run_id,kind,'queued',json.dumps(body),json.dumps(report),now()))
        self.executor.submit(self._work,ident,run_id,body,worker,config,token,report)
        return self.status(run_id,ident)
    def _work(self,ident,run_id,body,worker,config,token,report):
        def call(method,path,payload=None):return self.settings.transport(config['workspace_url'],token,method,path,payload)
        def checkpoint(stage,status=None):
            report['events'].append({'at':now(),'stage':stage});report['events']=report['events'][-150:];self._save(ident,status or ('deploying' if body['kind']=='deployment' else 'running'),report)
        try:worker(ident,run_id,body,config,call,report,checkpoint)
        except (ValueError,Conflict) as exc:
            report['error']=str(exc)[:1000];self._save(ident,'error',report)
        except Exception:
            report['error']='Workspace operation or verification failed. Inspect recorded resource/run IDs in Databricks; partial resources are retained. Provider output and credentials are not stored.';self._save(ident,'error',report)
        finally:
            with self.lock:self.flags.pop(ident,None)
    def deploy(self,run_id,payload):
        if payload.get('allow_deploy') is not True or payload.get('reviewed_code') is not True:raise ValueError('Confirm deployment of the exact reviewed bundle and enable its notebook/pipeline review gates.')
        config,token,revision=self.settings._credentials(payload);bundle,resources,paths,proof=self._ready(run_id)
        if payload.get('bundle_sha256')!=proof or payload.get('workspace_url')!=config['workspace_url']:raise Conflict('Bundle or workspace changed. Preview and review again.')
        body={'kind':'deployment','proof':proof,'resources':resources,'paths':paths,'revision':revision,'namespace':{k:bundle['report']['profile'][k] for k in ('catalog','schema')}}
        return self._queue(run_id,'deployment',body,self._deploy,config,token)
    def _deploy(self,ident,run_id,body,config,call,report,checkpoint):
        root='/Shared/Crosslake/'+run_id+'/'+ident;report['workspace_root']=root;report['bundle_sha256']=body['proof'];report['namespace']=body['namespace'];checkpoint('Creating isolated workspace folder')
        call('POST','/api/2.0/workspace/mkdirs',{'path':root})
        for name,code in body['paths'].items():
            path=root+'/'+name.removesuffix('.py');parent=path.rsplit('/',1)[0];call('POST','/api/2.0/workspace/mkdirs',{'path':parent})
            call('POST','/api/2.0/workspace/import',{'path':path,'format':'SOURCE','language':'PYTHON','content':base64.b64encode(code.encode()).decode(),'overwrite':False})
            report['notebooks'].append({'path':path,'source_sha256':source_hash(code)});checkpoint('Imported '+name)
        def path(value):
            if not value.startswith('${workspace.file_path}/'):raise ValueError('Unexpected workspace resource path.')
            return value.replace('${workspace.file_path}',root,1).removesuffix('.py')
        for resource,spec in body['resources'].get('pipelines',{}).items():
            spec=deepcopy(spec);spec['name']+='_'+ident[:8];spec['libraries']=[{'notebook':{'path':path(x['file']['path'])}} for x in spec['libraries']];spec['configuration']['crosslake.migration_reviewed']='true'
            response=call('POST','/api/2.0/pipelines',spec);pid=remote_id(response.get('pipeline_id',''));report['pipelines'][resource]={'id':pid,'spec':spec};checkpoint('Created pipeline '+resource)
            actual=call('GET','/api/2.0/pipelines/'+pid).get('spec',{})
            if not subset(spec,actual):raise ValueError('Pipeline verification failed after creation.')
            report['pipelines'][resource]['remote_spec_sha256']=fingerprint(actual);checkpoint('Verified pipeline '+resource)
        for resource,job in body['resources']['jobs'].items():
            job=deepcopy(job);job['name']+='_'+ident[:8]
            if job.get('schedule'):job['schedule']['pause_status']='PAUSED'
            for task in job['tasks']:
                if 'notebook_task' in task:task['notebook_task']['notebook_path']=path(task['notebook_task']['notebook_path']);task['notebook_task']['base_parameters']['migration_reviewed']='true'
                if 'pipeline_task' in task:
                    ref=task['pipeline_task']['pipeline_id'];match=re.fullmatch(r'\$\{resources\.pipelines\.([A-Za-z0-9_]+)\.id\}',ref)
                    if not match:raise ValueError('Unexpected pipeline resource reference.')
                    task['pipeline_task']['pipeline_id']=report['pipelines'][match[1]]['id']
            jid=remote_id(call('POST','/api/2.1/jobs/create',job).get('job_id',''))
            if not jid.isdigit():raise ValueError('Workspace returned a nonnumeric Job ID.')
            report['jobs'][resource]={'id':jid,'settings':job};checkpoint('Created Job '+resource)
            actual=call('GET','/api/2.1/jobs/get?job_id='+jid).get('settings',{})
            if not subset(job,actual):raise ValueError('Job verification failed after creation.')
            report['jobs'][resource]['remote_settings_sha256']=fingerprint(actual);checkpoint('Verified Job '+resource)
        checkpoint('Deployment complete; nothing has been executed');self._save(ident,'deployed',report)
    def run(self,run_id,payload):
        if payload.get('allow_run') is not True:raise ValueError('Confirm Job execution, compute cost and target-data changes.')
        deployment=self.status(run_id,payload.get('deployment_id',''))
        if deployment['kind']!='deployment' or deployment['status']!='deployed':raise Conflict('Select a completed Crosslake deployment.')
        config,token,revision=self.settings._credentials(payload)
        if config['workspace_url']!=deployment['report']['workspace_url']:raise Conflict('The saved workspace differs from the deployment workspace.')
        current_bundle,_,_,proof=self._ready(run_id)
        if proof!=deployment['report']['bundle_sha256']:raise Conflict('Lakeflow artifacts/mappings changed. Deploy the reviewed bundle again.')
        resource=payload.get('job_resource');job=deployment['report']['jobs'].get(resource)
        if not job:raise ValueError('Select a Job resource from this deployment.')
        manifest=None
        if payload.get('validate_after') is True:
            if payload.get('allow_validation') is not True:raise ValueError('Confirm post-run golden queries and SQL warehouse compute.')
            if not config.get('warehouse_id'):raise ValueError('Save a SQL warehouse for automatic validation.')
            manifest=self.automation.manifest(run_id,{'case_ids':payload.get('case_ids'),'fixture_ref':payload.get('fixture_ref'),'mode':'databricks'})
            for case in manifest['cases']:
                read_query(case['definition']['target_sql'])
                if json.loads(case['definition']['parameters']):raise ValueError('Automatic validation requires fully bound queries and empty case parameters.')
            namespace=deployment['report']['namespace']
            if any(config.get(k)!=namespace[k] for k in ('catalog','schema')):raise Conflict('Saved validation catalog/schema must match the Lakeflow deployment namespace.')
        expected_values={v['producer_task_key']:[o['task_value_key'] for o in task.get('task_values',[])] for package in current_bundle['report']['packages'] if package.get('job_resource')==resource for task in package['tasks'] for v in task.get('task_values',[]) }
        body={'kind':'execution','expected_values':expected_values,'deployment_id':deployment['id'],'job_resource':resource,'job':job,'deployment_report':deployment['report'],'revision':revision,'manifest':manifest}
        return self._queue(run_id,'execution',body,self._run_job,config,token)
    def _verify(self,call,deployment,job):
        for notebook in deployment['notebooks']:
            response=call('GET','/api/2.0/workspace/export?path='+quote(notebook['path'],safe='')+'&format=SOURCE');text=base64.b64decode(response['content'],validate=True).decode()
            if source_hash(text)!=notebook['source_sha256']:raise ValueError('Workspace notebook changed after deployment.')
        actual=call('GET','/api/2.1/jobs/get?job_id='+job['id'])
        if actual.get('has_more') or actual.get('next_page_token') or fingerprint(actual.get('settings',{}))!=job['remote_settings_sha256'] or not subset(job['settings'],actual.get('settings',{})):raise ValueError('Job settings changed or are incomplete.')
        for pipeline in deployment['pipelines'].values():
            actual=call('GET','/api/2.0/pipelines/'+pipeline['id'])
            if fingerprint(actual.get('spec',{}))!=pipeline['remote_spec_sha256'] or not subset(pipeline['spec'],actual.get('spec',{})):raise ValueError('Pipeline settings changed after deployment.')
    def _run_job(self,ident,run_id,body,config,call,report,checkpoint):
        report['deployment_id']=body['deployment_id'];report['job_resource']=body['job_resource'];report['job_id']=body['job']['id'];checkpoint('Verifying deployed notebook hashes and resource settings')
        self._verify(call,body['deployment_report'],body['job'])
        if self.flags[ident].is_set():self._save(ident,'cancelled',report);return
        response=call('POST','/api/2.1/jobs/run-now',{'job_id':int(body['job']['id']),'idempotency_token':ident});rid=remote_id(response.get('run_id',''))
        if not rid.isdigit():raise ValueError('Workspace returned a nonnumeric run ID.')
        report['remote_run_id']=rid;report['run_url']=config['workspace_url']+'/#job/'+body['job']['id']+'/run/'+rid;checkpoint('Job submitted; monitoring task states')
        start=self.clock();cancel_sent=False;cancel_at=None
        while True:
            response=call('GET','/api/2.1/jobs/runs/get?run_id='+rid)
            if response.get('has_more') or response.get('next_page_token'):raise ValueError('Task status is paginated; cannot verify a complete run.')
            state=response.get('state',{});report['state']={k:state.get(k) for k in ('life_cycle_state','result_state')};report['tasks']=[{'task_key':t.get('task_key'),'run_id':t.get('run_id'),'state':{k:t.get('state',{}).get(k) for k in ('life_cycle_state','result_state')},'start_time':t.get('start_time'),'end_time':t.get('end_time')} for t in response.get('tasks',[])]
            checkpoint('Refreshed remote task states','cancelling' if cancel_sent else 'running')
            if state.get('life_cycle_state') in TERMINAL:break
            if self.flags[ident].is_set() or self.clock()-start>self.timeout:
                if not cancel_sent:call('POST','/api/2.1/jobs/runs/cancel',{'run_id':int(rid)});cancel_sent=True;cancel_at=self.clock();checkpoint('Cancellation requested','cancelling')
                if cancel_at is not None and self.clock()-cancel_at>60:self._save(ident,'interrupted',report);return
            self.sleep(2)
        if state.get('result_state')!='SUCCESS' or cancel_sent:
            self._save(ident,'cancelled' if cancel_sent or state.get('result_state')=='CANCELED' else 'failed',report);return
        if {t['task_key'] for t in report['tasks']}!={t['task_key'] for t in body['job']['settings']['tasks']} or any(t['state']['result_state']!='SUCCESS' for t in report['tasks']):raise ValueError('Successful task evidence is incomplete; no automatic validation was started.')
        for task in report['tasks']:
            if task['run_id'] and task['state']['result_state']=='SUCCESS' and task['task_key'] in body['expected_values']:
                try:
                    output=call('GET','/api/2.1/jobs/runs/get-output?run_id='+remote_id(task['run_id']));raw=output.get('notebook_output',{}).get('result','')
                    if isinstance(raw,str) and len(raw)<=20000:
                        values=json.loads(raw).get('crosslake_task_values',{})
                        if isinstance(values,dict) and set(values)==set(body['expected_values'][task['task_key']]) and len(values)<=250 and all(re.fullmatch(r'ssis_[A-Za-z0-9_]{1,120}',k) and type(v) is int for k,v in values.items()):task['task_values']=values
                        else:task['output_note']='Structured output did not match declared task-value keys.'
                except Exception:task['output_note']='Structured task values unavailable; inspect the Databricks run for logs and outputs.'
        if self.flags[ident].is_set():report['validation_note']='Validation skipped because cancellation was requested.';self._save(ident,'succeeded',report);return
        if body['manifest']:
            checkpoint('Job succeeded; starting the selected golden suite','validating')
            try:
                parent={'lakeflow_operation_id':ident,'deployment_id':body['deployment_id'],'job_id':body['job']['id'],'remote_run_id':rid,'workspace_url':config['workspace_url']}
                payload={'mode':'databricks','revision':body['revision'],'allow_compute':True,'case_ids':[c['case_id'] for c in body['manifest']['cases']],'fixture_ref':body['manifest']['fixture_ref']}
                suite=self.automation.start(run_id,payload,prepared_manifest=body['manifest'],parent_execution=parent);report['suite_id']=suite['id'];checkpoint('Golden suite queued','validating')
                while suite['status'] in ('queued','running'):
                    if self.flags[ident].is_set():self.automation.cancel(run_id,{'id':suite['id']})
                    self.sleep(1);suite=self.automation.status(run_id,suite['id']);report['validation_status']=suite['status'];self._save(ident,'validating',report)
                report['validation_status']=suite['status'];report['validation_report']=suite['report'];self._save(ident,'validated' if suite['status']=='passed' else 'validation_'+suite['status'],report)
            except Exception:report['validation_status']='error';report['validation_note']='Post-run validation could not start or finish. Check settings revision, baseline/case versions and active suites; no pass was recorded.';self._save(ident,'validation_error',report)
        else:self._save(ident,'succeeded',report)
    def cancel(self,run_id,payload):
        op=self.status(run_id,payload.get('id',''))
        if op['kind']!='execution':raise ValueError('Deployment cannot be cancelled mid-resource creation. Inspect retained partial resources if it fails.')
        with self.lock:
            if op['id'] in self.flags:self.flags[op['id']].set()
        return self.status(run_id,op['id'])
    def close(self):
        with self.lock:
            for flag in self.flags.values():flag.set()
        self.executor.shutdown(wait=True)
