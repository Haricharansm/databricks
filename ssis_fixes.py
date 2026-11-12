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
import re
import xml.etree.ElementTree as ET

CELL_BREAK = '# COMMAND ----------'
NODE = re.compile(r'#\s*Processing node\s+(.+?),\s*type\s+(\w+)', re.I)
PLACEHOLDER = re.compile(r'%DF%')
UNUSED_IMPORTS = ('from curses import meta', 'from logging import exception')


def _clean(value):
    return (value or '').strip()


def component_specs(dtsx_text):
    """Return {refId: spec} for every data flow component in a DTSX package."""
    try:
        root = ET.fromstring(dtsx_text)
    except ET.ParseError:
        return {}
    specs = {}
    for comp in root.iter('component'):
        props = {p.get('name'): _clean(p.text) for p in comp.iter('property') if p.get('name')}
        outputs = [o for o in comp.iter('output') if o.get('isErrorOut') != 'true']
        columns = [oc.get('name') for oc in outputs[0].iter('outputColumn')] if outputs else []
        conn = next((c.get('connectionManagerRefId') or c.get('connectionManagerID') for c in comp.iter('connection')), None)
        ref = comp.get('refId') or comp.get('name') or ''
        specs[ref] = {'ref': ref, 'name': comp.get('name') or ref, 'class': comp.get('componentClassID') or '',
                      'type': props.get('UserComponentTypeName', ''), 'props': props, 'columns': columns,
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
    return m.group(1) if m else re.sub(r'\W+', '_', path).strip('_')


def _py(value):
    return repr(value)


def _sharepoint_list_cell(path, spec, variable):
    props = spec['props']
    params = dict(p.split('=', 1) for p in props.get('ResourceParameter', '').split(';') if '=' in p)
    list_name = params.get('list_id', '').strip()
    try:
        batch = max(1, min(int(props.get('BatchSize') or 200), 999))
    except ValueError:
        batch = 200
    try:
        limit = max(0, int(props.get('LimitResult') or 0))
    except ValueError:
        limit = 0
    columns = spec['columns']
    conn = spec['connection'] or 'the REST connection manager'
    return f'''# Processing node {path}, type MANAGEDCOMPONENTHOST
# Automatically rewritten: CozyRoc REST Source, resource "{props.get('Resource')}", list "{list_name}".
# Reads the SharePoint Online list through Microsoft Graph and returns the same output columns
# as the SSIS output "{spec['output']}".
# Connection details were in {conn}, which is not part of the package. Fill in the widgets below
# and store the app registration credentials in a Databricks secret scope. The app needs the
# Microsoft Graph application permission Sites.Read.All (or Sites.Selected for this site).
import requests

dbutils.widgets.text("sharepoint_host", "", "SharePoint host, e.g. contoso.sharepoint.com")
dbutils.widgets.text("sharepoint_site_path", "", "Site path, e.g. /sites/Legal")
dbutils.widgets.text("sharepoint_list", {_py(list_name)}, "List name or ID")
dbutils.widgets.text("graph_secret_scope", "sharepoint", "Secret scope with tenant-id, client-id, client-secret")

_scope = dbutils.widgets.get("graph_secret_scope")
_host = dbutils.widgets.get("sharepoint_host").strip()
_site_path = "/" + dbutils.widgets.get("sharepoint_site_path").strip().strip("/")
_list = dbutils.widgets.get("sharepoint_list").strip()
if not _host or _site_path == "/" or not _list:
    raise ValueError("Set the sharepoint_host, sharepoint_site_path and sharepoint_list widgets.")

_token = requests.post(
    f"https://login.microsoftonline.com/{{dbutils.secrets.get(_scope, 'tenant-id')}}/oauth2/v2.0/token",
    data={{"grant_type": "client_credentials",
          "client_id": dbutils.secrets.get(_scope, "client-id"),
          "client_secret": dbutils.secrets.get(_scope, "client-secret"),
          "scope": "https://graph.microsoft.com/.default"}},
    timeout=60,
)
_token.raise_for_status()
_headers = {{"Authorization": "Bearer " + _token.json()["access_token"]}}

_site = requests.get(f"https://graph.microsoft.com/v1.0/sites/{{_host}}:{{_site_path}}", headers=_headers, timeout=60)
_site.raise_for_status()
_url = (f"https://graph.microsoft.com/v1.0/sites/{{_site.json()['id']}}/lists/{{requests.utils.quote(_list)}}"
        f"/items?expand=fields&$top={batch}")
_limit = {limit}  # SSIS LimitResult; 0 means no limit
_items = []
while _url and (not _limit or len(_items) < _limit):
    _page = requests.get(_url, headers=_headers, timeout=120)
    _page.raise_for_status()
    _body = _page.json()
    _items.extend(_body.get("value", []))
    _url = _body.get("@odata.nextLink")
if _limit:
    _items = _items[:_limit]

# Same column names and string types as the SSIS output.
_columns = {_py(columns)}

def _value(item, column):
    value = item
    for part in column.split(".", 1):
        value = value.get(part) if isinstance(value, dict) else None
    return None if value is None else str(value)

from pyspark.sql.types import StructType, StructField, StringType
_schema = StructType([StructField(c, StringType(), True) for c in _columns])
{variable} = spark.createDataFrame([tuple(_value(i, c) for c in _columns) for i in _items], _schema)
{variable}.createOrReplaceTempView({_py(variable)})
print(f"Read {{len(_items)}} items from list {{_list}}")
'''


def _unsupported_cell(cell, path, kind, spec):
    detail = spec['type'] or spec['class'] if spec else kind
    lines = [line for line in cell.splitlines() if line.strip().startswith('#')]
    lines.append(f'# Automatically replaced an invalid placeholder: "{path}" ({detail}) was not converted.')
    lines.append(f'raise NotImplementedError({_py(f"SSIS component {path} ({detail}) was not converted. Rewrite this cell in PySpark.")})')
    return '\n'.join(lines) + '\n'


def repair_notebook(text, specs):
    """Return (text, notes) with unconvertible component cells repaired."""
    notes = []
    for line in UNUSED_IMPORTS:
        pattern = re.compile(r'^' + re.escape(line) + r'[ \t]*\r?\n', re.M)
        if pattern.search(text):
            text = pattern.sub('', text)
            notes.append(f'Removed unused import "{line}".')
    text, sql_notes = fix_sql_cells(text)
    notes.extend('Fixed spark.sql in ' + n for n in sql_notes)
    cells = text.split(CELL_BREAK)
    for i, cell in enumerate(cells):
        node = NODE.search(cell)
        if not node or not PLACEHOLDER.search(cell):
            continue
        path, kind = node.group(1), node.group(2)
        spec = _find_spec(path, specs)
        if spec and spec['type'].startswith('CozyRoc.RestSource') and spec['props'].get('Resource', '').startswith('Site | List Item'):
            cells[i] = '\n\n' + _sharepoint_list_cell(path, spec, _variable(cell, path)) + '\n'
            notes.append(f'Rewrote "{spec["name"]}" (CozyRoc REST Source, SharePoint list) as Microsoft Graph code. '
                         'Fill in the SharePoint widgets and secret scope before running.')
        else:
            cells[i] = '\n\n' + _unsupported_cell(cell, path, kind, spec) + '\n'
            notes.append(f'"{path}" could not be converted; replaced its invalid placeholder with a clear error. Rewrite that cell manually.')
    return CELL_BREAK.join(cells), notes


# ---- spark.sql cells -------------------------------------------------------
SQL_CELL = re.compile(r'^(?P<var>\w+) = f"""(?P<sql>.*?)"""[ \t]*\r?\n(?P<call>spark\.sql\((?P=var)\))[ \t]*$', re.M | re.S)
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
            return f'{var} = f"""{fixed[0]}"""\nspark.sql({var})'
        return '\n'.join(f'{var}_{i} = f"""{s}"""\nspark.sql({var}_{i})' for i, s in enumerate(fixed, 1))

    return SQL_CELL.sub(rewrite, text), notes
