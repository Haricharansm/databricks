"""Application-level grouping, reviewed dependency plans and migration handoff bundles."""
from collections import defaultdict
from copy import deepcopy
import hashlib
from html import escape
import json
import re
import uuid
from monitoring import ACTIVE,Conflict,now
from insights import build
from lakeflow import digest,key
from ci_read_query import read_query
from runtime_audit import RUNTIME


def sha(value):return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
def label(value,field):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,49}',value):raise ValueError(field+' must start with a letter and use at most 50 letters/digits/underscores.')
    return value

def dag(members,edges):
    incoming={m:0 for m in members};seen=set()
    for edge in edges:
        pair=(edge.get('from'),edge.get('to'))
        if any(not isinstance(v,str) for v in pair) or pair[0] not in incoming or pair[1] not in incoming or pair[0]==pair[1] or pair in seen:raise ValueError('Dependencies require distinct known member runs and unique directed edges.')
        if type(edge.get('reviewed',False)) is not bool:raise ValueError('Dependency reviewed must be boolean.')
        seen.add(pair);incoming[pair[1]]+=1
    ready=[m for m in members if not incoming[m]];order=[]
    while ready:
        m=ready.pop(0);order.append(m)
        for start,end in seen:
            if start==m:
                incoming[end]-=1
                if incoming[end]==0:ready.append(end)
    if len(order)!=len(members):raise ValueError('Application dependency graph has a cycle.')
    return order

class ApplicationService:
    def __init__(self,runs,lakeflow):
        self.runs=runs;self.lakeflow=lakeflow
        with runs.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS migration_applications(id TEXT PRIMARY KEY,version INTEGER NOT NULL,profile_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS application_members(application_id TEXT NOT NULL REFERENCES migration_applications(id) ON DELETE CASCADE,run_id TEXT NOT NULL REFERENCES runs(id),PRIMARY KEY(application_id,run_id));''')
    def listing(self):
        with self.runs.db() as db:rows=db.execute('SELECT * FROM migration_applications ORDER BY updated_at DESC LIMIT 100').fetchall()
        return {'applications':[{'id':r['id'],'version':r['version'],'name':json.loads(r['profile_json'])['name']} for r in rows]}
    def create(self,payload):
        name=label(payload.get('name'),'Application name');ident=uuid.uuid4().hex
        p={'name':name,'description':'','run_ids':[],'labels':{},'dependencies':[],'architecture':[],'schedules':[],'prefix':'crosslake','environment':'dev','release_reviewed':False,'review_note':''}
        with self.runs.db() as db:db.execute('INSERT INTO migration_applications VALUES(?,?,?,?,?)',(ident,1,json.dumps(p),now(),now()))
        return self.get(ident)
    def _row(self,ident):
        with self.runs.db() as db:r=db.execute('SELECT * FROM migration_applications WHERE id=?',(ident,)).fetchone()
        if not r:raise KeyError('Application not found.')
        return dict(r)
    def _profile(self,p):
        if not isinstance(p,dict):raise ValueError('Application profile must be an object.')
        clean={'name':label(p.get('name'),'Application name'),'prefix':label(p.get('prefix'),'Naming prefix'),'environment':label(p.get('environment'),'Environment name')}
        ids=p.get('run_ids');edges=p.get('dependencies',[])
        if not isinstance(ids,list) or not 0<=len(ids)<=20 or any(not isinstance(i,str) or not re.fullmatch('[a-f0-9]{32}',i) for i in ids) or len(set(ids))!=len(ids):raise ValueError('Select up to 20 unique saved migration runs.')
        if not isinstance(edges,list) or len(edges)>100 or any(not isinstance(e,dict) or set(e)-{'from','to','reviewed'} for e in edges):raise ValueError('Supply at most 100 dependency edges.')
        dag(ids,edges);clean.update(run_ids=ids,dependencies=edges)
        for i in ids:self.runs.get(i,False)
        labels=p.get('labels',{})
        if not isinstance(labels,dict) or set(labels)-set(ids):raise ValueError('Labels must refer to member run IDs.')
        labels={i:label(labels.get(i,'asset_'+i[:8]),'Asset business label') for i in ids}
        if len({v.casefold() for v in labels.values()})!=len(labels):raise ValueError('Business labels must be unique, ignoring case.')
        clean['labels']=labels
        for field,limit in [('description',4000),('review_note',4000)]:
            v=p.get(field,'')
            if not isinstance(v,str) or len(v)>limit:raise ValueError(field+' is too long.')
            clean[field]=v
        if type(p.get('release_reviewed',False)) is not bool:raise ValueError('Release review must be boolean.')
        clean['release_reviewed']=p.get('release_reviewed',False)
        architecture=p.get('architecture',[])
        if not isinstance(architecture,list) or len(architecture)>100:raise ValueError('Architecture register supports up to 100 mappings.')
        for a in architecture:
            if not isinstance(a,dict) or set(a)-{'run_id','source','target','decision','notes'} or a.get('run_id') not in ids:raise ValueError('Architecture mappings must reference member runs.')
            if a.get('decision') not in ('preserve','replace','consolidate','manual','unresolved'):raise ValueError('Invalid architecture decision.')
            if a.get('decision') in ('preserve','replace','consolidate') and any(not isinstance(a.get(k),str) or not a[k].strip() for k in ('source','target')):raise ValueError('Resolved architecture decisions require source and target descriptions.')
            if any(not isinstance(a.get(k,''),str) or len(a.get(k,''))>2000 for k in ('source','target','notes')):raise ValueError('Architecture fields must be text up to 2000 characters.')
        clean['architecture']=architecture
        schedules=p.get('schedules',[])
        if not isinstance(schedules,list) or len(schedules)>20:raise ValueError('Supply up to 20 explicit scheduler mappings.')
        seen=set()
        for entry in schedules:
            if not isinstance(entry,dict) or set(entry)-{'run_id','source_scheduler','source_reference','quartz_cron_expression','timezone_id','reviewed','notes'} or entry.get('run_id') not in ids or entry['run_id'] in seen:raise ValueError('Use one scheduler mapping per member run.')
            if entry.get('source_scheduler') not in ('SQLAgent','WindowsTaskScheduler','enterprise','manual'):raise ValueError('Unsupported scheduler source label.')
            if not isinstance(entry.get('source_reference'),str) or not entry['source_reference'].strip() or len(entry['source_reference'])>1000:raise ValueError('Record the original scheduler/job reference.')
            if type(entry.get('reviewed',False)) is not bool:raise ValueError('Scheduler mapping review must be boolean.')
            # Reuse bounded Quartz/timezone validation; never infer source calendar semantics.
            validated=self.lakeflow._validate({'catalog':'main','schema':'migration','task_mappings':[],'schedule':{k:entry.get(k) for k in ('quartz_cron_expression','timezone_id')}})
            entry={**entry,**{k:validated['schedule'][k] for k in ('quartz_cron_expression','timezone_id')}}
            seen.add(entry['run_id'])
        clean['schedules']=schedules
        if len(json.dumps(clean).encode())>300000:raise ValueError('Application profile exceeds 300 KB.')
        return clean
    def save(self,ident,payload):
        p=self._profile(payload.get('profile'))
        with self.runs.db() as db:
            row=db.execute('SELECT version FROM migration_applications WHERE id=?',(ident,)).fetchone()
            if row is None:raise KeyError('Application not found.')
            if payload.get('version')!=row['version']:raise Conflict('Application changed in another tab. Reload before saving.')
            db.execute('UPDATE migration_applications SET version=version+1,profile_json=?,updated_at=? WHERE id=?',(json.dumps(p),now(),ident));db.execute('DELETE FROM application_members WHERE application_id=?',(ident,))
            db.executemany('INSERT INTO application_members VALUES(?,?)',[(ident,i) for i in p['run_ids']])
        return self.get(ident)
    def get(self,ident):
        row=self._row(ident);p=json.loads(row['profile_json']);members=[];groups=defaultdict(list);unresolved=[];inventories=[];unit_budget=1200;analysis_truncated=False
        for rid in p['run_ids']:
            run=self.runs.get(rid);active=run['status'] in ACTIVE
            members.append({'run_id':rid,'label':p['labels'].get(rid,'asset_'+rid[:8]),'source_label':run['label'],'status':run['status'],'fingerprint':digest(run),'sources':[{k:s[k] for k in ('name','status','review')} for s in run['sources']]})
            if active:unresolved.append({'run_id':rid,'reason':'Run still active'});continue
            if any(f['name'].lower().endswith('.dtsx') for f in run['files']):
                lakeflow=self.lakeflow.get(rid);members[-1].update(lakeflow_profile_version=lakeflow['version'],lakeflow_profile_sha256=sha(lakeflow['profile']))
            report=build(run,{'entrypoints':[]},None)
            analysis_truncated |= report['truncated'] or len(report['queries'])>unit_budget
            for q in report['queries'][:unit_budget]:
                groups[q['query_hash']].append({'run_id':rid,'source':q['source'],'line':q['line'],'location':q['location'],'sql_preview':q['sql_preview'],'preview_truncated':q['preview_truncated']})
            unit_budget=max(0,unit_budget-len(report['queries']))
            unresolved.extend({'run_id':rid,'reference':u} for u in report['unresolved_package_references'])
            inventories.append({'run_id':rid,'entities':report['entities'][:200],'lineage':{'nodes':report['lineage']['nodes'][:400],'edges':report['lineage']['edges'][:800],'method':report['lineage']['method']},'scope':report['scope'],'truncated':report['truncated'] or len(report['entities'])>200 or len(report['lineage']['nodes'])>400 or len(report['lineage']['edges'])>800})
        declarations=defaultdict(set)
        for inv in inventories:
            for entity in inv['entities']:
                if entity['definitions']:declarations[entity['name'].casefold()].add(inv['run_id'])
        references=[]
        for inv in inventories:
            for entity in inv['entities']:
                providers=declarations.get(entity['name'].casefold(),set())
                for provider in providers-{inv['run_id']}:
                    if entity['references']:
                        references.append({'consumer_run_id':inv['run_id'],'provider_run_id':provider,'object':entity['name'],'usage':sorted({r['usage'] for r in entity['references']}),'ambiguous':len(providers)>1,'method':'Exact SQL object identity in uploaded static inventories; not an inferred execution edge.'})
        duplicates=[{'query_hash':h,'occurrences':items,'review_required':True,'recommendation':'Review matching SQL, parameter contracts, schema context and side effects before extracting shared logic. Text matches do not prove semantic equivalence.'} for h,items in groups.items() if len(items)>1]
        # Pin observed run content as well as profile/review versions; reports are not release approval.
        content_sha=sha({'profile':p,'members':members})
        evidence=[]
        with self.runs.db() as db:
            for rid in p['run_ids']:
                for table,kind in [('lakeflow_live','lakeflow'),('golden_suites','golden_suite')]:
                    if db.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?',(table,)).fetchone():
                        evidence.extend({'run_id':rid,'kind':kind,'id':r['id'],'status':r['status']} for r in db.execute('SELECT id,status FROM '+table+' WHERE run_id=? ORDER BY rowid DESC LIMIT 10',(rid,)))
        return {'id':ident,'version':row['version'],'profile':p,'members':members,'duplicates':duplicates,'inventories':inventories,'cross_asset_references':references,'unresolved_references':unresolved,'evidence':evidence,'fingerprint':content_sha,'limits':{'members':20,'duplicate_groups':len(duplicates),'max_sql_units':1200,'analysis_truncated':analysis_truncated},'limitations':['Static dependency and duplicate analysis only; uploaded definitions and dynamic SQL can be incomplete.','Application edges are user-entered success dependencies; stored procedure calls are not automatically translated.','No data/schema provisioning, external scheduler activation or automatic deduplication is performed.','Application exports require explicit review. Existing per-run validation evidence is not application equivalence.']}
    def export(self,ident,payload):
        value=self.get(ident)
        if payload.get('version')!=value['version'] or payload.get('fingerprint')!=value['fingerprint']:raise Conflict('Application/profile/member content changed. Reload the report before exporting.')
        if payload.get('include_code') is not True:raise ValueError('Confirm inclusion of generated code, query previews and application metadata in this local export.')
        p=value['profile'];files=[];jobs={};pipelines={};master=[];blockers=[];asset_jobs={};name_registry=[]
        if not p['run_ids']:blockers.append('Add member runs.')
        if not p['release_reviewed'] or not p['review_note'].strip():blockers.append('Record an application release review and review note.')
        if any(not e.get('reviewed') for e in p['dependencies']):blockers.append('Review each application execution dependency.')
        if any(not e.get('reviewed') for e in p['schedules']):blockers.append('Review each imported scheduler mapping.')
        if any(a['decision'] in ('manual','unresolved') for a in p['architecture']):blockers.append('Resolve manual/unresolved architecture mappings before executable release.')
        if value['unresolved_references']:blockers.append('Resolve uploaded package references before executable application release.')
        for member in value['members']:
            rid=member['run_id'];run=self.runs.get(rid)
            if run['status'] in ACTIVE or any(s['status']!='converted' or s['review']!='approved' for s in run['sources']):blockers.append(member['label']+': convert and approve every source.');continue
            if not any(f['name'].lower().endswith('.dtsx') for f in run['files']):blockers.append(member['label']+': SQL/procedure-only members remain architecture dependencies; provide reviewed orchestration before executable application release.');continue
            try:bundle=self.lakeflow.generate(rid)
            except (ValueError,Conflict) as exc:blockers.append(member['label']+': '+str(exc));continue
            if any(x['blockers'] for x in bundle['report']['packages']):blockers.extend(member['label']+': '+reason for x in bundle['report']['packages'] for reason in x['blockers']);continue
            resource=json.loads(next(f['sql'] for f in bundle['files'] if f['name']=='resources/lakeflow.json'))['resources'];prefix='r_'+rid+'_';asset_jobs[rid]=[]
            for f in bundle['files']:
                if f['name'].endswith('.py'):files.append({'name':'assets/'+rid+'/'+f['name'],'sql':f['sql']})
            def refs(obj):
                if isinstance(obj,dict):return {k:refs(v) for k,v in obj.items()}
                if isinstance(obj,list):return [refs(v) for v in obj]
                if isinstance(obj,str):
                    obj=obj.replace('${workspace.file_path}/','${workspace.file_path}/assets/'+rid+'/')
                    for kind in ('jobs','pipelines'):obj=re.sub(r'\$\{resources\.'+kind+r'\.([A-Za-z0-9_]+)\.id\}',lambda m:'${resources.'+kind+'.'+prefix+m[1]+'.id}',obj)
                    return obj
                return obj
            for resource_key,spec in resource['pipelines'].items():
                spec=refs(spec);spec['name']=p['prefix']+'_'+member['label']+'_pipeline_'+resource_key[-8:]+'_'+p['environment'];pipelines[prefix+resource_key]=spec
            for index,(resource_key,spec) in enumerate(resource['jobs'].items()):
                new_key=prefix+resource_key;spec=refs(spec);spec['name']=p['prefix']+'_'+member['label']+('_'+str(index+1) if len(resource['jobs'])>1 else '')+'_'+p['environment'];name_registry.append({'run_id':rid,'source':run['label'],'resource_key':new_key,'target_job_name':spec['name']})
                for task in spec['tasks']:
                    if 'notebook_task' in task and task['notebook_task']['notebook_path'].startswith('${workspace.file_path}'):
                        task['notebook_task']['base_parameters']['crosslake_application_id']=ident
                if spec.get('schedule') and not any(e['run_id']==rid for e in p['schedules']):blockers.append(member['label']+': existing source profile schedule requires an explicit reviewed application scheduler handoff.')
                # Child Jobs have no independent schedule; the application master owns its trigger.
                spec.pop('schedule',None);jobs[new_key]=spec;asset_jobs[rid].append(new_key)
        for rid in dag(p['run_ids'],p['dependencies']):
            for child in asset_jobs.get(rid,[]):
                task={'task_key':key(child),'run_job_task':{'job_id':'${resources.jobs.'+child+'.id}'},'run_if':'ALL_SUCCESS','max_retries':0}
                dependencies=[{'task_key':key(k)} for e in p['dependencies'] if e['to']==rid for k in asset_jobs.get(e['from'],[])]
                if dependencies:task['depends_on']=dependencies
                master.append(task)
        # Several distinct source schedules cannot be safely combined into one master trigger.
        patterns={(x['quartz_cron_expression'],x['timezone_id']) for x in p['schedules']}
        if len(patterns)>1:blockers.append('Multiple distinct schedules require separate reviewed master workflows; no trigger was guessed.')
        if len(master)>100:blockers.append('Application master exceeds 100 child tasks.')
        if not blockers and master:
            parent={'name':p['prefix']+'_'+p['name']+'_master_'+p['environment'],'tasks':master,'max_concurrent_runs':1}
            if patterns:
                cron,zone=next(iter(patterns));parent['schedule']={'quartz_cron_expression':cron,'timezone_id':zone,'pause_status':'PAUSED'}
            jobs['application_'+ident[:12]]=parent
        if blockers:
            jobs={};pipelines={};files=[]
        architecture={'application_id':ident,'source_assets':value['members'],'source_inventories':value['inventories'],'cross_asset_references':value['cross_asset_references'],'target_names':name_registry,'reviewed_mappings':p['architecture'],'execution_dependencies':p['dependencies'],'scheduler_handoff':p['schedules'],'unresolved_references':value['unresolved_references']}
        release={'application_id':ident,'version':value['version'],'fingerprint':value['fingerprint'],'status':'blocked' if blockers else 'review_required','blockers':blockers,'source_hashes':[{k:m[k] for k in ('run_id','fingerprint')} for m in value['members']],'execution_approved':False,'schedules_paused':True,'generated_at':now()}
        files.extend([{'name':'resources/application.json','sql':json.dumps({'resources':{'jobs':jobs,'pipelines':pipelines}},indent=2)},{'name':'databricks.yml','sql':'bundle:\n  name: '+p['prefix']+'_'+p['name']+'\ninclude:\n  - resources/*.json\ntargets:\n  dev:\n    default: true\n    mode: development\n'},{'name':'application-manifest.json','sql':json.dumps(release,indent=2)},{'name':'architecture.json','sql':json.dumps(architecture,indent=2)},{'name':'duplicate-analysis.json','sql':json.dumps(value['duplicates'],indent=2)},{'name':'runtime/crosslake_runtime.py','sql':RUNTIME},{'name':'README.md','sql':HANDOFF}])
        def table(headers,rows):
            return '<table><thead><tr>'+''.join('<th>'+escape(h)+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+escape(str(v))+'</td>' for v in row)+'</tr>' for row in rows)+'</tbody></table>'
        style='body{font:14px/1.6 Arial,sans-serif;color:#1e3558;background:#f5f7fb;margin:40px}h1{border-bottom:4px solid #ff7518;padding-bottom:12px}h2{margin-top:30px}table{width:100%;border-collapse:collapse;background:white}td,th{text-align:left;vertical-align:top;padding:10px;border:1px solid #dce2eb}th{background:#1e3558;color:white}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:18px}.brand{font-size:22px;font-weight:bold}.brand span{color:#ff7518}'
        html='<html><head><meta charset="utf-8"><title>Crosslake application handoff</title><style>'+style+'</style></head><body><div class="brand">Crosslake<span>.</span> | SAXON</div><h1>'+escape(p['name'])+'</h1><p>'+escape(p['description'])+'</p><p>Release: <strong>'+release['status']+'</strong> · Schedules paused · Explicit sandbox validation required</p>'
        html+='<h2>Application assets</h2>'+table(['Business asset','Source migration','Status','Run ID'],[(m['label'],m['source_label'],m['status'],m['run_id']) for m in value['members']])
        html+='<h2>Target naming register</h2>'+table(['Source','Target Job name','Stable resource key'],[(n['source'],n['target_job_name'],n['resource_key']) for n in name_registry])
        html+='<h2>Source-to-target decisions</h2>'+table(['Source','Target','Decision','Notes'],[(m.get('source',''),m.get('target',''),m['decision'],m.get('notes','')) for m in p['architecture']])
        html+='<h2>Reviewed execution dependencies</h2>'+table(['Upstream','Downstream','Reviewed'],[(p['labels'].get(e['from'],e['from']),p['labels'].get(e['to'],e['to']),e.get('reviewed',False)) for e in p['dependencies']])
        html+='<h2>Duplicate analysis</h2>'+table(['SQL hash','Occurrences','Locations'],[(g['query_hash'][:16],len(g['occurrences']),'; '.join(p['labels'].get(o['run_id'],o['run_id'])+' / '+o['source']+' line '+str(o['line']) for o in g['occurrences'])) for g in value['duplicates']])+'<p>Matching normalized SQL is a refactoring candidate, not proof of semantic equivalence. No caller was rewritten.</p>'
        html+='<h2>Scheduler handoff</h2>'+table(['Source scheduler','Original reference','Quartz / timezone','Reviewed'],[(s['source_scheduler'],s['source_reference'],s['quartz_cron_expression']+' / '+s['timezone_id'],s.get('reviewed',False)) for s in p['schedules']])
        html+='<h2>Execution and validation evidence</h2>'+table(['Run ID','Kind','Evidence ID','Status'],[(e['run_id'],e['kind'],e['id'],e['status']) for e in value['evidence']])
        html+='<h2>Blockers</h2><pre>'+escape(json.dumps(blockers,indent=2))+'</pre><details><summary>Complete architecture and manifest evidence</summary><pre>'+escape(json.dumps({'manifest':release,'architecture':architecture,'limitations':value['limitations']},indent=2))+'</pre></details></body></html>'
        files.append({'name':'application-report.html','sql':html})
        for group in value['duplicates'][:30]:
            q=group['occurrences'][0]
            if q['preview_truncated']:continue
            try:read_query(q['sql_preview'])
            except ValueError:continue
            code='# Review-only extraction candidate. No caller was rewritten. Verify schema/session/parameter contracts.\ndef read_shared(spark):\n    return spark.sql('+repr(q['sql_preview'])+')\n'
            files.append({'name':'refactoring/candidate_'+group['query_hash'][:16]+'.py','sql':code})
        if len(files)>190 or sum(len(f['sql'].encode()) for f in files)>8*1024*1024:raise ValueError('Application export exceeds 190 files / 8 MB. Use smaller application groups.')
        # Recheck snapshots to prevent an export racing reviews or conversions.
        if self.get(ident)['fingerprint']!=value['fingerprint']:raise Conflict('Application assets changed during export. Reload.')
        return {'files':files,'report':release}

HANDOFF='''# Crosslake application migration handoff

Read application-manifest.json and application-report.html first. After reviewing code and validating the bundle, explicitly enable reviewed notebook migration_reviewed parameters and pipeline review configuration for sandbox execution; nothing is enabled automatically. A blocked export contains planning/report material only and no executable Jobs or pipelines. Resolve every blocker and regenerate. A review_required export still requires code/runtime validation and explicit deployment.

The master Job calls child Jobs with reviewed ALL_SUCCESS dependencies. Source SQL/procedure-only assets, stored procedure calls, dynamic/cross-package variable contracts and unresolved mappings require explicit migration; they are not silently converted by this layer. Architecture records document intent and do not rewrite SQL or provision source/target data.

Review target catalog/schema, serverless environment versions, parameters, permissions and generated names. Configure a Databricks CLI profile. Run databricks bundle validate -t dev before any deployment. Code review gates remain false and all schedules are paused; enable only reviewed child notebook/pipeline gates before a sandbox test. Never use these synthetic samples against production tables. No credentials are included.

Duplicate candidates preserve literals and use normalized lexical SQL hashes. Equal text is not proof of semantic equivalence across connections, schemas, parameters or side effects. Refactoring candidates are inert functions for human review; no source/caller is changed automatically. No savings are estimated.

The shared audit library emits only approved execution identifiers, times, status and error class. It never logs SQL text, input values or exception messages. Configure stdout/Python logging forwarding to your enterprise collector externally; no Datadog/Splunk calls or credentials are configured by Crosslake.

Scheduler mappings are user-translated Quartz expressions with original scheduler references. SQL Agent/Windows/enterprise calendars, event triggers and holiday behavior are not inferred. Review timezone/DST and source schedules before enabling anything. Distinct schedule patterns block a single-master release.

Existing per-run evidence is linked in the report, not proof of full application parity. Run representative golden suites after sandbox execution, including failures/retries, and approve the complete application before production promotion. Customer data seeding and environment provisioning remain manual.
'''
