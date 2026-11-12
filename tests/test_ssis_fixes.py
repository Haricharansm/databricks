"""Regression tests for repairs, generated Graph code, and import-only deployment."""
import ast
import base64
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ssis_fixes import component_specs,repair_notebook,fix_sql_cells,CELL_BREAK
from deploy import DeployService,notebook_for_workspace,notebook_path
from bladebridge import GUARD,convert_package
from databricks_settings import DatabricksSettings
from monitoring import RunService
from test_validation import wait

DTS='www.microsoft.com/SqlServer/Dts'
XML='''<DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts" DTS:ExecutableType="Microsoft.Package" DTS:ObjectName="SyntheticSharePoint"><component refId="Package\\Flow\\REST Source" name="REST Source" componentClassID="MANAGEDCOMPONENTHOST"><properties><property name="UserComponentTypeName">CozyRoc.RestSource, CozyRoc.SSISPlus</property><property name="Resource">Site | List Item</property><property name="ResourceParameter">list_id=Fixture List</property><property name="BatchSize">200</property></properties><outputs><output name="Rows"><outputColumns><outputColumn name="id" dataType="wstr" length="255"/><outputColumn name="fields.Title" dataType="wstr" length="255"/></outputColumns></output></outputs></component></DTS:Executable>'''
PLACEHOLDER='''# Processing node Package\\Flow\\REST Source, type MANAGEDCOMPONENTHOST
# We do not support conversion of this component.
%DF% = f"""SELECT * FROM """
Package_Flow_REST_Source = spark.sql(%DF%)
Package_Flow_REST_Source.createOrReplaceTempView('Package_Flow_REST_Source')
'''

def sqlcell(sql,var='Task'):
    return var+' = f"""'+sql+'"""\nspark.sql('+var+')\n'

class RepairTests(unittest.TestCase):
    def test_multi_statement_default_and_stray_semicolon(self):
        text=GUARD+sqlcell('DROP TABLE IF EXISTS dbo.stage; CREATE OR REPLACE TABLE dbo.stage (id INT NULL, created TIMESTAMP DEFAULT current_timestamp(););')
        fixed,notes=repair_notebook(text,{})
        tree=ast.parse(fixed);calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='sql']
        self.assertEqual(len(calls),2);self.assertIn('allowColumnDefaults',fixed);self.assertNotIn('id INT NULL',fixed);self.assertTrue(any('split 2' in n for n in notes));self.assertEqual(repair_notebook(fixed,{})[0],fixed)
    def test_comments_literals_escaped_python_and_dynamic_sql(self):
        query="SELECT 'a;b' AS text; -- ; in comment\nSELECT 2 AS n;"
        fixed,_=fix_sql_cells(sqlcell(query));captured=[]
        exec(fixed,{'spark':types.SimpleNamespace(sql=lambda q:captured.append(q))})
        self.assertEqual(len(captured),2);self.assertIn("'a;b'",captured[0]);self.assertIn('-- ; in comment',captured[1])
        dynamic=sqlcell('SELECT {value}; SELECT 2;');fixed,notes=fix_sql_cells(dynamic);self.assertEqual(fixed,dynamic);self.assertIn('dynamic',notes[0])
    def test_no_regex_crossing_cells_or_unrelated_assignments(self):
        original='Unused = f"""SELECT 1"""\nprint(Unused)\n'+CELL_BREAK+'\n'+sqlcell('SELECT 2; SELECT 3;','Real')
        fixed,_=fix_sql_cells(original);ast.parse(fixed);self.assertIn('Unused = f"""SELECT 1"""\nprint(Unused)',fixed);self.assertIn('Real_2',fixed)
    def test_unsupported_placeholder_is_explicit_and_valid_python(self):
        fixed,notes=repair_notebook(GUARD+PLACEHOLDER,{})
        ast.parse(fixed);self.assertNotIn('%DF% =',fixed);self.assertIn('raise NotImplementedError',fixed);self.assertTrue(any('could not be converted' in n for n in notes))
        literal='# Processing node Package\\X, type EXECUTE_SQL\n'+sqlcell("SELECT '%DF%' AS marker")
        self.assertEqual(repair_notebook(literal,{})[0],literal)
    def test_xml_contract_and_nonstring_rest_output_requires_rewrite(self):
        self.assertEqual(component_specs(XML)['Package\\Flow\\REST Source']['columns'],['id','fields.Title'])
        with self.assertRaises(ValueError):component_specs('<!DOCTYPE a [<!ENTITY b "secret">]><a/>')
        bad=XML.replace('dataType="wstr"','dataType="i4"')
        fixed,_=repair_notebook(PLACEHOLDER,component_specs(bad));self.assertIn('NotImplementedError',fixed)
    def test_custom_filters_and_multiple_rest_sources_do_not_get_automatic_reader(self):
        filtered=XML.replace('list_id=Fixture List','list_id=Fixture List;filter=Active')
        code,_=repair_notebook(PLACEHOLDER,component_specs(filtered));self.assertIn('NotImplementedError',code)
        specs=component_specs(XML);specs['other']={**next(iter(specs.values())),'ref':'other','name':'Other REST'}
        code,_=repair_notebook(PLACEHOLDER,specs);self.assertIn('NotImplementedError',code)

    def test_import_used_by_notebook_is_retained(self):
        code='from logging import exception\nexception("keep this import")\n'
        self.assertEqual(repair_notebook(code,{})[0],code)

    def test_invalid_placeholder_becomes_blocking_engine_diagnostic(self):
        from email.message import EmailMessage
        message=EmailMessage();message.add_attachment(PLACEHOLDER,filename='main.py')
        client=types.SimpleNamespace(initialize=lambda *a:None,transpile=lambda *a:(message.as_string(),[]),close=lambda:None)
        with patch('bladebridge.status',return_value={'available':True}),patch('bladebridge.Morpheus',return_value=client):
            artifacts,diagnostics=convert_package({'name':'synthetic.dtsx','sql':XML.replace('CozyRoc.RestSource','UnsupportedVendor.Source')})
        self.assertTrue(any(d['severity']==1 and 'could not be converted' in d['message'] for d in diagnostics))
        notebook=next(a['sql'] for a in artifacts if a['kind']=='notebook');ast.parse(notebook);self.assertIn('NotImplementedError',notebook)

    def test_guard_survives_and_unused_template_imports_removed(self):
        fixed,_=repair_notebook(GUARD+'from curses import meta\nfrom logging import exception\n'+PLACEHOLDER,component_specs(XML));ast.parse(fixed)
        self.assertTrue(fixed.startswith(GUARD));self.assertNotIn('from curses',fixed);self.assertNotIn('from logging',fixed)

class GraphTests(unittest.TestCase):
    def run_generated(self,results,host='contoso.sharepoint.com'):
        fixed,notes=repair_notebook(PLACEHOLDER,component_specs(XML));ast.parse(fixed);self.calls=[]
        widgets={'sharepoint_host':host,'sharepoint_site_path':'/sites/Fixture','sharepoint_list':'Fixture List','graph_secret_scope':'sharepoint'}
        class Response:
            status_code=200;headers={}
            def __init__(self,data):self.data=data
            def raise_for_status(self):pass
            def json(self):return self.data
        def get(url,**kw):
            self.calls.append((url,kw));return Response(results.pop(0))
        requests=types.ModuleType('requests');requests.get=get;requests.post=lambda *a,**kw:Response({'access_token':'fixture-token'})
        sqltypes=types.ModuleType('pyspark.sql.types');sqltypes.StructType=lambda x:x;sqltypes.StructField=lambda *x:x;sqltypes.StringType=lambda:'string'
        spark=types.SimpleNamespace(createDataFrame=lambda rows,schema:types.SimpleNamespace(createOrReplaceTempView=lambda v:None,rows=rows))
        dbutils=types.SimpleNamespace(widgets=types.SimpleNamespace(text=lambda *a:None,get=lambda k:widgets[k]),secrets=types.SimpleNamespace(get=lambda scope,key:'00000000-0000-0000-0000-000000000000' if key=='tenant-id' else 'fixture-secret'))
        env={'spark':spark,'dbutils':dbutils}
        with patch.dict(sys.modules,{'requests':requests,'pyspark':types.ModuleType('pyspark'),'pyspark.sql':types.ModuleType('pyspark.sql'),'pyspark.sql.types':sqltypes}):exec(fixed,env)
        return env['Package_Flow_REST_Source'].rows
    def test_display_name_resolution_paging_nulls_and_headers(self):
        pages=[{'id':'site-id'},{'value':[{'id':'list-id','displayName':'Fixture List'}]}, {'value':[{'id':'1','fields':{'Title':'One'}}],'@odata.nextLink':'https://graph.microsoft.com/v1.0/next'}, {'value':[{'id':'2','fields':{'Title':None}}]}]
        self.assertEqual(self.run_generated(pages),[('1','One'),('2',None)])
        self.assertTrue(all(c[1]['allow_redirects'] is False for c in self.calls));self.assertTrue(all(c[1]['headers']['Authorization']=='Bearer fixture-token' for c in self.calls))
    def test_empty_result_keeps_declared_schema(self):
        self.assertEqual(self.run_generated([{'id':'s'},{'value':[{'id':'l','displayName':'Fixture List'}]},{'value':[]}]),[])
    def test_untrusted_paging_link_never_receives_token(self):
        pages=[{'id':'s'},{'value':[{'id':'l','displayName':'Fixture List'}]},{'value':[],'@odata.nextLink':'https://evil.example/next'}]
        with self.assertRaisesRegex(ValueError,'paging URL'):self.run_generated(pages)
        self.assertTrue(all(c[0].startswith('https://graph.microsoft.com/') for c in self.calls))
    def test_oversized_and_complex_strings_fail_instead_of_silent_conversion(self):
        for value in ['x'*256,{'unexpected':'object'}]:
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ValueError):self.run_generated([{'id':'s'},{'value':[{'id':'l','displayName':'Fixture List'}]},{'value':[{'id':'1','fields':{'Title':value}}]}])
    def test_host_validation_rejects_nonsharepoint_host(self):
        with self.assertRaises(ValueError):self.run_generated([],host='evil.example')
        self.assertFalse(self.calls)

class NotebookImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=RunService(Path(self.tmp.name)/'db');self.calls=[]
        def transport(host,token,method,path,payload=None):
            self.calls.append((method,path,payload))
            return {'userName':'fixture@example.invalid'} if path.endswith('/Me') else {}
        self.settings=DatabricksSettings(self.store,transport)
        self.settings.save({'workspace_url':'https://dbc-test.cloud.databricks.com','auth_mode':'session_token','token':'fixture-token','revision':0,'warehouse_id':'','catalog':'main','schema':'migration','cli_profile':''})
        run=self.store.create({'files':[{'name':'p.sql','sql':'SELECT 1;'}]});self.rid=wait(self.store,run['id'])['id']
        conversion={'files':[{'name':'notebooks/pkg/task.py','kind':'notebook','sql':GUARD+sqlcell('SELECT 1; SELECT 2;')}]}
        with self.store.db() as db:db.execute('UPDATE runs SET conversion_json=? WHERE id=?',(json.dumps(conversion),self.rid))
        self.service=DeployService(self.settings,self.store,sleep=lambda _:None)
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def test_notebook_only_import_needs_no_warehouse_never_executes_sql(self):
        preview=self.service.preview({'run_id':self.rid});self.assertFalse(self.calls);self.assertIn('Import notebook',preview['statements'][0]['preview'])
        job=self.service.start({'run_id':self.rid,'allow_execution':True,'revision':1})
        import time
        for _ in range(200):
            job=self.service.status(job['id'])
            if job['status'] in ('succeeded','failed'):break
            time.sleep(.01)
        self.assertEqual(job['status'],'succeeded',job);self.assertFalse(any('/sql/statements' in p for _,p,_ in self.calls))
        payload=next(b for _,p,b in self.calls if p.endswith('/import'));self.assertFalse(payload['overwrite']);self.assertEqual(payload['path'],'/Users/fixture@example.invalid/crosslake_migration/pkg/task')
        code=base64.b64decode(payload['content']).decode();ast.parse(code);self.assertLess(code.index('raise RuntimeError'),code.index('USE CATALOG'));self.assertIn('Task_2',code)
    def test_explicit_overwrite_and_custom_folder_preview(self):
        plan=self.service._plan({'run_id':self.rid,'notebook_folder':'/Shared/fixture','overwrite_notebooks':True})[2]
        self.assertEqual(plan[0]['path'],'/Shared/fixture/pkg/task');self.assertTrue(plan[0]['overwrite'])
    def test_rejects_unsafe_destinations_and_bad_python_before_any_import(self):
        for folder,name in [('/Shared/../Users/other','a.py'),('/Shared/a','../a.py'),('/Shared/a','/absolute.py'),('/Shared/a','a\\b.py')]:
            with self.subTest(folder=folder,name=name):
                with self.assertRaises(ValueError):notebook_path(folder,name)
        with self.store.db() as db:db.execute('UPDATE runs SET conversion_json=? WHERE id=?',(json.dumps({'files':[{'name':'bad.py','kind':'notebook','sql':'%DF% = 1'}]}),self.rid))
        with self.assertRaises(ValueError):self.service.preview({'run_id':self.rid})
        self.assertFalse(self.calls)
    def test_namespace_strings_quoted_and_gate_added_for_older_notebook(self):
        code,note=notebook_for_workspace('# Databricks notebook source\nprint(1)\n','main"catalog','my`schema')
        ast.parse(code);self.assertIn('my``schema',code);self.assertLess(code.index('raise RuntimeError'),code.index('USE CATALOG'));self.assertIn('explicit schema',note)

if __name__=='__main__':unittest.main()
