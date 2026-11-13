"""Reviewable Lakeflow Jobs and Spark Declarative Pipeline bundle generation.

No workspace mutation or execution happens here. SSIS semantics are supported only for
flat AND/success DAGs, static independent SQL tasks, and explicitly mapped copy flows.
"""
import ast
import hashlib
import json
from pathlib import PurePosixPath
import re
from ssis import inventory,parse_package,local,prop,package_key
from core import mask_sql
from bladebridge import GUARD
from monitoring import ACTIVE,Conflict

IDENT=re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,127}')
NODE=re.compile(r'^#\s*Processing node\s+(.+?),\s*type\s+(\w+)',re.M)


def key(text):
    return re.sub(r'[^A-Za-z0-9_]+','_',text).strip('_')[:45]+'_'+hashlib.sha256(text.encode()).hexdigest()[:8]


def identifier(value,label):
    if not isinstance(value,str) or not IDENT.fullmatch(value):raise ValueError(label+' must be a simple SQL identifier (letters, digits, underscore).')
    return value


def table_name(value):
    if not isinstance(value,str) or len(value.split('.'))!=3:raise ValueError('Source table must be catalog.schema.table.')
    for part in value.split('.'):identifier(part,'Source table part')
    return value


def digest(run):
    payload={'files':run.get('files'),'conversion':run.get('conversion')}
    return hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()


def static_sql(cell):
    """Extract only standalone static SQL assignments/calls, never evaluate Python."""
    tree=ast.parse(cell);values={};queries=[]
    for node in tree.body:
        if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name):
            value=node.value
            if isinstance(value,ast.Constant) and isinstance(value.value,str):values[node.targets[0].id]=value.value
            elif isinstance(value,ast.JoinedStr) and all(isinstance(v,ast.Constant) and isinstance(v.value,str) for v in value.values):values[node.targets[0].id]=''.join(v.value for v in value.values)
            else:raise ValueError('SQL cell contains runtime expressions or result assignments.')
        elif isinstance(node,ast.Expr) and isinstance(node.value,ast.Call):
            call=node.value
            if not (isinstance(call.func,ast.Attribute) and isinstance(call.func.value,ast.Name) and call.func.value.id=='spark' and call.func.attr=='sql' and len(call.args)==1 and not call.keywords):raise ValueError('SQL cell contains an unrecognized executable call.')
            arg=call.args[0]
            sql=values.get(arg.id) if isinstance(arg,ast.Name) else arg.value if isinstance(arg,ast.Constant) and isinstance(arg.value,str) else None
            if not sql:raise ValueError('SQL is not a static string.')
            masked=mask_sql(sql)
            if re.search(r'#|@\w|\b(?:TEMP|TEMPORARY|DECLARE)\b|(?:^|;)\s*(?:USE|SET)\b',masked,re.I):raise ValueError('Session state, temporary objects or parameter bindings require a package-level rewrite.')
            queries.append(sql)
        else:raise ValueError('SQL cell contains unsupported Python statements.')
    if not queries:raise ValueError('No executable static SQL was found for this task.')
    return queries


def task_sql(source,task,artifacts):
    candidates=[a for a in artifacts if a.get('kind')=='notebook' and a['name'].startswith('notebooks/'+package_key(source)+'/') and a['name'].endswith('.py')]
    if len(candidates)!=1:raise ValueError('Convert this package to one Python notebook before extracting SQL tasks.')
    matches=[]
    for cell in candidates[0]['sql'].split('# COMMAND ----------'):
        marker=NODE.search(cell)
        if marker and marker.group(1).strip()==task['id']:
            if marker.group(2)!='EXECUTE_SQL':raise ValueError('Converted cell is not an Execute SQL task.')
            matches.append(cell)
    if len(matches)!=1:raise ValueError('Task-to-notebook mapping is absent or ambiguous; no task order was guessed.')
    return static_sql(matches[0])


def copy_flow(node):
    """Recognize one direct source -> destination path, without transforms/expressions."""
    components=[n for n in node.iter() if local(n.tag)=='component']
    paths=[n for n in node.iter() if local(n.tag)=='path']
    if len(components)!=2 or len(paths)!=1:raise ValueError('Declarative mapping supports exactly one direct source-to-destination path; transforms, joins and branches need rewrite.')
    sources=[c for c in components if c.get('componentClassID','').lower() in ('microsoft.oledbsource','microsoft.flatfilesource','dtsadapter.oledbsource','dtsadapter.flatfilesource')]
    destinations=[c for c in components if c.get('componentClassID','').lower() in ('microsoft.oledbdestination','microsoft.flatfiledestination','dtsadapter.oledbdestination','dtsadapter.flatfiledestination')]
    if len(sources)!=1 or len(destinations)!=1:raise ValueError('Only recognized OLE DB/flat-file copy flows are supported; REST, scripts and custom components need rewrite.')
    source,destination=sources[0],destinations[0]
    for part,component in [('startId',source),('endId',destination)]:
        ref=component.get('refId','')
        endpoint_tag='output' if part=='startId' else 'input'
        endpoints={n.get('refId') for n in component.iter() if local(n.tag)==endpoint_tag}
        if paths[0].get(part) not in endpoints or not ref or not paths[0].get(part,'').startswith(ref+'.'):raise ValueError('Data-flow lineage does not establish a direct source/destination path.')
    if any(local(n.tag) in ('PropertyExpression','Expression') or n.get('evaluateAsExpression','').lower()=='true' for n in node.iter()):raise ValueError('Data-flow expressions require manual translation.')
    for n in source.iter():
        if local(n.tag)=='property' and n.get('name') in ('SqlCommand','SqlCommandVariable','OpenRowsetVariable') and (n.text or '').strip():raise ValueError('Source SQL/filter is not a direct table copy; provide a reviewed notebook rewrite.')
    connected_output=next(n for n in source.iter() if local(n.tag)=='output' and n.get('refId')==paths[0].get('startId'))
    connected_input=next(n for n in destination.iter() if local(n.tag)=='input' and n.get('refId')==paths[0].get('endId'))
    out_columns=[n.get('name') for n in connected_output.iter() if local(n.tag)=='outputColumn']
    in_columns=[n for n in connected_input.iter() if local(n.tag)=='inputColumn']
    external={n.get('refId') or n.get('id'):n.get('name') for n in destination.iter() if local(n.tag)=='externalMetadataColumn'}
    if out_columns and in_columns:
        mapped=[external.get(n.get('externalMetadataColumnId'),n.get('name')) for n in in_columns]
        if mapped!=out_columns:raise ValueError('Column selection, ordering or renaming is not a direct identity copy; rewrite explicitly.')
    return {'source_component':source.get('name'),'destination_component':destination.get('name')}


def package_graph(item):
    inv=inventory(item);root=parse_package(item['sql'])
    nodes={prop(n,'refId') or prop(n,'DTSID'):n for n in root.iter() if n is not root and local(n.tag)=='Executable'}
    if prop(root,'MaxConcurrentExecutables','-1') not in ('','-1'):raise ValueError('Custom SSIS concurrency limits need manual orchestration.')
    if any(local(n.tag)=='EventHandler' or local(n.tag) in ('PropertyExpression','Expression') and (n.text or '').strip() for n in root.iter()):raise ValueError('Event handlers and expressions require manual orchestration.')
    tasks=inv['tasks'];ids={t['id']:t['id'] for t in tasks};ids.update({t['dts_id']:t['id'] for t in tasks if t['dts_id']})
    aliases={}
    for t in tasks:
        for alias in (t['id'],t['dts_id']):
            if not alias:continue
            if alias in aliases and aliases[alias]!=t['id']:raise ValueError('Ambiguous task GUID/reference alias.')
            aliases[alias]=t['id']
    if not tasks or len({t['id'] for t in tasks})!=len(tasks):raise ValueError('Empty package or duplicate task IDs.')
    if any(t['parent'] or 'LOOP' in t['type'].upper() or 'SEQUENCE' in t['type'].upper() for t in tasks):raise ValueError('Nested containers and loops require manual orchestration.')
    if inv['variables'] or inv['parameters'] or item.get('origin_project'):raise ValueError('Package/project parameters, variables and scope require explicit notebook orchestration; they are not inferred.')
    for n in root.iter():
        if prop(n,'Disable','').lower() in ('true','1') or prop(n,'TransactionOption','') in ('2','Required'):raise ValueError('Disabled executables or required SSIS transactions need manual orchestration.')
    edges=[]
    for c in inv['constraints']:
        if c['Expression'] or c['EvalOp'] not in ('','0','Constraint') or c['Value'] not in ('','0','Success') or c['LogicalAnd'].lower() in ('false','0'):raise ValueError('Failure/completion branches, expressions and OR joins are not converted.')
        if c['From'] not in ids or c['To'] not in ids:raise ValueError('Unresolved precedence edge.')
        edge=(ids[c['From']],ids[c['To']])
        if edge not in edges:edges.append(edge)
    incoming={t['id']:0 for t in tasks}
    for _,end in edges:incoming[end]+=1
    ready=[t['id'] for t in tasks if incoming[t['id']]==0];order=[]
    while ready:
        current=ready.pop(0);order.append(current)
        for start,end in edges:
            if start==current:
                incoming[end]-=1
                if incoming[end]==0:ready.append(end)
    if len(order)!=len(tasks):raise ValueError('Cyclic precedence graph.')
    return inv,nodes,edges,order


def pipeline_code(config):
    mode=config.get('mode','batch')
    if mode not in ('batch','streaming'):raise ValueError('Pipeline mode must be batch or streaming.')
    target=identifier(config.get('target_table'),'Pipeline target table')
    source_kind=config.get('source_kind')
    if source_kind=='table':
        source=table_name(config.get('source'))
        read=('spark.readStream' if mode=='streaming' else 'spark.read')+'.table('+repr(source)+')'
        notes='Streaming table sources must be Delta tables without unsupported change operations.' if mode=='streaming' else 'Batch mapping produces a refreshed materialized view, not SSIS append/upsert behavior.'
    elif source_kind=='files':
        source=config.get('source','')
        if not isinstance(source,str) or not re.fullmatch(r'/Volumes/[A-Za-z_][\w]*/[A-Za-z_][\w]*/[A-Za-z_][\w]*/[^\x00-\x1f]+',source) or any(x in ('.','..','') for x in source.split('/')[1:]):raise ValueError('File source must be a Unity Catalog /Volumes/catalog/schema/volume/path without traversal.')
        fmt=config.get('format')
        if fmt not in ('csv','json','parquet'):raise ValueError('Supported file formats: csv, json, parquet.')
        schema=config.get('schema_ddl','')
        if not isinstance(schema,str) or not schema.strip() or len(schema)>10000:raise ValueError('Declare a Spark schema DDL for file sources (up to 10,000 characters).')
        read='spark.readStream.format("cloudFiles").option("cloudFiles.format",'+repr(fmt)+')' if mode=='streaming' else 'spark.read.format('+repr(fmt)+')'
        read+='.schema('+repr(schema)+')'
        if fmt=='csv':read+='.option("header","true")'
        read+='.load('+repr(source)+')'
        notes='Files use an explicit schema; CSV assumes a header. Confirm delimiter, parsing, schema, malformed-record and destination semantics. '+('Streaming uses Auto Loader append ingestion.' if mode=='streaming' else 'Batch mapping refreshes a materialized view snapshot.')
    else:raise ValueError('Pipeline source_kind must be table or files.')
    decorator='table' if mode=='streaming' else 'materialized_view'
    code='# Generated Lakeflow Spark Declarative Pipeline. Review mappings and semantics before enabling.\nfrom pyspark import pipelines as dp\n\nif spark.conf.get("crosslake.migration_reviewed","false") != "true":\n    raise RuntimeError("Review SSIS mapping, schema, permissions and output parity; then set crosslake.migration_reviewed=true.")\n\n@dp.'+decorator+'(name='+repr(target)+')\ndef migrated_dataset():\n    return '+read+'\n'
    ast.parse(code)
    return code,notes


class LakeflowService:
    def __init__(self,runs):
        self.runs=runs
        with runs.db() as db:db.execute('CREATE TABLE IF NOT EXISTS lakeflow_profiles(run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,version INTEGER NOT NULL,body TEXT NOT NULL,fingerprint TEXT NOT NULL)')

    def _run(self,run_id):
        run=self.runs.get(run_id)
        if run['status'] in ACTIVE:raise Conflict('Wait for the migration run to finish before generating Lakeflow assets.')
        if not any(f['name'].lower().endswith('.dtsx') for f in run['files']):raise ValueError('Select a saved run containing SSIS packages.')
        return run

    def get(self,run_id):
        run=self._run(run_id);packages=[];mappings=[]
        for item in run['files']:
            if not item['name'].lower().endswith('.dtsx'):continue
            inv=inventory(item)
            packages.append({'source':item['name'],'tasks':inv['tasks'],'constraints':inv['constraints'],'findings':inv['findings']})
            for t in inv['tasks']:
                mappings.append({'source':item['name'],'task_id':t['id'],'kind':'auto_sql' if 'ExecuteSQLTask' in t['type'] else 'unmapped'})
        if len(mappings)>500:raise ValueError('Lakeflow supports at most 500 tasks per run; split this migration into smaller runs.')
        with self.runs.db() as db:row=db.execute('SELECT * FROM lakeflow_profiles WHERE run_id=?',(run_id,)).fetchone()
        profile=json.loads(row['body']) if row else {'catalog':'main','schema':'migration','max_retries':0,'min_retry_interval_millis':60000,'schedule':None,'task_mappings':mappings}
        return {'run_id':run_id,'version':row['version'] if row else 0,'profile':profile,'packages':packages,'fingerprint':digest(run),'stale':bool(row and row['fingerprint']!=digest(run))}

    def _validate(self,profile):
        if not isinstance(profile,dict):raise ValueError('Supply a Lakeflow profile object.')
        result={'catalog':identifier(profile.get('catalog'),'Catalog'),'schema':identifier(profile.get('schema'),'Schema')}
        for name,default,limit in [('max_retries',0,10),('min_retry_interval_millis',60000,86400000)]:
            value=profile.get(name,default)
            if type(value)!=int or not 0<=value<=limit:raise ValueError(name+' is out of range.')
            result[name]=value
        schedule=profile.get('schedule')
        if schedule is not None:
            if not isinstance(schedule,dict):raise ValueError('Schedule must be null or an object.')
            cron=schedule.get('quartz_cron_expression','');zone=schedule.get('timezone_id','')
            if not isinstance(cron,str) or not 6<=len(cron.split())<=7 or not re.fullmatch(r'[A-Za-z0-9 *?,/#LW-]{1,120}',cron):raise ValueError('Supply a six/seven-field Quartz cron expression; Databricks validates its semantics.')
            if not isinstance(zone,str) or not re.fullmatch(r'(?:UTC|[A-Za-z_+-]+(?:/[A-Za-z0-9_+.-]+)+)',zone):raise ValueError('Supply a timezone ID such as America/Chicago or UTC.')
            result['schedule']={'quartz_cron_expression':cron,'timezone_id':zone,'pause_status':'PAUSED'}
        else:result['schedule']=None
        mappings=profile.get('task_mappings')
        if not isinstance(mappings,list) or len(mappings)>500 or any(not isinstance(m,dict) for m in mappings):raise ValueError('Supply at most 500 task mappings.')
        if any(set(m)-{'source','task_id','kind','pipeline','notebook_path'} for m in mappings):raise ValueError('Task mappings contain unknown fields.')
        if len(json.dumps(mappings))>500000:raise ValueError('Task mappings exceed the 500 KB limit.')
        seen=set()
        for m in mappings:
            identity=(m.get('source'),m.get('task_id'))
            if any(not isinstance(v,str) for v in identity) or identity in seen:raise ValueError('Task mappings must have unique source/task IDs.')
            seen.add(identity)
            if m.get('kind') not in ('auto_sql','pipeline','notebook','unmapped'):raise ValueError('Mapping kind must be auto_sql, pipeline, notebook or unmapped.')
        result['task_mappings']=mappings
        return result

    def save(self,run_id,payload):
        if not isinstance(payload,dict):raise ValueError('Invalid Lakeflow save request.')
        run=self._run(run_id);profile=self._validate(payload.get('profile'));fingerprint=digest(run)
        if payload.get('fingerprint')!=fingerprint:raise Conflict('Migration inputs changed; load the Lakeflow profile again.')
        known={(f['name'],t['id']) for f in run['files'] if f['name'].lower().endswith('.dtsx') for t in inventory(f)['tasks']}
        if any((m['source'],m['task_id']) not in known for m in profile['task_mappings']):raise ValueError('Map only tasks in this migration run.')
        with self.runs.db() as db:
            fresh=self.runs.get(run_id)
            if fresh['status'] in ACTIVE or digest(fresh)!=fingerprint:raise Conflict('Migration inputs changed while saving.')
            row=db.execute('SELECT version FROM lakeflow_profiles WHERE run_id=?',(run_id,)).fetchone();version=row['version'] if row else 0
            if payload.get('version')!=version:raise Conflict('Lakeflow profile changed in another tab; reload it.')
            db.execute('INSERT OR REPLACE INTO lakeflow_profiles VALUES(?,?,?,?)',(run_id,version+1,json.dumps(profile),fingerprint))
        return self.get(run_id)

    def generate(self,run_id):
        current=self.get(run_id)
        if current['version']==0:raise ValueError('Save the Lakeflow profile before generating assets.')
        if current['stale']:raise Conflict('Migration inputs changed; review and save the Lakeflow profile again.')
        run=self._run(run_id);profile=current['profile'];files=[];jobs={};pipelines={};outcomes=[];targets=set();artifacts=(run.get('conversion') or {}).get('files',[])
        mapping={(m['source'],m['task_id']):m for m in profile['task_mappings']}
        for item in run['files']:
            if not item['name'].lower().endswith('.dtsx'):continue
            outcome={'source':item['name'],'tasks':[],'blockers':[]};outcomes.append(outcome)
            try:inv,nodes,edges,order=package_graph(item)
            except ValueError as exc:outcome['blockers'].append(str(exc));continue
            tasks={t['id']:t for t in inv['tasks']};job_tasks=[];resource_key=key(item['name'])
            for task_id in order:
                task=tasks[task_id];m=mapping.get((item['name'],task_id),{});task_key=key(task_id);entry={'task_id':task_id,'name':task['name'],'status':'blocked'};outcome['tasks'].append(entry)
                try:
                    node=nodes.get(task_id)
                    if node is None:raise ValueError('Task XML cannot be resolved.')
                    kind=m.get('kind');job_task={'task_key':task_key,'max_retries':profile['max_retries'],'min_retry_interval_millis':profile['min_retry_interval_millis'],'run_if':'ALL_SUCCESS'}
                    dependencies=[{'task_key':key(start)} for start,end in edges if end==task_id]
                    if dependencies:job_task['depends_on']=dependencies
                    if kind=='auto_sql':
                        if 'ExecuteSQLTask' not in task['type']:raise ValueError('Automatic SQL requires an Execute SQL task.')
                        data=[n for n in node.iter() if local(n.tag)=='SqlTaskData']
                        if len(data)!=1 or prop(data[0],'SqlStmtSourceType','0') not in ('','0') or prop(data[0],'IsStoredProc','false').lower() in ('true','1') or prop(data[0],'ResultType','ResultSetType_None') not in ('','0','ResultSetType_None') or any(local(n.tag) in ('ParameterBinding','ResultBinding') for n in node.iter()):raise ValueError('SQL files/variables, result sets or parameter bindings require a reviewed notebook.')
                        queries=task_sql(item['name'],task,artifacts)
                        namespace='spark.sql('+repr('USE CATALOG `'+profile['catalog']+'`')+')\nspark.sql('+repr('USE SCHEMA `'+profile['schema']+'`')+')\n\n# COMMAND ----------\n'
                        code=GUARD+namespace+'\n'.join('display(spark.sql('+repr(q)+'))' for q in queries)+'\n'
                        name='notebooks/'+resource_key+'/'+task_key+'.py';files.append({'name':name,'sql':code});job_task.update(notebook_task={'notebook_path':'${workspace.file_path}/'+name.removesuffix('.py'),'source':'WORKSPACE','base_parameters':{'migration_reviewed':'false'}},environment_key='default')
                        entry.update(status='generated_review_required',kind=kind,artifact=name)
                    elif kind=='notebook':
                        path=m.get('notebook_path','')
                        if not isinstance(path,str) or not path.startswith(('/Shared/','/Users/','/Workspace/')) or any(p in ('','.','..') for p in path.split('/')[1:]) or re.search(r'[\x00-\x1f\\]',path):raise ValueError('Map an existing reviewed absolute workspace notebook path.')
                        job_task.update(notebook_task={'notebook_path':path,'source':'WORKSPACE','base_parameters':{'migration_reviewed':'false'}},environment_key='default');entry.update(status='generated_review_required',kind=kind,note='Existing notebook is user mapped; content, parameters and review gate must be checked in the workspace.')
                    elif kind=='pipeline':
                        if not any(x in task['type'] for x in ('Pipeline','DataFlowTask')):raise ValueError('Declarative pipelines require a supported data-flow task.')
                        lineage=copy_flow(node);config=m.get('pipeline')
                        if not isinstance(config,dict):raise ValueError('Supply a pipeline mapping with explicit source, target_table and mode.')
                        code,note=pipeline_code(config);target=config['target_table']
                        if target.casefold() in targets:raise ValueError('Two pipelines cannot own the same target table.')
                        targets.add(target.casefold());pipeline_key=key(item['name']+'_'+task_id);name='pipelines/'+pipeline_key+'.py';files.append({'name':name,'sql':code})
                        pipelines[pipeline_key]={'name':'crosslake_'+pipeline_key,'catalog':profile['catalog'],'schema':profile['schema'],'serverless':True,'continuous':False,'development':True,'configuration':{'crosslake.migration_reviewed':'false'},'libraries':[{'file':{'path':'${workspace.file_path}/'+name}}]}
                        job_task['pipeline_task']={'pipeline_id':'${resources.pipelines.'+pipeline_key+'.id}','full_refresh':False};entry.update(status='generated_review_required',kind=kind,artifact=name,lineage=lineage,note=note)
                    else:raise ValueError('Task has no supported mapping; select auto_sql, pipeline, or an existing notebook.')
                    job_tasks.append(job_task)
                except (ValueError,SyntaxError) as exc:entry['reason']=str(exc);outcome['blockers'].append(task['name']+': '+str(exc))
            if not outcome['blockers']:
                job={'name':'crosslake_'+resource_key,'max_concurrent_runs':1,'tasks':job_tasks}
                if any('environment_key' in t for t in job_tasks):job['environments']=[{'environment_key':'default','spec':{'client':'1'}}]
                if profile['schedule']:job['schedule']=profile['schedule']
                jobs[resource_key]=job;outcome['job_resource']=resource_key
        status='partial' if any(o['blockers'] for o in outcomes) and (jobs or pipelines) else 'blocked' if not (jobs or pipelines) else 'review_required'
        resources={'resources':{'jobs':jobs,'pipelines':pipelines}}
        if len(files)>176 or sum(len(f['sql'].encode()) for f in files)>16*1024*1024:raise ValueError('Generated bundle is too large; split this migration into smaller runs.')
        files.append({'name':'resources/lakeflow.json','sql':json.dumps(resources,indent=2)})
        files.append({'name':'databricks.yml','sql':'bundle:\n  name: crosslake-lakeflow-'+run_id[:8]+'\ninclude:\n  - resources/*.json\ntargets:\n  dev:\n    default: true\n    mode: development\n'})
        report={'run_id':run_id,'profile_version':current['version'],'profile':profile,'profile_sha256':hashlib.sha256(json.dumps(profile,sort_keys=True).encode()).hexdigest(),'fingerprint':current['fingerprint'],'status':status,'jobs':len(jobs),'pipelines':len(pipelines),'packages':outcomes,'review_required':True,'deployable':False,'executed_by_app':False,'limitations':['Generated resources are not deployed or executed. All schedules are paused and review gates are false.','Only flat success/AND task dependencies are preserved. SQL tasks run in separate sessions; review cross-task state and side effects.','Pipeline sources/targets are user mappings. Batch materialized-view refresh and streaming append semantics are not inferred from SSIS destinations.','No cross-package dependencies, transactions, loops, dynamic SQL, lookup/join logic, custom REST or script translation is inferred.','Serverless support, Unity Catalog permissions, pipeline capabilities, schema DDL, cron syntax and data parity require workspace validation.'],'artifacts':[{'path':f['name'],'sha256':hashlib.sha256(f['sql'].encode()).hexdigest()} for f in files]}
        files.append({'name':'lakeflow-plan.json','sql':json.dumps(report,indent=2)})
        files.append({'name':'README.md','sql':'# Crosslake Lakeflow review bundle\n\nRead lakeflow-plan.json first. Blocked packages have no generated Job; individually generated pipeline resources may remain for review. Do not assume partial resources implement the complete package.\n\n1. Review source/target mappings, schemas, output parity, task dependencies and transaction/session behavior.\n2. Configure your Databricks CLI profile and run `databricks bundle validate -t dev`.\n3. Review resources/lakeflow.json and generated code. Set each reviewed notebook parameter migration_reviewed=true and pipeline configuration crosslake.migration_reviewed=true only after testing.\n4. Deploy explicitly with `databricks bundle deploy -t dev`. No credentials or host are embedded.\n5. Start and test a job/pipeline in the workspace; compare outputs in Crosslake Validation workbench. Enable schedules only after validation.\n\nExisting notebook mappings reference workspace assets not included in this bundle. Parameters and secret references must be configured manually. Generated notebooks are imported as notebooks (extensionless workspace paths); pipeline Python is synced as a source file.\n\nDocs: https://docs.databricks.com/aws/en/ldp/developer/python-ref and https://docs.databricks.com/aws/en/dev-tools/bundles/resources\n'})
        return {'files':files,'report':report}
