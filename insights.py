"""Static SQL/SSIS insights plus explicitly imported runtime evidence; no DB execution."""
from collections import Counter,defaultdict
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation,localcontext
import csv
import hashlib
from html import escape
from io import StringIO
import json
import re
from core import assess_file,mask_sql
from deploy import split_statements
from monitoring import ACTIVE,Conflict,now
from ssis import inventory,parse_package,local,prop
from validation import strict_json

MAX_UNITS=3000
TOKEN=re.compile(r'(?:##?|@)?[A-Za-z_][A-Za-z0-9_$#@]*|\d+(?:\.\d+)?')
RESERVED=set('SELECT FROM JOIN LEFT RIGHT INNER OUTER FULL CROSS WHERE GROUP ORDER BY HAVING ON AS INTO VALUES SET WHEN THEN ELSE END UNION EXCEPT INTERSECT OPTION WITH INSERT UPDATE DELETE MERGE USING OUTPUT EXEC EXECUTE AND OR NOT NULL TABLE VIEW PROC PROCEDURE FUNCTION IF EXISTS DISTINCT TOP APPLY OFFSET FETCH FOR'.split())

def uid(kind,*values):return kind+'_'+hashlib.sha256(json.dumps(values,ensure_ascii=False).encode()).hexdigest()[:20]
def fingerprint(files):return hashlib.sha256(json.dumps(files,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def lex(text):
    tokens=[];issues=[];i=0;line=1
    while i<len(text):
        if len(tokens)>=20000:issues.append('Token limit reached; lexical analysis incomplete');break
        c=text[i];start=i;at=line
        if c.isspace():line+=c=='\n';i+=1;continue
        if text.startswith('--',i):
            end=text.find('\n',i);i=len(text) if end<0 else end;continue
        if text.startswith('/*',i):
            i+=2;depth=1
            while i<len(text) and depth:
                if text.startswith('/*',i):depth+=1;i+=2
                elif text.startswith('*/',i):depth-=1;i+=2
                else:line+=text[i]=='\n';i+=1
            if depth:issues.append('Unterminated block comment')
            continue
        if c in "'\"[`" or c in 'Nn' and text[i:i+2].lower()=="n'":
            unicode=c in 'Nn';quote="'" if unicode else c;close=']' if quote=='[' else quote;i+=2 if unicode else 1;value=[];closed=False
            while i<len(text):
                if text[i]==close:
                    if i+1<len(text) and text[i+1]==close:value.append(close);i+=2;continue
                    i+=1;closed=True;break
                line+=text[i]=='\n';value.append(text[i]);i+=1
            if not closed:issues.append('Unterminated literal or identifier')
            tokens.append({'k':'literal' if quote=="'" else 'identifier','v':''.join(value),'raw':text[start:i],'line':at});continue
        match=TOKEN.match(text,i)
        if match:
            raw=match.group();i+=len(raw);tokens.append({'k':'number' if raw[0].isdigit() else 'word','v':raw,'raw':raw,'line':at});continue
        i+=1;tokens.append({'k':'punct','v':c,'raw':c,'line':at})
    depth=0
    for t in tokens:
        if t['k']=='punct':depth+=(t['v']=='(')-(t['v']==')')
        if depth<0:break
    if depth:issues.append('Unbalanced parentheses')
    return tokens,issues

def word(t):return t['v'].upper() if t['k']=='word' else ''
def query_hash(sql):
    ts,issues=lex(sql)
    if any('Token limit' in v for v in issues):return hashlib.sha256(('raw-oversize:'+sql).encode()).hexdigest()
    while ts and ts[-1]['v']==';':ts.pop()
    return hashlib.sha256(json.dumps([(t['k'],t['raw'] if t['k']=='literal' else t['v'].casefold()) for t in ts],ensure_ascii=False).encode()).hexdigest()

def name_at(ts,i):
    if i>=len(ts) or ts[i]['k'] not in ('word','identifier') or word(ts[i]) in RESERVED:return None,i
    parts=[ts[i]['v']];raw=[ts[i]['raw']];j=i+1
    while j<len(ts) and ts[j]['v']=='.':
        raw.append('.')
        if j+1<len(ts) and ts[j+1]['v']=='.':parts.append('');j+=1;continue
        if j+1>=len(ts) or ts[j+1]['k'] not in ('word','identifier'):break
        parts.append(ts[j+1]['v']);raw.append(ts[j+1]['raw']);j+=2
    return {'parts':tuple(p.casefold() for p in parts),'name':''.join(raw),'line':ts[i]['line']},j

def cte_names(ts):
    names=set()
    for i,t in enumerate(ts):
        if word(t)!='WITH':continue
        j=i+1
        while j<len(ts):
            name,j=name_at(ts,j)
            if not name or len(name['parts'])!=1:break
            if j<len(ts) and ts[j]['v']=='(':
                depth=1;j+=1
                while j<len(ts) and depth:depth+=((ts[j]['v']=='(')-(ts[j]['v']==')')) if ts[j]['k']=='punct' else 0;j+=1
            if j+1>=len(ts) or word(ts[j])!='AS' or ts[j+1]['v']!='(':break
            names.add(name['parts']);j+=2;depth=1
            while j<len(ts) and depth:depth+=((ts[j]['v']=='(')-(ts[j]['v']==')')) if ts[j]['k']=='punct' else 0;j+=1
            if j>=len(ts) or ts[j]['v']!=',':break
            j+=1
    return names

def sql_details(sql):
    ts,issues=lex(sql);defs=[];refs=[];aliases={};ctes=cte_names(ts);counts=Counter(word(t) for t in ts)
    def add(i,usage):
        name,end=name_at(ts,i)
        if name:
            if end<len(ts) and ts[end]['v']=='(' and usage=='READ':name['function_call']=True
            refs.append({**name,'usage':usage})
        return name,end
    for i,t in enumerate(ts):
        w=word(t)
        if w in ('CREATE','ALTER'):
            j=i+1
            if j+1<len(ts) and word(ts[j])=='OR' and word(ts[j+1]) in ('ALTER','REPLACE'):j+=2
            if j<len(ts) and word(ts[j]) in ('TABLE','VIEW','PROC','PROCEDURE','FUNCTION'):
                kind=word(ts[j]);j+=1
                if [word(x) for x in ts[j:j+3]]==['IF','NOT','EXISTS']:j+=3
                name,_=name_at(ts,j)
                if name:defs.append({**name,'kind':{'TABLE':'table','VIEW':'view','PROC':'procedure','PROCEDURE':'procedure','FUNCTION':'function'}[kind]})
        if w in ('FROM','JOIN','APPLY','USING'):
            name,j=add(i+1,'READ')
            if name:
                if j<len(ts) and word(ts[j])=='AS':j+=1
                alias,end=name_at(ts,j)
                if alias and len(alias['parts'])==1:aliases[alias['parts']]=name
                if w=='FROM':
                    j=end if alias else j
                    while j<len(ts) and ts[j]['v']==',':
                        name,j=add(j+1,'READ')
                        if not name:break
                        if j<len(ts) and word(ts[j])=='AS':j+=1
                        alias,end=name_at(ts,j)
                        if alias:aliases[alias['parts']]=name;j=end
        if w in ('INSERT','UPDATE','MERGE'):
            j=i+1
            if j<len(ts) and word(ts[j])=='INTO':j+=1
            add(j,w)
        if w=='DELETE':add(i+2 if i+1<len(ts) and word(ts[i+1])=='FROM' else i+1,'DELETE')
        if w in ('TRUNCATE','DROP'):
            j=i+1
            if j<len(ts) and word(ts[j]) in ('TABLE','VIEW'):j+=1
            if [word(x) for x in ts[j:j+2]]==['IF','EXISTS']:j+=2
            add(j,w)
        if w=='INTO':
            prior=next((word(x) for x in reversed(ts[:i]) if word(x) in ('SELECT','INSERT','MERGE','OUTPUT')),None)
            usage='SELECT_INTO' if prior=='SELECT' else 'MERGE' if prior=='MERGE' else 'INSERT'
            name,_=add(i+1,usage)
            if name and usage=='SELECT_INTO':defs.append({**name,'kind':'table'})
        if w in ('EXEC','EXECUTE'):
            j=i+1
            if j+1<len(ts) and ts[j]['v'].startswith('@') and ts[j+1]['v']=='=':j+=2
            if j<len(ts) and not ts[j]['v'].startswith('@'):add(j,'CALL')
        if w=='REFERENCES':add(i+1,'REFERENCE')
        if w=='DECLARE' and i+2<len(ts) and ts[i+1]['v'].startswith('@') and word(ts[i+2])=='TABLE':
            name,_=name_at(ts,i+1)
            if name:defs.append({**name,'kind':'table_variable'})
    for r in refs:
        if r['usage'] in ('UPDATE','DELETE') and r['parts'] in aliases:r.update(parts=aliases[r['parts']]['parts'],name=aliases[r['parts']]['name'])
        r['local_cte']=r['parts'] in ctes
    unique={(r['parts'],r['usage'],r['line']):r for r in refs};category=next((word(t) for t in ts if t['k']=='word'),'UNKNOWN')
    if defs and category in ('CREATE','ALTER'):category+='_'+defs[0]['kind'].upper()
    elif category=='WITH':category='WITH_QUERY'
    return {'definitions':defs,'references':list(unique.values()),'issues':issues,'category':category,'tokens':ts,'cte_count':len(ctes),'metrics':{'conditions':counts['IF']+counts['CASE'],'cursors':counts['CURSOR'],'loops':counts['WHILE'],'goto':counts['GOTO'],'dynamic_exec':sum(word(t) in ('EXEC','EXECUTE') and i+1<len(ts) and (ts[i+1]['v']=='(' or ts[i+1]['v'].startswith('@') and not(i+2<len(ts) and ts[i+2]['v']=='=')) for i,t in enumerate(ts))}}

class Graph:
    def __init__(self):self.nodes={};self.edges={};self.truncated=False
    def node(self,ident,label,kind,source='',**extra):
        if ident not in self.nodes:
            if len(self.nodes)>=8000:self.truncated=True;return ident
            self.nodes[ident]={'id':ident,'label':str(label)[:500],'kind':kind,'source':source,**extra}
        return ident
    def edge(self,start,end,kind,mode='data',**evidence):
        if start not in self.nodes or end not in self.nodes:return
        ident=(start,end,kind,mode)
        if ident not in self.edges:
            if len(self.edges)>=16000:self.truncated=True;return
            self.edges[ident]={'from':start,'to':end,'kind':kind,'mode':mode,**evidence}

def extract(files,g):
    units=[];packages=[];links=[];truncated=False
    for f in files:
        name=f['name'];fid=g.node(uid('file',name),name,'package' if name.lower().endswith('.dtsx') else 'file',name);texts=[]
        if name.lower().endswith('.dtsx'):
            inv=inventory(f);root=parse_package(f['sql']);packages.append({'source':name,**{k:v for k,v in inv.items() if k!='embedded_sql'}});aliases={};task_nodes={}
            for task in inv['tasks']:
                ident=g.node(uid('task',name,task['id']),task['name'],'task',name,task_type=task['type']);task_nodes[task['id']]=ident;aliases[task['id']]=ident
                if task['dts_id']:aliases[task['dts_id']]=ident
                g.edge(task_nodes.get(task['parent'],fid),ident,'contains','process',method='SSIS executable inventory')
            for c in inv['constraints']:
                if c['From'] in aliases and c['To'] in aliases:g.edge(aliases[c['From']],aliases[c['To']],'precedes','process',condition=c['Value'] or 'Success',expression=c['Expression'][:300],method='SSIS precedence')
            connections={}
            for n in root.iter():
                if local(n.tag)=='ConnectionManager' and prop(n,'ObjectName'):
                    label=prop(n,'ObjectName');kind='connection'
                    if prop(n,'CreationName').upper()=='FLATFILE':
                        path=next((prop(c,'ConnectionString') for c in n.iter() if prop(c,'ConnectionString')),'')
                        if len(path)<=1024 and not re.search(r'[;=\r\n]',path) and re.match(r'^(?:[A-Za-z]:[\\/]|/Volumes/)',path):label+=' · '+path;kind='file_endpoint'
                    ident=g.node(uid('connection',name,prop(n,'ObjectName')),label,kind,name)
                    for alias in (prop(n,'refId'),prop(n,'DTSID')):
                        if alias:connections[alias]=ident
            def visit(n,parent):
                tag=local(n.tag)
                if tag=='Executable':parent=aliases.get(prop(n,'refId') or prop(n,'DTSID'),parent)
                if tag=='SqlTaskData' and prop(n,'SqlStatementSource') and prop(n,'SqlStmtSourceType','0') in ('','0'):texts.append((prop(n,'SqlStatementSource'),parent,'Embedded SQL line; not XML line'))
                if tag in ('PackageName','PackageNameFromProjectReference') or tag=='Property' and (n.get('Name') or n.get('name')) in ('PackageName','PackageNameFromProjectReference'):
                    if n.text:links.append({'consumer':name,'target_file':n.text.replace('\\','/').rsplit('/',1)[-1],'method':'SSIS package-name reference'})
                if tag=='pipeline':
                    comps={}
                    for c in n.iter():
                        if local(c.tag)!='component':continue
                        ref=c.get('refId') or c.get('name','component');ident=g.node(uid('component',name,ref),c.get('name',ref),'component',name,component_type=c.get('componentClassID','unknown'));comps[ref]=ident;g.edge(parent,ident,'contains','process',method='SSIS component inventory')
                        typ=c.get('componentClassID','')+' '+next((p.text or '' for p in c.iter() if p.get('name')=='UserComponentTypeName'),'')
                        for con in c.iter():
                            if local(con.tag)!='connection':continue
                            ref_id=con.get('connectionManagerRefId') or con.get('connectionManagerID') or ''
                            conn=connections.get(ref_id) or g.node(uid('connection',name,ref_id),ref_id or 'Unresolved connection','connection',name,unresolved=True)
                            if 'source' in typ.lower():g.edge(conn,ident,'reads','data',method='SSIS source connection')
                            elif 'destination' in typ.lower():g.edge(ident,conn,'writes','data',method='SSIS destination connection')
                            else:g.edge(conn,ident,'connects','data',method='Unclassified SSIS connection')
                    for p in n.iter():
                        if local(p.tag)!='path':continue
                        ends=[]
                        for attr in ('startId','endId'):
                            matches=[(len(k),v) for k,v in comps.items() if p.get(attr,'').startswith(k+'.')];ends.append(max(matches)[1] if matches else None)
                        if all(ends):g.edge(*ends,'flows','process',method='SSIS component path');g.edge(*ends,'flows','data',method='SSIS path; not column-level lineage')
                for child in n:visit(child,parent)
            visit(root,fid)
        else:texts=[(f['sql'],fid,'SQL source line')]
        for text,parent,location in texts:
            batch_matches=list(re.finditer(r'(?im)^[ \t]*GO(?:[ \t]+[0-9]+)?[ \t]*$',mask_sql(text)))
            for batch in reversed(batch_matches):text=text[:batch.start()]+';'+' '*(batch.end()-batch.start()-1)+text[batch.end():]
            cursor=0
            for i,sql in enumerate(split_statements(text)):
                if len(units)>=MAX_UNITS:truncated=True;break
                start=max(0,text.find(sql,cursor));cursor=start+len(sql);line=text.count('\n',0,start)+1;detail=sql_details(sql)
                if not detail['tokens']:continue
                ident=g.node(uid('query',name,parent,i,query_hash(sql)),name+' · '+detail['category']+' · line '+str(line),'query',name);g.edge(parent,ident,'contains','process',method=location);scan=assess_file({'name':'statement.sql','sql':sql})
                detail['pattern_flags']={'select_star':any(word(t)=='SELECT' and j+1<len(detail['tokens']) and detail['tokens'][j+1]['v']=='*' for j,t in enumerate(detail['tokens'])),'leading_wildcard':any(word(t)=='LIKE' and j+1<len(detail['tokens']) and detail['tokens'][j+1]['k']=='literal' and detail['tokens'][j+1]['v'].startswith('%') for j,t in enumerate(detail['tokens'])),'distinct':any(word(t)=='DISTINCT' for t in detail['tokens'])}
                detail.pop('tokens')
                if any('Token limit' in v for v in detail['issues']):truncated=True
                units.append({'id':ident,'source':name,'parent':parent,'line':line,'location':location,'query_hash':query_hash(sql),'category':detail['category'],'sql_preview':sql[:4000],'preview_truncated':len(sql)>4000,'complexity':scan['complexity'],'findings':scan['findings'],**detail})
    return units,packages,links,truncated

def object_key(parts,source,cte=''):return ((parts[-1],) if parts[-1].startswith(('#','@')) else tuple(parts),source if parts[-1].startswith(('#','@')) else '',cte)

def entities_for(units,g):
    entities={};deps=[]
    for q in units:
        for d in q['definitions']:
            parts=d['parts'];identity=object_key(parts,q['source']);ident=uid('entity',identity)
            if identity not in entities and len(entities)>=8000:g.truncated=True;continue
            if identity not in entities:entities[identity]={'id':ident,'name':d['name'],'kind':d['kind'],'temporary':parts[-1].startswith(('#','@')),'definitions':[],'references':[],'resolution':'definition_in_input'}
            entities[identity]['definitions'].append({'source':q['source'],'query_id':q['id'],'line':q['line']+d['line']-1});g.node(ident,d['name'],'entity',entity_kind=d['kind']);g.edge(q['id'],ident,'declares',method='SQL lexical declaration',source=q['source'])
    declared=list(entities)
    reference_count=0
    for q in units:
        for r in q['references']:
            reference_count+=1
            if reference_count>16000:g.truncated=True;break
            parts=r['parts'];identity=object_key(parts,q['source'],q['id'] if r['local_cte'] else '');resolution='local_cte' if r['local_cte'] else 'reference_only'
            if not r['local_cte']:
                matches=[e for e in declared if e[0]==parts and (not e[1] or e[1]==q['source'])]
                if not matches and not parts[-1].startswith(('#','@')):matches=[e for e in declared if e[0][-len(parts):]==parts and not e[1]]
                if len(matches)==1:identity=matches[0];resolution='exact_name_in_input' if identity[0]==parts else 'unique_suffix_heuristic'
                elif len(matches)>1:resolution='ambiguous_reference'
            if resolution=='reference_only' and identity in entities and entities[identity]['definitions']:resolution='temporary_scope_heuristic'
            ident=uid('entity',identity)
            if identity not in entities and len(entities)>=8000:g.truncated=True;continue
            if identity not in entities:entities[identity]={'id':ident,'name':r['name'],'kind':'cte' if r['local_cte'] else 'function_reference' if r.get('function_call') else 'unknown','temporary':parts[-1].startswith(('#','@')),'definitions':[],'references':[],'resolution':resolution}
            e=entities[identity];record={'source':q['source'],'query_id':q['id'],'line':q['line']+r['line']-1,'usage':r['usage'],'resolution':resolution};e['references'].append(record);g.node(ident,e['name'],'entity',entity_kind=e['kind'])
            if r['usage'] in ('READ','REFERENCE'):g.edge(ident,q['id'],'reads',method='SQL lexical reference',**record)
            elif r['usage']=='CALL':g.edge(q['id'],ident,'calls',method='SQL lexical procedure call',**record)
            else:g.edge(q['id'],ident,'writes',method='SQL lexical '+r['usage'],**record)
            for d in e['definitions']:
                if d['source']!=q['source']:deps.append({'consumer':q['source'],'provider':d['source'],'object':e['name'],'method':resolution})
    return list(entities.values()),deps

def scope_plan(names,deps,entries):
    adj={f:set() for f in names}
    for d in deps:adj[d['consumer']].add(d['provider'])
    reached=set(entries);queue=list(entries)
    while queue:
        for p in adj[queue.pop()]:
            if p not in reached:reached.add(p);queue.append(p)
    index=0;indices={};low={};stack=[];active=set();groups=[]
    def visit(n):
        nonlocal index
        indices[n]=low[n]=index;index+=1;stack.append(n);active.add(n)
        for p in sorted(adj[n]&reached):
            if p not in indices:visit(p);low[n]=min(low[n],low[p])
            elif p in active:low[n]=min(low[n],indices[p])
        if low[n]==indices[n]:
            group=[]
            while True:
                p=stack.pop();active.remove(p);group.append(p)
                if p==n:break
            groups.append(sorted(group))
    for n in sorted(reached):
        if n not in indices:visit(n)
    owner={f:i for i,g in enumerate(groups) for f in g};remaining=set(range(len(groups)));done=set();waves=[]
    while remaining:
        ready=sorted(i for i in remaining if all(owner[p] in done or owner[p]==i for f in groups[i] for p in adj[f]&reached))
        if not ready:break
        waves.append({'wave':len(waves)+1,'groups':[{'files':groups[i],'cycle':len(groups[i])>1} for i in ready]});done.update(ready);remaining.difference_update(ready)
    return {'entrypoints':entries,'files':[{'source':f,'status':'entry_point' if f in entries else 'referenced_by_entrypoint' if f in reached else 'not_reached_in_uploaded_graph' if entries else 'scope_not_selected'} for f in names],'waves':waves,'dependencies':deps,'method':'Reachability through uploaded static references. Not reached does not mean unused. Waves are planning groups, not executable orchestration.'}

def numeric(v,label,integer=False):
    if isinstance(v,bool):raise ValueError(label+' must be numeric.')
    try:n=Decimal(str(v))
    except (InvalidOperation,ValueError):raise ValueError(label+' must be numeric.') from None
    if not n.is_finite() or abs(n.as_tuple().exponent)>12 or len(n.as_tuple().digits)>30 or n<0 or n>Decimal('1e18') or integer and n!=n.to_integral_value():raise ValueError(label+' must be a bounded nonnegative '+('integer' if integer else 'number')+'.')
    return int(n) if integer else str(n)

def timestamp(v,label):
    if not isinstance(v,str):raise ValueError(label+' requires ISO time with an offset.')
    try:d=datetime.fromisoformat(v.replace('Z','+00:00'))
    except ValueError:raise ValueError(label+' requires ISO time with an offset.') from None
    if d.tzinfo is None:raise ValueError(label+' requires an explicit timezone offset.')
    return d.astimezone(timezone.utc).isoformat()

def workload_input(payload):
    data=payload.get('data');evidence=payload.get('evidence')
    if not isinstance(evidence,str) or not evidence.strip() or len(evidence)>4000:raise ValueError('Record workload export origin/window as evidence, up to 4000 characters.')
    if not isinstance(data,dict) or data.get('format') not in ('csv','json') or not isinstance(data.get('content'),str) or len(data['content'].encode())>2*1024*1024:raise ValueError('Supply CSV/JSON workload text up to 2 MB.')
    if data['format']=='csv':
        csv.field_size_limit(2*1024*1024)
        reader=csv.reader(StringIO(data['content'].lstrip('\ufeff')),strict=True)
        try:columns=next(reader)
        except StopIteration:raise ValueError('CSV header required.') from None
        if len(columns)!=len(set(columns)):raise ValueError('Duplicate CSV headers.')
        raw=[]
        for r in reader:
            if len(r)!=len(columns):raise ValueError('CSV row width differs from header.')
            raw.append(dict(zip(columns,r)))
            if len(raw)>5000:raise ValueError('At most 5000 workload rows per import.')
    else:raw=strict_json(data['content'])
    if not isinstance(raw,list) or len(raw)>5000:raise ValueError('Workload JSON must be an array of at most 5000 records.')
    rows=[]
    for r in raw:
        if not isinstance(r,dict):raise ValueError('Workload rows must be objects.')
        text=r.get('query_text')
        if not isinstance(text,str) or not text.strip() or len(text)>200000:raise ValueError('Each row needs query_text up to 200,000 characters.')
        start=timestamp(r.get('interval_start'),'interval_start');end=timestamp(r.get('interval_end'),'interval_end')
        if start>=end:raise ValueError('interval_end must follow interval_start.')
        clean={'query_hash':query_hash(text),'query_text':text,'interval_start':start,'interval_end':end,'execution_count':numeric(r.get('execution_count'),'execution_count',True)}
        for metric in ('total_duration_ms','total_cpu_ms','logical_reads'):clean[metric]=None if r.get(metric) in (None,'') else numeric(r[metric],metric,metric=='logical_reads')
        if not clean['execution_count'] and any(Decimal(str(clean[m] or 0)) for m in ('total_duration_ms','total_cpu_ms','logical_reads')):raise ValueError('Zero-execution rows cannot have positive totals.')
        for k in ('application','user'):
            v=r.get(k,'')
            if not isinstance(v,str) or len(v)>300:raise ValueError(k+' must be text up to 300 characters.')
            clean[k]=v or '(not supplied)'
        rows.append(clean)
    return {'rows':rows,'evidence':evidence.strip(),'imported_at':now(),'sha256':hashlib.sha256(data['content'].encode()).hexdigest(),'verified_by_app':False}

def workload_report(workload,units):
    with localcontext() as context:
        context.prec=50
        if workload is None:return {'available':False,'note':'Import evidence for runtime usage, execution counts, CPU totals and logical reads. Static code cannot establish runtime usage.'}
        lookup=defaultdict(set)
        for q in units:lookup[q['query_hash']].add(q['source'])
        rows=workload['rows'];observed={r['query_hash'] for r in rows if r['execution_count']};keys=('execution_count','total_duration_ms','total_cpu_ms','logical_reads');totals={k:Decimal(0) for k in keys};present={k:False for k in keys};groups={k:{} for k in ('application','user','file','interval')}
        for r in rows:
            files=lookup.get(r['query_hash'],set());file=next(iter(files)) if len(files)==1 else '(ambiguous duplicate SQL across sources)' if files else '(unmatched query text)'
            for k in keys:
                if r[k] is not None:totals[k]+=Decimal(str(r[k]));present[k]=True
            for kind,label in [('application',r['application']),('user',r['user']),('file',file),('interval',r['interval_start']+' / '+r['interval_end'])]:
                target=groups[kind].setdefault(label,{k:Decimal(0) for k in keys})
                for k in keys:
                    if r[k] is not None:target[k]+=Decimal(str(r[k]))
        rankings={}
        for kind,entries in groups.items():
            ranking=[{'label':label,**{k:str(v) if present[k] else None for k,v in metrics.items()},'shares_percent':{k:float(round(v/totals[k]*100,2)) if totals[k] and present[k] else None for k,v in metrics.items()}} for label,metrics in entries.items()]
            rankings[kind]=sorted(ranking,key=lambda r:Decimal(r['total_cpu_ms'] or r['total_duration_ms'] or r['execution_count']),reverse=True)[:100]
        return {'available':True,'rows':len(rows),'evidence':workload['evidence'],'imported_at':workload['imported_at'],'sha256':workload['sha256'],'verified_by_app':False,'totals':{k:str(v) if present[k] else None for k,v in totals.items()},'rankings':rankings,'ranking_group_counts':{k:len(v) for k,v in groups.items()},'timeline':[{'label':label,**{k:str(v) if present[k] else None for k,v in metrics.items()}} for label,metrics in sorted(groups['interval'].items())],'observed_hashes':sorted(observed),'matched_uploaded_unique_texts':len(observed & set(lookup)),'unmatched_unique_texts':len(observed-set(lookup)),'note':'Observed means normalized query text in imported evidence; literals are preserved. Duplicate SQL prevents unique file attribution. Shares are of imported totals, not CPU utilization. Overlapping/repeated exported rows are summed, not deduplicated. Not observed never proves unused.'}

def recommendations(units,packages):
    items=[]
    for q in units:
        for f in q['findings']:items.append({'id':uid('rec',q['id'],f['code']),'source':q['source'],'query_id':q['id'],'line':q['line']+(f.get('lines') or [1])[0]-1,'location':q['location'],'severity':f['severity'],'category':'compatibility','title':f['title'],'action':f['action'],'evidence':f['code']})
        flags=q['pattern_flags'];patterns=[]
        if flags['select_star']:patterns.append(('projection','low','Review SELECT *','Project required columns after checking downstream schema dependencies.'))
        if flags['leading_wildcard']:patterns.append(('leading-wildcard','medium','Leading-wildcard search','Inspect target query plans and data skipping; a leading-wildcard scan may remain expensive.'))
        if q['metrics']['cursors'] or q['metrics']['loops']:patterns.append(('set-based','high','Review procedural iteration','Evaluate set-based SQL/Spark while preserving order, transactions and side effects.'))
        if flags['distinct']:patterns.append(('dedup','low','Review deduplication requirements','Measure DISTINCT cost; remove it only after proving duplicate semantics are preserved.'))
        for code,severity,title,action in patterns:items.append({'id':uid('rec',q['id'],code),'source':q['source'],'query_id':q['id'],'line':q['line'],'location':q['location'],'severity':severity,'category':'optimization_review','title':title,'action':action,'evidence':'Lexical pattern '+code})
    for p in packages:
        for f in p['findings']:items.append({'id':uid('rec',p['source'],f['code']),'source':p['source'],'query_id':None,'line':None,'location':'SSIS inventory','severity':f['severity'],'category':'ssis','title':f['title'],'action':f['action'],'evidence':f['code']})
    return sorted(items,key=lambda r:({'high':0,'medium':1,'low':2}.get(r['severity'],3),r['source'],r['id']))

def build(run,settings,workload):
    g=Graph();units,packages,links,truncated=extract(run['files'],g);entities,deps=entities_for(units,g);names=[f['name'] for f in run['files']];unresolved=[]
    for link in links:
        matches=[f for f in names if f.casefold()==link['target_file'].casefold()]
        if len(matches)==1:deps.append({'consumer':link['consumer'],'provider':matches[0],'method':link['method']});g.edge(uid('file',link['consumer']),uid('file',matches[0]),'calls_package','process',method=link['method'])
        else:unresolved.append(link)
    deps=list({json.dumps(d,sort_keys=True):d for d in deps}.values());scope=scope_plan(names,deps,settings.get('entrypoints',[]));runtime=workload_report(workload,units);observed=set(runtime.get('observed_hashes',[]));sources=defaultdict(set)
    for q in units:sources[q['query_hash']].add(q['source'])
    files=[];metrics=('conditions','cursors','loops','goto','dynamic_exec')
    for f in run['files']:
        qs=[q for q in units if q['source']==f['name']];hashes={q['query_hash'] for q in qs}
        files.append({'name':f['name'],'kind':'ssis' if f['name'].lower().endswith('.dtsx') else 'sql','lines':len(f['sql'].splitlines()),'statement_units':len(qs),'unique_query_texts':len(hashes),'observed_unique_texts':len(hashes & observed) if workload is not None else None,'unambiguous_observed_texts':sum(h in observed and len(sources[h])==1 for h in hashes) if workload is not None else None,'scope_status':next(x['status'] for x in scope['files'] if x['source']==f['name']),'complexity':next((s['complexity'] for s in run['sources'] if s['name']==f['name']),None),'metrics':{k:sum(q['metrics'][k] for q in qs) for k in metrics}})
    for q in units:q['workload_status']='unknown' if workload is None else 'observed_text_ambiguous_source' if q['query_hash'] in observed and len(sources[q['query_hash']])>1 else 'observed_text' if q['query_hash'] in observed else 'not_observed_in_import'
    recs=recommendations(units,packages)
    summary={'files':len(files),'lines':sum(f['lines'] for f in files),'statement_units':len(units),'unique_query_texts':len({q['query_hash'] for q in units}),'units_with_lexical_issues':sum(bool(q['issues']) for q in units),'declared_procedures':sum(e['kind']=='procedure' for e in entities),'ssis_packages':len(packages),'ssis_tasks':sum(len(p['tasks']) for p in packages),'entities':len(entities),'declared_tables':sum(e['kind']=='table' and bool(e['definitions']) for e in entities),'declared_views':sum(e['kind']=='view' for e in entities),'reference_only_objects':sum(not e['definitions'] and e['kind']!='cte' for e in entities),'recommendations':len(recs),'metrics':{k:sum(f['metrics'][k] for f in files) for k in metrics},'statement_categories':dict(Counter(q['category'] for q in units)),'complexity':dict(Counter(q['complexity'] for q in units)),'entity_usage':dict(Counter(r['usage'] for e in entities for r in e['references']))}
    for q in units:q.pop('definitions');q.pop('references')
    return {'run_id':run['id'],'label':run['label'],'generated_at':now(),'fingerprint':fingerprint(run['files']),'summary':summary,'files':files,'queries':units,'entities':entities,'packages':packages,'lineage':{'nodes':list(g.nodes.values()),'edges':list(g.edges.values()),'truncated':g.truncated,'method':'SQL object-level lexical references and SSIS component/control XML; not column-level or live-database lineage.'},'scope':scope,'unresolved_package_references':unresolved,'recommendations':recs,'workload':runtime,'truncated':truncated or g.truncated,'method':'Static lexical SQL analysis and SSIS inventory, with optional imported workload evidence. Procedure/compound bodies remain one statement unit; this is not grammar-parser certification.','limitations':['Reference-only objects are not proven missing from SQL Server; dynamic SQL and schema/database context can prevent resolution.','Not observed in imported workload never means unused; duplicate SQL can prevent file attribution.','Scope waves are incomplete static planning groups, not execution dependencies; review cycles and external references.','CPU/duration are imported totals; logical reads are pages/counts, not bytes or live utilization. No savings are estimated.','Only SQL Server SQL and SSIS are assessed. Recommendations are review actions, not tested automatic optimizations.']}

class InsightsService:
    def __init__(self,runs):
        self.runs=runs
        with runs.db() as db:db.execute('CREATE TABLE IF NOT EXISTS assessment_evidence(run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,version INTEGER NOT NULL,settings TEXT NOT NULL,workload TEXT)')
    def _run(self,run_id):
        run=self.runs.get(run_id)
        if run['status'] in ACTIVE:raise Conflict('Wait for this migration run to finish before loading assessment insights.')
        return run
    def get(self,run_id):
        run=self._run(run_id)
        with self.runs.db() as db:row=db.execute('SELECT * FROM assessment_evidence WHERE run_id=?',(run_id,)).fetchone()
        result=build(run,json.loads(row['settings']) if row else {'entrypoints':[]},json.loads(row['workload']) if row and row['workload'] else None);result['version']=row['version'] if row else 0
        return result
    def save(self,run_id,payload,workload_action=False):
        if not isinstance(payload,dict):raise ValueError('Invalid assessment evidence request.')
        run=self._run(run_id);settings=None;workload=workload_input(payload) if workload_action else None
        if not workload_action:
            entries=payload.get('entrypoints')
            if not isinstance(entries,list) or any(not isinstance(e,str) or e not in [f['name'] for f in run['files']] for e in entries) or len(entries)!=len(set(entries)):raise ValueError('Select unique entrypoint files from this run.')
            settings={'entrypoints':entries}
        with self.runs.db() as db:
            row=db.execute('SELECT * FROM assessment_evidence WHERE run_id=?',(run_id,)).fetchone();version=row['version'] if row else 0
            if payload.get('version')!=version:raise Conflict('Assessment evidence changed in another tab; reload it.')
            if settings is None:settings=json.loads(row['settings']) if row else {'entrypoints':[]}
            raw=json.dumps(workload) if workload_action else row['workload'] if row else None
            db.execute('INSERT OR REPLACE INTO assessment_evidence VALUES(?,?,?,?)',(run_id,version+1,json.dumps(settings),raw))
        return self.get(run_id)
    def clear_workload(self,run_id,payload):
        if not isinstance(payload,dict):raise ValueError('Invalid clear-workload request.')
        self._run(run_id)
        with self.runs.db() as db:
            row=db.execute('SELECT * FROM assessment_evidence WHERE run_id=?',(run_id,)).fetchone()
            if not row or payload.get('version')!=row['version']:raise Conflict('Reload assessment evidence before clearing workload.')
            db.execute('UPDATE assessment_evidence SET workload=NULL,version=version+1 WHERE run_id=?',(run_id,))
        return self.get(run_id)
    def export(self,run_id):
        report=self.get(run_id)
        def text_csv(columns,rows):
            out=StringIO();writer=csv.writer(out);writer.writerow(columns)
            for r in rows:
                values=[]
                for c in columns:
                    v=r.get(c,'');v='' if v is None else str(v)
                    values.append("'"+v if v.lstrip().startswith(('=','+','-','@')) else v)
                writer.writerow(values)
            return out.getvalue()
        return {'files':[{'name':'assessment.json','sql':json.dumps(report,indent=2)},{'name':'assessment.html','sql':html_report(report)},{'name':'queries.csv','sql':text_csv(['source','line','location','category','complexity','query_hash','workload_status','sql_preview'],report['queries'])},{'name':'entities.csv','sql':text_csv(['name','kind','temporary','resolution'],report['entities'])},{'name':'recommendations.csv','sql':text_csv(['source','line','severity','category','title','action','evidence'],report['recommendations'])}],'report':{'run_id':run_id,'method':report['method'],'truncated':report['truncated']}}

def html_report(report):
    def table(headers,rows):return '<table><thead><tr>'+''.join('<th>'+escape(h)+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+escape(str(v if v is not None else 'Unknown'))+'</td>' for v in r)+'</tr>' for r in rows)+'</tbody></table>'
    s=report['summary'];body='<h1>Crosslake assessment</h1><h2>'+escape(report['label'])+'</h2><p>'+escape(report['method'])+'</p><p>Generated '+escape(report['generated_at'])+'</p>'
    body+=table(['Metric','Value'],[(k,v) for k,v in s.items() if not isinstance(v,dict)])
    body+='<h2>Files and scope</h2>'+table(['File','Units','Unique text','Complexity','Scope'],[(f['name'],f['statement_units'],f['unique_query_texts'],f['complexity'],f['scope_status']) for f in report['files']])
    body+='<h2>Entities</h2>'+table(['Object','Kind','Resolution','References'],[(e['name'],e['kind'],e['resolution'],len(e['references'])) for e in report['entities']])
    body+='<h2>Recommendations</h2>'+table(['Source','Line','Severity','Action'],[(r['source'],r['line'],r['severity'],r['action']) for r in report['recommendations']])
    body+='<h2>Workload evidence</h2><p>'+escape(report['workload'].get('evidence',report['workload'].get('note','')))+'</p>'
    if report['workload']['available']:body+=table(['Metric','Imported total'],report['workload']['totals'].items())
    nodes={n['id']:n['label'] for n in report['lineage']['nodes']};body+='<h2>Lineage edges</h2>'+table(['From','Relation','To','Evidence'],[(nodes[e['from']],e['kind'],nodes[e['to']],e.get('method','')) for e in report['lineage']['edges']])
    body+='<h2>Limits</h2>'+''.join('<p>'+escape(v)+'</p>' for v in report['limitations'])
    if report['truncated']:body+='<p><strong>Analysis limits reached: this report is incomplete.</strong></p>'
    return '<!doctype html><html lang="en"><meta charset="utf-8"><title>Crosslake assessment</title><style>body{font:14px system-ui;color:#1e3558;max-width:1200px;margin:30px auto;padding:20px}h1{border-bottom:4px solid #ff7518}table{width:100%;border-collapse:collapse;margin:15px 0;font-size:12px}td,th{padding:9px;border:1px solid #dce2eb;text-align:left;overflow-wrap:anywhere}th{background:#edf1f8}tr{break-inside:avoid}@media print{body{margin:0}}</style>'+body+'</html>'
