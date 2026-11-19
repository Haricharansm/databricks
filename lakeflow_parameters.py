"""Explicit, nonsecret package inputs for static positional Execute SQL bindings."""
import json
import re
from ci_read_query import mask_sql
from ssis import local,prop


def input_bindings(root):
    variables={prop(v,'Namespace')+'::'+prop(v,'ObjectName'):v for container in root if local(container.tag)=='Variables' for v in container if local(v.tag)=='Variable'}
    result={};used={}
    written={prop(n,'DtsVariableName') for n in root.iter() if local(n.tag)=='ResultBinding'}
    for node in root.iter():
        if local(node.tag)!='Executable' or node is root:continue
        bindings=[n for n in node.iter() if local(n.tag)=='ParameterBinding']
        if not bindings:continue
        if 'ExecuteSQLTask' not in prop(node,'ExecutableType'):raise ValueError('Input bindings require Execute SQL tasks.')
        task=prop(node,'refId') or prop(node,'DTSID');entries=[]
        for binding in bindings:
            variable=prop(binding,'DtsVariableName');v=variables.get(variable)
            if v is None or variable in written:raise ValueError('Input variable must be package-scoped and must not be written by another task.')
            if prop(binding,'ParameterDirection')!='Input':raise ValueError('Only explicit Input parameter direction is supported.')
            if not re.fullmatch(r'User::[A-Za-z_][A-Za-z0-9_]{0,100}',variable):raise ValueError('Use a simple qualified User input variable.')
            if any(prop(v,k,'').lower() in ('true','1') for k in ('Sensitive','EvaluateAsExpression')):raise ValueError('Sensitive/expression inputs require external secret and expression orchestration.')
            values=[x for x in v if local(x.tag)=='VariableValue'];typ=prop(values[0],'DataType') if len(values)==1 else ''
            binding_type=prop(binding,'DataType')
            if not (typ in ('3','20') and binding_type==typ or typ=='8' and binding_type in ('8','129','130')):raise ValueError('Input bindings support matching Int32/Int64 or string variable types only.')
            ordinal=prop(binding,'ParameterName')
            if not re.fullmatch(r'[0-9]{1,3}',ordinal):raise ValueError('Only zero-based positional input bindings are supported.')
            entries.append({'variable':variable,'data_type':typ,'ordinal':int(ordinal),'widget':'input_'+variable.replace('::','_')});used[variable]=v
        entries.sort(key=lambda e:e['ordinal'])
        if [e['ordinal'] for e in entries]!=list(range(len(entries))):raise ValueError('Input ordinals must be unique and contiguous from zero.')
        result[task]=entries
    return result,used


def bind_code(queries,entries,values):
    required={e['variable'] for e in entries}
    if not isinstance(values,dict) or set(values)!=required:raise ValueError('Supply explicit input values for exactly these variables: '+', '.join(sorted(required)))
    lines=[];parameters={};seen=set();argument_names=[]
    for e in entries:
        value=values[e['variable']];typ=e['data_type'];widget=e['widget'];ordinal=e['ordinal']
        if typ in ('3','20'):
            bits=32 if typ=='3' else 64
            if type(value) is not int or not -(2**(bits-1))<=value<2**(bits-1):raise ValueError('Integer input is outside its declared range: '+e['variable'])
        elif not isinstance(value,str) or len(value)>4000:raise ValueError('String input must be text up to 4000 characters.')
        if widget not in seen:
            lines.extend(['dbutils.widgets.text('+repr(widget)+', '+repr(str(value))+')','_input_'+str(ordinal)+' = dbutils.widgets.get('+repr(widget)+')'])
            if typ in ('3','20'):
                lines.extend(['if not __import__("re").fullmatch(r"-?[0-9]+", _input_'+str(ordinal)+'): raise ValueError("Invalid integer widget value")','_input_'+str(ordinal)+' = int(_input_'+str(ordinal)+')','if not -(2**'+str(bits-1)+') <= _input_'+str(ordinal)+' < 2**'+str(bits-1)+': raise ValueError("Integer widget out of range")'])
            else:lines.append('if len(_input_'+str(ordinal)+') > 4000: raise ValueError("String widget exceeds 4000 characters")')
            seen.add(widget);parameters[widget]=str(value)
        else:
            first=next(x['ordinal'] for x in entries if x['widget']==widget);lines.append('_input_'+str(ordinal)+' = _input_'+str(first))
        argument_names.append(repr('crosslake_p'+str(ordinal))+': _input_'+str(ordinal))
    if len(queries)!=1:raise ValueError('Input binding currently requires one static SQL statement per task.')
    query=queries[0];positions=[i for i,c in enumerate(mask_sql(query)) if c=='?']
    if len(positions)!=len(entries):raise ValueError('Converted SQL placeholder count does not match SSIS input ordinals; no interpolation was attempted.')
    for i,pos in reversed(list(enumerate(positions))):query=query[:pos]+':crosslake_p'+str(i)+query[pos+1:]
    lines.append('display(spark.sql('+repr(query)+', args={'+', '.join(argument_names)+'}))')
    return '\n'.join(lines)+'\n',parameters
