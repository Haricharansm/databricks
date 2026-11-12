"""Repairs for SSIS notebooks that BladeBridge leaves incomplete.

BladeBridge emits a placeholder (`%DF% = f\"\"\"SELECT * FROM \"\"\"`) for components it cannot convert,
which is not valid Python. This module:

* replaces CozyRoc REST Source cells that read a SharePoint list ("Site | List Item") with
  Microsoft Graph code that reads the same list and produces the same output columns;
* replaces any other unconverted placeholder with a clear NotImplementedError, so the notebook is
  valid Python and fails loudly at that step instead of with a syntax error;
* removes unused template imports (`from curses import meta`, `from logging import exception`).

Every change is reported. Connection details that live outside the package (project connection
managers, credentials) are never guessed: they become notebook widgets and Databricks secrets.
"""
import ast
import keyword
import re
from ssis import parse_package, local

CELL_BREAK = '# COMMAND ----------'
NODE = re.compile(r'#\s*Processing node\s+(.+?),\s*type\s+(\w+)', re.I)
PLACEHOLDER = re.compile(r'^\s*%DF%\s*=',re.M)
UNUSED_IMPORTS = ('from curses import meta', 'from logging import exception')


def _clean(value):
    return (value or '').strip()


def component_specs(dtsx_text):
    """Return {refId: spec} for every data flow component in a DTSX package."""
    root = parse_package(dtsx_text)
    specs = {}
    for comp in (n for n in root.iter() if local(n.tag)=='component'):
        descendants=list(comp.iter())
        props = {p.get('name'): _clean(p.text) for p in descendants if local(p.tag)=='property' and p.get('name')}
        outputs = [o for o in descendants if local(o.tag)=='output' and o.get('isErrorOut','false').lower() not in ('true','1')]
        column_nodes=[n for n in outputs[0].iter() if local(n.tag)=='outputColumn'] if outputs else []
        columns = [n.get('name') for n in column_nodes]
        column_specs=[{'name':n.get('name'),'type':n.get('dataType','').lower(),'length':n.get('length','')} for n in column_nodes]
        conn = next((c.get('connectionManagerRefId') or c.get('connectionManagerID') for c in descendants if local(c.tag)=='connection'), None)
        ref = comp.get('refId') or comp.get('name') or ''
        specs[ref] = {'ref': ref, 'name': comp.get('name') or ref, 'class': comp.get('componentClassID') or '',
                      'type': props.get('UserComponentTypeName', ''), 'props': props, 'columns': columns, 'column_specs':column_specs,
                      'output': outputs[0].get('name') if outputs else '', 'connection': conn or ''}
    return specs


def _find_spec(path, specs):
    path = path.strip()
    if path in specs:
        return specs[path]
    tail = path.replace('/', '\\').split('\\')[-1]
    matches = [s for s in specs.values() if s['name'] == tail]
    return matches[0] if len(matches) == 1 else None


def _variable(cell, path):
    m = re.search(r'^(\w+)\s*=\s*spark\.sql\(', cell, re.M) or re.search(r"^(\w+)\.createOrReplaceTempView\(", cell, re.M)
    value=m.group(1) if m else re.sub(r'\W+', '_', path).strip('_')
    return value if value.isidentifier() and not keyword.iskeyword(value) else 'ssis_output_'+value


def _py(value):
    return repr(value)


def _sharepoint_list_cell(path, spec, variable):
    params=dict(p.split('=',1) for p in spec['props'].get('ResourceParameter','').split(';') if '=' in p)
    list_name=params.get('list_id','').strip()
    try:batch=max(1,min(int(spec['props'].get('BatchSize') or 200),999))
    except ValueError:batch=200
    try:limit=max(0,int(spec['props'].get('LimitResult') or 0))
    except ValueError:limit=0
    # Constant substitutions are Python literals, never executable package text.
    values={'LIST':list_name,'BATCH':batch,'LIMIT':limit,'COLUMNS':spec['columns'],'COLUMN_SPECS':spec['column_specs'],'VIEW':variable}
    template=re.sub(r'__(LIST|BATCH|LIMIT|COLUMNS|COLUMN_SPECS|VIEW|VARIABLE)__',lambda m:variable if m.group(1)=='VARIABLE' else repr(values[m.group(1)]),GRAPH_TEMPLATE)
    return '# Processing node '+path.replace('\n',' ').replace('\r',' ')+', type MANAGEDCOMPONENTHOST\n'+template


GRAPH_TEMPLATE='''# Review required: experimental SharePoint Online list reader replacing a CozyRoc placeholder.
# Verify field mapping, filters, expressions, permissions and row parity against SSIS.
# Configure Graph Sites.Selected with site grants, or an admin-approved Sites.Read.All permission.
import json
import re
import time
import uuid
from urllib.parse import urlsplit, quote
import requests
from pyspark.sql.types import StructType, StructField, StringType

dbutils.widgets.text("sharepoint_host", "", "SharePoint host, e.g. contoso.sharepoint.com")
dbutils.widgets.text("sharepoint_site_path", "", "Site path, e.g. /sites/Legal")
dbutils.widgets.text("sharepoint_list", __LIST__, "List display name or ID")
dbutils.widgets.text("graph_secret_scope", "sharepoint", "Scope: tenant-id, client-id, client-secret")
_scope=dbutils.widgets.get("graph_secret_scope")
_host=dbutils.widgets.get("sharepoint_host").strip()
_site_path=dbutils.widgets.get("sharepoint_site_path").strip()
_list=dbutils.widgets.get("sharepoint_list").strip()
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*[.]sharepoint[.]com",_host) or not _site_path.startswith("/") or not _list:
    raise ValueError("Set a SharePoint Online host, absolute site path, and list name/ID.")
_tenant=str(uuid.UUID(dbutils.secrets.get(_scope,"tenant-id")))
_token=requests.post("https://login.microsoftonline.com/"+_tenant+"/oauth2/v2.0/token",
    data={"grant_type":"client_credentials","client_id":dbutils.secrets.get(_scope,"client-id"),
          "client_secret":dbutils.secrets.get(_scope,"client-secret"),"scope":"https://graph.microsoft.com/.default"},
    timeout=60,allow_redirects=False)
if 300 <= _token.status_code < 400:raise RuntimeError("OAuth redirects are not supported.")
_token.raise_for_status()
_headers={"Authorization":"Bearer "+_token.json()["access_token"]}

def _graph_get(url):
    parsed=urlsplit(url)
    if parsed.scheme!="https" or parsed.netloc!="graph.microsoft.com" or not parsed.path.startswith("/v1.0/"):
        raise ValueError("Unexpected Graph paging URL; credentials were not sent.")
    for attempt in range(4):
        response=requests.get(url,headers=_headers,timeout=120,allow_redirects=False)
        if response.status_code in (429,503) and attempt<3:
            delay=response.headers.get("Retry-After","2")
            time.sleep(min(30,max(1,int(delay))) if str(delay).isdigit() else 2)
            continue
        if 300 <= response.status_code < 400:raise RuntimeError("Graph redirects are not supported.")
        response.raise_for_status()
        return response.json()

def _pages(url):
    seen=set()
    while url:
        if url in seen or len(seen)>=1000:raise RuntimeError("Graph page loop or 1,000 page limit reached; no partial result accepted.")
        seen.add(url)
        body=_graph_get(url)
        yield body
        url=body.get("@odata.nextLink")

_site=_graph_get("https://graph.microsoft.com/v1.0/sites/"+_host+":"+quote(_site_path,safe="/"))
_base="https://graph.microsoft.com/v1.0/sites/"+quote(_site["id"],safe="")+"/lists/"
try:
    _list_id=str(uuid.UUID(_list))
except ValueError:
    _matches=[]
    for _page in _pages(_base+"?$select=id,displayName"):
        _matches.extend(x["id"] for x in _page.get("value",[]) if x.get("displayName")==_list)
    if len(_matches)!=1:raise ValueError("List display name must resolve to exactly one list; use its ID otherwise.")
    _list_id=_matches[0]
_items=[]
_limit=__LIMIT__
_url=_base+quote(_list_id,safe="")+"/items?$expand=fields&$top=__BATCH__"
for _page in _pages(_url):
    _items.extend(_page.get("value",[]))
    if _limit and len(_items)>=_limit:
        _items=_items[:_limit]
        break
    if len(_items)>100000:raise RuntimeError("100,000 item driver limit exceeded; use a production ingestion pipeline.")
if len(_items)>100000:raise RuntimeError("100,000 item driver limit exceeded; no partial result accepted.")
_columns=__COLUMNS__
_specs=__COLUMN_SPECS__
def _value(item,column):
    value=item
    for part in column.split("."):
        value=value.get(part) if isinstance(value,dict) else None
    if value is None:return None
    if isinstance(value,(dict,list)):raise ValueError("Complex Graph field requires an explicit mapping: "+column)
    return str(value)
_rows=[]
for _item in _items:
    _row=tuple(_value(_item,c) for c in _columns)
    for _value_text,_spec in zip(_row,_specs):
        if _value_text is not None and str(_spec["length"]).isdigit() and int(_spec["length"])>0 and len(_value_text)>int(_spec["length"]):
            raise ValueError("Graph field exceeds SSIS output length: "+_spec["name"])
    _rows.append(_row)
_schema=StructType([StructField(c,StringType(),True) for c in _columns])
__VARIABLE__=spark.createDataFrame(_rows,_schema)
__VARIABLE__.createOrReplaceTempView(__VIEW__)
print("Read",len(_items),"items from SharePoint list",_list)
'''


def _unsupported_cell(cell, path, kind, spec):
    detail = spec['type'] or spec['class'] if spec else kind
    lines = [line for line in cell.splitlines() if line.strip().startswith('#')]
    lines.append(f'# Automatically replaced an invalid placeholder: "{path}" ({detail}) was not converted.'.replace('\n',' ').replace('\r',' '))
    lines.append(f'raise NotImplementedError({_py(f"SSIS component {path} ({detail}) was not converted. Rewrite this cell in PySpark.")})')
    return '\n'.join(lines) + '\n'


def repair_notebook(text, specs):
    """Return (text, notes) with unconvertible component cells repaired."""
    notes = []
    cells = text.split(CELL_BREAK)
    for i, cell in enumerate(cells):
        node = NODE.search(cell)
        if not node or not PLACEHOLDER.search(cell):
            continue
        path, kind = node.group(1), node.group(2)
        spec = _find_spec(path, specs)
        params=dict(p.split('=',1) for p in spec['props'].get('ResourceParameter','').split(';') if '=' in p) if spec else {}
        simple_params=all(k.strip()=='list_id' or not v.strip() for k,v in params.items())
        has_custom_query=spec and any(re.search(r'filter|query|expression',k,re.I) and v.strip() for k,v in spec['props'].items())
        one_rest=sum(s['type'].startswith('CozyRoc.RestSource') for s in specs.values())==1
        if simple_params and not has_custom_query and one_rest and spec and spec['type'].startswith('CozyRoc.RestSource') and spec['props'].get('Resource', '')=='Site | List Item' and spec['columns'] and len(set(spec['columns']))==len(spec['columns']) and all(c['name'] and c['type'] in ('wstr','str','text','ntext') for c in spec['column_specs']):
            cells[i] = '\n\n' + _sharepoint_list_cell(path, spec, _variable(cell, path)) + '\n'
            notes.append(f'Rewrote "{spec["name"]}" (CozyRoc REST Source, SharePoint list) as Microsoft Graph code. '
                         'Fill in the SharePoint widgets and secret scope before running.')
        else:
            cells[i] = '\n\n' + _unsupported_cell(cell, path, kind, spec) + '\n'
            notes.append(f'"{path}" could not be converted; replaced its invalid placeholder with a clear error. Rewrite that cell manually.')
    fixed,sql_notes=fix_sql_cells(CELL_BREAK.join(cells))
    notes.extend('Fixed spark.sql in '+n for n in sql_notes)
    try:used={n.id for n in ast.walk(ast.parse(fixed)) if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Load)}
    except SyntaxError:used={'meta','exception'}
    for line in UNUSED_IMPORTS:
        if line.rsplit(' ',1)[-1] in used:continue
        pattern=re.compile(r'^'+re.escape(line)+r'[ \t]*\r?\n',re.M)
        if pattern.search(fixed):
            fixed=pattern.sub('',fixed)
            notes.append(f'Removed unused import "{line}".')
    return fixed, notes


# ---- spark.sql cells -------------------------------------------------------
SQL_CELL = re.compile(r'^(?P<var>\w+)[ \t]*=[ \t]*(?P<prefix>[fF]?)"""(?P<sql>(?:(?!""").)*?)"""[ \t]*\r?\n(?P<call>spark\.sql\((?P=var)\))[ \t]*$', re.M | re.S)
CREATE_TABLE_HEAD = re.compile(r'^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:EXTERNAL\s+)?TABLE\b', re.I)


def _fix_statement(statement):
    """Databricks-specific repairs for one SQL statement. Returns (statement, notes)."""
    from deploy import strip_null_constraints
    from sqlfix import _mask
    notes = []
    statement, count = strip_null_constraints(statement)
    if count:
        notes.append(f'removed {count} bare NULL column constraint(s)')
    masked = _mask(statement)
    if CREATE_TABLE_HEAD.match(masked) and re.search(r'\bDEFAULT\b', masked, re.I) \
            and 'allowcolumndefaults' not in statement.lower() and not re.search(r'\bTBLPROPERTIES\b', masked, re.I) \
            and not re.search(r'\bAS\s+SELECT\b', masked, re.I):
        statement = statement.rstrip() + "\nTBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported')"
        notes.append('enabled column defaults (delta.feature.allowColumnDefaults) for a DEFAULT column value')
    return statement, notes


def fix_sql_cells(text):
    """Split multi-statement spark.sql strings and repair their SQL. Returns (text, notes)."""
    from deploy import split_statements
    from sqlfix import _mask
    notes = []

    def rewrite(m):
        var, sql = m.group('var'), m.group('sql')
        try:
            value=ast.parse(var+' = '+m.group('prefix')+'\"\"\"'+sql+'\"\"\"').body[0].value
            if isinstance(value,ast.JoinedStr):
                if not all(isinstance(v,ast.Constant) for v in value.values):
                    notes.append(var+': dynamic f-string SQL was not rewritten; review statement boundaries manually')
                    return m.group(0)
                sql=''.join(v.value for v in value.values)
            elif isinstance(value,ast.Constant):sql=value.value
            else:return m.group(0)
        except (SyntaxError,ValueError):return m.group(0)
        masked = _mask(sql)
        # A ';' directly before ')' (for example "current_timestamp(););") is never valid.
        stray = [s.start() for s in re.finditer(r';(?=\s*\))', masked)]
        for pos in reversed(stray):
            sql = sql[:pos] + sql[pos + 1:]
        parts = split_statements(sql)
        if not parts:
            return m.group(0)
        fixed, changes = [], []
        for part in parts:
            part, part_notes = _fix_statement(part)
            fixed.append(part)
            changes.extend(part_notes)
        if stray:
            changes.insert(0, f'removed {len(stray)} stray semicolon(s) before ")"')
        if len(fixed) > 1:
            changes.insert(0, f'split {len(fixed)} statements into separate spark.sql calls (spark.sql runs one statement at a time)')
        if not changes and fixed == [sql.strip().rstrip(';').strip()] and sql == sql.strip() and not sql.rstrip().endswith(';'):
            return m.group(0)
        notes.append(f'{var}: ' + '; '.join(changes) if changes else f'{var}: removed trailing semicolon')
        if len(fixed) == 1:
            return f'{var} = {fixed[0]!r}\nspark.sql({var})'
        return '\n'.join(f'{var}_{i} = {s!r}\nspark.sql({var}_{i})' for i, s in enumerate(fixed, 1))

    return SQL_CELL.sub(rewrite, text), notes
