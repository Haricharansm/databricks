"""Shared golden-suite engine used by the web worker and command-line CI runner.

Fixtures are supplied by an operator; input snapshot alignment is recorded, not inferred.
Optional source/target job execution happens only through separately consented adapters.
"""
import hashlib
import json
from datetime import datetime,timezone,timedelta
from decimal import Decimal
import math
import os
import re
import time
import uuid
from ci_read_query import read_query
from validation import definition,dataset,read_dataset,compare,MAX_ROWS,MAX_DATA_BYTES

class Cancelled(RuntimeError):pass

def stamp():return datetime.now(timezone.utc).isoformat(timespec='milliseconds')
def sha(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
def check_cancel(cancel):
    if cancel():raise Cancelled('Suite cancellation requested.')

def validate_manifest(manifest):
    if not isinstance(manifest,dict) or manifest.get('schema_version')!=1:raise ValueError('Unsupported golden suite manifest; expected schema_version 1.')
    fixture=manifest.get('fixture_ref')
    if not isinstance(fixture,str) or not fixture.strip() or len(fixture)>2000:raise ValueError('Record the input fixture/snapshot reference (up to 2000 characters).')
    cases=manifest.get('cases')
    if not isinstance(cases,list) or not 1<=len(cases)<=100:raise ValueError('A suite requires 1–100 cases.')
    seen=set()
    for case in cases:
        if not isinstance(case,dict):raise ValueError('Invalid suite case.')
        ident=case.get('case_id')
        if not isinstance(ident,str) or not ident or ident in seen:raise ValueError('Case IDs must be unique nonempty strings.')
        seen.add(ident);d=definition(case.get('definition'));expected=dataset(case.get('expected'));read_dataset(expected)
        if compare(d,expected,expected)['status']!='passed':raise ValueError('Golden baseline does not satisfy the declared test definition: '+d['title'])
        if case.get('expected_sha256')!=hashlib.sha256(expected['content'].encode()).hexdigest():raise ValueError('Golden baseline hash mismatch: '+ident)
        if not isinstance(case.get('expected_ref'),str) or not case['expected_ref'].strip():raise ValueError('Record each baseline export reference.')
    if len(json.dumps(manifest).encode())>20*1024*1024:raise ValueError('Suite manifest exceeds 20 MB; use smaller suites.')
    return manifest


def run_suite(manifest,target=None,source=None,*,fixture_mode=False,source_job=None,target_job=None,cancel=lambda:False,progress=lambda event:None):
    validate_manifest(manifest);result={'schema_version':1,'id':uuid.uuid4().hex,'started_at':stamp(),'status':'running','fixture_ref':manifest['fixture_ref'],'fixture_alignment_verified':False,'manifest_sha256':sha(manifest),'artifact_fingerprint':manifest.get('artifact_fingerprint'),'mode':'fixtures' if fixture_mode else 'source_and_target' if source else 'golden_to_target','cases':[],'jobs':{},'deployable':False,'limits':{'max_rows_per_result':MAX_ROWS,'max_bytes_per_result':MAX_DATA_BYTES},'limitations':['Only declared queries, fixtures and optional jobs are checked. A pass is not complete SSIS semantic equivalence.','Input fixture alignment and golden export provenance are user supplied. Jobs must be configured to consume the referenced inputs.','Job success does not prove artifact immutability; external workspace/source changes require independent control.']}
    source_results={};case_rows={};submissions=[]
    def remote_event(event,label):
        record={'stage':label+'_remote_submission','at':stamp(),**event};submissions.append(record);progress(record)
    for endpoint,label in [(source,'source'),(target,'target')]:
        if endpoint is not None and hasattr(endpoint,'on_event'):endpoint.on_event=lambda event,label=label:remote_event(event,label)
    def event(stage,**extra):progress({'stage':stage,'at':stamp(),**extra})
    try:
        if fixture_mode and (source_job or target_job or source or target):raise ValueError('Fixture mode never permits external adapters or jobs.')
        if not fixture_mode and target is None:raise ValueError('A target adapter is required for live execution.')
        if source_job and source is None:raise ValueError('Source job execution requires a configured source adapter.')
        if not fixture_mode:
            for case in manifest['cases']:
                d=definition(case['definition']);read_query(d['target_sql'])
                if source:read_query(d['source_sql'])
                if json.loads(d['parameters']):raise ValueError('Use fully bound queries and empty parameters for automation.')
            if target_job and not re.fullmatch(r'[0-9]{1,20}',str(target_job)):raise ValueError('Target job ID must be numeric.')
            if source_job and hasattr(source,'allowed_jobs') and source_job not in source.allowed_jobs:raise ValueError('Source job is not allowlisted.')
        if source_job:
            check_cancel(cancel);event('source_job_started');result['jobs']['source']=source.run_job(source_job,cancel);event('source_job_succeeded')
        for case in manifest['cases']:
            check_cancel(cancel);d=definition(case['definition']);row={'case_id':case['case_id'],'case_version':case.get('case_version'),'baseline_version':case.get('baseline_version'),'title':d['title'],'definition':d,'expected_ref':case['expected_ref'],'status':'running','checks':{},'target':None,'source':None};result['cases'].append(row);case_rows[case['case_id']]=row
            if source:
                event('source_query',case_id=case['case_id']);sql=read_query(d['source_sql'])
                if json.loads(d['parameters']):raise ValueError('Use fully bound queries and empty parameters for automation.')
                actual,metadata=source.query(sql,cancel);source_results[case['case_id']]=actual;row['source']=metadata
                row['checks']['golden_to_source']=compare(d,case['expected'],actual)
                if row['checks']['golden_to_source']['status']!='passed':row['status']='failed';raise RuntimeError('Source output differs from the golden baseline; target job was not started.')
        if target_job:
            check_cancel(cancel);event('target_job_started');result['jobs']['target']=target.run_job(target_job,cancel);event('target_job_succeeded')
        for case in manifest['cases']:
            check_cancel(cancel);row=case_rows[case['case_id']];d=definition(case['definition']);event('target_query',case_id=case['case_id'])
            if fixture_mode:
                actual=dataset(case.get('actual'));read_dataset(actual);metadata={'executed_by_app':False,'mode':'supplied_fixture'}
            else:
                if json.loads(d['parameters']):raise ValueError('Use fully bound queries and empty parameters for automation.')
                actual,metadata=target.query(read_query(d['target_sql']),cancel)
            row['target']=metadata;row['checks']['golden_to_target']=compare(d,case['expected'],actual)
            if source:row['checks']['source_to_target']=compare(d,source_results[case['case_id']],actual)
            row['status']='passed' if all(c['status']=='passed' for c in row['checks'].values()) else 'error' if any(c['status']=='error' for c in row['checks'].values()) else 'failed'
            row['input_hashes']={'expected':case['expected_sha256'],'actual':hashlib.sha256(actual['content'].encode()).hexdigest()};event('case_finished',case_id=case['case_id'],status=row['status'])
        result['status']='passed' if all(r['status']=='passed' for r in result['cases']) else 'failed'
    except Cancelled:
        result['status']='cancelled';result['error']='Suite cancellation requested.'
    except (ValueError,RuntimeError) as exc:
        result['status']='error' if all(r['status']!='failed' for r in result['cases']) else 'failed';result['error']=str(exc)
    except Exception:
        result['status']='error';result['error']='Suite adapter failed. Check configuration and permissions; provider details are not included.'
    for row in result['cases']:
        if row['status']=='running':row['status']='not_completed'
    for endpoint,label in [(source,'source'),(target,'target')]:
        if endpoint is not None and getattr(endpoint,'last_job',None) and label not in result['jobs']:result['jobs'][label]=endpoint.last_job
    result['finished_at']=stamp();result['summary']={k:sum(r['status']==k for r in result['cases']) for k in ('passed','failed','error','not_completed')};result['execution_requested']=not fixture_mode;result['remote_submissions']=submissions;result['executed_by_app']=bool(submissions) or bool(result['jobs']) or any(r.get('target',{} ) and r['target'].get('executed_by_app') or r.get('source',{}) and r['source'].get('executed_by_app') for r in result['cases'])
    event('suite_finished',status=result['status']);return result


class DatabricksEndpoint:
    def __init__(self,config,token,transport,*,sleep=time.sleep,clock=time.monotonic,timeout=900):self.config=config;self.token=token;self.transport=transport;self.sleep=sleep;self.clock=clock;self.timeout=timeout;self.on_event=lambda event:None
    def call(self,method,path,body=None):return self.transport(self.config['workspace_url'],self.token,method,path,body)
    def _cancel(self,path):
        try:self.call('POST',path,{})
        except Exception:pass
    def query(self,sql,cancel):
        read_query(sql)
        if not self.config.get('warehouse_id'):raise ValueError('A SQL warehouse is required for target queries.')
        body={'statement':sql,'warehouse_id':self.config['warehouse_id'],'wait_timeout':'0s','disposition':'INLINE','format':'JSON_ARRAY','row_limit':MAX_ROWS+1,'byte_limit':900000}
        for k in ('catalog','schema'):
            if self.config.get(k):body[k]=self.config[k]
        response=self.call('POST','/api/2.0/sql/statements',body);sid=response.get('statement_id','')
        if not re.fullmatch(r'[A-Za-z0-9-]{1,100}',sid):raise RuntimeError('Databricks returned an invalid statement ID.')
        self.on_event({'statement_id':sid,'query_sha256':hashlib.sha256(sql.encode()).hexdigest()})
        path='/api/2.0/sql/statements/'+sid;start=self.clock()
        while response.get('status',{}).get('state') in ('PENDING','RUNNING'):
            if cancel() or self.clock()-start>self.timeout:
                self._cancel(path+'/cancel');check_cancel(cancel);raise RuntimeError('Target query timed out; cancellation was requested.')
            self.sleep(1);response=self.call('GET',path)
        if response.get('status',{}).get('state')!='SUCCEEDED':raise RuntimeError('Target query did not succeed. Inspect statement '+sid+' in Databricks.')
        m=response.get('manifest',{});r=response.get('result',{});rows=r.get('data_array',[]);columns=[c['name'] for c in m.get('schema',{}).get('columns',[])]
        if m.get('truncated') or m.get('total_chunk_count',1)>1 or r.get('next_chunk_index') is not None or r.get('next_chunk_internal_link') or r.get('external_links') or len(rows)>MAX_ROWS:raise ValueError('Target result is truncated or exceeds the supported single-chunk/10,000-row limit; no pass was recorded.')
        if not columns or len(columns)!=len(set(columns)) or any(len(v)!=len(columns) for v in rows) or m.get('total_row_count')!=len(rows):raise ValueError('Target result is incomplete or has ambiguous columns.')
        data={'format':'json','content':json.dumps({'columns':columns,'rows':[dict(zip(columns,row)) for row in rows]})};dataset(data)
        return data,{'executed_by_app':True,'statement_id':sid,'workspace_url':self.config['workspace_url'],'warehouse_id':self.config['warehouse_id'],'catalog':self.config.get('catalog'),'schema':self.config.get('schema'),'query_sha256':hashlib.sha256(sql.encode()).hexdigest(),'completed_at':stamp(),'columns':m.get('schema',{}).get('columns',[])}
    def run_job(self,job_id,cancel):
        ident=str(job_id)
        if not re.fullmatch(r'[0-9]{1,20}',ident):raise ValueError('Target job ID must be numeric.')
        settings=self.call('GET','/api/2.1/jobs/get?job_id='+ident);settings_hash=sha(settings.get('settings',{}))
        response=self.call('POST','/api/2.1/jobs/run-now',{'job_id':int(ident),'idempotency_token':uuid.uuid4().hex});run_id=str(response.get('run_id',''))
        if not re.fullmatch(r'[0-9]{1,20}',run_id):raise RuntimeError('Databricks did not return a valid job run ID.')
        self.last_job={'job_id':ident,'run_id':run_id,'settings_sha256':settings_hash,'workspace_url':self.config['workspace_url'],'state':{'life_cycle_state':'PENDING'}}
        self.on_event({'job':self.last_job})
        start=self.clock()
        while True:
            response=self.call('GET','/api/2.1/jobs/runs/get?run_id='+run_id);state=response.get('state',{})
            self.last_job['state']={k:state.get(k) for k in ('life_cycle_state','result_state')}
            if state.get('life_cycle_state') in ('TERMINATED','SKIPPED','INTERNAL_ERROR'):
                if state.get('result_state')!='SUCCESS':raise RuntimeError('Target job did not succeed. Inspect Databricks run '+run_id+'.')
                return {'job_id':ident,'run_id':run_id,'settings_sha256':settings_hash,'state':{k:state.get(k) for k in ('life_cycle_state','result_state')},'workspace_url':self.config['workspace_url'],'completed_at':stamp()}
            if cancel() or self.clock()-start>self.timeout:
                try:self.call('POST','/api/2.1/jobs/runs/cancel',{'run_id':int(run_id)})
                except Exception:pass
                check_cancel(cancel);raise RuntimeError('Target job timed out; cancellation was requested.')
            self.sleep(2)


class SqlServerEndpoint:
    """Optional ODBC reader and narrowly allowlisted SQL Agent job starter."""
    def __init__(self,connection_string=None,allowed_jobs=None,connect=None,sleep=time.sleep,clock=time.monotonic,timeout=900):
        self.connection_string=connection_string or os.environ.get('CROSSLAKE_SQLSERVER_CONNECTION_STRING','');self.allowed_jobs=allowed_jobs if allowed_jobs is not None else json.loads(os.environ.get('CROSSLAKE_SQLSERVER_ALLOWED_JOBS','[]'));self.connect=connect;self.sleep=sleep;self.clock=clock;self.timeout=timeout;self.on_event=lambda event:None
        if not self.connection_string:raise ValueError('Configure CROSSLAKE_SQLSERVER_CONNECTION_STRING on the app/CI server; credentials are never saved in suite manifests.')
        if not isinstance(self.allowed_jobs,list) or any(not isinstance(j,str) for j in self.allowed_jobs):raise ValueError('CROSSLAKE_SQLSERVER_ALLOWED_JOBS must be a JSON array of exact job names.')
    def connection(self):
        if self.connect:return self.connect(self.connection_string)
        try:import pyodbc
        except ImportError:raise RuntimeError('Install pyodbc and Microsoft ODBC Driver 18 on the app/CI host to enable SQL Server execution.') from None
        try:return pyodbc.connect(self.connection_string,timeout=10,autocommit=True)
        except Exception:raise RuntimeError('SQL Server connection failed. Check driver, TLS, permissions and configured environment credentials.') from None
    def query(self,sql,cancel):
        read_query(sql);check_cancel(cancel);conn=self.connection()
        try:
            cur=conn.cursor();cur.timeout=60;cur.execute(sql);description=cur.description
            if not description:raise ValueError('Source query returned no result schema.')
            columns=[c[0] for c in description]
            if len(columns)!=len(set(columns)):raise ValueError('Source query returned duplicate column names.')
            rows=cur.fetchmany(MAX_ROWS+1);check_cancel(cancel)
            if len(rows)>MAX_ROWS:raise ValueError('Source result exceeds 10,000 rows; no pass was recorded.')
            def value(v):
                if v is None or isinstance(v,(str,int,bool)):return v
                if isinstance(v,Decimal):return str(v)
                if isinstance(v,float) and math.isfinite(v):return str(v)
                if hasattr(v,'isoformat'):return v.isoformat()
                raise ValueError('Source value type requires an explicit SQL conversion (for example binary data).')
            data={'format':'json','content':json.dumps({'columns':columns,'rows':[dict(zip(columns,map(value,row))) for row in rows]})};dataset(data)
            return data,{'executed_by_app':True,'provider':'sqlserver_odbc','query_sha256':hashlib.sha256(sql.encode()).hexdigest(),'completed_at':stamp(),'columns':[{'name':c[0],'python_type':getattr(c[1],'__name__','unknown')} for c in description]}
        except (ValueError,Cancelled):raise
        except Exception:raise RuntimeError('SQL Server query failed. Check source query, driver, permissions and timeout; provider details are not exposed.') from None
        finally:conn.close()
    def run_job(self,job_name,cancel):
        if job_name not in self.allowed_jobs:raise ValueError('Source job is not in CROSSLAKE_SQLSERVER_ALLOWED_JOBS. An administrator must allowlist its exact name.')
        conn=self.connection()
        try:
            cur=conn.cursor();cur.timeout=30;cur.execute('SELECT job_id, version_number, SYSDATETIME() FROM msdb.dbo.sysjobs WHERE name = ?',job_name);job=cur.fetchone()
            if not job:raise ValueError('Allowlisted source job was not found.')
            job_id,version,watermark=job;cur.execute('SELECT MAX(session_id) FROM msdb.dbo.syssessions');session=cur.fetchone()[0]
            cur.execute('SELECT start_execution_date, stop_execution_date FROM msdb.dbo.sysjobactivity WHERE job_id=? AND session_id=?',job_id,session);previous=cur.fetchone()
            if previous and previous[0] and previous[1] is None:raise ValueError('Source SQL Agent job is already running; no additional run was started.')
            check_cancel(cancel);cur.execute('EXEC msdb.dbo.sp_start_job @job_name=?',job_name);self.last_job={'provider':'sqlserver_agent','job_name':job_name,'job_id':str(job_id),'job_version':version,'outcome':'unknown'};self.on_event({'job':self.last_job});start=self.clock();started=None
            while True:
                cur.execute('SELECT MAX(session_id) FROM msdb.dbo.syssessions')
                if cur.fetchone()[0]!=session:raise RuntimeError('SQL Agent restarted during the source run; outcome is unknown.')
                cur.execute('SELECT start_execution_date,stop_execution_date,job_history_id FROM msdb.dbo.sysjobactivity WHERE job_id=? AND session_id=?',job_id,session);activity=cur.fetchone()
                if activity and activity[0] and activity[0]>=watermark-timedelta(seconds=1) and (not previous or activity[0]!=previous[0]):
                    if started is not None and activity[0]!=started:raise RuntimeError('Source job activity changed before its outcome could be verified. Inspect SQL Agent history.')
                    started=activity[0]
                    self.last_job['started_at_server_local']=str(started)
                    if activity[1] and activity[2]:
                        cur.execute('SELECT run_status,step_id FROM msdb.dbo.sysjobhistory WHERE instance_id=? AND job_id=?',activity[2],job_id);history=cur.fetchone()
                        if history and history[1]==0:
                            if history[0]!=1:raise RuntimeError('Source SQL Agent job did not succeed. Inspect its history in SQL Server.')
                            return {'provider':'sqlserver_agent','job_name':job_name,'job_id':str(job_id),'job_version':version,'history_instance_id':activity[2],'started_at_server_local':str(started),'finished_at_server_local':str(activity[1]),'completed_at':stamp()}
                if cancel() or self.clock()-start>self.timeout:
                    if started and activity and activity[0]==started and activity[1] is None:
                        try:cur.execute('EXEC msdb.dbo.sp_stop_job @job_name=?',job_name)
                        except Exception:pass
                    check_cancel(cancel);raise RuntimeError('Source job timed out; inspect SQL Agent for final state.')
                self.sleep(2)
        except (ValueError,RuntimeError,Cancelled):raise
        except Exception:raise RuntimeError('SQL Agent operation failed. Check allowlist, job permissions and SQL Server Agent availability.') from None
        finally:conn.close()
