'use strict';
(()=>{
let runId=null,settings=null,job=null,timer=null,busy=false;
const terminal=s=>['succeeded','failed','cancelled'].includes(s);
function note(text,error=false){$('dp-message').textContent=text;$('dp-message').className=error?'monitor-error':'section-description';}
function deployFiles(run){return (run?.conversion?.files||[]).filter(f=>(/\.sql$/i.test(f.name)&&!['coverage_report','migration_plan','workflow_template','notebook','engine_artifact'].includes(f.kind))||(f.kind==='notebook'&&/\.py$/i.test(f.name)));}
function selected(){return [...document.querySelectorAll('#dp-files input:checked')].map(i=>i.value);}
function needsWarehouse(){return [...document.querySelectorAll('#dp-files input:checked')].some(i=>/\.sql$/i.test(i.value));}
function options(){return {notebook_folder:$('dp-folder').value.trim(),overwrite_notebooks:$('dp-overwrite').checked};}
function running(){return job&&!terminal(job.status);}
function sync(){
 const files=selected().length;
 $('dp-preview').disabled=busy||!runId||!files;
 $('dp-start').disabled=busy||running()||!runId||!files||!$('dp-consent').checked||(needsWarehouse()&&!settings?.warehouse_id)||!settings?.credential_configured;
 $('dp-cancel').hidden=!running();
}
async function loadSettings(){
 try{const r=await fetch('/api/databricks/settings');settings=await r.json();}catch{settings=null;}
 const t=settings||{};
 $('dp-target').textContent=!t.workspace_url?'Databricks is not configured. Fill in Databricks settings first.'
  :!t.credential_configured?'No credential is available in this server session. Re-enter your token in Databricks settings and save.'
  :!t.warehouse_id?'No SQL warehouse saved. Notebook imports are available; select a warehouse to execute SQL.'
  :`Target: warehouse ${t.warehouse_id} · catalog ${t.catalog||'(warehouse default)'} · schema ${t.schema||'(warehouse default)'}`;
 $('dp-target').className='engine-message'+(t.workspace_url&&t.credential_configured?' ready':'');
 sync();
}
function renderFiles(run){
 const box=$('dp-files');box.replaceChildren();
 const files=deployFiles(run);
 if(!run){$('dp-context').textContent='Open a converted run in the Migration monitor to deploy it.';}
 else if(!files.length){$('dp-context').textContent=`Run ${run.id.slice(0,8)} has no converted SQL files or Python notebooks yet. Convert sources first.`;}
 else{$('dp-context').textContent=`Run ${run.id.slice(0,8)} · ${run.label||''} · ${files.length} converted artifact(s). SQL runs first; notebooks are imported without execution.`;}
 files.forEach(f=>{const label=el('label',undefined,'dbx-consent');const box2=el('input');box2.type='checkbox';box2.value=f.name;box2.checked=true;box2.onchange=sync;label.append(box2,document.createTextNode(' '+f.name+(f.kind==='notebook'?' · import only':'')));box.append(label);});
 sync();
}
function renderStatements(list,withStatus){
 const table=$('dp-table');table.replaceChildren();
 if(!list?.length){table.hidden=true;return;}
 table.hidden=false;
 const head=el('tr');['#','File','Statement',withStatus?'Result':''].forEach(h=>head.append(el('th',h)));table.append(head);
 list.forEach((s,i)=>{
  const row=el('tr');row.append(el('td',String(i+1)),el('td',`${s.file} (#${s.number})`));
  const code=el('td');code.append(el('pre',s.preview));if(s.note)code.append(el('small',s.note,'wb-result-note'));row.append(code);
  const result=el('td');
  if(withStatus){result.append(el('span',s.status,'run-state '+({succeeded:'completed',running:'converting',pending:'queued',skipped:'queued',cancelled:'blocked'}[s.status]||s.status)));if(s.seconds!=null)result.append(el('small',` ${s.seconds}s`));if(s.error)result.append(el('pre',s.error,'monitor-error'));}
  row.append(result);table.append(row);
 });
}
function renderJob(data){
 job=data;
 if(!job){$('dp-status').textContent='';return;}
 $('dp-status').textContent=`Deployment ${job.status} · ${job.completed} of ${job.total} actions completed${job.failed?` · ${job.failed} failed`:''}\n${job.message}`;
 $('dp-status').className='notice'+(job.status==='failed'?' error':'');
 $('dp-progress').hidden=false;$('dp-progress').max=job.total;$('dp-progress').value=job.completed;
 renderStatements(job.statements,true);sync();
 clearTimeout(timer);
 if(running())timer=setTimeout(poll,2000);
}
async function poll(){
 if(!job)return;
 try{const r=await fetch('/api/databricks/deploy/'+job.id);const data=await r.json();if(!r.ok)throw new Error(data.error);renderJob(data);}
 catch(error){note(error.message,true);}
}
$('dp-preview').onclick=async()=>{
 busy=true;sync();note('Preparing SQL statements and notebook imports. Nothing is sent to Databricks.');
 try{const p=await api('/api/databricks/deploy/preview',{run_id:runId,files:selected(),...options(),create_schemas:$('dp-schemas').checked,existing:$('dp-existing').value});job=null;$('dp-status').textContent='';$('dp-progress').hidden=true;renderStatements(p.statements,false);note(`${p.statement_count} deployment action(s) planned. Notebooks are imported only.${p.adjusted?` ${p.adjusted} were automatically adjusted for Databricks (see notes).`:''} Check them before deploying.`);}
 catch(error){note(error.message,true);}finally{busy=false;sync();}
};
$('dp-start').onclick=async()=>{
 await loadSettings();
 if(needsWarehouse()&&$('dp-existing').value==='replace'&&!confirm('You chose Replace: existing tables with the same names will be rebuilt and their data deleted. Continue?'))return;
 if(!confirm(`Deploy selected artifacts to ${settings.workspace_url}?\n\nSQL actions use warehouse ${settings.warehouse_id||'(none)'} and can change objects. Python notebooks are imported without running.${$('dp-overwrite').checked?' Existing notebooks at the destination may be overwritten.':''}`))return;
 busy=true;sync();note('Starting deployment…');
 try{renderJob(await api('/api/databricks/deploy/start',{run_id:runId,files:selected(),...options(),create_schemas:$('dp-schemas').checked,existing:$('dp-existing').value,on_error:$('dp-on-error').value,allow_execution:true,revision:settings.revision}));note('Deployment running. You can leave this page; progress continues on the server.');$('dp-consent').checked=false;}
 catch(error){note(error.message,true);}finally{busy=false;sync();}
};
$('dp-cancel').onclick=async()=>{try{renderJob(await api('/api/databricks/deploy/cancel',{id:job.id}));}catch(error){note(error.message,true);}};
$('dp-consent').onchange=sync;
$('dp-refresh').onclick=loadSettings;
setInterval(()=>{const run=window.monitor?.selectedRun();const id=run?.conversion?run.id:null;if(id!==runId||(run&&!$('dp-files').childElementCount&&deployFiles(run).length)){runId=id;renderFiles(run);if(!running()){renderStatements([],false);}}},1500);
loadSettings();
fetch('/api/databricks/deploy/latest').then(r=>r.json()).then(d=>{if(d.deployment)renderJob(d.deployment);}).catch(()=>{});
})();
