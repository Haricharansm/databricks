"""Bounded reconciliation of uploaded result sets. This module never executes SQL."""
from collections import Counter
import csv
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from io import StringIO
import json
import re

MAX_DATA_BYTES=2*1024*1024
MAX_ROWS=10000
TYPES={'string','integer','decimal','boolean','date','timestamp'}


def strict_json(text):
    def pairs(values):
        result={}
        for key,value in values:
            if key in result:raise ValueError('Duplicate JSON field: '+key)
            result[key]=value
        return result
    def constant(value):raise ValueError('Non-finite JSON numbers are not supported.')
    return json.loads(text,parse_float=Decimal,parse_int=Decimal,parse_constant=constant,object_pairs_hook=pairs)


def definition(payload):
    if not isinstance(payload,dict):raise ValueError('Supply a test-case definition.')
    result={}
    for key,maximum in [('title',120),('source',160),('scenario',4000),('source_sql',20000),('target_sql',20000),('parameters',8000)]:
        value=payload.get(key,'{}' if key=='parameters' else '')
        if not isinstance(value,str) or len(value)>maximum:raise ValueError(f'{key} must be text up to {maximum} characters.')
        result[key]=value.strip() if key in ('title','source') else value
    if not result['title'] or not result['source']:raise ValueError('Select a source and name the test case.')
    try:
        if not isinstance(strict_json(result['parameters']),dict):raise ValueError('Parameters must be a JSON object.')
    except (ValueError,RecursionError) as exc:raise ValueError('Parameters must be a valid JSON object.') from exc
    mode=payload.get('mode','unordered')
    if mode not in ('unordered','ordered','keyed'):raise ValueError('Comparison mode must be unordered, ordered, or keyed.')
    result['mode']=mode
    columns=payload.get('columns')
    if not isinstance(columns,list) or not 1<=len(columns)<=100:raise ValueError('Declare between 1 and 100 result columns.')
    names=set();result['columns']=[]
    for column in columns:
        if not isinstance(column,dict):raise ValueError('Each column must have a name and type.')
        name=column.get('name');kind=column.get('type')
        if not isinstance(name,str) or not name or len(name)>128 or name in names:raise ValueError('Column names must be nonempty, unique, and at most 128 characters.')
        if not isinstance(kind,str) or kind not in TYPES:raise ValueError('Unsupported column type: '+str(kind))
        tolerance=column.get('tolerance','0')
        if not isinstance(tolerance,(str,int)) or isinstance(tolerance,bool) or len(str(tolerance))>256:raise ValueError('Tolerance must be a finite nonnegative decimal string.')
        try:tolerance=Decimal(str(tolerance))
        except InvalidOperation as exc:raise ValueError('Invalid numeric tolerance.') from exc
        if not tolerance.is_finite() or tolerance<0 or abs(tolerance.as_tuple().exponent)>100:raise ValueError('Invalid numeric tolerance.')
        if tolerance and (kind not in ('integer','decimal') or mode=='unordered'):raise ValueError('Nonzero tolerances require ordered or keyed numeric comparison.')
        nullable=column.get('nullable',True)
        if not isinstance(nullable,bool):raise ValueError('nullable must be true or false.')
        result['columns'].append({'name':name,'type':kind,'tolerance':str(tolerance),'nullable':nullable});names.add(name)
    keys=payload.get('key_columns',[])
    if not isinstance(keys,list) or any(not isinstance(k,str) or k not in names for k in keys) or len(set(keys))!=len(keys):raise ValueError('Key columns must be unique declared column names.')
    if mode=='keyed' and not keys:raise ValueError('Keyed comparison requires at least one key column.')
    if mode!='keyed' and keys:raise ValueError('Key columns apply only to keyed mode.')
    if any(c['name'] in keys and Decimal(c['tolerance']) for c in result['columns']):raise ValueError('Key columns must use exact comparison.')
    token=payload.get('null_token',r'\N')
    if not isinstance(token,str) or len(token)>32:raise ValueError('CSV null token must be text up to 32 characters.')
    result.update(key_columns=keys,null_token=token)
    return result


def dataset(payload):
    if not isinstance(payload,dict) or payload.get('format') not in ('csv','json') or not isinstance(payload.get('content'),str):
        raise ValueError('Supply a CSV or JSON result set with text content.')
    if len(payload['content'].encode('utf-8'))>MAX_DATA_BYTES:raise ValueError('Each result set is limited to 2 MB.')
    return {'format':payload['format'],'content':payload['content']}


def read_dataset(payload):
    text=payload['content'].lstrip('\ufeff')
    if payload['format']=='csv':
        reader=csv.reader(StringIO(text,newline=''),strict=True)
        try:columns=next(reader)
        except StopIteration:raise ValueError('CSV requires a header row.')
        if not columns or len(columns)>100 or len(set(columns))!=len(columns) or any(not c or len(c)>128 for c in columns):raise ValueError('CSV headers must be unique, nonempty column names (up to 100).')
        rows=[]
        for values in reader:
            if len(rows)>=MAX_ROWS:raise ValueError('Result sets are limited to 10,000 rows.')
            if len(values)!=len(columns):raise ValueError('CSV row has a different field count from its header.')
            rows.append(dict(zip(columns,values)))
    else:
        data=strict_json(text)
        if isinstance(data,dict) and set(data)=={'columns','rows'}:
            columns=data['columns'];rows=data['rows']
        elif isinstance(data,list) and data and isinstance(data[0],dict):columns=list(data[0]);rows=data
        else:raise ValueError('JSON must be a nonempty array of row objects, or {"columns":[...],"rows":[...]} for empty or nonempty results.')
        if not isinstance(columns,list) or not 1<=len(columns)<=100 or any(not isinstance(c,str) or not c or len(c)>128 for c in columns) or len(set(columns))!=len(columns):raise ValueError('Invalid JSON column names.')
        if not isinstance(rows,list) or len(rows)>MAX_ROWS or any(not isinstance(r,dict) for r in rows):raise ValueError('JSON rows must be objects (up to 10,000 rows).')
    return columns,rows


def normalize(value,column,csv_input,null_token):
    if value is None or csv_input and value==null_token:
        if not column['nullable']:raise ValueError('NULL is forbidden for this column.')
        return None
    kind=column['type']
    if kind=='string':
        if not isinstance(value,str):raise ValueError('Expected a string; no implicit numeric-to-string conversion.')
        return value
    if kind=='boolean':
        if isinstance(value,bool):return value
        if isinstance(value,str) and value.lower() in ('true','false'):return value.lower()=='true'
        raise ValueError('Expected true or false.')
    if kind in ('integer','decimal'):
        if isinstance(value,bool) or not isinstance(value,(str,int,Decimal)) or len(str(value))>256:raise ValueError('Expected a bounded finite number.')
        if isinstance(value,str) and not re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?',value.strip()):raise ValueError('Expected a standard decimal numeric literal.')
        try:number=Decimal(value)
        except InvalidOperation as exc:raise ValueError('Expected a valid number.') from exc
        if not number.is_finite() or abs(number.as_tuple().exponent)>100:raise ValueError('Non-finite or excessively large-exponent numbers are unsupported.')
        if kind=='integer' and number!=number.to_integral_value():raise ValueError('Expected an integer.')
        return number
    if not isinstance(value,str):raise ValueError('Dates and timestamps must be ISO text.')
    if kind=='date':
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):raise ValueError('Date must use YYYY-MM-DD.')
        return date.fromisoformat(value).isoformat()
    timestamp=datetime.fromisoformat(value.replace('Z','+00:00'))
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:raise ValueError('Timestamp requires an explicit UTC offset or Z.')
    # Do not silently truncate sub-microsecond precision in datetime.fromisoformat.
    fraction=re.search(r'[.,](\d+)',value)
    if fraction and len(fraction.group(1))>6:raise ValueError('Timestamp precision greater than six fractional digits requires explicit upstream normalization.')
    return timestamp.astimezone(timezone.utc).isoformat(timespec='microseconds')


def preview(value):
    if isinstance(value,Decimal):return str(value)
    if isinstance(value,str) and len(value)>500:return value[:500]+'…'
    return value


def compare(case,expected,actual):
    result={'status':'error','comparison_method':'uploaded_result_comparison','executed_by_app':False,'checks':[],
            'mismatches':[],'mismatch_count':0,'limitations':'A match compares only the supplied result snapshots under this test definition. It does not verify their provenance, execute SQL, prove complete equivalence, or validate side effects/transactions.'}
    try:
        ec,er=read_dataset(expected);ac,ar=read_dataset(actual);columns=case['columns'];names=[c['name'] for c in columns]
        shapes=[];shape_count=0;declared=set(names)
        for side,rows in [('expected',er),('actual',ar)]:
            for index,row in enumerate(rows):
                if set(row)!=declared:
                    shape_count+=1
                    if len(shapes)<20:shapes.append({'side':side,'row':index+1,'missing':[preview(n) for n in sorted(declared-set(row))[:20]],'extra':[preview(n) for n in sorted(set(row)-declared)[:20]]})
        schema_ok=set(ec)==set(ac)==declared and not shape_count
        result['checks']=[{'name':'row_count','passed':len(er)==len(ar),'expected':len(er),'actual':len(ar)},
                          {'name':'column_schema','passed':schema_ok,'expected':ec,'actual':ac,'declared':names,'invalid_row_count':shape_count,'row_samples':shapes}]
        result['row_counts']={'expected':len(er),'actual':len(ar)}
        if not schema_ok:
            result['status']='failed';return result
        normalized=[];errors=[];error_count=0
        for side,rows,payload in [('expected',er,expected),('actual',ar,actual)]:
            values=[]
            for index,row in enumerate(rows):
                typed=[]
                for column in columns:
                    try:typed.append(normalize(row[column['name']],column,payload['format']=='csv',case['null_token']))
                    except (ValueError,InvalidOperation,OverflowError) as exc:
                        error_count+=1
                        if len(errors)<20:errors.append({'side':side,'row':index+1,'column':column['name'],'message':str(exc)})
                        typed.append(None)
                values.append(tuple(typed))
            normalized.append(values)
        result['checks'].append({'name':'declared_types','passed':not error_count,'error_count':error_count,'samples':errors})
        if error_count:result['status']='failed';return result
        expected_rows,actual_rows=normalized
        nulls=lambda rows:{name:sum(row[i] is None for row in rows) for i,name in enumerate(names)}
        en,an=nulls(expected_rows),nulls(actual_rows)
        result['checks'].append({'name':'null_counts','passed':en==an,'expected':en,'actual':an})
        result['duplicate_rows']={'expected':len(er)-len(set(expected_rows)),'actual':len(ar)-len(set(actual_rows)),
                                  'method':'Exact normalized row duplicates; unordered value comparison preserves multiplicity.'}
        samples=[];mismatches=0
        def record(sample,count=1):
            nonlocal mismatches
            mismatches+=count
            if len(samples)<20:samples.append(sample)
        def values_equal(left,right,col):
            if left is None or right is None:return left is right
            if col['type'] in ('integer','decimal'):
                with localcontext() as context:
                    context.prec=1024
                    return abs(left-right)<=Decimal(col['tolerance'])
            return left==right
        if case['mode']=='unordered':
            missing=Counter(expected_rows)-Counter(actual_rows);extra=Counter(actual_rows)-Counter(expected_rows)
            for kind,rows in [('missing',missing),('unexpected',extra)]:
                for row,count in rows.items():record({'kind':kind,'row':{name:preview(row[i]) for i,name in enumerate(names)},'count':count},count)
        else:
            if case['mode']=='keyed':
                indexes=[names.index(name) for name in case['key_columns']]
                def indexed(rows):return {tuple(row[i] for i in indexes):row for row in rows}
                ei,ai=indexed(expected_rows),indexed(actual_rows)
                invalid=any(any(row[i] is None for i in indexes) for row in expected_rows+actual_rows) or len(ei)!=len(expected_rows) or len(ai)!=len(actual_rows)
                result['checks'].append({'name':'unique_nonnull_keys','passed':not invalid,'key_columns':case['key_columns']})
                if invalid:result['status']='failed';return result
                pairs=[]
                for key in sorted(ei.keys()|ai.keys(),key=repr):
                    if key not in ei or key not in ai:record({'kind':'unexpected_key' if key not in ei else 'missing_key','key':[preview(v) for v in key]})
                    else:pairs.append(([preview(v) for v in key],ei[key],ai[key]))
            else:pairs=[(i+1,left,right) for i,(left,right) in enumerate(zip(expected_rows,actual_rows))]
            for position,left,right in pairs:
                for i,column in enumerate(columns):
                    if not values_equal(left[i],right[i],column):record({'position':position,'column':column['name'],'expected':preview(left[i]),'actual':preview(right[i])})
        result['checks'].append({'name':'values','passed':not mismatches,'mode':case['mode'],'mismatch_count':mismatches})
        result.update(mismatches=samples,mismatch_count=mismatches,status='passed' if all(c['passed'] for c in result['checks']) else 'failed')
    except (ValueError,TypeError,csv.Error,RecursionError,InvalidOperation,OverflowError) as exc:result['error']=str(exc)
    return result
