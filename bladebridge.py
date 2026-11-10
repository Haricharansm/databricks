"""Official BladeBridge LSP adapter and guarded notebook/workflow packaging."""
import ast
from email import policy
from email.parser import Parser
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from morpheus import Morpheus
from ssis import inventory, package_key

ROOT=Path(__file__).parent

def python_path():
    return Path(os.environ.get('BLADEBRIDGE_PYTHON',str(ROOT/'engine'/'bladebridge'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')))).absolute()

def status():
    available=python_path().is_file()
    return {'engine':'Lakebridge / BladeBridge 0.3.0','available':available,'experimental':True,
            'message':'SSIS notebook conversion is configured (experimental).' if available else 'SSIS conversion needs python setup_ssis.py. SQL conversion remains separate.'}

GUARD='''# Databricks notebook source
# Migration review gate: engine output is experimental and has not been executed.
dbutils.widgets.dropdown("migration_reviewed", "false", ["false", "true"])
if dbutils.widgets.get("migration_reviewed") != "true":
    raise RuntimeError("Review generated code, SSIS findings, connections, parameters, and workflow paths before setting migration_reviewed=true.")

# COMMAND ----------
'''

def unpack_outputs(text,key):
    msg=Parser(policy=policy.default).parsestr(text)
    if not msg.is_multipart():raise RuntimeError('BladeBridge did not return its expected attachment bundle.')
    files=[];names=set();total=0
    for part in msg.iter_attachments():
        raw=(part.get_filename() or '').replace('\\','/')
        relative=raw.split('/transpiled/',1)[-1] if '/transpiled/' in raw else raw
        path=PurePosixPath(relative)
        if path.is_absolute() or '..' in path.parts:raise RuntimeError('BladeBridge returned an unsafe output path.')
        # Keep module-relative paths; normalize whitespace while preventing collisions.
        parts=[re.sub(r'[^\w.()-]+','_',p) for p in path.parts]
        if not parts or any(p.startswith('.') for p in parts):raise RuntimeError('BladeBridge returned an invalid output name.')
        suffix=path.suffix.lower()
        if suffix not in ('.py','.sql','.json','.ipynb','.txt','.md'):
            raise RuntimeError('Unsupported BladeBridge output type: '+suffix)
        content=part.get_content()
        if not isinstance(content,str):raise RuntimeError('BladeBridge returned non-text output.')
        total+=len(content.encode())
        if total>16*1024*1024 or len(files)>=180:raise RuntimeError('BladeBridge output limit exceeded.')
        name='notebooks/'+key+'/'+'/'.join(parts)
        if name.casefold() in names:raise RuntimeError('BladeBridge returned duplicate output names.')
        names.add(name.casefold())
        # Python source notebooks must start with the import marker. Gate all executable notebooks.
        if suffix=='.py':content=GUARD+content.removeprefix('# Databricks notebook source').lstrip('\r\n')
        if suffix=='.ipynb':
            notebook=json.loads(content)
            notebook['cells'].insert(0,{'cell_type':'code','metadata':{},'execution_count':None,'outputs':[],'source':GUARD.split('# COMMAND ----------')[0].splitlines(keepends=True)})
            content=json.dumps(notebook,indent=2)
        files.append({'name':name,'sql':content,'kind':'notebook' if suffix in ('.py','.ipynb') else 'engine_artifact'})
    if not files:raise RuntimeError('BladeBridge returned no output files.')
    return files

def convert_package(item):
    info=inventory(item);key=package_key(item['name'])
    if not status()['available']:raise RuntimeError(status()['message'])
    with tempfile.TemporaryDirectory(prefix='ssis-conversion-') as scratch:
        root=Path(scratch);source=root/item['name'];source.write_text(item['sql'],encoding='utf-8')
        client=Morpheus(None,root,command=[str(python_path()),'-m','databricks.labs.bladebridge.server'])
        try:
            client.initialize(root,'ssis',{'target-tech':'SPARKSQL'})
            mime,diagnostics=client.transpile(source,item['sql'])
            if any(d.get('severity')==1 for d in diagnostics):
                raise RuntimeError('BladeBridge could not convert the package: '+json.dumps(diagnostics))
            files=unpack_outputs(mime,key)
            for artifact in files:
                if artifact['name'].endswith('.py'):
                    try:ast.parse(artifact['sql'])
                    except SyntaxError as exc:
                        diagnostics.append({'severity':1,'message':f"Generated notebook syntax needs repair: {artifact['name']} line {exc.lineno}: {exc.msg}"})
        finally:client.close()
    notebooks=[f for f in files if f['kind']=='notebook']
    workflow=None
    if len(notebooks)==1:
        path=notebooks[0]['name'].rsplit('.',1)[0]
        workflow={'name':'REVIEW_REQUIRED_'+key,'max_concurrent_runs':1,
                  'tasks':[{'task_key':key,'notebook_task':{'notebook_path':'/REVIEW_REQUIRED/'+path,'source':'WORKSPACE',
                            'base_parameters':{'migration_reviewed':'false'}}}]}
        files.append({'name':'workflows/'+key+'.job.json','sql':json.dumps(workflow,indent=2),'kind':'workflow_template'})
    plan={'source':item['name'],'experimental':True,'deployable':False,'workflow_granularity':'one package per job',
          'tasks':info['tasks'],'components':info['components'],'precedence_constraints':info['constraints'],
          'connections':info['connections'],'parameters':info['parameters'],'variables':info['variables'],
          'findings':info['findings'],'engine_diagnostics':diagnostics,
          'review_required':['Review all notebook cells; the engine can emit incomplete code without LSP diagnostics.',
                             'Import notebooks and replace /REVIEW_REQUIRED paths. Select supported compute for your workspace.',
                             'Map connections, variables, and parameters. Never embed database passwords in exported code.',
                             'Review intra-package ordering and explicitly connect cross-package dependencies.',
                             'Set migration_reviewed=true only after testing with representative data.'],
          'workflow_template_generated':workflow is not None}
    if workflow is None:plan['review_required'].append('Multiple or no notebook outputs: identify the entrypoint and construct a workflow manually; no order was invented.')
    files.append({'name':'plans/'+key+'.json','sql':json.dumps(plan,indent=2),'kind':'migration_plan'})
    return files,diagnostics
