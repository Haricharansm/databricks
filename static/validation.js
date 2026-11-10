'use strict';
(()=>{
let run=null,currentCase=null,columns=[],dirty=true,working=false,generation=0,activeReport=null;
const active=new Set(['uploaded','assessing','queued','converting']);
const supported=source=>['sql','sql_script','stored_procedure'].includes(source.kind);
function notify(text,error=false){$('wb-message').textContent=text;$('wb-message').className=error?'monitor-error':'section-description';}
async function get(path){const response=await fetch(path);const data=await response.json();if(!response.ok)throw new Error(data.error||'Unable to load validation data.');return data;}
function base(){return '/api/runs/'+run.id+'/validation';}
function sync(){
  $('wb-fields').disabled=working||!run||active.has(run.status)||!run.sources.some(supported);
  $('wb-compare').disabled=!currentCase||dirty||working;
  $('wb-report-download').disabled=!activeReport||working;
  $('wb-restore-inputs').disabled=!$('wb-attempt').value||working;
  $('wb-version').textContent=currentCase?`Version ${currentCase.version}${dirty?' · unsaved edits; save before comparing':' · saved'}`:'Save the definition before comparing.';
}
function markDirty(){dirty=true;sync();}
function inputField(label,control){const wrapper=el('label',label);wrapper.append(control);return wrapper;}
function renderColumns(){
  const root=$('wb-columns');root.replaceChildren();
  columns.forEach((column,index)=>{
    const row=el('div',undefined,'wb-column');
    const name=el('input');name.value=column.name;name.maxLength=128;name.required=true;name.setAttribute('aria-label',`Column ${index+1} name`);name.oninput=()=>{column.name=name.value;markDirty();};
    const type=el('select');type.setAttribute('aria-label',`Column ${index+1} type`);
    ['string','integer','decimal','boolean','date','timestamp'].forEach(t=>{const option=el('option',t);option.value=t;type.append(option);});type.value=column.type;
    type.onchange=()=>{column.type=type.value;if(!['integer','decimal'].includes(column.type))column.tolerance='0';renderColumns();markDirty();};
    const tolerance=el('input');tolerance.value=column.tolerance||'0';tolerance.setAttribute('aria-label',`Column ${index+1} tolerance`);tolerance.inputMode='decimal';tolerance.disabled=$('wb-mode').value==='unordered'||column.key||!['integer','decimal'].includes(column.type);tolerance.oninput=()=>{column.tolerance=tolerance.value;markDirty();};
    const nullable=el('input');nullable.type='checkbox';nullable.checked=column.nullable!==false;nullable.setAttribute('aria-label',`Column ${index+1} allows NULL`);nullable.onchange=()=>{column.nullable=nullable.checked;markDirty();};
    const key=el('input');key.type='checkbox';key.checked=Boolean(column.key);key.disabled=$('wb-mode').value!=='keyed';key.setAttribute('aria-label',`Column ${index+1} is a key`);key.onchange=()=>{column.key=key.checked;if(column.key)column.tolerance='0';renderColumns();markDirty();};
    const remove=el('button','Remove','remove');remove.type='button';remove.setAttribute('aria-label',`Remove column ${index+1}`);remove.onclick=()=>{if(columns.length===1)return notify('Declare at least one result column.',true);columns.splice(index,1);renderColumns();markDirty();};
    const nullLabel=inputField('Allow NULL',nullable);nullLabel.className='wb-check';const keyLabel=inputField('Key',key);keyLabel.className='wb-check';
    row.append(inputField('Column name',name),inputField('Type',type),inputField('Absolute tolerance',tolerance),nullLabel,keyLabel,remove);root.append(row);
  });
}
function clearData(){['expected','actual','expected-ref','actual-ref','evidence'].forEach(id=>$('wb-'+id).value='');$('wb-expected-format').value='csv';$('wb-actual-format').value='json';activeReport=null;$('wb-result').hidden=true;}
function populate(value){
  $('wb-source').value=value.source||run?.sources.find(supported)?.name||'';
  $('wb-title').value=value.title||'';$('wb-scenario').value=value.scenario||'';$('wb-source-sql').value=value.source_sql||'';$('wb-target-sql').value=value.target_sql||'';$('wb-parameters').value=value.parameters||'{}';$('wb-mode').value=value.mode||'unordered';$('wb-null').value=value.null_token??'\\N';
  columns=(value.columns||[{name:'id',type:'integer',nullable:false,tolerance:'0'}]).map(c=>({...c,key:(value.key_columns||[]).includes(c.name)}));renderColumns();
}
function newCase(){currentCase=null;$('wb-case').value='';populate({});clearData();$('wb-attempt').replaceChildren(el('option','No comparisons yet'));$('wb-attempt').options[0].value='';dirty=true;sync();}
function collect(){return {source:$('wb-source').value,title:$('wb-title').value,scenario:$('wb-scenario').value,source_sql:$('wb-source-sql').value,target_sql:$('wb-target-sql').value,parameters:$('wb-parameters').value,mode:$('wb-mode').value,null_token:$('wb-null').value,columns:columns.map(({key,...column})=>column),key_columns:columns.filter(c=>c.key).map(c=>c.name),...(currentCase?{version:currentCase.version}:{})};}
async function listCases(selectId){
  const token=generation;const data=await get(base());if(token!==generation)return;
  const selected=selectId??currentCase?.id??'';$('wb-case').replaceChildren();const option=el('option','New test case');option.value='';$('wb-case').append(option);
  data.cases.forEach(c=>{const status=c.latest_attempt?(c.latest_attempt.stale?'definition changed':c.latest_attempt.status):'not compared';const option=el('option',`${c.definition.title} · v${c.version} · ${status}`);option.value=c.id;$('wb-case').append(option);});$('wb-case').value=selected;
  const s=data.summary;$('wb-stats').textContent=`${s.cases} cases · ${s.passed} matched · ${s.failed} mismatched · ${s.error} input errors · ${s.not_compared} not compared for current version`;
  return data;
}
function attempts(caseData){
  $('wb-attempt').replaceChildren();if(!caseData.attempts.length){const option=el('option','No comparisons yet');option.value='';$('wb-attempt').append(option);}
  caseData.attempts.forEach(a=>{const option=el('option',`${new Date(a.created_at).toLocaleString()} · v${a.case_version} · ${a.status}${a.stale?' · superseded definition':''}`);option.value=a.id;$('wb-attempt').append(option);});
}
async function loadCase(id){
  if(!id){newCase();notify('Define a test case, save it, then supply both result snapshots.');return;}
  const token=generation;working=true;sync();
  try{const data=await get(base()+'/cases/'+id);if(token!==generation)return;currentCase=data;populate(data.definition);dirty=false;clearData();attempts(data);if(data.attempts.length)await loadAttempt(data.attempts[0].id);notify(`Loaded test case version ${data.version}.`);}
  catch(error){notify(error.message,true);}finally{if(token===generation){working=false;sync();}}
}
async function loadAttempt(id){
  if(!id)return;const token=generation;const data=await get(base()+'/attempts/'+id);if(token!==generation)return;
  activeReport=data.report;$('wb-attempt').value=id;renderResult(data.report,data.stale);sync();
}
function renderResult(report,stale){
  const root=$('wb-result');root.hidden=false;root.replaceChildren();
  const header=el('div',undefined,'wb-result-header');header.append(el('span',report.status==='passed'?'Result snapshots matched':report.status==='failed'?'Result snapshots differ':'Comparison input error','run-state comparison-'+report.status),el('strong',`Test version ${report.case_version}${stale?' · superseded definition':''}`));root.append(header);
  root.append(el('p',`Compared ${new Date(report.created_at).toLocaleString()} · no SQL executed · provenance supplied manually`,'wb-result-note'));
  if(report.error)root.append(el('p',report.error,'monitor-error'));
  const table=el('table',undefined,'wb-table'),head=el('thead'),heading=el('tr');['Check','Result','Details'].forEach(t=>heading.append(el('th',t)));head.append(heading);table.append(head);const body=el('tbody');
  report.checks.forEach(check=>{const row=el('tr');row.append(el('td',check.name.replaceAll('_',' ')),el('td',check.passed?'Matched':'Failed'));const detail=el('td');const {name,passed,...rest}=check;detail.append(el('pre',JSON.stringify(rest,null,2)));row.append(detail);body.append(row);});table.append(body);root.append(table);
  if(report.duplicate_rows)root.append(el('p',`Exact duplicate rows: expected ${report.duplicate_rows.expected}, actual ${report.duplicate_rows.actual}. Value mismatch count: ${report.mismatch_count}. Counts alone do not establish a match.`,'wb-result-note'));
  if(report.mismatches.length){const detail=el('details',undefined,'wb-result-details');detail.open=true;detail.append(el('summary',`Mismatch samples (up to 20; total ${report.mismatch_count})`),el('pre',JSON.stringify(report.mismatches,null,2)));root.append(detail);}
  const evidence=el('details',undefined,'wb-result-details');evidence.append(el('summary','Definition, input hashes, and recorded evidence'),el('pre',JSON.stringify({definition:report.definition,inputs:report.inputs,provenance:report.provenance,source_conversion_status:report.source_conversion_status,source_snapshot:report.source_snapshot},null,2)));root.append(evidence,el('p',report.limitations,'wb-result-note'));
}
async function selectRun(value){
  const changed=run?.id!==value?.id;run=value;
  if(!run){generation++;working=false;currentCase=null;activeReport=null;$('wb-context').textContent='Open a saved SQL migration run in the monitor to create test cases.';$('wb-stats').textContent='';$('wb-result').hidden=true;sync();return;}
  $('wb-context').textContent=`Run ${run.id.slice(0,8)} · ${run.label}${active.has(run.status)?' · wait for this run to finish before comparing.':' · result snapshots are compared locally; queries are not executed.'}`;
  if(!changed){sync();return;}
  generation++;working=false;notify('');$('wb-source').replaceChildren();run.sources.filter(supported).forEach(source=>{const option=el('option',source.name);option.value=source.name;$('wb-source').append(option);});newCase();
  try{const cases=await listCases('');if(cases?.cases.length){$('wb-case').value=cases.cases[0].id;await loadCase(cases.cases[0].id);}}
  catch(error){notify(error.message,true);}sync();
}
$('wb-definition').oninput=markDirty;
$('wb-mode').onchange=()=>{columns.forEach(c=>{if($('wb-mode').value!=='keyed')c.key=false;if($('wb-mode').value==='unordered')c.tolerance='0';});renderColumns();markDirty();};
$('wb-null').onchange=markDirty;
$('wb-case').onchange=()=>loadCase($('wb-case').value);
$('wb-new').onclick=()=>{newCase();notify('New test case. Save it before comparing results.');};
$('wb-add-column').onclick=()=>{if(columns.length>=100)return notify('A test case supports at most 100 columns.',true);columns.push({name:'',type:'string',nullable:true,tolerance:'0'});renderColumns();markDirty();};
$('wb-definition').onsubmit=async event=>{
  event.preventDefault();const payload=collect();const token=generation;const path=base()+'/cases'+(currentCase?'/'+currentCase.id:'');working=true;sync();
  try{const saved=await api(path,payload);if(token!==generation)return;currentCase=saved;populate(saved.definition);dirty=false;attempts(saved);if(activeReport)renderResult(activeReport,activeReport.case_version!==saved.version);await listCases(saved.id);notify(`Saved test case version ${saved.version}. Existing comparisons retain their original definitions.`);}
  catch(error){notify(error.message,true);}finally{if(token===generation){working=false;sync();}}
};
$('wb-compare').onclick=async()=>{
  if(!currentCase||dirty)return notify('Save the test definition before comparing.',true);
  const payload={version:currentCase.version,expected:{format:$('wb-expected-format').value,content:$('wb-expected').value},actual:{format:$('wb-actual-format').value,content:$('wb-actual').value},expected_ref:$('wb-expected-ref').value,actual_ref:$('wb-actual-ref').value,evidence:$('wb-evidence').value};
  const token=generation;const path=base()+'/cases/'+currentCase.id;working=true;sync();notify('Comparing uploaded result snapshots…');
  try{const result=await api(path+'/compare',payload);if(token!==generation)return;activeReport=result;currentCase=await get(path);attempts(currentCase);$('wb-attempt').value=result.id;renderResult(result,false);await listCases(currentCase.id);notify('Comparison saved. Source approval and manual Databricks validation are unchanged.');}
  catch(error){notify(error.message,true);}finally{if(token===generation){working=false;sync();}}
};
$('wb-attempt').onchange=()=>loadAttempt($('wb-attempt').value).catch(error=>notify(error.message,true));
$('wb-report-download').onclick=()=>{if(activeReport)download(`comparison-${activeReport.id}.json`,JSON.stringify(activeReport,null,2),'application/json');};
$('wb-restore-inputs').onclick=async()=>{
  const token=generation;working=true;sync();try{const data=await get(base()+'/attempts/'+$('wb-attempt').value+'?inputs=1');if(token!==generation)return;for(const side of ['expected','actual']){$('wb-'+side).value=data.recorded_inputs[side].content;$('wb-'+side+'-format').value=data.recorded_inputs[side].format;}$('wb-expected-ref').value=data.report.provenance.expected_ref;$('wb-actual-ref').value=data.report.provenance.actual_ref;$('wb-evidence').value=data.report.provenance.evidence;notify(`Restored recorded inputs from version ${data.report.case_version}. A new comparison will use saved version ${currentCase.version}.`);}catch(error){notify(error.message,true);}finally{if(token===generation){working=false;sync();}}
};
for(const side of ['expected','actual'])$('wb-'+side+'-file').onchange=async event=>{
  try{const file=event.target.files[0];if(!file)return;if(file.size>2*1024*1024)throw new Error('Each result file is limited to 2 MB.');if(!/\.(csv|json)$/i.test(file.name))throw new Error('Use a CSV or JSON result export.');const text=new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer());$('wb-'+side).value=text;$('wb-'+side+'-format').value=/\.json$/i.test(file.name)?'json':'csv';notify(`Loaded ${file.name}. Supply the export reference and execution evidence before comparing.`);}catch(error){notify(error.message,true);}finally{event.target.value='';}
};
$('wb-demo').onclick=()=>{
  newCase();populate({title:'Demo · nulls, decimals, and duplicate rows',scenario:'Synthetic result comparison only. Demonstrates exact unordered matching with duplicate preservation.',columns:[{name:'id',type:'integer',nullable:false,tolerance:'0'},{name:'amount',type:'decimal',nullable:false,tolerance:'0'},{name:'note',type:'string',nullable:true,tolerance:'0'}]});
  $('wb-expected-format').value='csv';$('wb-expected').value='id,amount,note\n1,10.00,paid\n2,20.50,\\N\n2,20.50,\\N\n';$('wb-actual-format').value='json';$('wb-actual').value=JSON.stringify([{id:2,amount:'20.50',note:null},{id:1,amount:'10.0',note:'paid'},{id:2,amount:'20.50',note:null}],null,2);$('wb-expected-ref').value='Synthetic SQL Server-style fixture; no database execution';$('wb-actual-ref').value='Synthetic Databricks-style fixture; no database execution';$('wb-evidence').value='Demo data only. The same synthetic rows are supplied in different formats and order. No SQL Server or Databricks queries were run.';markDirty();notify('Synthetic demo loaded. Save the test case, then compare the supplied fixtures.');
};
window.workbench={selectRun};selectRun(window.monitor?.selectedRun()||null);
})();
