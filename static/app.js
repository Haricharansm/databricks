'use strict';
const $ = id => document.getElementById(id);
let files = [], report = null, conversion = null, engine = null, busy = false, revision = 0;
const samples = [
  {name:'sales_summary.sql',sql:`-- Monthly sales summary\nSELECT TOP (25)\n    [CustomerID],\n    ISNULL([Region], 'Unknown') AS [Region],\n    SUM([Amount]) AS [TotalSales],\n    GETDATE() AS [ExtractedAt]\nFROM [dbo].[Sales]\nGROUP BY [CustomerID], [Region]\nORDER BY [TotalSales] DESC;`},
  {name:'refresh_customers.sql',sql:`CREATE PROCEDURE dbo.RefreshCustomers\nAS\nBEGIN\n    SET NOCOUNT ON;\n    BEGIN TRANSACTION;\n    SELECT CustomerID, UpdatedAt\n    INTO #RecentCustomers\n    FROM dbo.Customers WITH (NOLOCK)\n    WHERE UpdatedAt >= DATEADD(day, -7, GETDATE());\n    SELECT * FROM #RecentCustomers;\n    COMMIT TRANSACTION;\nEND;\nGO`}
];
const ssisSample = {"name": "LegacyLoad.dtsx", "sql": "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<DTS:Executable xmlns:DTS=\"www.microsoft.com/SqlServer/Dts\" DTS:refId=\"Package\" DTS:DTSID=\"{11111111-1111-1111-1111-111111111111}\" DTS:ExecutableType=\"Microsoft.Package\" DTS:CreationName=\"Microsoft.Package\" DTS:ObjectName=\"LegacyLoad\" DTS:PackageType=\"5\" DTS:LocaleID=\"1033\">\n  <DTS:Property DTS:Name=\"PackageFormatVersion\">8</DTS:Property>\n  <DTS:Variables />\n  <DTS:Executables>\n    <DTS:Executable DTS:refId=\"Package\\Prepare\" DTS:DTSID=\"{22222222-2222-2222-2222-222222222222}\" DTS:ObjectName=\"Prepare\" DTS:ExecutableType=\"Microsoft.ExecuteSQLTask\" DTS:CreationName=\"Microsoft.ExecuteSQLTask\" DTS:LocaleID=\"-1\">\n      <DTS:Variables />\n      <DTS:ObjectData><SQLTask:SqlTaskData xmlns:SQLTask=\"www.microsoft.com/sqlserver/dts/tasks/sqltask\" SQLTask:SqlStatementSource=\"SELECT TOP (10) CustomerID FROM dbo.Customers;\" /></DTS:ObjectData>\n    </DTS:Executable>\n    <DTS:Executable DTS:refId=\"Package\\Summarize\" DTS:DTSID=\"{33333333-3333-3333-3333-333333333333}\" DTS:ObjectName=\"Summarize\" DTS:ExecutableType=\"Microsoft.ExecuteSQLTask\" DTS:CreationName=\"Microsoft.ExecuteSQLTask\" DTS:LocaleID=\"-1\">\n      <DTS:Variables />\n      <DTS:ObjectData><SQLTask:SqlTaskData xmlns:SQLTask=\"www.microsoft.com/sqlserver/dts/tasks/sqltask\" SQLTask:SqlStatementSource=\"SELECT COUNT(*) AS CustomerCount FROM dbo.Customers;\" /></DTS:ObjectData>\n    </DTS:Executable>\n  </DTS:Executables>\n  <DTS:PrecedenceConstraints>\n    <DTS:PrecedenceConstraint DTS:From=\"Package\\Prepare\" DTS:To=\"Package\\Summarize\" DTS:Value=\"0\" DTS:EvalOp=\"0\" DTS:LogicalAnd=\"True\" />\n  </DTS:PrecedenceConstraints>\n</DTS:Executable>\n"};
function sourceText(f) {return f?.sql || (f?.base64 ? '-- ISPAC project archive. Assessment expands DTSX packages on the server.' : '');}
function message(text, error=false) { const el=$('message'); el.textContent=text; el.className=error?'notice error':'notice'; el.hidden=!text; }
function el(tag, text, className) { const node=document.createElement(tag); if(text!==undefined)node.textContent=text; if(className)node.className=className; return node; }
function download(name, content, type) {const blob=content instanceof Blob?content:new Blob([content],{type});const url=URL.createObjectURL(blob);const a=el('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function sync() {
  $('assess').disabled=busy||!files.length;
  const needsSQL=files.some(f=>/\.sql$/i.test(f.name));
  const needsSSIS=files.some(f=>/\.(dtsx|ispac)$/i.test(f.name));
  $('convert').disabled=busy||!files.length||!report||!((needsSQL&&engine?.available)||(needsSSIS&&engine?.ssis?.available));
  $('download-report').disabled=busy||!report;
  $('download-bundle').disabled=busy||!conversion;
  ['sample','ssis-sample','browse','add-paste','paste-toggle','file-input'].forEach(id=>$(id).disabled=busy);
  document.querySelectorAll('.remove').forEach(b=>b.disabled=busy);
}
function invalidate() {
  revision++; report=null; conversion=null;
  ['files','lines','high','findings'].forEach(k=>$('metric-'+k).textContent='—');
  $('type-summary').textContent='';$('assessment-results').replaceChildren();$('assessment-empty').hidden=false;
  $('output-select').replaceChildren();$('output-code').textContent='-- Converted SQL will appear here when Lakebridge is configured.';
  $('conversion-details').hidden=true;
}
function renderFiles() {
  const list=$('file-list');list.replaceChildren();
  files.forEach((f,i)=>{
    const row=el('div',undefined,'file-row');const name=el('span',f.name);name.append(el('small',`${sourceText(f).split('\n').length} lines · ${((f.base64 ? Math.floor(f.base64.length*3/4) : new TextEncoder().encode(sourceText(f)).length)/1024).toFixed(1)} KB`));
    const button=el('button','Remove','remove');button.setAttribute('aria-label','Remove '+f.name);button.onclick=()=>{if(busy)return;files.splice(i,1);invalidate();renderFiles();};row.append(name,button);list.append(row);
  });
  $('file-count').textContent=files.length?`${files.length} source${files.length===1?'':'s'} ready for assessment`:'No scripts added yet';
  const selected=$('source-select').value; $('source-select').replaceChildren();
  files.forEach((f,i)=>{const option=el('option',f.name);option.value=String(i);$('source-select').append(option);});
  if(selected!==''&&files[Number(selected)])$('source-select').value=selected;
  else if(files.length)$('source-select').value='0';
  renderSource();sync();
}
function renderSource() {$('source-code').textContent=sourceText(files[Number($('source-select').value)])||'-- Your source script will appear here.';}
function addFiles(incoming) {
  if(busy)return;
  if(files.length+incoming.length>50)return message('Upload up to 50 files.',true);
  const names=new Set(files.map(f=>f.name.toLowerCase()));
  for(const f of incoming) {
    if(!/^[\w .()-]{1,120}\.(sql|dtsx|ispac)$/i.test(f.name)||f.name.startsWith('.'))return message('Use plain .sql, .dtsx, or .ispac file names without folders or special symbols.',true);
    if(names.has(f.name.toLowerCase()))return message('Duplicate file name: '+f.name,true);
    if(!f.base64&&(!f.sql?.trim()||f.sql.includes('\0')))return message(f.name+': provide nonempty UTF-8 SQL text.',true);
    names.add(f.name.toLowerCase());
  }
  const total=[...files,...incoming].reduce((n,f)=>n+(f.base64 ? Math.floor(f.base64.length*3/4) : new TextEncoder().encode(sourceText(f)).length),0);
  if(total>4*1024*1024)return message('The total upload limit is 4 MB.',true);
  files.push(...incoming);invalidate();renderFiles();message('');
}
async function readFiles(list) {
  try {
    if(busy)return;
    const values=Array.from(list);
    if(values.length+files.length>50||values.reduce((n,f)=>n+f.size,0)>4*1024*1024)throw new Error('Upload up to 50 files and 4 MB total.');
    const decoded=await Promise.all(values.map(async f=>{
      const bytes=new Uint8Array(await f.arrayBuffer());
      if(/\.ispac$/i.test(f.name)){
        let binary='';for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));
        return {name:f.name,base64:btoa(binary)};
      }
      const encoding=bytes[0]===255&&bytes[1]===254?'utf-16le':bytes[0]===254&&bytes[1]===255?'utf-16be':'utf-8';
      return {name:f.name,sql:new TextDecoder(encoding,{fatal:true}).decode(bytes)};
    }));
    addFiles(decoded);
  } catch(e){message('Could not read files: '+e.message,true);}
  finally{$('file-input').value='';}
}
async function api(path,payload,blob=false) {
  const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Accelerator-Request':'1'},body:JSON.stringify(payload)});
  if(!response.ok){let data;try{data=await response.json();}catch{throw new Error('The server returned an unexpected response.');}throw new Error(data.error||'Request failed.');}
  return blob?response.blob():response.json();
}
function renderReport() {
  $('assessment-empty').hidden=true;
  const map={files:'files',lines:'lines',high:'high_complexity',findings:'findings'};
  Object.entries(map).forEach(([id,key])=>$('metric-'+id).textContent=report.summary[key].toLocaleString());
  $('type-summary').textContent=`${report.summary.stored_procedures||0} stored procedures · ${report.summary.ssis_packages||0} SSIS packages · ${report.summary.files} expanded sources`;
  const target=$('assessment-results');target.replaceChildren();
  report.files.forEach(f=>{
    const section=el('div',undefined,'assessment-file'), heading=el('div',undefined,'file-heading');
    heading.append(el('strong',f.name),el('span',f.complexity+' complexity','badge '+f.complexity));section.append(heading);
    const detail=el('details'),summary=el('summary',`${f.lines} lines · ${f.findings.length} finding categories · ${f.status}`);detail.append(summary);
    if(!f.findings.length)detail.append(el('p','No configured rules matched. Review the full source and test converted behavior.'));
    if(f.procedures?.length)f.procedures.forEach(p=>detail.append(el('p',`Procedure: ${p.name} · parameters: ${p.parameters.map(v=>v.name+' '+v.type+(v.output?' OUTPUT':'')).join(', ')||'none detected'}`)));
    if(f.dependencies?.length)detail.append(el('p','Textual references (unresolved): '+f.dependencies.map(d=>d.name).join(', ')));
    if(f.package){
      detail.append(el('p',`${f.package.tasks.length} tasks · ${f.package.components.length} data-flow components · ${f.package.connections.length} connections · ${f.package.constraints.length} precedence constraints`));
      f.package.tasks.forEach(t=>detail.append(el('p',t.name+' — '+t.type)));
      f.package.components.forEach(t=>detail.append(el('p','Component: '+t.name+' — '+t.type)));
      if(f.package.project_metadata?.length)detail.append(el('p','Project settings require mapping: '+f.package.project_metadata.join(', ')));
    }
    f.findings.forEach(finding=>{const row=el('div',undefined,'finding');row.append(el('strong',finding.title+' · '+finding.severity),el('p',finding.action),el('small',`${finding.location||'Source'} ${finding.lines.length?'lines '+finding.lines.join(', '):'package inventory'} · ${finding.count} occurrence(s)`));detail.append(row);});
    section.append(detail);target.append(section);
  });sync();
}
$('browse').onclick=()=>$('file-input').click();
$('file-input').onchange=e=>readFiles(e.target.files);
$('source-select').onchange=renderSource;
$('output-select').onchange=()=>$('output-code').textContent=conversion?.files[Number($('output-select').value)]?.sql||'';
$('ssis-sample').onclick=()=>addFiles([{...ssisSample}]);
$('sample').onclick=()=>addFiles(samples.map(f=>({...f})));
$('paste-toggle').onclick=()=>{$('paste-area').hidden=!$('paste-area').hidden;if(!$('paste-area').hidden)$('paste-sql').focus();};
$('add-paste').onclick=()=>{let n=1;while(files.some(f=>f.name===`pasted-${n}.sql`))n++;const before=files.length;addFiles([{name:`pasted-${n}.sql`,sql:$('paste-sql').value}]);if(files.length>before){$('paste-sql').value='';$('paste-area').hidden=true;}};
const zone=$('dropzone');
['dragenter','dragover'].forEach(type=>zone.addEventListener(type,e=>{e.preventDefault();if(!busy)zone.classList.add('dragging');}));
['dragleave','drop'].forEach(type=>zone.addEventListener(type,e=>{e.preventDefault();zone.classList.remove('dragging');}));
zone.addEventListener('drop',e=>readFiles(e.dataTransfer.files));
$('assess').onclick=async()=>{
  busy=true;sync();message('Assessing SQL source…');const started=revision;
  try {const result=await api('/api/assess',{files});if(started!==revision)return;report=result;renderReport();message('Assessment complete. Review the findings before conversion.');$('readiness').scrollIntoView({behavior:'smooth'});}
  catch(e){message(e.message,true);}finally{busy=false;sync();}
};
$('convert').onclick=async()=>{
  busy=true;sync();message('Lakebridge is converting your sources. Each engine batch can take up to 3 minutes; keep this page open.');const started=revision;
  try {
    const result=await api('/api/convert',{files});if(started!==revision)return;conversion=result;
    $('output-select').replaceChildren();conversion.files.forEach((f,i)=>{const option=el('option',f.name);option.value=String(i);$('output-select').append(option);});
    $('output-code').textContent=conversion.files[0]?.sql||'';
    $('conversion-warning').textContent=conversion.warning;
    $('diagnostics').textContent=[conversion.coverage?JSON.stringify(conversion.coverage,null,2):'',conversion.diagnostics,conversion.conversion_errors].filter(Boolean).join('\n\n')||'No diagnostics returned.';
    $('conversion-details').hidden=false;message(conversion.status);$('conversion').scrollIntoView({behavior:'smooth'});
  }catch(e){conversion=null;message(e.message,true);}finally{busy=false;sync();}
};
$('download-report').onclick=()=>download('migration-readiness.json',JSON.stringify(report,null,2),'application/json');
$('download-bundle').onclick=async()=>{
  busy=true;sync();try{const blob=await api('/api/export',{files:conversion.files,report:{assessment:report,conversion:{...conversion,files:conversion.files.map(f=>({name:f.name}))}}},true);download('migration-bundle.zip',blob);}
  catch(e){message(e.message,true);}finally{busy=false;sync();}
};
async function health(){
  try{const response=await fetch('/api/health');if(!response.ok)throw new Error('Health check failed');engine=await response.json();$('engine-badge').textContent=`SQL ${engine.available?'ready':'setup needed'} · SSIS ${engine.ssis?.available?'ready':'setup needed'}`;$('engine-message').textContent=engine.message+' '+(engine.ssis?.message||'');$('engine-message').classList.toggle('ready',engine.available);}
  catch(e){$('engine-badge').textContent='Server unavailable';$('engine-message').textContent='Run python server.py and open the local application URL to enable assessment and conversion.';}
  sync();
}
health();renderFiles();
