import contextlib
import io
import json
from types import SimpleNamespace
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from lakeflow_parameters import input_bindings,bind_code
from lakeflow_results import result_bindings
from runtime_audit import RUNTIME,instrument
from lakeflow import package_graph

XML='''<DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts" xmlns:SQLTask="www.microsoft.com/sqlserver/dts/tasks/sqltask" DTS:refId="Package" DTS:ExecutableType="Microsoft.Package"><DTS:Variables><DTS:Variable DTS:Namespace="User" DTS:ObjectName="BusinessDate"><DTS:VariableValue DTS:DataType="8">2026-01-31</DTS:VariableValue></DTS:Variable></DTS:Variables><DTS:Executables><DTS:Executable DTS:refId="Package\\Read" DTS:ObjectName="Read" DTS:ExecutableType="Microsoft.ExecuteSQLTask"><DTS:ObjectData><SQLTask:SqlTaskData SQLTask:SqlStatementSource="SELECT Amount FROM dbo.Ledger WHERE BusinessDate = ?"><SQLTask:ParameterBinding SQLTask:ParameterName="0" SQLTask:DtsVariableName="User::BusinessDate" SQLTask:ParameterDirection="Input" SQLTask:DataType="130" /></SQLTask:SqlTaskData></DTS:ObjectData></DTS:Executable></DTS:Executables></DTS:Executable>'''
class ParametersAuditTests(unittest.TestCase):
    def test_input_scope_is_supported_without_inferring_downstream_outputs(self):
        root=ET.fromstring(XML);plan,variables=input_bindings(root);self.assertIn('User::BusinessDate',variables);self.assertEqual(result_bindings(root),{});package_graph({'name':'Input.dtsx','sql':XML})
    def test_parameter_value_remains_bound_not_interpolated(self):
        entries=input_bindings(ET.fromstring(XML))[0]['Package\\Read'];value="2026-01-31' OR 1=1 --";code,params=bind_code(["SELECT '?' AS label, Amount FROM dbo.Ledger WHERE BusinessDate = ?"],entries,{'User::BusinessDate':value});calls=[];widgets={}
        def text(k,v):widgets[k]=v
        env={'dbutils':SimpleNamespace(widgets=SimpleNamespace(text=text,get=lambda k:widgets[k])),'spark':SimpleNamespace(sql=lambda sql,**kwargs:calls.append((sql,kwargs))),'display':lambda v:None};exec(code,env)
        self.assertNotIn(value,calls[0][0]);self.assertEqual(calls[0][1]['args']['crosslake_p0'],value);self.assertIn("SELECT '?'",calls[0][0])
    def test_declared_fstring_inputs_are_rebound_without_evaluation(self):
        from lakeflow import static_sql
        entries=input_bindings(ET.fromstring(XML))[0]['Package\\Read']
        sql=static_sql('q=f"SELECT Amount FROM dbo.Ledger WHERE BusinessDate = {BusinessDate}"\nspark.sql(q)',entries)
        self.assertEqual(sql,['SELECT Amount FROM dbo.Ledger WHERE BusinessDate = ?'])
        for cell in ['q=f"SELECT {unknown}"\nspark.sql(q)','q=f"SELECT {BusinessDate!r}"\nspark.sql(q)','q=f"SELECT {BusinessDate.upper()}"\nspark.sql(q)']:
            with self.assertRaises(ValueError):static_sql(cell,entries)
    def test_missing_values_ordinals_directions_and_sensitive_inputs_block(self):
        entries=input_bindings(ET.fromstring(XML))[0]['Package\\Read']
        with self.assertRaises(ValueError):bind_code(['SELECT ?'],entries,{})
        with self.assertRaises(ValueError):bind_code(['SELECT 1'],entries,{'User::BusinessDate':'x'})
        for xml in [XML.replace('ParameterName="0"','ParameterName="1"'),XML.replace('ParameterDirection="Input"','ParameterDirection="Output"'),XML.replace('DTS:ObjectName="BusinessDate"','DTS:ObjectName="BusinessDate" DTS:Sensitive="True"')]:
            with self.assertRaises(ValueError):input_bindings(ET.fromstring(xml))
    def test_audit_events_share_context_and_exclude_business_values(self):
        env={};exec(RUNTIME,env);stream=io.StringIO()
        with contextlib.redirect_stdout(stream):
            context=env['crosslake_start']('app','DailyFinance','Load','run-123');env['crosslake_event'](context,'completed')
        events=[json.loads(x) for x in stream.getvalue().splitlines()];self.assertEqual([e['event'] for e in events],['started','completed']);self.assertEqual(events[0]['correlation_id'],events[1]['correlation_id']);self.assertNotIn('_start',events[0]);self.assertNotIn('parameters',events[0])
    def test_instrumented_failure_logs_error_class_and_reraises(self):
        env={};exec(RUNTIME,env);env['dbutils']=SimpleNamespace(widgets=SimpleNamespace(text=lambda *a:None,get=lambda k:'test'))
        stream=io.StringIO();code=instrument('',"raise ValueError('private value')",'pkg','task','app')
        with contextlib.redirect_stdout(stream):
            with self.assertRaises(ValueError):exec(code,env)
        self.assertIn('failed',stream.getvalue());self.assertNotIn('private value',stream.getvalue())
