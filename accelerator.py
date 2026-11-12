"""Migration toolkit: synthetic fixtures, mapping, bundles, and bounded workspace checks."""
import hashlib
import json
import re
import uuid
import threading
from functools import wraps

def serialized(method):
    @wraps(method)
    def wrapped(self,*args,**kwargs):
        with self.lock:return method(self,*args,**kwargs)
    return wrapped
from pathlib import Path, PurePosixPath
from legacy import sql_inventory
from ssis import inventory
from core import mask_sql
from validation import dataset

ROOT=Path(__file__).parent

def read_query(sql):
    if not isinstance(sql,str) or not sql.strip() or len(sql)>20000:raise ValueError('Supply a SELECT query up to 20,000 characters.')
    masked=mask_sql(sql).strip().rstrip(';').strip()
    if ';' in masked or not re.match(r'^(SELECT|WITH)\b',masked,re.I):raise ValueError('Use one SELECT or WITH query; multiple statements are not supported.')
    if re.search(r'\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|COPY|CALL|EXEC|GRANT|REVOKE|INTO|PUT|REMOVE)\b',masked,re.I):raise ValueError('Validation accepts read queries only.')
    return sql

class Toolkit:
    def __init__(self,runs,settings):
        self.runs=runs;self.settings=settings;self.lock=threading.RLock()
        with runs.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS toolkit_operations(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,body TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS dependency_mappings(run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,body TEXT NOT NULL)')

    def suite(self):
        files=[{'name':'NullsAndDuplicates.sql','sql':"SELECT * FROM (VALUES (1, CAST(10.25 AS DECIMAL(12,2))), (1, CAST(10.25 AS DECIMAL(12,2))), (2, CAST(NULL AS DECIMAL(12,2)))) AS sales(Id, Amount);"},
               {'name':'ReadSales.sql','sql':'CREATE PROCEDURE dbo.ReadSales @MinimumAmount DECIMAL(12,2) = 0 AS BEGIN SET NOCOUNT ON; SELECT Id, Amount FROM dbo.Sales WHERE Amount >= @MinimumAmount; END;'},
               {'name':'IncrementalLoad.sql','sql':'SELECT Id, ModifiedAt FROM dbo.Customers WHERE ModifiedAt > @Watermark;'},
               {'name':'Lookup.sql','sql':'SELECT c.Id, r.RegionName FROM dbo.Customers c LEFT JOIN dbo.Regions r ON c.RegionId = r.Id;'}]
        sample=ROOT/'samples/ssis/ReadOnlySalesSmokeTest/ReadOnlySalesSmokeTest.dtsx'
        if sample.exists():files.append({'name':sample.name,'sql':sample.read_text()})
        files.append({'name':'UnsupportedScript.dtsx','sql':'<DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts" DTS:ObjectName="UnsupportedScript" DTS:ExecutableType="Microsoft.Package"><DTS:Executables><DTS:Executable DTS:refId="Package\\Script" DTS:ObjectName="Script" DTS:ExecutableType="Microsoft.ScriptTask" /></DTS:Executables></DTS:Executable>'})
        return {'files':files,'scenarios':[
            {'source':files[0]['name'],'expected':'3 rows including duplicate Id=1 and a NULL Amount; decimal sum 20.50'},
            {'source':files[1]['name'],'expected':'Review procedure parameter and SQL scripting support on the target runtime.'},
            {'source':files[2]['name'],'expected':'Bind watermark and map source table before execution; external dependency.'},
            {'source':files[3]['name'],'expected':'Map both tables; compare unmatched lookup rows and duplicate keys.'},
            {'source':'ReadOnlySalesSmokeTest.dtsx','expected':'Active rows 4, non-null amounts 3, total 37.75; see samples expected CSVs.'},
            {'source':'UnsupportedScript.dtsx','expected':'Script rewrite finding; never classify this fixture as executable migration success.'}],
            'note':'Synthetic conversion/readiness regression inputs. Creating a suite run does not execute these workloads or verify output values.'}

    def checks(self):
        from validation import definition,compare
        suite=self.suite();checks=[]
        for f in suite['files']:
            if f['name'].endswith('.dtsx'):
                inv=inventory(f)
                passed=bool(inv['tasks'])
                if f['name']=='UnsupportedScript.dtsx':passed=any(x['code']=='ssis-script' for x in inv['findings'])
            else:
                inv=sql_inventory(f['sql']);passed=inv['object_type']=='stored_procedure' if f['name']=='ReadSales.sql' else bool(f['sql'])
            checks.append({'source':f['name'],'check':'Inventory recognizes fixture and required rewrite findings','passed':passed})
        case=definition({'title':'Duplicates and NULL regression','source':'fixture.sql','columns':[{'name':'id','type':'integer'},{'name':'amount','type':'decimal'}]})
        expected={'format':'csv','content':'id,amount\n1,10.25\n1,10.25\n2,\\N\n'}
        actual={'format':'json','content':json.dumps([{'id':1,'amount':'10.25'},{'id':2,'amount':None},{'id':1,'amount':'10.25'}])}
        checks.append({'check':'Comparator preserves duplicates, NULLs, decimal values across formats','passed':compare(case,expected,actual)['status']=='passed'})
        actual['content']=json.dumps([{'id':1,'amount':'10.25'},{'id':2,'amount':None},{'id':2,'amount':None}])
        checks.append({'check':'Comparator rejects changed duplicate distribution','passed':compare(case,expected,actual)['status']=='failed'})
        return {'checks':checks,'passed':all(c['passed'] for c in checks),'scope':'Local inventory and comparator regressions only. Use the saved suite run to test conversion; live output correctness requires Databricks validation.'}

    def dependencies(self,run_id,payload=None):
        run=self.runs.get(run_id);references=[]
        for f in run['files']:
            if f['name'].lower().endswith('.dtsx'):
                inv=inventory(f)
                references.extend({'source':f['name'],'name':c['name'],'kind':'connection','detail':c['type']} for c in inv['connections'])
                references.extend({'source':f['name'],'name':t['name'],'kind':'task','detail':t['type']} for t in inv['tasks'])
                for statement in inv['embedded_sql']:
                    references.extend({'source':f['name'],**d,'kind':'sql_reference'} for d in sql_inventory(statement['sql'])['dependencies'])
            else:references.extend({'source':f['name'],**d,'kind':'sql_reference'} for d in sql_inventory(f['sql'])['dependencies'])
        with self.runs.db() as db:
            if payload is not None:
                mappings=payload.get('mappings')
                if not isinstance(mappings,list) or len(mappings)>500:raise ValueError('Supply at most 500 mappings.')
                allowed={(r['source'],r['name'],r['kind']) for r in references}
                for m in mappings:
                    if not isinstance(m,dict) or (m.get('source'),m.get('name'),m.get('kind')) not in allowed:raise ValueError('Map only inventoried references.')
                    if any(not isinstance(m.get(k,''),str) or len(m.get(k,''))>1000 for k in ('target','notes')):raise ValueError('Mapping text is limited to 1000 characters.')
                db.execute('INSERT OR REPLACE INTO dependency_mappings VALUES(?,?)',(run_id,json.dumps(mappings)))
            row=db.execute('SELECT body FROM dependency_mappings WHERE run_id=?',(run_id,)).fetchone()
        return {'references':references,'mappings':json.loads(row[0]) if row else [],'limitations':'Textual inventory; dynamic SQL, database metadata, and project-level bindings require review. Mappings document intent and do not rewrite code. Store secret references only, never credentials.'}

    def package(self,run_id):
        run=self.runs.get(run_id);conversion=run.get('conversion') or {};files=conversion.get('files',[])
        if not files:raise ValueError('Convert this migration run before packaging.')
        out=[]
        for f in files:
            p=PurePosixPath(f['name'].replace('\\','/'))
            if p.is_absolute() or '..' in p.parts or any(x.startswith('.') for x in p.parts):raise ValueError('Unsafe artifact path.')
            out.append({'name':'artifacts/'+str(p),'sql':f['sql']})
        jobs={}
        for i,f in enumerate(list(out)):
            if f['name'].endswith('.py') and f['sql'].startswith('# Databricks notebook source'):
                jobs['notebook_'+str(i+1)]={'name':'migration-'+run_id[:8]+'-'+str(i+1),'tasks':[{'task_key':'reviewed_notebook','notebook_task':{'notebook_path':'${workspace.file_path}/'+f['name'],'source':'WORKSPACE','base_parameters':{'migration_reviewed':'false'}},'environment_key':'default'}],'environments':[{'environment_key':'default','spec':{'client':'1'}}]}
        if jobs:out.append({'name':'resources/notebooks.json','sql':json.dumps({'resources':{'jobs':jobs}},indent=2)})
        mapping=self.dependencies(run_id)
        manifest={'run_id':run_id,'status':run['status'],'artifacts':[{'path':f['name'],'sha256':hashlib.sha256(f['sql'].encode()).hexdigest()} for f in out],
                  'mappings':mapping,'validation':self.runs.validation_cases(run_id),'sources':run['sources'],'deployable':False}
        if not jobs:out.append({'name':'resources/notebooks.json','sql':json.dumps({'resources':{'jobs':{}}})})
        out.extend([{'name':'migration-manifest.json','sql':json.dumps(manifest,indent=2)},
                    {'name':'databricks.yml','sql':'bundle:\n  name: migration-'+run_id[:8]+'\n\ninclude:\n  - resources/*.json\n\n# Add reviewed job resources after resolving notebook paths and dependencies.\n# Authentication comes from your Databricks CLI profile, never this bundle.\ntargets:\n  dev:\n    default: true\n    mode: development\n  prod:\n    mode: production\n'},
                    {'name':'DEPLOYMENT.md','sql':'# Migration handoff\n\nThis is a deployment review package. Recognized Databricks Python notebooks receive separate serverless job definitions, each gated by migration_reviewed=false. Jobs have no schedule. Task order, compute support, and pipeline equivalence require review.\n\n1. Verify manifest hashes, mapping targets, source reviews, and current validation evidence.\n2. Configure a Databricks CLI profile for the chosen environment.\n3. Review generated resources/notebooks.json. Separate jobs do not infer SSIS orchestration. Set migration_reviewed=true only after reviewing each notebook. Define task dependencies, parameters, and compute for your workspace. SQL scripts need a reviewed SQL task or notebook wrapper.\n4. Run `databricks bundle validate -t dev`, then `databricks bundle deploy -t dev`.\n5. Test results and failure/retry behavior before configuring prod workspace/run_as and deployment.\n\nGenerated engine workflow templates remain drafts. Credentials and source result datasets are excluded.\n'}])
        return {'files':out,'report':{'run_id':run_id,'review_required':True}}

    def _save(self,op):
        with self.runs.db() as db:
            if not db.execute('SELECT 1 FROM toolkit_operations WHERE id=?',(op['id'],)).fetchone() and db.execute('SELECT COUNT(*) FROM toolkit_operations WHERE run_id=?',(op['run_id'],)).fetchone()[0]>=100:raise ValueError('Maximum 100 tracked operations per migration run.')
            db.execute('INSERT OR REPLACE INTO toolkit_operations VALUES(?,?,?)',(op['id'],op['run_id'],json.dumps(op)))
        return {k:v for k,v in op.items() if k!='expected'}

    def operations(self,run_id):
        self.runs.get(run_id,False)
        with self.runs.db() as db:return {'operations':[{k:v for k,v in json.loads(r[0]).items() if k!='expected'} for r in db.execute('SELECT body FROM toolkit_operations WHERE run_id=? ORDER BY rowid DESC LIMIT 50',(run_id,))]}

    def query(self,run_id,payload):
        if payload.get('allow_compute') is not True:raise ValueError('Confirm target query execution and potential warehouse cost.')
        case=self.runs.validation_case(run_id,payload.get('case_id',''))
        if payload.get('version')!=case['version']:raise ValueError('Reload the current test definition.')
        with self.runs.db() as db:self.runs._validation_source(db,run_id,case['source'])
        sql=read_query(case['definition']['target_sql'])
        if json.loads(case['definition']['parameters']):raise ValueError('Use a fully bound target query and empty parameters for this execution mode.')
        expected=dataset(payload.get('expected'))
        from validation import read_dataset
        read_dataset(expected)
        ref=payload.get('expected_ref','')
        if not isinstance(ref,str) or not ref.strip() or len(ref)>2000:raise ValueError('Record the source export reference.')
        config,token,revision=self.settings._credentials(payload)
        if not config['warehouse_id']:raise ValueError('Select a SQL warehouse in settings.')
        body={'statement':sql,'warehouse_id':config['warehouse_id'],'wait_timeout':'0s','disposition':'INLINE','format':'JSON_ARRAY','row_limit':10001,'byte_limit':900000}
        for key in ('catalog','schema'):
            if config.get(key):body[key]=config[key]
        result=self.settings.transport(config['workspace_url'],token,'POST','/api/2.0/sql/statements',body)
        sid=result.get('statement_id','')
        if not re.fullmatch(r'[a-fA-F0-9-]{1,100}',sid):raise RuntimeError('Workspace did not return a valid statement ID.')
        op={'id':uuid.uuid4().hex,'run_id':run_id,'kind':'validation','remote_id':sid,'host':config['workspace_url'],'revision':revision,'warehouse_id':config['warehouse_id'],'case_id':case['id'],'version':case['version'],'expected':expected,'expected_ref':ref,'query_sha256':hashlib.sha256(sql.encode()).hexdigest(),'state':'PENDING'}
        self._save(op)
        return self.refresh(run_id,{'id':op['id'],'revision':revision})

    def attach(self,run_id,payload):
        self.runs.get(run_id,False)
        remote=str(payload.get('remote_id',''))
        if not re.fullmatch(r'[0-9]{1,20}',remote):raise ValueError('Enter a numeric Databricks job run ID.')
        config,token,revision=self.settings._credentials(payload)
        op={'id':uuid.uuid4().hex,'run_id':run_id,'kind':'job','remote_id':remote,'host':config['workspace_url'],'revision':revision,'state':'ATTACHED'}
        self._save(op)
        return self.refresh(run_id,{'id':op['id'],'revision':revision})

    @serialized
    def refresh(self,run_id,payload):
        with self.runs.db() as db:row=db.execute('SELECT body FROM toolkit_operations WHERE id=? AND run_id=?',(payload.get('id'),run_id)).fetchone()
        if not row:raise KeyError('Operation not found.')
        op=json.loads(row[0]);config,token,revision=self.settings._credentials(payload)
        if config['workspace_url']!=op['host'] or revision!=op['revision']:raise ValueError('Workspace settings changed. Restore the original settings or start a new operation.')
        if op['kind']=='job':
            result=self.settings.transport(op['host'],token,'GET','/api/2.1/jobs/runs/get?run_id='+op['remote_id'])
            op.update(state=result.get('state',{}),tasks=[{'task_key':t.get('task_key'),'state':t.get('state',{})} for t in result.get('tasks',[])],start_time=result.get('start_time'),end_time=result.get('end_time'))
        elif op.get('attempt_id'):return op
        else:
            result=self.settings.transport(op['host'],token,'GET','/api/2.0/sql/statements/'+op['remote_id'])
            op['state']=result.get('status',{}).get('state','UNKNOWN')
            if op['state'] in ('FAILED','CANCELED','CLOSED'):op['message']='Target statement '+op['state'].lower()+'. Inspect the statement in Databricks; no comparison was saved.'
            if op['state']=='SUCCEEDED':
                manifest=result.get('manifest',{});data=result.get('result',{});rows=data.get('data_array',[])
                if manifest.get('truncated') or manifest.get('total_chunk_count',1)>1 or data.get('external_links') or manifest.get('total_row_count',0)>10000 or data.get('next_chunk_index') is not None or data.get('next_chunk_internal_link') or len(rows)>10000:raise ValueError('Result exceeds the supported single-chunk/10,000 row limit. No comparison was saved; narrow the validation query.')
                columns=[c['name'] for c in manifest.get('schema',{}).get('columns',[])]
                if not columns or len(set(columns))!=len(columns) or any(len(r)!=len(columns) for r in rows) or manifest.get('total_row_count')!=len(rows):raise ValueError('Incomplete or ambiguous target result; comparison refused.')
                actual={'format':'json','content':json.dumps({'columns':columns,'rows':[dict(zip(columns,r)) for r in rows]})}
                report=self.runs.evaluate_case(run_id,op['case_id'],{'version':op['version'],'expected':op['expected'],'actual':actual,'expected_ref':op['expected_ref'],'actual_ref':'Databricks statement '+op['remote_id'],'evidence':'Target executed by accelerator; source export provenance remains user supplied.'},target_execution={k:op[k] for k in ('host','warehouse_id','remote_id','query_sha256','revision')})
                op['attempt_id']=report['id'];op['comparison_status']=report['status'];op.pop('expected',None)
        from monitoring import now
        op['checked_at']=now()
        return self._save(op)
