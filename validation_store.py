"""Versioned test definitions and immutable uploaded-result comparison attempts."""
import hashlib
import json
import uuid
from validation import definition,dataset,compare

SCHEMA='''
CREATE TABLE IF NOT EXISTS validation_cases (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL, source TEXT NOT NULL, definition_json TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(run_id,source) REFERENCES sources(run_id,name) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS validation_attempts (
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES validation_cases(id) ON DELETE CASCADE,
 run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE, case_version INTEGER NOT NULL,
 created_at TEXT NOT NULL, inputs_json TEXT NOT NULL, report_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS validation_case_run ON validation_cases(run_id);
CREATE INDEX IF NOT EXISTS validation_attempt_case ON validation_attempts(case_id,created_at);
'''

class ValidationStore:
    def _validation_source(self,db,run_id,source):
        from monitoring import ACTIVE,Conflict
        run=self.require(db,run_id)
        if run['status'] in ACTIVE:raise Conflict('Wait for the active run to finish before changing or comparing validation cases.')
        row=db.execute('SELECT * FROM sources WHERE run_id=? AND name=?',(run_id,source)).fetchone()
        if row is None:raise KeyError('Source not found in this run.')
        if row['kind'] not in ('sql','sql_script','stored_procedure','ssis'):raise ValueError('Select a SQL source or SSIS package. SSIS cases validate an explicit output query, not the entire pipeline.')
        return row

    def save_case(self,run_id,payload,case_id=None):
        from monitoring import now,encoded,Conflict
        case=definition(payload);timestamp=now()
        with self.db() as db:
            self._validation_source(db,run_id,case['source'])
            if case_id:
                current=db.execute('SELECT * FROM validation_cases WHERE id=? AND run_id=?',(case_id,run_id)).fetchone()
                if current is None:raise KeyError('Test case not found.')
                if payload.get('version')!=current['version']:raise Conflict('This test case changed in another tab. Reload it before saving.')
                if case['source']!=current['source']:raise ValueError('A saved test case cannot be moved to another source. Create a separate case.')
                db.execute('UPDATE validation_cases SET definition_json=?,version=version+1,updated_at=? WHERE id=?',(encoded(case),timestamp,case_id))
            else:
                if db.execute('SELECT COUNT(*) FROM validation_cases WHERE run_id=?',(run_id,)).fetchone()[0]>=100:raise ValueError('A run supports up to 100 test cases.')
                case_id=uuid.uuid4().hex
                db.execute('INSERT INTO validation_cases(id,run_id,source,definition_json,created_at,updated_at) VALUES(?,?,?,?,?,?)',(case_id,run_id,case['source'],encoded(case),timestamp,timestamp))
            db.execute('UPDATE runs SET updated_at=? WHERE id=?',(timestamp,run_id))
            self.event(db,run_id,('Updated' if case_id and payload.get('version') else 'Created')+' validation case: '+case['title'],case['source'])
        return self.validation_case(run_id,case_id)

    def validation_cases(self,run_id):
        with self.db() as db:
            self.require(db,run_id)
            rows=db.execute('SELECT * FROM validation_cases WHERE run_id=? ORDER BY created_at,id',(run_id,)).fetchall()
            cases=[]
            for row in rows:
                latest=db.execute('SELECT id,case_version,created_at,report_json FROM validation_attempts WHERE case_id=? ORDER BY rowid DESC LIMIT 1',(row['id'],)).fetchone()
                case={**dict(row),'definition':json.loads(row['definition_json'])};case.pop('definition_json')
                case['latest_attempt']={'id':latest['id'],'case_version':latest['case_version'],'created_at':latest['created_at'],'status':json.loads(latest['report_json'])['status'],'stale':latest['case_version']!=row['version']} if latest else None
                cases.append(case)
        return {'cases':cases,'execution_mode':'uploaded_result_comparison','databricks_connected':False,
                'summary':{'cases':len(cases),'passed':sum(c['latest_attempt'] and not c['latest_attempt']['stale'] and c['latest_attempt']['status']=='passed' or False for c in cases),
                           'failed':sum(c['latest_attempt'] and not c['latest_attempt']['stale'] and c['latest_attempt']['status']=='failed' or False for c in cases),
                           'error':sum(c['latest_attempt'] and not c['latest_attempt']['stale'] and c['latest_attempt']['status']=='error' or False for c in cases),
                           'not_compared':sum(not c['latest_attempt'] or c['latest_attempt']['stale'] for c in cases)}}

    def validation_case(self,run_id,case_id):
        with self.db() as db:
            self.require(db,run_id)
            row=db.execute('SELECT * FROM validation_cases WHERE id=? AND run_id=?',(case_id,run_id)).fetchone()
            if row is None:raise KeyError('Test case not found.')
            case={**dict(row),'definition':json.loads(row['definition_json'])};case.pop('definition_json')
            attempts=[]
            for a in db.execute('SELECT id,case_version,created_at,report_json FROM validation_attempts WHERE case_id=? ORDER BY rowid DESC LIMIT 50',(case_id,)):
                report=json.loads(a['report_json'])
                attempts.append({'id':a['id'],'case_version':a['case_version'],'created_at':a['created_at'],'status':report['status'],'stale':a['case_version']!=row['version']})
            case['attempts']=attempts
            case['attempt_total']=db.execute('SELECT COUNT(*) FROM validation_attempts WHERE case_id=?',(case_id,)).fetchone()[0]
        return case

    def evaluate_case(self,run_id,case_id,payload,*,target_execution=None):
        from monitoring import now,encoded,Conflict
        if not isinstance(payload,dict):raise ValueError('Invalid comparison request.')
        expected=dataset(payload.get('expected'));actual=dataset(payload.get('actual'))
        evidence={}
        for key,limit in [('expected_ref',2000),('actual_ref',2000),('evidence',4000)]:
            value=payload.get(key)
            if not isinstance(value,str) or not value.strip() or len(value)>limit:raise ValueError('Record '+key.replace('_',' ')+' as nonempty text (up to '+str(limit)+' characters).')
            evidence[key]=value.strip()
        with self.db() as db:
            current=db.execute('SELECT * FROM validation_cases WHERE id=? AND run_id=?',(case_id,run_id)).fetchone()
            if current is None:raise KeyError('Test case not found.')
            source=dict(self._validation_source(db,run_id,current['source']))
            run=self.require(db,run_id)
            source_files=json.loads(run['source_json'])
            source_hash=next(hashlib.sha256(f['sql'].encode()).hexdigest() for f in source_files if f['name']==current['source'])
            artifacts=json.loads(run['conversion_json'])['files'] if run['conversion_json'] else []
            artifact_hashes=[{'name':f['name'],'sha256':hashlib.sha256(f['sql'].encode()).hexdigest()} for f in artifacts]
            if payload.get('version')!=current['version']:raise Conflict('Test definition changed. Reload it before comparing.')
            case=json.loads(current['definition_json']);version=current['version']
        report=compare(case,expected,actual);attempt_id=uuid.uuid4().hex;timestamp=now()
        report.update(id=attempt_id,run_id=run_id,case_id=case_id,case_version=version,created_at=timestamp,definition=case,
                      source_conversion_status=source['status'],source_review=source['review'],source_snapshot={'source_sha256':source_hash,'run_artifacts':artifact_hashes},provenance={**evidence,'verified_by_app':False},
                      inputs={side:{'format':data['format'],'bytes':len(data['content'].encode()),'sha256':hashlib.sha256(data['content'].encode()).hexdigest()} for side,data in [('expected',expected),('actual',actual)]},
                      validated_on_databricks=False,deployable=False)
        if target_execution is not None:
            report['target_execution']=target_execution
            report['executed_by_app']=True
            report['limitations']=['Only the recorded target query was executed by the app. Source export provenance is user supplied. This is not whole-pipeline validation.']
        with self.db() as db:
            current=db.execute('SELECT version,source FROM validation_cases WHERE id=? AND run_id=?',(case_id,run_id)).fetchone()
            if current is None:raise KeyError('Test case no longer exists.')
            self._validation_source(db,run_id,current['source'])
            if current['version']!=version:raise Conflict('Test definition changed while comparing. Run the comparison again.')
            if db.execute('SELECT COUNT(*) FROM validation_attempts WHERE run_id=?',(run_id,)).fetchone()[0]>=500:raise ValueError('A run supports up to 500 saved comparisons. Start a separate migration run for more.')
            db.execute('INSERT INTO validation_attempts(id,case_id,run_id,case_version,created_at,inputs_json,report_json) VALUES(?,?,?,?,?,?,?)',
                       (attempt_id,case_id,run_id,version,timestamp,encoded({'expected':expected,'actual':actual}),encoded(report)))
            db.execute('UPDATE runs SET updated_at=? WHERE id=?',(timestamp,run_id))
            self.event(db,run_id,f'{"Databricks target" if target_execution else "Uploaded-result"} comparison {report["status"]}: {case["title"]}, version {version}. '+('Target query executed; source provenance is user supplied.' if target_execution else 'No SQL was executed.'),case['source'])
        return report

    def validation_attempt(self,run_id,attempt_id,include_inputs=False):
        with self.db() as db:
            self.require(db,run_id)
            row=db.execute('SELECT * FROM validation_attempts WHERE id=? AND run_id=?',(attempt_id,run_id)).fetchone()
            if row is None:raise KeyError('Comparison not found.')
            current=db.execute('SELECT version FROM validation_cases WHERE id=?',(row['case_id'],)).fetchone()
            result={'report':json.loads(row['report_json']),'current_case_version':current['version'],'stale':current['version']!=row['case_version']}
            if include_inputs:result['recorded_inputs']=json.loads(row['inputs_json'])
        return result
