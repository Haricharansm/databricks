import ast
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from lakeflow import LakeflowService,package_graph,key
from lakeflow_results import output_code
from monitoring import RunService
from ssis import inventory,package_key
from bladebridge import GUARD
from test_validation import wait

ROOT=Path(__file__).resolve().parents[1]
XML=(ROOT/'samples/lakeflow/ProductPackage/ProductPackage.dtsx').read_text()
class BindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.runs=RunService(Path(self.tmp.name)/'db');self.service=LakeflowService(self.runs)
        self.item={'name':'ProductPackage.dtsx','sql':XML};self.rid=wait(self.runs,self.runs.create({'files':[self.item]})['id'])['id']
        tasks=inventory(self.item)['tasks'];queries=['CREATE TABLE dbo.Product (ProductID INT)','INSERT INTO dbo.Product VALUES (1)','SELECT COUNT(*) AS ProductCnt FROM dbo.Product']
        cells=['# Processing node '+t['id']+', type EXECUTE_SQL\nq = '+repr(q)+'\nspark.sql(q)\n' for t,q in zip(tasks,queries)]
        with self.runs.db() as db:db.execute('UPDATE runs SET conversion_json=? WHERE id=?',(json.dumps({'files':[{'name':'notebooks/'+package_key(self.item['name'])+'/Product.py','kind':'notebook','sql':GUARD+'\n# COMMAND ----------\n'.join(cells)}]}),self.rid))
    def tearDown(self):self.runs.close();self.tmp.cleanup()
    def bundle(self):
        p=self.service.get(self.rid);p['profile']['schedule']={'quartz_cron_expression':'0 0 2 * * ?','timezone_id':'America/Chicago'};self.service.save(self.rid,{'version':p['version'],'fingerprint':p['fingerprint'],'profile':p['profile']});return self.service.generate(self.rid)
    def test_product_generates_complete_job_and_output_evidence(self):
        out=self.bundle();self.assertEqual(out['report']['jobs'],1);self.assertEqual(out['report']['pipelines'],0);self.assertEqual(out['report']['packages'][0]['blockers'],[])
        resource=json.loads(next(f['sql'] for f in out['files'] if f['name']=='resources/lakeflow.json'))['resources'];job=next(iter(resource['jobs'].values()));self.assertEqual(job['schedule']['pause_status'],'PAUSED');self.assertEqual(len(job['tasks']),3)
        for previous,current in zip(job['tasks'],job['tasks'][1:]):self.assertEqual(current['depends_on'],[{'task_key':previous['task_key']}])
        output=out['report']['packages'][0]['tasks'][-1]['task_values'][0];self.assertEqual(output['variable'],'User::ProductCount');self.assertEqual(output['task_value_key'],'ssis_User_ProductCount')
        for f in out['files']:
            if f['name'].endswith('.py'):ast.parse(f['sql']);self.assertIn('migration_reviewed',f['sql'])
        self.assertTrue(all(t['notebook_task']['base_parameters']['migration_reviewed']=='false' for t in job['tasks']))
    def test_unsafe_variable_patterns_remain_blocked(self):
        variants=[XML.replace('DTS:Namespace="User"','DTS:Namespace="System"'),XML.replace('DTS:DataType="3"','DTS:DataType="8"'),XML.replace('DTS:IncludeInDebugDump="6789"','DTS:EvaluateAsExpression="True"'),XML.replace('SELECT COUNT(*) AS ProductCnt','SELECT @ProductCount AS ProductCnt'),XML.replace('INSERT INTO dbo.Product','-- User::ProductCount&#xA;INSERT INTO dbo.Product'),XML.replace('DTS:ObjectName="ProductCount"','DTS:ObjectName="Different"'),XML.replace('DTS:IncludeInDebugDump="6789"','DTS:Sensitive="True"')]
        # SQL parameter references are rejected later by static extraction, all other XML references here.
        for xml in variants[:3]+variants[4:]:
            with self.subTest(xml=xml[-300:]):
                with self.assertRaises(ValueError):package_graph({'name':'p.dtsx','sql':xml})
    def test_multiple_writers_rejected(self):
        binding='<SQLTask:ResultBinding SQLTask:ResultName="ProductCnt" SQLTask:DtsVariableName="User::ProductCount" />'
        with self.assertRaises(ValueError):package_graph({'name':'p.dtsx','sql':XML.replace('</SQLTask:SqlTaskData>',binding+'</SQLTask:SqlTaskData>')})
    def execute(self,rows,columns=['ProductCnt'],outputs=None):
        values={};calls=[]
        class Frame:
            def __init__(self):self.columns=columns
            def take(self,n):calls.append(n);return rows[:n]
        code,evidence=output_code(['SELECT COUNT(*) AS ProductCnt FROM dbo.Product'],outputs or [{'variable':'User::ProductCount','result_name':'ProductCnt','data_type':'3'}],'count_task')
        spark=SimpleNamespace(sql=lambda sql:Frame());dbutils=SimpleNamespace(jobs=SimpleNamespace(taskValues=SimpleNamespace(set=lambda **kw:values.update({kw['key']:kw['value']}))))
        return code,evidence,values,calls,{'spark':spark,'dbutils':dbutils}
    def test_runtime_named_and_ordinal_and_int64(self):
        for name,typ,value in [('ProductCnt','3',4),('0','20',2**40)]:
            code,evidence,values,calls,env=self.execute([(value,)],outputs=[{'variable':'User::ProductCount','result_name':name,'data_type':typ}]);exec(code,env);self.assertEqual(values,{'ssis_User_ProductCount':value});self.assertEqual(calls,[2])
    def test_cardinality_null_type_range_and_missing_columns_fail_without_publish(self):
        for rows,cols in [([],['ProductCnt']),([(1,),(2,)],['ProductCnt']),([(None,)],['ProductCnt']),([(True,)],['ProductCnt']),([('4',)],['ProductCnt']),([(2**31,)],['ProductCnt']),([(4,)],['Other']),([(4,4)],['ProductCnt','productcnt'])]:
            code,_,values,_,env=self.execute(rows,cols)
            with self.assertRaises(RuntimeError):exec(code,env)
            self.assertEqual(values,{})
    def test_all_bindings_validate_before_publishing(self):
        outputs=[{'variable':'User::A','result_name':'0','data_type':'3'},{'variable':'User::B','result_name':'1','data_type':'3'}]
        code,_,values,_,env=self.execute([(4,None)],['A','B'],outputs)
        with self.assertRaises(RuntimeError):exec(code,env)
        self.assertEqual(values,{})
    def test_single_select_only_and_sql_is_not_interpolated(self):
        outputs=[{'variable':'User::ProductCount','result_name':'0','data_type':'3'}]
        for qs in [['SELECT 1','SELECT 2'],['UPDATE x SET a=1']]:
            with self.assertRaises(ValueError):output_code(qs,outputs,'task')
        code,_=output_code(["SELECT 'BINDINGS QUERY {query}'"],outputs,'task');ast.parse(code);self.assertIn("BINDINGS QUERY {query}",code)
if __name__=='__main__':unittest.main()
