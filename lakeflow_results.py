"""Conservative SSIS single-row, output-only integer result bindings for Lakeflow."""
import re
from ssis import local,prop
from ci_read_query import read_query


def result_bindings(root):
    """Validate package-scope outputs. No mutable variable reads or scope inference."""
    nodes={prop(n,'refId') or prop(n,'DTSID'):n for n in root.iter() if n is not root and local(n.tag)=='Executable'}
    variable_nodes=[n for n in root.iter() if local(n.tag)=='Variable']
    package_variables=[v for container in root if local(container.tag)=='Variables' for v in container if local(v.tag)=='Variable']
    if len(variable_nodes)!=len(package_variables):raise ValueError('Task-scoped variables require explicit notebook orchestration.')
    if len(package_variables)>250:raise ValueError('At most 250 package output variables are supported.')
    declarations={};excluded=set()
    for var in package_variables:
        name=prop(var,'ObjectName');namespace=prop(var,'Namespace');qualified=namespace+'::'+name
        if namespace!='User' or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,100}',name or ''):raise ValueError('Result outputs require simple package-scope User variable names.')
        if qualified.casefold() in declarations:raise ValueError('Duplicate package variable declaration.')
        if prop(var,'EvaluateAsExpression','').lower() in ('true','1') or prop(var,'Sensitive','').lower() in ('true','1') or prop(var,'ReadOnly','').lower() in ('true','1'):raise ValueError('Expression-based or sensitive variables cannot be exported as task values.')
        values=[n for n in var if local(n.tag)=='VariableValue']
        if len(values)!=1 or prop(values[0],'DataType') not in ('3','20'):raise ValueError('Single-row outputs currently support SSIS Int32 (3) and Int64 (20) variables only.')
        declarations[qualified.casefold()]={'variable':qualified,'data_type':prop(values[0],'DataType'),'guid':prop(var,'DTSID')}
        excluded.update(var.iter())
    plan={};writers=set();binding_nodes=set()
    for task_id,node in nodes.items():
        bindings=[n for n in node.iter() if local(n.tag)=='ResultBinding']
        if not bindings:continue
        data=[n for n in node.iter() if local(n.tag)=='SqlTaskData']
        if 'ExecuteSQLTask' not in prop(node,'ExecutableType') or len(data)!=1 or prop(data[0],'ResultType') not in ('1','ResultSetType_SingleRow'):raise ValueError('Result bindings require an Execute SQL single-row result set.')
        outputs=[];seen_results=set()
        for binding in bindings:
            var=prop(binding,'DtsVariableName');result=prop(binding,'ResultName');decl=declarations.get(var.casefold())
            if decl is None:raise ValueError('Result binding references an undeclared package-scope User variable: '+var)
            if var.casefold() in writers:raise ValueError('A package variable has multiple result writers; mutable state is not inferred.')
            if not re.fullmatch(r'(?:[0-9]{1,4}|[A-Za-z_][A-Za-z0-9_]{0,127})',result or ''):raise ValueError('ResultName must be a simple column name or zero-based ordinal.')
            identity=('ordinal',int(result)) if result.isdigit() else ('name',result.casefold())
            if identity in seen_results:raise ValueError('Duplicate result binding column.')
            seen_results.add(identity);writers.add(var.casefold());binding_nodes.add(binding)
            outputs.append({k:v for k,v in decl.items() if k!='guid'}|{'result_name':result})
        plan[task_id]=outputs
    if writers!=set(declarations):raise ValueError('Every package variable must have exactly one single-row result writer; input/default variables and mutable state require explicit orchestration.')
    # Reject all other references, even when a writer exists: no downstream reads are guessed.
    for n in root.iter():
        if n in excluded or n in binding_nodes:continue
        content=' '.join([n.text or '',*n.attrib.values()]).casefold()
        for decl in declarations.values():
            if decl['variable'].casefold() in content or decl['guid'] and decl['guid'].casefold() in content:raise ValueError('Variable '+decl['variable']+' is read/referenced outside its result binding; downstream variable consumers require explicit orchestration.')
    return plan


def output_code(queries,outputs,task_key):
    if len(queries)!=1:raise ValueError('Single-row bindings require exactly one static SELECT query.')
    read_query(queries[0])
    config=[dict(o,task_value_key='ssis_'+re.sub(r'[^A-Za-z0-9_]+','_',o['variable'])) for o in outputs]
    # Values are serialized as data, not interpolated Python identifiers or executable XML.
    code='''# Preserve declared SSIS single-row result outputs as Databricks task values.
_lf_result = spark.sql({query})
_lf_rows = _lf_result.take(2)
if len(_lf_rows) != 1:
    raise RuntimeError("SSIS single-row result requires exactly one row; no task values were published.")
_lf_values = {{}}
for _lf_binding in {bindings}:
    _lf_name = _lf_binding["result_name"]
    if _lf_name.isdigit():
        _lf_index = int(_lf_name)
        if _lf_index >= len(_lf_result.columns):
            raise RuntimeError("SSIS result ordinal is outside the query schema.")
    else:
        _lf_matches = [i for i, c in enumerate(_lf_result.columns) if c.casefold() == _lf_name.casefold()]
        if len(_lf_matches) != 1:
            raise RuntimeError("SSIS result column is missing or ambiguous: " + _lf_name)
        _lf_index = _lf_matches[0]
    _lf_value = _lf_rows[0][_lf_index]
    _lf_bits = 32 if _lf_binding["data_type"] == "3" else 64
    if type(_lf_value) is not int or not -(2 ** (_lf_bits - 1)) <= _lf_value < 2 ** (_lf_bits - 1):
        raise RuntimeError("SSIS integer result is NULL, has the wrong type, or exceeds its declared range: " + _lf_binding["variable"])
    _lf_values[_lf_binding["task_value_key"]] = _lf_value
for _lf_key, _lf_value in _lf_values.items():
    dbutils.jobs.taskValues.set(key=_lf_key, value=_lf_value)
print("Published SSIS task values:", _lf_values)
import json as _lf_json
dbutils.notebook.exit(_lf_json.dumps({{"crosslake_task_values": _lf_values}}))
'''.format(query=repr(queries[0]),bindings=repr(config))
    evidence=[dict(o,producer_task_key=task_key) for o in config]
    return code,evidence
