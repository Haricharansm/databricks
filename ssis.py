"""SSIS XML inventory. Unsupported or unknown behavior is surfaced for manual review."""
from collections import Counter
import hashlib
from pathlib import Path
import re
import xml.etree.ElementTree as ET

DTS='www.microsoft.com/SqlServer/Dts'

def local(tag):return tag.rsplit('}',1)[-1]
def attrs(node):return {local(k):v for k,v in node.attrib.items()}
def prop(node,key,default=''):
    a=attrs(node)
    if key in a:return a[key]
    for child in node:
        if local(child.tag)=='Property' and attrs(child).get('Name')==key:return child.text or ''
    return default

def parse_package(text):
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)',text,re.I):raise ValueError('DTSX DTDs and entities are not supported.')
    try:root=ET.fromstring(text)
    except ET.ParseError as exc:raise ValueError('DTSX is not well-formed XML. Export an unencrypted DTSX package.') from exc
    if root.tag!='{'+DTS+'}Executable':raise ValueError('DTSX must contain a Microsoft DTS Executable root.')
    nodes=list(root.iter())
    if len(nodes)>30000:raise ValueError('DTSX exceeds the 30,000-node assessment limit.')
    if any('Encrypted' in local(n.tag) for n in nodes):raise ValueError('Encrypted DTSX must be decrypted in SSIS before upload.')
    return root

SUPPORTED_TASKS=('ExecuteSQLTask','DataFlowTask','Pipeline','ExecutePackageTask','FileSystemTask','STOCK:SEQUENCE','STOCK:FORLOOP','STOCK:FOREACHLOOP','ExtensibleFileTask')
SUPPORTED_COMPONENTS=('OLEDBSource','FlatFileSource','ExcelSource','RawSource','Aggregate','Audit','Cache','CharacterMap','ConditionalSplit','CopyColumn','DataConvert','DerivedColumn','Lookup','Merge','MergeJoin','Multicast','OLEDBCommand','PercentageSampling','Pivot','RowCount','SCD','Sort','UnionAll','UnPivot','OLEDBDestination','FlatFileDestination','ExcelDestination','RawDestination')

def inventory(item):
    root=parse_package(item['sql']);tasks=[];components=[];connections=[];variables=[];parameters=[];constraints=[];embedded=[];findings=[]
    def finding(code,severity,title,action,count=1):
        findings.append({'code':code,'severity':severity,'title':title,'action':action,'count':count,'lines':[]})
    pending=[(root,None,0)]
    while pending:
        node,parent,depth=pending.pop()
        if depth>200:raise ValueError('DTSX nesting exceeds the 200-level limit.')
        if node is not root and local(node.tag)=='Executable':
            typ=prop(node,'ExecutableType') or prop(node,'CreationName');ref=prop(node,'refId') or prop(node,'DTSID') or f'task-{len(tasks)+1}'
            tasks.append({'id':ref,'dts_id':prop(node,'DTSID'),'name':prop(node,'ObjectName') or ref,'type':typ,'parent':parent})
            if len(tasks)>500:raise ValueError('DTSX exceeds the 500-task inventory limit.')
            parent=ref
        pending.extend((child,parent,depth+1) for child in reversed(list(node)))
    for node in root.iter():
        tag=local(node.tag);a=attrs(node)
        if tag=='component':components.append({'name':a.get('name','component'),'type':a.get('componentClassID','unknown')})
        elif tag=='ConnectionManager' and prop(node,'ObjectName'):
            connections.append({'name':prop(node,'ObjectName'),'type':prop(node,'CreationName')})
        elif tag=='Variable':variables.append({'name':prop(node,'ObjectName'),'namespace':prop(node,'Namespace'),'expression':prop(node,'EvaluateAsExpression') in ('True','true','1')})
        elif tag in ('PackageParameter','ProjectParameter'):parameters.append({'name':prop(node,'ObjectName'),'sensitive':prop(node,'Sensitive') in ('1','True','true')})
        elif tag=='PrecedenceConstraint':constraints.append({k:prop(node,k) for k in ('From','To','Value','EvalOp','Expression','LogicalAnd')})
        elif tag=='SqlTaskData':
            statement=prop(node,'SqlStatementSource')
            if statement:
                embedded.append({'sql':statement,'source_type':prop(node,'SqlStmtSourceType','0'),'has_parameters':any(local(c.tag)=='ParameterBinding' for c in node)})
    types=Counter(t['type'] for t in tasks)
    scripts=[x for x in [*tasks,*components] if 'script' in x['type'].lower()]
    if scripts:finding('ssis-script','high','C#/VB.NET code requires rewrite','Rewrite Script Tasks and Script Components in Python/PySpark; generated placeholders do not preserve their implementation.',len(scripts))
    unknown=[t for t in tasks if not any(s.lower() in t['type'].lower() for s in SUPPORTED_TASKS) and 'script' not in t['type'].lower()]
    unknown+=[t for t in components if not any(s.lower() in t['type'].lower() for s in SUPPORTED_COMPONENTS) and 'script' not in t['type'].lower()]
    if unknown:finding('ssis-unsupported','high','Unsupported or unrecognized components','Manually map these types: '+', '.join(sorted({t['type'] for t in unknown})),len(unknown))
    if any(t['parent'] or 'LOOP' in t['type'].upper() for t in tasks):finding('ssis-containers','high','Nested containers or loops','Review scope, iteration, and execution ordering; package-level workflow templates do not model inner control flow.')
    if any('ExecutePackage' in t['type'] for t in tasks):finding('ssis-package-call','high','Package-to-package dependency','Map called package paths, parameters, and dependencies explicitly before running workflows.')
    if connections:finding('ssis-connections','high','Connections require Databricks mappings','Map connection managers to approved JDBC, Unity Catalog, or file locations; use Databricks secrets rather than embedded passwords.',len(connections))
    if variables or parameters:finding('ssis-parameters','medium','Variables and parameters','Review variable scope, expression evaluation, data types, sensitive values, and notebook parameter mappings.',len(variables)+len(parameters))
    if any(e['has_parameters'] or e['source_type'] not in ('','0') for e in embedded):finding('ssis-sql-binding','high','SQL source or parameter bindings','Resolve SQL stored in files/variables and verify positional parameters and result bindings before execution.')
    complex_constraints=[c for c in constraints if c['Expression'] or c['EvalOp'] not in ('','0','Constraint') or c['Value'] not in ('','0','Success') or c['LogicalAnd'].lower() in ('false','0')]
    if complex_constraints:finding('ssis-conditional-flow','high','Conditional or failure-based precedence','Preserve expressions, OR joins, and failure/completion branches manually. Workflow templates stay at package level.',len(complex_constraints))
    ids={t['id']:t['id'] for t in tasks};ids.update({t['dts_id']:t['id'] for t in tasks if t['dts_id']})
    edges=[];unresolved=False
    for c in constraints:
        if c['From'] not in ids or c['To'] not in ids:unresolved=True
        else:edges.append((ids[c['From']],ids[c['To']]))
    if unresolved:finding('ssis-unresolved-edge','high','Unresolved precedence reference','Review package constraint references; execution order cannot be established.')
    incoming={t['id']:0 for t in tasks};outgoing={k:[] for k in incoming}
    for start,end in edges:incoming[end]+=1;outgoing[start].append(end)
    ready=[k for k,v in incoming.items() if v==0];visited=0
    while ready:
        node=ready.pop();visited+=1
        for end in outgoing[node]:
            incoming[end]-=1
            if incoming[end]==0:ready.append(end)
    if visited!=len(incoming):finding('ssis-cycle','high','Cyclic precedence graph','Resolve cyclic task dependencies before migration.')
    if item.get('origin_project'):finding('ssis-project-metadata','high','ISPAC project settings require mapping','Project parameters and external connection-manager files are inventoried but not applied by the per-package converter.')
    if not tasks:finding('ssis-empty','high','No executable tasks found','Verify the package export contains unencrypted executable content.')
    return {'package_name':prop(root,'ObjectName') or Path(item['name']).stem,'tasks':tasks,'components':components,
            'task_types':dict(types),'connections':connections,'variables':variables,'parameters':parameters,
            'constraints':constraints,'embedded_sql':embedded,'findings':findings,
            'project_metadata':item.get('project_metadata',[])}

def package_key(name):
    stem=re.sub(r'[^A-Za-z0-9_]+','_',Path(name).stem).strip('_')[:60] or 'package'
    return stem+'_'+hashlib.sha256(name.encode()).hexdigest()[:8]
