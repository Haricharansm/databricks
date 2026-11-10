"""Legacy SQL/procedure metadata and mixed SQL/SSIS conversion coverage."""
import json
from bisect import bisect_right
import re
from core import mask_sql

IDENT=r'(?:\[(?:\]\]|[^\]])+\]|"(?:""|[^"])+"|[A-Za-z_]\w*)'
QUALIFIED=IDENT+r'(?:\s*\.\s*'+IDENT+r'){0,3}'
REFERENCE_NAME=re.compile(QUALIFIED)
PROCEDURE_NAME=re.compile(r'\s*('+QUALIFIED+r')')
DECL=re.compile(r'\b(?:CREATE|ALTER)\s+(?:OR\s+ALTER\s+)?PROC(?:EDURE)?\b',re.I)

def sql_inventory(text):
    masked=mask_sql(text);declarations=list(DECL.finditer(masked));procedures=[]
    newlines=[m.start() for m in re.finditer('\n',masked)]
    dependencies=[]
    for m in re.finditer(r'\b(?:FROM|JOIN|UPDATE|INTO|EXEC(?:UTE)?)\s+',masked,re.I):
        name=REFERENCE_NAME.match(text,m.end())
        if name:dependencies.append({'name':name.group().strip(),'line':bisect_right(newlines,m.start())+1,'usage':m.group().strip().upper()})
    for i,m in enumerate(declarations):
        name=PROCEDURE_NAME.match(text,m.end())
        end=declarations[i+1].start() if i+1<len(declarations) else len(text)
        segment=masked[m.end():end]
        # Header only; AS inside EXECUTE AS is an option boundary and is reviewed manually.
        boundary=re.search(r'\bAS\b',segment,re.I)
        header=segment[:boundary.start()] if boundary else segment
        parameters=[]
        for p in re.finditer(r'(@\w+)\s+([A-Za-z_]\w*(?:\s*\([^)]*\))?)([^,@]*)',header,re.I):
            parameters.append({'name':p.group(1),'type':p.group(2),'output':bool(re.search(r'\bOUT(?:PUT)?\b',p.group(3),re.I)),
                               'has_default':'=' in p.group(3)})
        procedures.append({'name':name.group(1).strip() if name else '(unresolved name)',
                           'line':bisect_right(newlines,m.start())+1,'parameters':parameters,
                           'target':'Databricks SQL scripting; verify target runtime procedure support',
                           'validation_required':True})
    return {'object_type':'stored_procedure' if procedures else 'sql_script','procedures':procedures,
            'dependencies':dependencies,'dependency_method':'Unresolved textual references, not a database dependency graph.'}

def convert_mixed(files):
    from core import convert_sql,ENGINE_LOCK
    from bladebridge import convert_package
    sql_files=[f for f in files if not f['name'].lower().endswith('.dtsx')]
    packages=[f for f in files if f['name'].lower().endswith('.dtsx')]
    output=[];coverage=[];messages=[];engines=[]
    if sql_files:
        try:
            result=convert_sql(sql_files);engines.append(result['engine']);output.extend(result['files'])
            messages.append(result.get('diagnostics',''));messages.append(result.get('conversion_errors',''))
            # The engine may aggregate or split source files; do not invent individual output mappings.
            errors=result.get('conversion_errors','').strip() not in ('','[]')
            for f in sql_files:coverage.append({'source':f['name'],'kind':sql_inventory(f['sql'])['object_type'],
                 'status':'engine_errors' if errors else 'generated_review_required','engine':result['engine'],
                 'output_mapping':'SQL batch output; review source coverage in generated files'})
        except RuntimeError as exc:
            for f in sql_files:coverage.append({'source':f['name'],'kind':'sql','status':'blocked','reason':str(exc)})
    for item in packages:
        if not ENGINE_LOCK.acquire(blocking=False):raise RuntimeError('Another conversion is running. Try again later.')
        try:
            try:
                artifacts,diagnostics=convert_package(item);output.extend(artifacts)
                if 'Lakebridge / BladeBridge 0.3.0' not in engines:engines.append('Lakebridge / BladeBridge 0.3.0')
                coverage.append({'source':item['name'],'kind':'ssis','status':'engine_errors' if any(d.get('severity')==1 for d in diagnostics) else 'generated_review_required',
                                 'outputs':[a['name'] for a in artifacts],'experimental':True})
                messages.append(json.dumps({'source':item['name'],'diagnostics':diagnostics}))
            except RuntimeError as exc:coverage.append({'source':item['name'],'kind':'ssis','status':'blocked','reason':str(exc)})
        finally:ENGINE_LOCK.release()
    if not output and all(c['status']=='blocked' for c in coverage):
        raise RuntimeError('No sources converted. '+'; '.join(c['source']+': '+c['reason'] for c in coverage))
    blocked=[c for c in coverage if c['status'] in ('blocked','engine_errors')]
    manifest={'coverage':coverage,'validated_on_databricks':False,'deployable':False,
              'limitations':'SSIS conversion is experimental. Scripts/custom components, expressions, parameter bindings, data-flow semantics, and external dependencies require review. Workflow templates are per-package drafts, not a resolved multi-package graph.'}
    output.append({'name':'migration-coverage.json','sql':json.dumps(manifest,indent=2),'kind':'coverage_report'})
    if len(output)>200 or sum(len(f['sql'].encode()) for f in output)>16*1024*1024:raise RuntimeError('Combined output exceeds 200 artifacts or 16 MB. Convert fewer sources.')
    return {'engine':', '.join(engines),'files':output,'coverage':coverage,'diagnostics':'\n'.join(m for m in messages if m),
            'conversion_errors':json.dumps(blocked,indent=2) if blocked else '',
            'status':'Partial conversion — review blocked sources' if blocked else 'Generated — review and validation required',
            'validated_on_databricks':False,'deployable':False,
            'warning':'SQL and notebooks have not been executed on Databricks. SSIS notebooks are guarded and workflows are draft templates. Review every source in the coverage report before migration.'}
