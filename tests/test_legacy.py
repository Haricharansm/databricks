import base64
from email.message import EmailMessage
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import assess,convert,validate_files
from ssis import inventory,parse_package
from bladebridge import unpack_outputs,status
from legacy import sql_inventory

ROOT=Path(__file__).resolve().parents[1]
SAMPLE=(ROOT/'tests/fixtures/LegacyLoad.dtsx').read_text()

def archive(entries):
    buffer=BytesIO()
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as z:
        for name,text in entries.items():z.writestr(name,text)
    return {'name':'project.ispac','base64':base64.b64encode(buffer.getvalue()).decode()}

class InventoryTests(unittest.TestCase):
    def test_package_tasks_constraints_and_sql(self):
        report=assess(validate_files({'files':[{'name':'load.dtsx','sql':SAMPLE}]}))
        package=report['files'][0]
        self.assertEqual(report['summary']['ssis_packages'],1)
        self.assertEqual(len(package['package']['tasks']),2)
        self.assertEqual(len(package['package']['constraints']),1)
        self.assertEqual(package['embedded_sql_count'],2)
        self.assertTrue(any(f['code'].endswith('-top') for f in package['findings']))

    def test_procedure_parameters_and_dependencies_ignore_comments(self):
        sql="""-- CREATE PROC fake AS EXEC fake;
        CREATE OR ALTER PROCEDURE [dbo].[RealProc] @id INT, @value DECIMAL(12,2) = 0 OUTPUT AS
        BEGIN SELECT * FROM [dbo].[Customers] WHERE id=@id; EXEC dbo.Child @id; END;"""
        result=sql_inventory(sql)
        self.assertEqual(len(result['procedures']),1)
        proc=result['procedures'][0];self.assertEqual(proc['name'],'[dbo].[RealProc]')
        self.assertEqual([p['name'] for p in proc['parameters']],['@id','@value'])
        self.assertTrue(proc['parameters'][1]['output']);self.assertTrue(proc['parameters'][1]['has_default'])
        report=assess([{'name':'p.sql','sql':sql}])
        self.assertIn('procedure-output-parameters',{f['code'] for f in report['files'][0]['findings']})
        self.assertIn('dbo.Child',[d['name'] for d in result['dependencies']])
        self.assertNotIn('fake',[d['name'] for d in result['dependencies']])

    def test_scripts_unknown_components_and_conditions_flagged(self):
        xml=SAMPLE.replace('Microsoft.ExecuteSQLTask','Microsoft.ScriptTask',1).replace('DTS:Value="0"','DTS:Value="1"')
        xml=xml.replace('<DTS:Variables />','<DTS:Variables /><component name="custom" componentClassID="Vendor.Custom" />',1)
        codes={f['code'] for f in inventory({'name':'p.dtsx','sql':xml})['findings']}
        self.assertTrue({'ssis-script','ssis-unsupported','ssis-conditional-flow'}<=codes)

    def test_cyclic_and_unresolved_constraints(self):
        edge='<DTS:PrecedenceConstraint DTS:From="Package\\Summarize" DTS:To="Package\\Prepare" />'
        xml=SAMPLE.replace('</DTS:PrecedenceConstraints>',edge+'</DTS:PrecedenceConstraints>')
        self.assertIn('ssis-cycle',{f['code'] for f in inventory({'name':'p.dtsx','sql':xml})['findings']})
        xml=SAMPLE.replace('DTS:From="Package\\Prepare"','DTS:From="missing"')
        self.assertIn('ssis-unresolved-edge',{f['code'] for f in inventory({'name':'p.dtsx','sql':xml})['findings']})

    def test_xml_rejects_entities_invalid_roots_and_encryption(self):
        for xml in ['<!DOCTYPE x [<!ENTITY y "x">]><x/>','<x/>',SAMPLE.replace('<DTS:Variables />','<DTS:EncryptedData>secret</DTS:EncryptedData>',1)]:
            with self.assertRaises(ValueError):parse_package(xml)

    def test_connection_inventory_does_not_expose_credentials(self):
        xml=SAMPLE.replace('<DTS:Variables />','<DTS:ConnectionManager DTS:ObjectName="DB" DTS:CreationName="OLEDB" DTS:ConnectionString="Password=secret123" />',1)
        result=inventory({'name':'p.dtsx','sql':xml})
        self.assertNotIn('secret123',json.dumps(result));self.assertEqual(result['connections'][0]['name'],'DB')

    def test_ispac_expansion_metadata_and_utf16(self):
        files=validate_files({'files':[archive({'Load.dtsx':SAMPLE.encode('utf-16'),'Project.params':'<parameters/>'})]})
        self.assertEqual(files[0]['kind'],'ssis');self.assertEqual(files[0]['origin_project'],'project.ispac')
        self.assertEqual(files[0]['project_metadata'],['Project.params'])
        self.assertIn('ssis-project-metadata',{f['code'] for f in inventory(files[0])['findings']})

    def test_ispac_rejects_traversal_duplicate_names_and_oversize(self):
        for entries in [{'../evil.dtsx':SAMPLE},{'a/load.dtsx':SAMPLE,'b/load.dtsx':SAMPLE},{'load.dtsx':SAMPLE,'bomb.bin':'x'*(4*1024*1024)}]:
            with self.assertRaises(ValueError):validate_files({'files':[archive(entries)]})

    def test_mime_output_guard_and_paths(self):
        msg=EmailMessage();msg.add_attachment('# Databricks notebook source\nprint("converted")',filename='/tmp/id/transpiled/p/main.py')
        outputs=unpack_outputs(msg.as_string(),'pkg')
        self.assertEqual(outputs[0]['name'],'notebooks/pkg/p/main.py')
        self.assertLess(outputs[0]['sql'].index('raise RuntimeError'),outputs[0]['sql'].index('print("converted")'))
        evil=EmailMessage();evil.add_attachment('text',filename='../escape.py')
        with self.assertRaises(RuntimeError):unpack_outputs(evil.as_string(),'pkg')

    @patch('bladebridge.convert_package',side_effect=RuntimeError('engine unavailable'))
    @patch('core.convert_sql',return_value={'engine':'Morpheus','files':[{'name':'a.sql','sql':'SELECT 1;'}]})
    def test_partial_conversion_has_explicit_coverage(self,*mocks):
        result=convert(validate_files({'files':[{'name':'a.sql','sql':'SELECT 1;'},{'name':'p.dtsx','sql':SAMPLE}]}))
        self.assertEqual(result['coverage'][1]['status'],'blocked')
        self.assertIn('Partial',result['status']);self.assertFalse(result['deployable'])
        self.assertIn('migration-coverage.json',[f['name'] for f in result['files']])

    @unittest.skipUnless(status()['available'],'BladeBridge not installed')
    def test_real_ssis_engine_notebook_and_workflow(self):
        result=convert(validate_files({'files':[{'name':'LegacyLoad.dtsx','sql':SAMPLE}]}))
        self.assertEqual(len(result['coverage']),1)
        py=next(f for f in result['files'] if f['name'].endswith('.py'))
        self.assertIn('spark.sql',py['sql']);self.assertIn('migration_reviewed',py['sql'])
        job=json.loads(next(f['sql'] for f in result['files'] if f['name'].endswith('.job.json')))
        self.assertTrue(job['tasks'][0]['notebook_task']['notebook_path'].startswith('/REVIEW_REQUIRED/'))
        self.assertEqual(job['tasks'][0]['notebook_task']['base_parameters']['migration_reviewed'],'false')
        self.assertNotIn('schedule',job);self.assertFalse(result['validated_on_databricks'])

if __name__=='__main__':unittest.main()
