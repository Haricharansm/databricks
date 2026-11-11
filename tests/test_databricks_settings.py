from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from databricks_settings import DatabricksSettings,DatabricksError,NoRedirect,workspace_url,FIXED_SQL
from monitoring import RunService,Conflict

HOST='https://dbc-test.cloud.databricks.com'
TOKEN='fixture-token-never-persist-123456'

def settings(**extra):return {'workspace_url':HOST,'auth_mode':'session_token','warehouse_id':'abc123','catalog':'','schema':'','cli_profile':'','revision':0,**extra}

class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'runs.db';self.store=RunService(self.path);self.calls=[]
        def transport(host,token,method,path,payload=None):
            self.calls.append((host,token,method,path,payload))
            if path.endswith('/Me'):return {'id':'1','userName':'fixture-user@example.invalid'}
            if path.endswith('/warehouses'):return {'warehouses':[{'id':'abc123','name':'Fixture warehouse','state':'STOPPED','enable_serverless_compute':True}]}
            return {'statement_id':'fixture-statement','status':{'state':'SUCCEEDED'},'result':{'data_array':[['1']]}}
        self.client=DatabricksSettings(self.store,transport)
    def tearDown(self):self.store.close();self.temp.cleanup()
    def save(self,**extra):return self.client.save(settings(token=TOKEN,**extra))

    def test_url_allowlist_rejects_ssrf_and_malformed_urls(self):
        for host in [HOST,'https://adb-123.45.azuredatabricks.net/','https://123.45.gcp.databricks.com']:
            self.assertTrue(workspace_url(host).startswith('https://'))
        for host in ['http://dbc-test.cloud.databricks.com','https://127.0.0.1','https://localhost','https://dbc.cloud.databricks.com.evil.example','https://token@dbc.cloud.databricks.com','https://dbc.cloud.databricks.com:8443','https://dbc.cloud.databricks.com/editor','https://dbc.cloud.databricks.com/?o=1','https://accounts.cloud.databricks.com']:
            with self.subTest(host=host):
                with self.assertRaises(ValueError):workspace_url(host)

    def test_token_not_in_public_settings_database_or_restarted_session(self):
        config=self.save();self.assertTrue(config['credential_configured']);self.assertNotIn(TOKEN,json.dumps(config))
        result=self.client.test_workspace({'revision':1});self.assertEqual(result['status'],'succeeded')
        self.assertEqual(len(self.calls),1);self.assertEqual(self.calls[0][2:4],('GET','/api/2.0/preview/scim/v2/Me'))
        self.assertNotIn(TOKEN,self.path.read_bytes().decode('latin1'))
        restarted=DatabricksSettings(self.store,self.client.transport)
        public=restarted.public();self.assertFalse(public['credential_configured']);self.assertEqual(public['workspace_test']['status'],'succeeded')
        with self.assertRaises(DatabricksError):restarted.test_workspace({'revision':1})

    def test_blank_token_preserved_only_for_same_host_and_mode(self):
        self.save();same=self.client.save(settings(revision=1));self.assertTrue(same['credential_configured'])
        changed=self.client.save(settings(workspace_url='https://dbc-other.cloud.databricks.com',revision=2));self.assertFalse(changed['credential_configured'])
        with self.assertRaises(DatabricksError):self.client.test_workspace({'revision':3})

    def test_stale_revision_clear_credential_and_tests_reset(self):
        self.save();self.client.test_workspace({'revision':1})
        with self.assertRaises(Conflict):self.client.save(settings(revision=0))
        cleared=self.client.forget({'revision':1});self.assertFalse(cleared['credential_configured']);self.assertIsNone(cleared['workspace_test'])
        with self.assertRaises(Conflict):self.client.test_workspace({'revision':1})

    def test_environment_token_is_bound_to_matching_host(self):
        self.client.save(settings(auth_mode='environment_token'))
        with patch.dict(os.environ,{'DATABRICKS_HOST':'https://dbc-other.cloud.databricks.com','DATABRICKS_TOKEN':TOKEN}):
            self.assertFalse(self.client.public()['credential_configured'])
            with self.assertRaises(DatabricksError):self.client.test_workspace({'revision':1})
        self.assertFalse(self.calls)
        with patch.dict(os.environ,{'DATABRICKS_HOST':HOST,'DATABRICKS_TOKEN':TOKEN}):
            self.assertEqual(self.client.test_workspace({'revision':1})['status'],'succeeded')
        self.assertNotIn(TOKEN,json.dumps(self.client.public()))

    def test_cli_oauth_uses_host_and_profile_without_exposing_output(self):
        self.client.save(settings(auth_mode='cli_oauth',cli_profile='migration'))
        result=type('CLIResult',(),{'returncode':0,'stdout':json.dumps({'access_token':TOKEN})})()
        with patch('databricks_settings.shutil.which',return_value='/bin/databricks'),patch('databricks_settings.subprocess.run',return_value=result) as run:
            self.assertEqual(self.client.test_workspace({'revision':1})['status'],'succeeded')
            command=run.call_args.args[0];self.assertEqual(command,['/bin/databricks','auth','token','--host',HOST,'--profile','migration']);self.assertNotIn(TOKEN,str(command))
        result.returncode=1;result.stdout=TOKEN
        with patch('databricks_settings.shutil.which',return_value='/bin/databricks'),patch('databricks_settings.subprocess.run',return_value=result):
            with self.assertRaises(DatabricksError) as failure:self.client.test_workspace({'revision':1})
            self.assertNotIn(TOKEN,str(failure.exception))

    def test_warehouse_listing_does_not_start_compute(self):
        self.save();result=self.client.warehouses({'revision':1})
        self.assertEqual(result['warehouses'][0]['id'],'abc123');self.assertTrue(result['warehouses'][0]['serverless'])
        self.assertEqual(self.calls[0][2:4],('GET','/api/2.0/sql/warehouses'))

    def test_fixed_sql_requires_consent_and_ignores_submitted_sql(self):
        self.save()
        with self.assertRaises(ValueError):self.client.test_warehouse({'revision':1})
        self.assertFalse(self.calls)
        result=self.client.test_warehouse({'revision':1,'allow_compute':True,'statement':'DROP TABLE important'})
        self.assertEqual(result['status'],'succeeded');body=self.calls[0][4]
        self.assertEqual(body['statement'],FIXED_SQL);self.assertEqual(body['on_wait_timeout'],'CANCEL');self.assertEqual(body['row_limit'],1)
        self.assertNotIn(TOKEN,json.dumps(result))

    def test_pending_sql_is_not_verified_and_cancellation_is_requested(self):
        self.save()
        def transport(host,token,method,path,payload=None):
            self.calls.append(path)
            return {'statement_id':'123-abc','status':{'state':'PENDING'}}
        self.client.transport=transport
        result=self.client.test_warehouse({'revision':1,'allow_compute':True})
        self.assertEqual(result['status'],'not_verified');self.assertTrue(self.calls[-1].endswith('/123-abc/cancel'))
        self.client.transport=lambda *args:{'status':{'state':'CANCELED'}}
        self.assertEqual(self.client.test_warehouse({'revision':1,'allow_compute':True})['status'],'not_verified')

    def test_wrong_scalar_result_is_not_claimed_as_success(self):
        self.save();self.client.transport=lambda *args:{'status':{'state':'SUCCEEDED'},'result':{'data_array':[['2']]}}
        self.assertEqual(self.client.test_warehouse({'revision':1,'allow_compute':True})['status'],'failed')

    def test_sanitized_http_error_and_redirect_rejection(self):
        error=HTTPError(HOST,401,TOKEN,{},BytesIO(TOKEN.encode()))
        opener=type('Opener',(),{'open':lambda *args,**kwargs:(_ for _ in ()).throw(error)})()
        with patch('databricks_settings.build_opener',return_value=opener):
            with self.assertRaises(DatabricksError) as failure:self.client._http(HOST,TOKEN,'GET','/api/2.0/preview/scim/v2/Me')
            self.assertNotIn(TOKEN,str(failure.exception));self.assertIn('Authentication failed',str(failure.exception))
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'https://evil.example'))

    def test_settings_changed_during_probe_do_not_get_stale_success(self):
        self.save()
        def transport(*args):
            self.client.save(settings(revision=1));return {'id':'user'}
        self.client.transport=transport
        with self.assertRaises(Conflict):self.client.test_workspace({'revision':1})
        self.assertIsNone(self.client.public()['workspace_test'])

class SettingsAPITests(unittest.TestCase):
    def test_settings_api_does_not_echo_tokens_and_rejects_untrusted_host(self):
        from functools import partial
        import threading
        from urllib.request import Request,urlopen
        from server import AppServer,Handler,ROOT
        with tempfile.TemporaryDirectory() as directory:
            store=RunService(Path(directory)/'runs.db');server=AppServer(('127.0.0.1',0),partial(Handler,directory=str(ROOT/'static')),run_service=store)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base=f'http://127.0.0.1:{server.server_port}'
            def post(path,payload):return urlopen(Request(base+path,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Accelerator-Request':'1'}))
            try:
                with post('/api/databricks/settings',settings(token=TOKEN)) as response:self.assertNotIn(TOKEN,response.read().decode())
                server.databricks.transport=lambda *args:{'id':'fixture-user','userName':'fixture@example.invalid'}
                with post('/api/databricks/test-workspace',{'revision':1}) as response:self.assertEqual(json.load(response)['status'],'succeeded')
                with urlopen(base+'/api/databricks/settings') as response:self.assertNotIn(TOKEN,response.read().decode())
                with self.assertRaises(HTTPError) as failure:urlopen(Request(base+'/api/databricks/settings',headers={'Host':'attacker.example'}))
                self.assertEqual(failure.exception.code,403)
                with self.assertRaises(HTTPError) as failure:post('/api/databricks/test-warehouse',{'revision':1})
                self.assertEqual(failure.exception.code,400)
            finally:
                server.shutdown();server.server_close();thread.join()
                self.assertIsNone(server.databricks.session_token)

if __name__=='__main__':unittest.main()
