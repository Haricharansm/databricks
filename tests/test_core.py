import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import assess, convert, mask_sql, validate_files
from server import Handler, ThreadingHTTPServer, ROOT
from functools import partial

class AssessmentTests(unittest.TestCase):
    def test_literals_identifiers_and_comments_do_not_trigger_rules(self):
        sql="SELECT 'CURSOR GETDATE() #temp', [CURSOR], N'BEGIN TRANSACTION'; -- EXEC(x)\n/* outer /* CURSOR */ COMMIT */ SELECT 1;"
        result = assess([{'name':'clean.sql','sql':sql}])
        self.assertEqual(result['summary']['findings'], 0)
        self.assertEqual(len(mask_sql(sql)), len(sql))
        self.assertEqual(mask_sql(sql).count('\n'), sql.count('\n'))

    def test_high_risk_and_line_references(self):
        result = assess([{'name':'proc.sql','sql':'-- header\nCREATE PROCEDURE dbo.p AS\nBEGIN TRANSACTION;\nEXEC(@sql);\nSELECT * INTO #t FROM dbo.s;'}])['files'][0]
        self.assertEqual(result['complexity'], 'High')
        findings={f['code']:f for f in result['findings']}
        self.assertEqual(findings['procedure']['lines'], [2])
        self.assertEqual(findings['dynamic-sql']['lines'], [4])
        self.assertIn('temp-table',findings)

    def test_input_validation(self):
        for files in [[],[{'name':'../x.sql','sql':'select 1'}],[{'name':'ok.sql','sql':''}],
                      [{'name':'ok.sql','sql':'x'},{'name':'OK.SQL','sql':'y'}]]:
            with self.assertRaises(ValueError):validate_files({'files':files})
        with self.assertRaises(ValueError):validate_files({'files':[{'name':'x.sql','sql':'x'* (4*1024*1024+1)}]})

    @patch('core.engine_status',return_value={'available':False,'message':'Engine missing'})
    def test_missing_engine_never_returns_fake_conversion(self,status):
        with self.assertRaisesRegex(RuntimeError,'Engine missing'):convert([{'name':'a.sql','sql':'select 1'}])

    def test_adapter_invokes_expected_flags_and_cleans_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'morpheus.json';config.write_text('{}')
            locations=[]
            def run(command, **kwargs):
                self.assertEqual(command[1:4], ['labs','lakebridge','transpile'])
                self.assertEqual(command[command.index('--source-dialect')+1], 'mssql')
                self.assertEqual(command[command.index('--skip-validation')+1], 'true')
                source=Path(command[command.index('--input-source')+1]);locations.append(source)
                self.assertEqual((source/'a.sql').read_text(),'SELECT GETDATE();')
                output=Path(command[command.index('--output-folder')+1]);(output/'a.sql').write_text('SELECT CURRENT_TIMESTAMP();')
                kwargs['stdout'].write('engine diagnostics')
                return type('Result',(),{'returncode':0})()
            with patch.dict(os.environ,{'LAKEBRIDGE_TRANSPILER_CONFIG':str(config)}),patch('core.shutil.which',return_value='/bin/databricks'),patch('core.subprocess.run',side_effect=run):
                result=convert([{'name':'a.sql','sql':'SELECT GETDATE();'}])
            self.assertEqual(result['files'][0]['sql'],'SELECT CURRENT_TIMESTAMP();')
            self.assertFalse(result['validated_on_databricks'])
            self.assertFalse(locations[0].exists())

class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(ROOT/'static')))
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.url=f'http://127.0.0.1:{cls.server.server_port}'
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def post(self,path,payload,**headers):
        return urlopen(Request(self.url+path,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Accelerator-Request':'1',**headers}))
    def test_assessment_and_static(self):
        with self.post('/api/assess',{'files':[{'name':'x.sql','sql':'SELECT GETDATE();'}]}) as response:
            self.assertEqual(json.load(response)['summary']['files'],1)
        with urlopen(self.url) as response:
            self.assertIn(b'Crosslake',response.read())
            self.assertIn("script-src 'self'",response.headers['Content-Security-Policy'])
        with self.assertRaises(HTTPError) as e:urlopen(self.url+'/../core.py')
        self.assertEqual(e.exception.code,404)
    def test_cross_origin_rejected(self):
        with self.assertRaises(HTTPError) as e:self.post('/api/assess',{},Origin='https://other.example')
        self.assertEqual(e.exception.code,403)
    def test_zip_has_safe_names_and_report(self):
        with self.post('/api/export',{'files':[{'name':'../../x.sql','sql':'SELECT 1;'}],'report':{'ok':True}}) as response:
            with zipfile.ZipFile(io.BytesIO(response.read())) as z:
                self.assertEqual(z.namelist(),['converted/001-x.sql','report.json'])
                self.assertEqual(json.loads(z.read('report.json')),{'ok':True})


class LSPTests(unittest.TestCase):
    def test_full_document_and_unicode_edits(self):
        from morpheus import apply_edits
        self.assertEqual(apply_edits('one', [{'range':{'start':{'line':0,'character':0},'end':{'line':1,'character':0}},'newText':'two'}]), 'two')
        self.assertEqual(apply_edits('a😀b', [{'range':{'start':{'line':0,'character':1},'end':{'line':0,'character':3}},'newText':'x'}]),'axb')
        with self.assertRaises(RuntimeError):
            apply_edits('abc',[{'range':{'start':{'line':0,'character':3},'end':{'line':0,'character':1}},'newText':'x'}])

    @unittest.skipUnless((ROOT/'engine'/'morpheus.jar').exists() and __import__('shutil').which('java'), 'Morpheus not installed')
    def test_real_morpheus_conversion_and_diagnostics(self):
        from morpheus import convert_with_morpheus
        result=convert_with_morpheus([
            {'name':'simple.sql','sql':"SELECT TOP (2) [CustomerID], ISNULL([Region], 'Unknown') FROM [dbo].[Sales];"},
            {'name':'clock.sql','sql':'SELECT GETDATE();'}],ROOT/'engine'/'morpheus.jar')
        self.assertEqual(len(result['files']),2)
        self.assertIn('LIMIT',result['files'][0]['sql'])
        self.assertIn('IFNULL',result['files'][0]['sql'])
        self.assertIn('CURRENT_TIMESTAMP',result['files'][1]['sql'])
        self.assertTrue(result['files'][1]['diagnostics'])
        self.assertFalse(result['validated_on_databricks'])

if __name__=='__main__':unittest.main()
