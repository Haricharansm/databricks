'use strict';
(()=>{
let saved=null,dirty=false,working=false;
function message(text,error=false){$('dbx-message').textContent=text;$('dbx-message').className=error?'monitor-error':'section-description';}
function authFields(){const mode=$('dbx-auth').value;$('dbx-session').hidden=mode!=='session_token';$('dbx-cli').hidden=mode!=='cli_oauth';$('dbx-environment').hidden=mode!=='environment_token';const host=$('dbx-host').value.trim();const safe=/^https:\/\/[a-zA-Z0-9.-]+\/?$/.test(host);$('dbx-cli-command').textContent='databricks auth login --host '+(safe?host.replace(/\/$/,''):'<workspace-url>');}
function sync(){
 $('dbx-fields').disabled=working;
 const ready=Boolean(saved?.workspace_url&&saved.credential_configured&&!dirty&&!working);
 $('dbx-test-workspace').disabled=!ready;$('dbx-load-warehouses').disabled=!ready;
 $('dbx-test-warehouse').disabled=!ready||!saved.warehouse_id||!$('dbx-allow-compute').checked;
 $('dbx-forget').disabled=working||!saved;
}
function changed(){dirty=true;authFields();sync();}
function testText(result){if(!result)return 'Not tested.';return `${result.status.replaceAll('_',' ')} · ${new Date(result.checked_at).toLocaleString()}\n${result.message}${result.identity?'\nIdentity: '+result.identity:''}${result.statement_id?'\nStatement ID: '+result.statement_id:''}`;}
function render(config){
 saved=config;dirty=false;
 $('dbx-host').value=config.workspace_url;$('dbx-auth').value=config.auth_mode;$('dbx-warehouse').value=config.warehouse_id;$('dbx-catalog').value=config.catalog;$('dbx-schema').value=config.schema;$('dbx-profile').value=config.cli_profile;$('dbx-token').value='';$('dbx-allow-compute').checked=false;
 $('dbx-badge').textContent=!config.workspace_url?'Not configured':!config.credential_configured?'Credential needed':config.workspace_test?.status==='succeeded'?'Last workspace test succeeded':'Ready to test';
 const explanation=config.auth_mode==='cli_oauth'?(config.cli_available?'CLI is installed; the workspace test will verify your OAuth login.':'Databricks CLI was not found on the app server.'):config.auth_mode==='environment_token'?(config.credential_configured?'Matching environment credential is configured; test to verify access.':'Matching DATABRICKS_HOST and DATABRICKS_TOKEN are not configured.'):(config.credential_configured?'A token is held for this server session.':'No token is held in this server session. Enter one locally and save.');
 $('dbx-credential').textContent=explanation+` Settings revision ${config.revision}.`;
 $('dbx-workspace-result').textContent=testText(config.workspace_test);$('dbx-warehouse-result').textContent=testText(config.warehouse_test);
 authFields();sync();
}
async function load(){const previous=working;working=true;sync();try{const response=await fetch('/api/databricks/settings');const config=await response.json();if(!response.ok)throw new Error(config.error||'Unable to load Databricks settings.');render(config);}finally{working=previous;sync();}}
async function action(path,payload){
 working=true;sync();
 try{return await api('/api/databricks/'+path,payload);}finally{working=false;sync();}
}
$('dbx-form').oninput=event=>{if(event.target.id!=='dbx-allow-compute')changed();else sync();};
$('dbx-auth').onchange=changed;
$('dbx-warehouses').onchange=()=>{if($('dbx-warehouses').value){$('dbx-warehouse').value=$('dbx-warehouses').value;changed();message('Warehouse selected. Save settings before testing it.');}};
$('dbx-form').onsubmit=async event=>{
 event.preventDefault();const payload={workspace_url:$('dbx-host').value,auth_mode:$('dbx-auth').value,warehouse_id:$('dbx-warehouse').value,catalog:$('dbx-catalog').value,schema:$('dbx-schema').value,cli_profile:$('dbx-profile').value,token:$('dbx-auth').value==='session_token'?$('dbx-token').value:'',revision:saved?.revision};$('dbx-token').value='';
 try{render(await action('settings',payload));message('Non-secret settings saved. Session credentials stay in memory. Test the workspace before using the warehouse probe.');}catch(error){message(error.message+' If you entered a new token, re-enter it before saving again.',true);}
};
$('dbx-reload').onclick=async()=>{working=true;sync();try{await load();message('Saved settings reloaded. Unsaved edits were discarded.');}catch(error){message(error.message,true);}finally{working=false;sync();}};
$('dbx-forget').onclick=async()=>{if(!saved)return;try{render(await action('forget-credential',{revision:saved.revision}));message('Session credential cleared and session-token mode selected. External environment/CLI credentials were not changed.');}catch(error){message(error.message,true);}};
$('dbx-test-workspace').onclick=async()=>{
 message('Testing workspace authentication without starting compute…');try{const result=await action('test-workspace',{revision:saved.revision});await load();message(result.message,result.status!=='succeeded');}catch(error){message(error.message,true);}
};
$('dbx-load-warehouses').onclick=async()=>{
 message('Loading SQL warehouses without starting compute…');try{const result=await action('warehouses',{revision:saved.revision});$('dbx-warehouses').replaceChildren();const blank=el('option',result.warehouses.length?'Choose a warehouse':'No visible SQL warehouses');blank.value='';$('dbx-warehouses').append(blank);result.warehouses.forEach(warehouse=>{const option=el('option',`${warehouse.name} · ${warehouse.state}${warehouse.serverless?' · serverless':''}`);option.value=warehouse.id;$('dbx-warehouses').append(option);});message(result.message+(result.truncated?' Showing the first 100 warehouses. You can enter a warehouse ID manually.':''));}catch(error){message(error.message,true);}
};
$('dbx-test-warehouse').onclick=async()=>{
 if(!$('dbx-allow-compute').checked)return;message('Running the fixed SELECT 1 connection test. No uploaded SQL is sent.');
 try{const result=await action('test-warehouse',{revision:saved.revision,allow_compute:true});await load();message(result.message,result.status!=='succeeded');}catch(error){message(error.message,true);}
};
load().catch(error=>message(error.message,true));
})();
