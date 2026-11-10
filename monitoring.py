"""Durable local migration runs and a single background conversion worker."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
import uuid
from core import assess, convert, validate_files

ACTIVE=('uploaded','assessing','queued','converting')

def now():return datetime.now(timezone.utc).isoformat(timespec='milliseconds')
def encoded(value):return json.dumps(value,ensure_ascii=False)
def seconds(start,end):
    return max(0,round((datetime.fromisoformat(end)-datetime.fromisoformat(start)).total_seconds(),2)) if start and end else None

class Conflict(ValueError):pass

class RunService:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.lock=threading.RLock();self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='migration')
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, label TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                source_json TEXT NOT NULL, assessment_json TEXT, conversion_json TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS sources (
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE, name TEXT NOT NULL,
                kind TEXT NOT NULL, status TEXT NOT NULL, complexity TEXT, findings INTEGER NOT NULL DEFAULT 0,
                engine TEXT, started_at TEXT, finished_at TEXT, error TEXT,
                review TEXT NOT NULL DEFAULT 'pending', review_note TEXT NOT NULL DEFAULT '',
                validation TEXT NOT NULL DEFAULT 'not_run', validation_note TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(run_id,name));
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL, source TEXT, message TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS run_time ON runs(created_at);
            ''')
            # Work is never automatically replayed after a crash: conversion may have produced incomplete outputs.
            interrupted=db.execute("SELECT id FROM runs WHERE status IN ('uploaded','assessing','queued','converting')").fetchall()
            for row in interrupted:
                timestamp=now();reason='Server restarted before this run finished. Create a new run to retry.'
                db.execute("UPDATE runs SET status='interrupted',error=?,finished_at=?,updated_at=? WHERE id=?",(reason,timestamp,timestamp,row['id']))
                db.execute("UPDATE sources SET status='blocked',error=?,finished_at=? WHERE run_id=? AND status IN ('uploaded','assessing','queued','converting')",(reason,timestamp,row['id']))
                self.event(db,row['id'],reason)
        if os.name != 'nt':self.path.chmod(0o600)

    @contextmanager
    def db(self):
        with self.lock:
            db=sqlite3.connect(self.path,timeout=30);db.row_factory=sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            try:
                with db:yield db
            finally:db.close()

    def event(self,db,run_id,message,source=None):
        db.execute('INSERT INTO events(run_id,created_at,source,message) VALUES(?,?,?,?)',(run_id,now(),source,message))

    def require(self,db,run_id):
        row=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if row is None:raise KeyError('Run not found.')
        return row

    def create(self,payload):
        files=validate_files(payload)
        label=payload.get('label') or f'Migration · {files[0]["name"]}'
        if not isinstance(label,str) or not 1<=len(label.strip())<=120:raise ValueError('Run label must contain 1–120 characters.')
        run_id=uuid.uuid4().hex;timestamp=now()
        with self.db() as db:
            if db.execute("SELECT COUNT(*) FROM runs WHERE status IN ('uploaded','assessing','queued','converting')").fetchone()[0]>=20:
                raise Conflict('There are 20 active runs. Wait before submitting another.')
            db.execute('INSERT INTO runs(id,label,status,created_at,updated_at,source_json) VALUES(?,?,?,?,?,?)',
                       (run_id,label.strip(),'uploaded',timestamp,timestamp,encoded(files)))
            for item in files:
                db.execute('INSERT INTO sources(run_id,name,kind,status) VALUES(?,?,?,?)',(run_id,item['name'],item['kind'],'uploaded'))
            self.event(db,run_id,f'{len(files)} source(s) saved. Assessment queued.')
        self.executor.submit(self._assess,run_id)
        return self.get(run_id,False)

    def _assess(self,run_id):
        try:
            with self.db() as db:
                run=self.require(db,run_id);files=json.loads(run['source_json']);timestamp=now()
                db.execute("UPDATE runs SET status='assessing',started_at=?,updated_at=? WHERE id=?",(timestamp,timestamp,run_id))
                db.execute("UPDATE sources SET status='assessing' WHERE run_id=?",(run_id,));self.event(db,run_id,'Assessing source code and package inventory.')
            report=assess(files)
            with self.db() as db:
                timestamp=now()
                db.execute("UPDATE runs SET status='assessed',assessment_json=?,finished_at=?,updated_at=? WHERE id=?",(encoded(report),timestamp,timestamp,run_id))
                for item in report['files']:
                    db.execute("UPDATE sources SET status='assessed',kind=?,complexity=?,findings=? WHERE run_id=? AND name=?",(item.get('object_type','sql'),item['complexity'],len(item['findings']),run_id,item['name']))
                self.event(db,run_id,'Assessment finished. Conversion has not started.')
        except Exception as exc:self._fail(run_id,str(exc))

    def start_conversion(self,run_id):
        with self.db() as db:
            run=self.require(db,run_id)
            if run['status']!='assessed':raise Conflict('Only an assessed run can start conversion. Use a new run to retry completed or failed work.')
            timestamp=now()
            db.execute("UPDATE runs SET status='queued',finished_at=NULL,started_at=NULL,updated_at=? WHERE id=?",(timestamp,run_id))
            db.execute("UPDATE sources SET status='queued' WHERE run_id=?",(run_id,));self.event(db,run_id,'Conversion queued. Sources will be processed in upload order.')
        self.executor.submit(self._convert,run_id)
        return self.get(run_id,False)

    def _fail(self,run_id,error):
        with self.db() as db:
            timestamp=now();error=error[:16000]
            db.execute("UPDATE runs SET status='failed',error=?,finished_at=?,updated_at=? WHERE id=?",(error,timestamp,timestamp,run_id))
            db.execute("UPDATE sources SET status='blocked',error=?,finished_at=? WHERE run_id=? AND status IN ('uploaded','assessing','queued','converting')",(error,timestamp,run_id))
            self.event(db,run_id,'Run failed: '+error[:1000])

    def _convert(self,run_id):
        artifacts=[];coverage=[];logs=[];engines=[];names=set()
        try:
            with self.db() as db:
                run=self.require(db,run_id);files=json.loads(run['source_json']);timestamp=now()
                db.execute("UPDATE runs SET status='converting',started_at=?,updated_at=? WHERE id=?",(timestamp,timestamp,run_id));self.event(db,run_id,'Conversion started.')
            for item in files:
                source=item['name'];timestamp=now()
                with self.db() as db:
                    db.execute("UPDATE sources SET status='converting',started_at=? WHERE run_id=? AND name=?",(timestamp,run_id,source))
                    db.execute('UPDATE runs SET updated_at=? WHERE id=?',(timestamp,run_id));self.event(db,run_id,'Converting source.',source)
                error=None;engine=None;result=None
                try:
                    result=convert([item]);engine=result['engine']
                    candidate=[f for f in result['files'] if f['name']!='migration-coverage.json']
                    if any(f['name'].casefold() in names for f in candidate):raise RuntimeError('Output name collision. Split these sources into separate runs.')
                    if len(artifacts)+len(candidate)>195 or sum(len(f['sql'].encode()) for f in artifacts+candidate)>16*1024*1024:
                        raise RuntimeError('Combined output exceeds this run’s 195-artifact / 16 MB limit. Split sources into smaller runs.')
                    names.update(f['name'].casefold() for f in candidate);artifacts.extend(candidate)
                    if engine not in engines:engines.append(engine)
                    if result.get('conversion_errors','').strip() not in ('','[]'):
                        error='Engine reported errors. Inspect the saved diagnostics and generated artifacts.'
                    logs.append({'source':source,'diagnostics':result.get('diagnostics',''),'errors':result.get('conversion_errors','')})
                except Exception as exc:error=str(exc)[:16000]
                state='blocked' if error else 'converted'
                coverage.append({'source':source,'status':state,'engine':engine,'error':error})
                aggregate=self._aggregate(artifacts,coverage,logs,engines)
                with self.db() as db:
                    timestamp=now()
                    db.execute('UPDATE sources SET status=?,engine=?,error=?,finished_at=? WHERE run_id=? AND name=?',(state,engine,error,timestamp,run_id,source))
                    db.execute('UPDATE runs SET conversion_json=?,updated_at=? WHERE id=?',(encoded(aggregate),timestamp,run_id))
                    self.event(db,run_id,'Blocked: '+error[:1000] if error else 'Generated artifacts saved; awaiting review.',source)
            blocked=sum(c['status']=='blocked' for c in coverage)
            status='failed' if blocked==len(files) else 'partial' if blocked else 'completed'
            with self.db() as db:
                timestamp=now()
                db.execute('UPDATE runs SET status=?,finished_at=?,updated_at=?,error=? WHERE id=?',(status,timestamp,timestamp,'All sources were blocked.' if status=='failed' else None,run_id))
                self.event(db,run_id,f'Conversion finished: {len(files)-blocked} generated, {blocked} blocked. Databricks validation is manual.')
        except Exception as exc:self._fail(run_id,str(exc))

    def _aggregate(self,artifacts,coverage,logs,engines):
        report={'coverage':coverage,'deployable':False,'validated_on_databricks':False}
        return {'engine':', '.join(engines),'files':[*artifacts,{'name':'migration-coverage.json','sql':json.dumps(report,indent=2),'kind':'coverage_report'}],
                'coverage':coverage,'diagnostics':json.dumps(logs,indent=2),'conversion_errors':json.dumps([c for c in coverage if c['error']],indent=2),
                'status':'Review blocked sources' if any(c['error'] for c in coverage) else 'Generated — review required',
                'validated_on_databricks':False,'deployable':False,
                'warning':'These artifacts have not been executed on Databricks. Review engine diagnostics, SSIS notebook gates, and workflow paths. Dashboard validation entries are manual attestations, not automated verification.'}

    def get(self,run_id,include_payload=True):
        with self.db() as db:
            run=dict(self.require(db,run_id));sources=[dict(s) for s in db.execute('SELECT * FROM sources WHERE run_id=? ORDER BY rowid',(run_id,))]
            events=[dict(e) for e in db.execute('SELECT * FROM events WHERE run_id=? ORDER BY id DESC LIMIT 250',(run_id,))]
        result={k:v for k,v in run.items() if not k.endswith('_json')}
        result.update(sources=sources,events=events,duration_seconds=seconds(run['started_at'],run['finished_at'] or now()))
        result['progress']={'total':len(sources),'finished':sum(s['status'] in ('converted','blocked') for s in sources),
                            'converted':sum(s['status']=='converted' for s in sources),'blocked':sum(s['status']=='blocked' for s in sources)}
        if include_payload:
            result.update(files=json.loads(run['source_json']),assessment=json.loads(run['assessment_json']) if run['assessment_json'] else None,
                          conversion=json.loads(run['conversion_json']) if run['conversion_json'] else None)
        return result

    def listing(self,limit=25,offset=0,status='',query=''):
        limit=max(1,min(50,int(limit)));offset=max(0,int(offset));clauses=[];params=[]
        if status:
            if status=='active':clauses.append("status IN ('uploaded','assessing','queued','converting')")
            else:clauses.append('status=?');params.append(status)
        if query:clauses.append('(label LIKE ? OR id LIKE ?)');params.extend(['%'+query[:120]+'%']*2)
        where=' WHERE '+' AND '.join(clauses) if clauses else ''
        with self.db() as db:
            total=db.execute('SELECT COUNT(*) FROM runs'+where,params).fetchone()[0]
            rows=[dict(row) for row in db.execute('SELECT id,label,status,created_at,updated_at,started_at,finished_at,error FROM runs'+where+' ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?',(*params,limit,offset))]
            for row in rows:
                row['sources']=[dict(s) for s in db.execute('SELECT name,kind,status,engine,review,validation FROM sources WHERE run_id=?',(row['id'],))]
                row['duration_seconds']=seconds(row['started_at'],row['finished_at'] or now())
            stats={'runs':db.execute('SELECT COUNT(*) FROM runs').fetchone()[0],
                   'active':db.execute("SELECT COUNT(*) FROM runs WHERE status IN ('uploaded','assessing','queued','converting')").fetchone()[0],
                   'blocked':db.execute("SELECT COUNT(*) FROM sources WHERE status='blocked'").fetchone()[0],
                   'awaiting_review':db.execute("SELECT COUNT(*) FROM sources WHERE status='converted' AND review!='approved'").fetchone()[0],
                   'manual_passed':db.execute("SELECT COUNT(*) FROM sources WHERE validation='passed'").fetchone()[0]}
        return {'runs':rows,'total':total,'limit':limit,'offset':offset,'stats':stats,'databricks_connected':False}

    def review(self,run_id,payload):
        if not isinstance(payload,dict):raise ValueError('Invalid review update.')
        name=payload.get('source')
        if not isinstance(name,str) or not name:raise ValueError('Select a source to review.')
        review=payload.get('review');validation=payload.get('validation');note=payload.get('review_note','');evidence=payload.get('validation_note','')
        if review not in ('pending','in_review','approved') or validation not in ('not_run','passed','failed'):
            raise ValueError('Invalid review or manual-validation status.')
        if not all(isinstance(x,str) and len(x)<=4000 for x in (note,evidence)):raise ValueError('Notes must be text with at most 4,000 characters.')
        if validation!='not_run' and not evidence.strip():raise ValueError('Record test evidence or a failure reason for manual validation.')
        with self.db() as db:
            run=self.require(db,run_id)
            if run['status'] in ACTIVE:raise Conflict('Wait until the active run finishes before recording review.')
            row=db.execute('SELECT * FROM sources WHERE run_id=? AND name=?',(run_id,name)).fetchone()
            if row is None:raise KeyError('Source not found.')
            if payload.get('version')!=row['version']:raise Conflict('This review changed in another tab. Reload the run before saving.')
            if (review=='approved' or validation=='passed') and row['status']!='converted':raise Conflict('Blocked or unconverted sources cannot be approved or marked as passed.')
            if validation=='passed' and review!='approved':raise ValueError('Approve the code review before recording passed manual validation.')
            db.execute('UPDATE sources SET review=?,review_note=?,validation=?,validation_note=?,version=version+1 WHERE run_id=? AND name=?',
                       (review,note.strip(),validation,evidence.strip(),run_id,name))
            db.execute('UPDATE runs SET updated_at=? WHERE id=?',(now(),run_id))
            self.event(db,run_id,f'Review: {review}; manual validation: {validation}. Review note: {note.strip()} Evidence: {evidence.strip()}',name)
        return self.get(run_id,False)

    def delete(self,run_id):
        with self.db() as db:
            run=self.require(db,run_id)
            if run['status'] in ACTIVE:raise Conflict('Active runs cannot be deleted.')
            db.execute('DELETE FROM runs WHERE id=?',(run_id,))
        return {'deleted':run_id}

    def close(self):self.executor.shutdown(wait=True)
