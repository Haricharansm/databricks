"""Databricks configuration and bounded connection probes; credentials are never persisted."""
from datetime import datetime, timezone
import json
import os
import re
import shutil
import subprocess
import threading
from urllib.error import HTTPError,URLError
from urllib.parse import urlsplit
from urllib.request import Request,build_opener,HTTPRedirectHandler

HOST_PATTERN=re.compile(r'[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:cloud\.databricks\.com|azuredatabricks\.net|gcp\.databricks\.com)')
WAREHOUSE_PATTERN=re.compile(r'[A-Za-z0-9_-]{1,64}')
FIXED_SQL='SELECT 1 AS connection_test'

class DatabricksError(RuntimeError):pass
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):return None

def stamp():return datetime.now(timezone.utc).isoformat(timespec='seconds')

def workspace_url(value):
    if not isinstance(value,str) or len(value)>300:raise ValueError('Enter a Databricks workspace HTTPS URL.')
    try:
        parsed=urlsplit(value.strip())
        if parsed.scheme!='https' or not parsed.hostname or not HOST_PATTERN.fullmatch(parsed.hostname.lower()) or parsed.hostname.lower() in ('accounts.cloud.databricks.com','accounts.azuredatabricks.net','accounts.gcp.databricks.com') or parsed.port not in (None,443) or parsed.username is not None or parsed.password is not None or parsed.path not in ('','/') or parsed.query or parsed.fragment:
            raise ValueError('Enter the base workspace HTTPS URL without a path, query, credentials, or custom port. Supported domains: cloud.databricks.com, azuredatabricks.net, gcp.databricks.com.')
    except (ValueError,UnicodeError) as exc:raise ValueError('Enter a valid base workspace HTTPS URL on a supported Databricks domain.') from exc
    return 'https://'+parsed.hostname.lower()

def token_value(value):
    if not isinstance(value,str):raise ValueError('Token must be text.')
    value=value.strip()
    if not re.fullmatch(r'[!-~]{8,8192}',value):raise ValueError('Enter a token without whitespace or control characters.')
    return value

def metadata(payload):
    if not isinstance(payload,dict):raise ValueError('Invalid Databricks settings.')
    host=workspace_url(payload.get('workspace_url'))
    auth=payload.get('auth_mode','session_token')
    if auth not in ('session_token','environment_token','cli_oauth'):raise ValueError('Choose a supported authentication method.')
    warehouse=payload.get('warehouse_id','')
    if not isinstance(warehouse,str) or warehouse and not WAREHOUSE_PATTERN.fullmatch(warehouse):raise ValueError('Warehouse ID must contain only letters, digits, underscores, or hyphens (up to 64 characters).')
    result={'workspace_url':host,'auth_mode':auth,'warehouse_id':warehouse}
    for key in ('catalog','schema'):
        value=payload.get(key,'')
        if not isinstance(value,str) or len(value)>255 or any(ord(c)<32 for c in value):raise ValueError(key+' must be text without control characters (up to 255 characters).')
        result[key]=value.strip()
    profile=payload.get('cli_profile','')
    if not isinstance(profile,str) or profile and not re.fullmatch(r'[A-Za-z0-9_. -]{1,100}',profile):raise ValueError('Invalid CLI profile name.')
    result['cli_profile']=profile.strip()
    return result

class DatabricksSettings:
    def __init__(self,store,transport=None):
        self.store=store;self.lock=threading.RLock();self.session_token=None;self.transport=transport or self._http
        with store.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS databricks_settings (id INTEGER PRIMARY KEY CHECK(id=1), metadata_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, workspace_test_json TEXT, warehouse_test_json TEXT)')
            if db.execute('SELECT 1 FROM databricks_settings WHERE id=1').fetchone() is None:
                try:host=workspace_url(os.environ.get('DATABRICKS_HOST',''))
                except ValueError:host=''
                initial={'workspace_url':host,'auth_mode':'session_token','warehouse_id':'','catalog':'','schema':'','cli_profile':''}
                db.execute('INSERT INTO databricks_settings(id,metadata_json) VALUES(1,?)',(json.dumps(initial),))

    def _row(self):
        with self.store.db() as db:return dict(db.execute('SELECT * FROM databricks_settings WHERE id=1').fetchone())

    def public(self):
        with self.lock:
            row=self._row();config=json.loads(row['metadata_json']);mode=config['auth_mode']
            if mode=='session_token':configured=bool(self.session_token)
            elif mode=='environment_token':
                try:configured=bool(os.environ.get('DATABRICKS_TOKEN')) and workspace_url(os.environ.get('DATABRICKS_HOST',''))==config['workspace_url']
                except ValueError:configured=False
            else:configured=bool(shutil.which('databricks'))
            return {**config,'revision':row['revision'],'credential_configured':configured,
                    'credential_storage':'Session memory only; environment and CLI credentials remain managed outside the app.',
                    'cli_available':bool(shutil.which('databricks')),
                    'workspace_test':json.loads(row['workspace_test_json']) if row['workspace_test_json'] else None,
                    'warehouse_test':json.loads(row['warehouse_test_json']) if row['warehouse_test_json'] else None,
                    'execution_mode':'settings_and_fixed_connection_probe_only'}

    def save(self,payload):
        from monitoring import Conflict
        config=metadata(payload);new_token=payload.get('token','')
        if not isinstance(new_token,str):raise ValueError('Token must be text.')
        if new_token and config['auth_mode']!='session_token':raise ValueError('Token input applies only to session-token authentication.')
        if new_token:new_token=token_value(new_token)
        with self.lock:
            row=self._row();old=json.loads(row['metadata_json'])
            if payload.get('revision')!=row['revision']:raise Conflict('Settings changed in another tab. Reload them before saving.')
            same_credential=old['workspace_url']==config['workspace_url'] and old['auth_mode']==config['auth_mode']=='session_token'
            token=new_token or (self.session_token if same_credential else None)
            with self.store.db() as db:
                db.execute('UPDATE databricks_settings SET metadata_json=?,revision=revision+1,workspace_test_json=NULL,warehouse_test_json=NULL WHERE id=1',(json.dumps(config),))
            self.session_token=token if config['auth_mode']=='session_token' else None
            return self.public()

    def forget(self,payload):
        from monitoring import Conflict
        with self.lock:
            row=self._row()
            if not isinstance(payload,dict) or payload.get('revision')!=row['revision']:raise Conflict('Reload settings before clearing the session credential.')
            config=json.loads(row['metadata_json']);config['auth_mode']='session_token';self.session_token=None
            with self.store.db() as db:db.execute('UPDATE databricks_settings SET metadata_json=?,revision=revision+1,workspace_test_json=NULL,warehouse_test_json=NULL WHERE id=1',(json.dumps(config),))
            return self.public()

    def _credentials(self,payload):
        from monitoring import Conflict
        with self.lock:
            row=self._row();config=json.loads(row['metadata_json'])
            if not isinstance(payload,dict) or payload.get('revision')!=row['revision']:raise Conflict('Save or reload current settings before testing.')
            if not config['workspace_url']:raise ValueError('Save a workspace URL first.')
            mode=config['auth_mode']
            if mode=='session_token':
                if not self.session_token:raise DatabricksError('Enter a session token and save settings. Session tokens are cleared when the server restarts.')
                token=self.session_token
            elif mode=='environment_token':
                try:env_host=workspace_url(os.environ.get('DATABRICKS_HOST',''))
                except ValueError:raise DatabricksError('Set DATABRICKS_HOST and DATABRICKS_TOKEN in the app server environment.') from None
                if env_host!=config['workspace_url']:raise DatabricksError('DATABRICKS_HOST must match the saved workspace URL before the environment token can be used.')
                try:token=token_value(os.environ.get('DATABRICKS_TOKEN',''))
                except ValueError:raise DatabricksError('Set a valid DATABRICKS_TOKEN in the server environment.') from None
            else:
                cli=shutil.which('databricks')
                if not cli:raise DatabricksError('Install the Databricks CLI and run databricks auth login for this workspace on the app server.')
                command=[cli,'auth','token','--host',config['workspace_url']]
                if config['cli_profile']:command+=['--profile',config['cli_profile']]
                try:
                    result=subprocess.run(command,capture_output=True,text=True,stdin=subprocess.DEVNULL,timeout=15,check=False)
                    if result.returncode:raise ValueError('CLI login unavailable')
                    value=json.loads(result.stdout)
                    token=token_value(value.get('access_token',''))
                except (OSError,ValueError,TypeError,subprocess.TimeoutExpired):raise DatabricksError('Unable to obtain a CLI OAuth token. Run databricks auth login for the saved host/profile, then retry. CLI output is not exposed.') from None
            return config,token,row['revision']

    def _http(self,host,token,method,path,payload=None):
        # Paths are internal constants or validated provider IDs; credentials never follow redirects.
        host=workspace_url(host)
        request=Request(host+path,data=json.dumps(payload).encode() if payload is not None else None,
                        headers={'Authorization':'Bearer '+token,'Accept':'application/json','Content-Type':'application/json'},method=method)
        try:
            with build_opener(NoRedirect()).open(request,timeout=20) as response:
                raw=response.read(1024*1024+1)
                if len(raw)>1024*1024:raise DatabricksError('Workspace response exceeded the 1 MB connection-test limit.')
                return json.loads(raw.decode()) if raw else {}
        except HTTPError as exc:
            code=exc.code
            try:
                body=json.loads(exc.read(65536).decode() or '{}')
                detail=str(body.get('message') or '')[:500] if isinstance(body,dict) else ''
            except (ValueError,UnicodeError,OSError):detail=''
            finally:exc.close()
            messages={400:'Workspace rejected the request. Check the warehouse, catalog/schema, and supported API.',401:'Authentication failed or expired. Refresh your credential and retry.',403:'Permission denied. Check workspace and SQL warehouse permissions.',404:'Resource not found. Check the workspace URL, warehouse ID, and API availability.',429:'Workspace rate limit reached. Wait before retrying.'}
            text=messages.get(code,f'Workspace request failed (HTTP {code}). Redirects are not followed.')
            raise DatabricksError(text+(' Databricks says: '+detail if detail and token not in detail else '')) from None
        except (URLError,OSError,TimeoutError):raise DatabricksError('Could not reach the workspace securely. Check the URL, TLS trust, proxy/VPN, and network access.') from None
        except (ValueError,UnicodeError):raise DatabricksError('Workspace returned an unexpected response format.') from None

    def _cache(self,kind,revision,result):
        from monitoring import Conflict
        with self.lock:
            row=self._row()
            if row['revision']!=revision:raise Conflict('Settings changed during the test. Repeat it using current settings.')
            column='workspace_test_json' if kind=='workspace' else 'warehouse_test_json'
            with self.store.db() as db:db.execute(f'UPDATE databricks_settings SET {column}=? WHERE id=1',(json.dumps(result),))
        return result

    def test_workspace(self,payload):
        config,token,revision=self._credentials(payload)
        try:
            me=self.transport(config['workspace_url'],token,'GET','/api/2.0/preview/scim/v2/Me')
            if not isinstance(me,dict) or not any(isinstance(me.get(k),str) and me[k] for k in ('id','userName','displayName')):raise DatabricksError('Workspace identity response was incomplete; authentication could not be verified.')
            identity=str(me.get('userName') or me.get('displayName') or me['id'])[:255]
            result={'status':'succeeded','checked_at':stamp(),'identity':identity,'message':'Workspace authentication verified. No SQL or compute was started.'}
        except DatabricksError as exc:result={'status':'failed','checked_at':stamp(),'message':str(exc)}
        return self._cache('workspace',revision,result)

    def warehouses(self,payload):
        config,token,revision=self._credentials(payload)
        response=self.transport(config['workspace_url'],token,'GET','/api/2.0/sql/warehouses')
        items=response.get('warehouses',[]) if isinstance(response,dict) else None
        if not isinstance(items,list):raise DatabricksError('Workspace returned an invalid warehouse list.')
        results=[]
        for item in items[:100]:
            if not isinstance(item,dict) or not isinstance(item.get('id'),str) or not WAREHOUSE_PATTERN.fullmatch(item['id']):continue
            results.append({'id':item['id'],'name':str(item.get('name',item['id']))[:255],'state':str(item.get('state','UNKNOWN'))[:40],
                            'serverless':bool(item.get('enable_serverless_compute'))})
        from monitoring import Conflict
        if self._row()['revision']!=revision:raise Conflict('Settings changed while listing warehouses. Reload settings.')
        return {'warehouses':results,'truncated':len(items)>100,'message':'Listing warehouses does not start compute. Save your selection before testing it.'}

    def test_warehouse(self,payload):
        if not isinstance(payload,dict) or payload.get('allow_compute') is not True:raise ValueError('Explicitly allow the SELECT 1 test; it may start a warehouse and use compute.')
        config,token,revision=self._credentials(payload)
        if not config['warehouse_id']:raise ValueError('Select and save a SQL warehouse first.')
        body={'warehouse_id':config['warehouse_id'],'statement':FIXED_SQL,'wait_timeout':'10s','on_wait_timeout':'CANCEL','row_limit':1,'byte_limit':1024,'disposition':'INLINE','format':'JSON_ARRAY'}
        for key in ('catalog','schema'):
            if config[key]:body[key]=config[key]
        try:
            response=self.transport(config['workspace_url'],token,'POST','/api/2.0/sql/statements',body)
            if not isinstance(response,dict):raise DatabricksError('Workspace returned an invalid statement response.')
            state=response.get('status',{}).get('state') if isinstance(response.get('status',{}),dict) else None;statement_id=response.get('statement_id')
            safe_id=statement_id if isinstance(statement_id,str) and re.fullmatch(r'[A-Za-z0-9-]{1,64}',statement_id) else None
            rows=response.get('result',{}).get('data_array') if isinstance(response.get('result',{}),dict) else None
            if state=='SUCCEEDED' and isinstance(rows,list) and len(rows)==1 and isinstance(rows[0],list) and len(rows[0])==1 and str(rows[0][0])=='1':
                result={'status':'succeeded','message':'Fixed SELECT 1 completed on the saved SQL warehouse. Uploaded SQL was not executed.'}
            elif state in ('PENDING','RUNNING'):
                cancelled=False
                if safe_id:
                    try:self.transport(config['workspace_url'],token,'POST',f'/api/2.0/sql/statements/{safe_id}/cancel',{});cancelled=True
                    except DatabricksError:pass
                result={'status':'not_verified','message':'The test did not finish in the inline wait window. '+('Cancellation was requested.' if cancelled else 'Check the warehouse query history for a pending test.')+' A cold warehouse may need a longer warm-up before retrying.'}
            elif state=='CANCELED':result={'status':'not_verified','message':'The fixed test was canceled before a result was verified. A cold warehouse may need warm-up; check query history before retrying.'}
            else:result={'status':'failed','message':'The fixed warehouse test did not return a verified SELECT 1 result. Check warehouse permissions, availability, and catalog/schema settings.'}
            result.update(checked_at=stamp(),warehouse_id=config['warehouse_id'],statement_id=safe_id,statement=FIXED_SQL)
        except DatabricksError as exc:result={'status':'failed','checked_at':stamp(),'warehouse_id':config['warehouse_id'],'message':str(exc),'statement':FIXED_SQL}
        return self._cache('warehouse',revision,result)
