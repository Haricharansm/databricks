'use strict';
(()=>{
let runId=null,settings=null,job=null,timer=null,busy=false;
const terminal=s=>['succeeded','failed','cancelled'].includes(s);
function note(text,error=false){$('dp-message').textContent=text;$('dp-message').className=error?'monitor-error':'section-description';}
function sqlFiles(run){return (run?.conversion?.files||[]).filter(f=>(/\.sql$/i.test(f.name)&&!['coverage_report','migration_plan','workflow_template','notebook','engine_artifact'].includes(f.kind))||(f.kind==='notebook'&&/\.py$/i.test(f.name)));}
function onlyNotebooks(){const names=selected();return names.length&&names.every(n=>/\.py$/i.test(n));}
function selected(){return [...document.querySelectorAll('#dp-files input:checked')].map(i=>i.value);}
function running(){return job&&!terminal(job.status);}
function sync(){
 const files=selected().length;
 $('dp-preview').disabled=busy||!runId||!files;
 $('dp-start').disabled=busy||running()||!runId||!files||!$('dp-consent').checked||(!settings?.warehouse_id&&!onlyNotebooks())||!settings?.credential_configured;
 $('dp-cancel').hidden=!running();
}
async function loadSettings(){
 try{const r=await fetch('/api/databricks/settings');settings=await r.json();}catch{settings=null;}
 const t=settings||{};
 $('dp-target').textContent=!t.workspace_url?'Databricks is not configured. Fill in Databricks settings first.'
  :!t.warehouse_id?'No SQL warehouse saved. Load warehouses in Databricks settings, choose one and save.'
  :!t.credential_configured?'No credential is available in this server session. Re-enter your token in Databricks settings and save.'
  :`Target: warehouse ${t.warehouse_id} · catalog ${t.catalog||'(warehouse default)'} · schema ${t.schema||'(warehouse default)'}`;
 $('dp-target').className='engine-message'+(t.warehouse_id&&t.credential_configured?' ready':'');
 sync();
}
function renderFiles(run){
 const box=$('dp-files');box.replaceChildren();
 const files=sqlFiles(run);
 if(!run){$('dp-context').textContent='Open a converted run in the Migration monitor to deploy it.';}
 else if(!files.length){$('dp-context').textContent=`Run ${run.id.slice(0,8)} has no converted SQL files or notebooks yet. Convert sources first.`;}
 else{const nb=files.filter(f=>f.kind==='notebook').length;$('dp-context').textContent=`Run ${run.id.slice(0,8)} · ${run.label||''} · ${files.length-nb} SQL file(s) run in order, then ${nb} notebook(s) are imported into your workspace.`;}
 files.forEach(f=>{const label=el('label',undefined,'dbx-consent');const box2=el('input');box2.type='checkbox';box2.value=f.name;box2.checked=true;box2.onchange=sync;label.append(box2,document.createTextNode(' '+f.name+(f.kind==='notebook'?' (notebook — imported, not run)':'')));box.append(label);});
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
 $('dp-status').textContent=`Deployment ${job.status} · ${job.completed} of ${job.total} statements succeeded${job.failed?` · ${job.failed} failed`:''}\n${job.message}`;
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
 busy=true;sync();note('Splitting converted SQL into statements. Nothing is sent to Databricks.');
 try{const p=await api('/api/databricks/deploy/preview',{run_id:runId,files:selected(),create_schemas:$('dp-schemas').checked,existing:$('dp-existing').value,notebook_folder:$('dp-folder').value.trim()});job=null;$('dp-status').textContent='';$('dp-progress').hidden=true;renderStatements(p.statements,false);note(`${p.statement_count} statement(s) will run in this order.${p.adjusted?` ${p.adjusted} were automatically adjusted for Databricks (see notes).`:''} Check them before deploying.`);}
 catch(error){note(error.message,true);}finally{busy=false;sync();}
};
$('dp-start').onclick=async()=>{
 await loadSettings();
 if($('dp-existing').value==='replace'&&!confirm('You chose Replace: existing tables with the same names will be rebuilt and their data deleted. Continue?'))return;
 if(!confirm(`Run the selected SQL on warehouse ${settings.warehouse_id}, catalog ${settings.catalog||'(default)'}, schema ${settings.schema||'(default)'}?\n\nThis can create, replace or drop objects in that catalog.`))return;
 busy=true;sync();note('Starting deployment…');
 try{renderJob(await api('/api/databricks/deploy/start',{run_id:runId,files:selected(),create_schemas:$('dp-schemas').checked,existing:$('dp-existing').value,notebook_folder:$('dp-folder').value.trim(),on_error:$('dp-on-error').value,allow_execution:true,revision:settings.revision}));note('Deployment running. You can leave this page; progress continues on the server.');$('dp-consent').checked=false;}
 catch(error){note(error.message,true);}finally{busy=false;sync();}
};
$('dp-cancel').onclick=async()=>{try{renderJob(await api('/api/databricks/deploy/cancel',{id:job.id}));}catch(error){note(error.message,true);}};
$('dp-consent').onchange=sync;
$('dp-refresh').onclick=loadSettings;
setInterval(()=>{const run=window.monitor?.selectedRun();const id=run?.conversion?run.id:null;if(id!==runId||(run&&!$('dp-files').childElementCount&&sqlFiles(run).length)){runId=id;renderFiles(run);if(!running()){renderStatements([],false);}}},1500);
loadSettings();
fetch('/api/databricks/deploy/latest').then(r=>r.json()).then(d=>{if(d.deployment)renderJob(d.deployment);}).catch(()=>{});
})();
