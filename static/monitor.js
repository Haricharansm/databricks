'use strict';
(()=>{
const active=new Set(['uploaded','assessing','queued','converting']);
let currentId=null,selectedId=null,selectedRun=null,offset=0,total=0,pollToken=0,lastUpdated='',refreshing=false;
const stateLabel=s=>({uploaded:'Uploaded',assessing:'Assessing',assessed:'Assessed',queued:'Queued',converting:'Converting',converted:'Converted',completed:'Conversion complete',partial:'Partially blocked',failed:'Failed',blocked:'Blocked',interrupted:'Interrupted'})[s]||s;
function timestamp(value){return value?new Date(value).toLocaleString():'';}
function duration(value){if(value===null||value===undefined)return 'Not started';return value<60?`${value.toFixed(1)}s`:`${Math.floor(value/60)}m ${Math.floor(value%60)}s`;}
function pointer(value){try{if(value)localStorage.setItem('crosslake.currentRun',value);else localStorage.removeItem('crosslake.currentRun');}catch{}}
async function get(path){const response=await fetch(path);const data=await response.json();if(!response.ok)throw new Error(data.error||'Unable to load run.');return data;}
function runState(state){return el('span',stateLabel(state),'run-state '+state);}
function clearWorkspace(){currentId=null;pointer(null);pollToken++;busy=false;}
function showConversion(){
  $('output-select').replaceChildren();
  if(!conversion)return;
  conversion.files.forEach((f,i)=>{const option=el('option',f.name);option.value=String(i);$('output-select').append(option);});
  $('output-code').textContent=conversion.files[0]?.sql||'';
  $('conversion-warning').textContent=conversion.warning;
  $('diagnostics').textContent=[conversion.coverage?JSON.stringify(conversion.coverage,null,2):'',conversion.diagnostics,conversion.conversion_errors].filter(Boolean).join('\n\n');
  $('conversion-details').hidden=false;
}
function restore(run){
  currentId=run.id;selectedId=run.id;pointer(run.id);selectedRun=run;
  files=run.files;report=run.assessment;conversion=run.conversion;busy=active.has(run.status);
  renderFiles();
  if(report)renderReport();
  else{['files','lines','high','findings'].forEach(k=>$('metric-'+k).textContent='—');$('assessment-results').replaceChildren();$('type-summary').textContent='';$('assessment-empty').hidden=false;}
  if(conversion)showConversion();
  else{$('output-select').replaceChildren();$('output-code').textContent='-- Generated artifacts will appear after conversion.';$('conversion-details').hidden=true;}
  sync();renderDetail(run,true);
  if(active.has(run.status))message(`${stateLabel(run.status)} · run ${run.id.slice(0,8)}. Progress is saved; you can refresh this page.`);
  else message(run.error||`${stateLabel(run.status)} · ${run.progress.converted} generated, ${run.progress.blocked} blocked.`,Boolean(run.error));
}
async function openRun(id){
  const run=await get('/api/runs/'+id);pollToken++;restore(run);await refresh();
  if(active.has(run.status))watch(id,++pollToken);
}
async function watch(id,token){
  if(token!==pollToken||id!==currentId)return;
  try{
    const summary=await get('/api/runs/'+id+'?summary=1');
    if(token!==pollToken||id!==currentId)return;
    renderDetail(summary);await refresh();
    if(active.has(summary.status)){
      const current=summary.sources.find(s=>s.status==='converting');
      message(`${stateLabel(summary.status)} · ${summary.progress.finished}/${summary.progress.total} sources finished${current?' · '+current.name:''}. Progress is saved.`);
    }else{restore(await get('/api/runs/'+id));return;}
  }catch(error){$('monitor-error').textContent='Unable to refresh progress: '+error.message+' The server may still be processing; retrying.';}
  setTimeout(()=>watch(id,token),1500);
}
async function startAssessment(){
  busy=true;sync();message('Saving run and queueing assessment…');
  try{
    const run=await api('/api/runs',{files});currentId=run.id;selectedId=run.id;pointer(run.id);
    const token=++pollToken;renderDetail(run,true);await refresh();watch(run.id,token);
  }catch(error){busy=false;sync();message(error.message,true);}
}
async function startConversion(){
  if(!currentId)return message('Assess the sources to create a saved run before converting.',true);
  busy=true;sync();message('Queueing conversion…');
  try{const run=await api('/api/runs/'+currentId+'/convert',{});renderDetail(run,true);await refresh();watch(currentId,++pollToken);}
  catch(error){busy=false;sync();message(error.message,true);}
}
function field(label,control){const wrapper=el('label',label);wrapper.append(control);return wrapper;}
function choice(options,value,aria){const select=el('select');select.setAttribute('aria-label',aria);options.forEach(([key,label])=>{const option=el('option',label);option.value=key;select.append(option);});select.value=value;return select;}
function renderDetail(run,force=false){
  if(selectedId&&selectedId!==run.id)return;
  selectedId=run.id;
  if(!force&&lastUpdated===run.id+run.updated_at)return;
  const focused=$('source-progress').contains(document.activeElement);
  if(!force&&focused)return;
  lastUpdated=run.id+run.updated_at;
  if(selectedRun?.id===run.id)selectedRun={...selectedRun,...run};else selectedRun=run;
  $('run-detail').hidden=false;$('run-title').textContent=run.label;
  $('run-meta').textContent=`Run ${run.id.slice(0,8)} · created ${timestamp(run.created_at)} · ${stateLabel(run.status)} · phase duration ${duration(run.duration_seconds)}`;
  const progress=$('run-progress');progress.replaceChildren();
  progress.append(el('div',`${run.progress.finished} / ${run.progress.total} sources finished conversion · ${run.progress.converted} generated · ${run.progress.blocked} blocked`,'progress-label'));
  const bar=el('progress',undefined,'run-progress-bar');bar.max=Math.max(1,run.progress.total);bar.value=run.progress.finished;bar.setAttribute('aria-label','Sources finished conversion');progress.append(bar);
  $('run-error').textContent=run.error||'';$('delete-run').disabled=active.has(run.status);
  const sources=$('source-progress');sources.replaceChildren();
  run.sources.forEach(source=>{
    const card=el('div',undefined,'source-card'),heading=el('div',undefined,'source-status');
    heading.append(el('strong',source.name),runState(source.status),el('small',`${source.kind} · ${source.engine||'engine not started'}`));card.append(heading);
    card.append(el('p',`${source.complexity||'Not assessed'} complexity · ${source.findings} finding categories · review: ${source.review.replaceAll('_',' ')} · validation: ${source.validation==='not_run'?'not run (manual)':source.validation+' (manual)'}`,'section-description'));
    if(source.error)card.append(el('p',source.error,'monitor-error'));
    const detail=el('details',undefined,'run-details-toggle');detail.append(el('summary','Review notes & manual validation'));
    const form=el('form',undefined,'review-form');
    const review=choice([['pending','Awaiting review'],['in_review','In review'],['approved','Review approved']],source.review,'Review status for '+source.name);
    const validation=choice([['not_run','Not run'],['passed','Passed (manual)'],['failed','Failed (manual)']],source.validation,'Manual validation for '+source.name);
    const note=el('textarea');note.value=source.review_note;note.maxLength=4000;note.setAttribute('aria-label','Review notes for '+source.name);
    const evidence=el('textarea');evidence.value=source.validation_note;evidence.maxLength=4000;evidence.placeholder='Test date, workspace/job reference, sample data, and observed results';evidence.setAttribute('aria-label','Validation evidence for '+source.name);
    const save=el('button','Save review','secondary');save.type='submit';
    const feedback=el('span','Recorded by you; no automatic Databricks checks.','review-note');feedback.setAttribute('role','status');
    form.append(field('Code review',review),field('Databricks validation (manual)',validation),field('Review notes',note),field('Test evidence / failure reason',evidence),save,feedback);
    if(active.has(run.status))for(const control of form.elements)control.disabled=true;
    form.onsubmit=async event=>{
      event.preventDefault();save.disabled=true;
      try{
        const updated=await api('/api/runs/'+run.id+'/review',{source:source.name,review:review.value,validation:validation.value,review_note:note.value,validation_note:evidence.value,version:source.version});
        renderDetail(updated,true);await refresh();
      }catch(error){feedback.textContent=error.message;feedback.className='monitor-error';save.disabled=false;}
    };
    detail.append(form);card.append(detail);sources.append(card);
  });
  const timeline=$('run-events');timeline.replaceChildren();
  run.events.forEach(event=>{const row=el('div',event.message,'timeline-event');row.prepend(el('small',timestamp(event.created_at)+(event.source?' · '+event.source:'')));timeline.append(row);});
}
async function refresh(){
  if(refreshing)return;refreshing=true;
  try{
    const params=new URLSearchParams({limit:'25',offset:String(offset),status:$('run-filter').value,q:$('run-search').value});
    const data=await get('/api/runs?'+params);total=data.total;
    const stats={runs:'runs',active:'active',blocked:'blocked',review:'awaiting_review',passed:'manual_passed'};
    for(const [id,key] of Object.entries(stats))$('mon-'+id).textContent=data.stats[key];
    const list=$('run-list');list.replaceChildren();
    if(!data.runs.length)list.append(el('p','No matching runs. Add sources below and assess them to start.','empty-state'));
    data.runs.forEach(run=>{
      const button=el('button',undefined,'history-row'+(run.id===selectedId?' selected':''));
      const title=el('span');title.append(el('strong',run.label),el('small',timestamp(run.created_at)+' · '+run.id.slice(0,8)));
      const count=el('span',`${run.sources.length} sources · ${run.sources.filter(s=>s.status==='blocked').length} blocked`);
      const time=el('span',duration(run.duration_seconds));button.append(title,count,time,runState(run.status));
      button.onclick=()=>openRun(run.id).catch(error=>$('monitor-error').textContent=error.message);list.append(button);
    });
    $('runs-page').textContent=total?`${offset+1}–${Math.min(offset+25,total)} of ${total} runs`:'0 runs';
    $('runs-prev').disabled=offset===0;$('runs-next').disabled=offset+25>=total;
    $('monitor-error').textContent='';
  }catch(error){$('monitor-error').textContent='Run history unavailable: '+error.message;}
  finally{refreshing=false;}
}
$('refresh-runs').onclick=refresh;
$('run-filter').onchange=()=>{offset=0;refresh();};
let searchTimer;$('run-search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{offset=0;refresh();},250);};
$('runs-prev').onclick=()=>{offset=Math.max(0,offset-25);refresh();};$('runs-next').onclick=()=>{offset+=25;refresh();};
$('new-run').onclick=()=>{const saved=selectedRun?.files;if(saved)files=saved.map(f=>({...f}));clearWorkspace();invalidate();renderFiles();message('Ready for a new run. Select Assess readiness to save a separate run.');$('workspace').scrollIntoView({behavior:'smooth'});};
$('delete-run').onclick=async()=>{
  if(!selectedId||!confirm('Delete this saved run, including its source code, artifacts, and review history?'))return;
  try{await api('/api/runs/'+selectedId+'/delete',{});if(currentId===selectedId){clearWorkspace();invalidate();renderFiles();}selectedId=null;selectedRun=null;lastUpdated='';$('run-detail').hidden=true;offset=0;await refresh();}
  catch(error){$('monitor-error').textContent=error.message;}
};
window.monitor={startAssessment,startConversion,clearWorkspace,exportMetadata:()=>selectedRun?{id:selectedRun.id,label:selectedRun.label,status:selectedRun.status,created_at:selectedRun.created_at,updated_at:selectedRun.updated_at,sources:selectedRun.sources,events:selectedRun.events,validation_method:'Manual attestations; no Databricks execution connection.'}:null,canConvert:()=>Boolean(currentId&&selectedRun?.id===currentId&&selectedRun.status==='assessed')};
(async()=>{await refresh();let id;try{id=localStorage.getItem('crosslake.currentRun');}catch{}if(id)openRun(id).catch(error=>{pointer(null);$('monitor-error').textContent=error.message;});})();
setInterval(()=>{if(!document.hidden)refresh();},5000);
})();
