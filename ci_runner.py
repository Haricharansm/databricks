"""Run an exported Crosslake golden suite; nonzero exit means no passing suite."""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import subprocess
import sys
import xml.etree.ElementTree as ET
from suite_runner import run_suite,validate_manifest,DatabricksEndpoint,SqlServerEndpoint
from databricks_settings import DatabricksSettings,workspace_url,token_value

def load_manifest(path):
    path=Path(path).resolve();root=path.parent
    if path.stat().st_size>20*1024*1024:raise ValueError('Manifest exceeds 20 MB.')
    manifest=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(manifest,dict) or not isinstance(manifest.get('cases'),list):raise ValueError('Invalid suite manifest.')
    for case in manifest.get('cases',[]):
        if not isinstance(case,dict):raise ValueError('Invalid suite case.')
        for side in ('expected','actual'):
            ref=case.get(side+'_file')
            if ref is None:continue
            if not isinstance(ref,str) or '\\' in ref:raise ValueError('Unsafe fixture path.')
            p=PurePosixPath(ref);file=(root/ref).resolve()
            if p.is_absolute() or '..' in p.parts or not file.is_relative_to(root) or not file.is_file() or file.stat().st_size>2*1024*1024:raise ValueError('Fixture path is unsafe, missing, or exceeds 2 MB.')
            content=file.read_bytes().decode('utf-8')
            case[side]={'format':case.get(side+'_format'),'content':content}
    if not isinstance(manifest.get('candidate_artifacts',[]),list):raise ValueError('Invalid candidate artifacts.')
    for entry in manifest.get('candidate_artifacts',[]):
        if not isinstance(entry,dict) or not isinstance(entry.get('path'),str):raise ValueError('Invalid candidate artifact.')
        ref=entry.get('path','');file=(root/ref).resolve()
        if not isinstance(ref,str) or not ref.startswith('migration/') or not file.is_relative_to(root) or not file.is_file() or file.stat().st_size>16*1024*1024:raise ValueError('Candidate artifact path is missing or unsafe.')
        if hashlib.sha256(file.read_bytes()).hexdigest()!=entry.get('sha256'):raise ValueError('Candidate artifact hash changed: '+ref)
    return manifest

def credential():
    raw=os.environ.get('DATABRICKS_TOKEN')
    if raw:return token_value(raw)
    command=['databricks','auth','token','--host',workspace_url(os.environ.get('DATABRICKS_HOST',''))]
    try:
        result=subprocess.run(command,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=30,check=False)
        if result.returncode:raise ValueError('CLI token unavailable')
        return token_value(json.loads(result.stdout)['access_token'])
    except (ValueError,KeyError,OSError,subprocess.TimeoutExpired):raise RuntimeError('Configure Databricks CLI unified authentication or DATABRICKS_TOKEN. Credential command output is not exposed.') from None

def public_report(report,include_values=False):
    result=deepcopy(report)
    if include_values:return result
    for row in result.get('cases',[]):
        if isinstance(row.get('definition'),dict):row['definition']={k:v for k,v in row['definition'].items() if k not in ('source_sql','target_sql','parameters')}
        for name,comparison in list(row.get('checks',{}).items()):
            row['checks'][name]={k:comparison.get(k) for k in ('status','comparison_method','row_counts','mismatch_count')}
            row['checks'][name]['checks']=[{'name':c['name'],'passed':c['passed']} for c in comparison.get('checks',[])]
            if comparison.get('error'):row['checks'][name]['error']='Comparison input error; inspect local definitions and datasets.'
    return result

def write_reports(report,output,include_values=False):
    output=Path(output);output.mkdir(parents=True,exist_ok=True);clean=public_report(report,include_values)
    clean['ci_context']={key:os.environ.get(env) for key,env in [('repository','GITHUB_REPOSITORY'),('commit','GITHUB_SHA'),('workflow_run','GITHUB_RUN_ID')]};(output/'suite-report.json').write_text(json.dumps(clean,indent=2),encoding='utf-8')
    suite=ET.Element('testsuite',name='Crosslake golden suite');failures=0;count=0
    for row in clean.get('cases',[]):
        count+=1;case=ET.SubElement(suite,'testcase',name=row['title'],classname=row['case_id'])
        if row['status']!='passed':failures+=1;ET.SubElement(case,'failure',message=row['status']).text=json.dumps(row.get('checks',{}))
    if report['status']!='passed' and not failures:count+=1;failures+=1;ET.SubElement(ET.SubElement(suite,'testcase',name='Suite execution'),'failure',message=report.get('error',report['status']))
    suite.set('tests',str(count));suite.set('failures',str(failures));ET.ElementTree(suite).write(output/'junit.xml',encoding='utf-8',xml_declaration=True)

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('manifest');parser.add_argument('--mode',choices=['fixtures','databricks','sqlserver'],default='fixtures');parser.add_argument('--output',default='suite-results');parser.add_argument('--target-job');parser.add_argument('--source-job');parser.add_argument('--allow-compute',action='store_true');parser.add_argument('--allow-source-queries',action='store_true');parser.add_argument('--allow-target-job',action='store_true');parser.add_argument('--allow-source-job',action='store_true');parser.add_argument('--include-mismatch-values',action='store_true');parser.add_argument('--check-only',action='store_true');args=parser.parse_args(argv)
    try:
        manifest=load_manifest(args.manifest);validate_manifest(manifest)
        if args.check_only:print('Manifest, golden baselines and candidate hashes verified; no execution.');return 0
        target=None;source=None
        if args.mode!='fixtures':
            if not args.allow_compute:raise ValueError('Live mode requires --allow-compute.')
            config={'workspace_url':workspace_url(os.environ.get('DATABRICKS_HOST','')),'warehouse_id':os.environ.get('DATABRICKS_WAREHOUSE_ID',''),'catalog':os.environ.get('DATABRICKS_CATALOG',''),'schema':os.environ.get('DATABRICKS_SCHEMA','')}
            target=DatabricksEndpoint(config,credential(),lambda host,token,method,path,body=None:DatabricksSettings._http(None,host,token,method,path,body))
        if args.mode=='sqlserver':
            if not args.allow_source_queries:raise ValueError('SQL Server mode requires --allow-source-queries.')
            source=SqlServerEndpoint()
        if args.target_job and (target is None or not args.allow_target_job):raise ValueError('Target job requires live mode and --allow-target-job.')
        if args.source_job and (source is None or not args.allow_source_job):raise ValueError('Source job requires SQL Server mode and --allow-source-job.')
        report=run_suite(manifest,target,source,fixture_mode=args.mode=='fixtures',source_job=args.source_job,target_job=args.target_job)
        report['candidate_artifacts']=manifest.get('candidate_artifacts',[])
        write_reports(report,args.output,args.include_mismatch_values);print('Crosslake suite '+report['status']+'; reports: '+str(Path(args.output).resolve()));return 0 if report['status']=='passed' else 1
    except (ValueError,RuntimeError,OSError,json.JSONDecodeError) as exc:
        report={'schema_version':1,'status':'error','error':str(exc),'cases':[]};write_reports(report,args.output);print('Crosslake suite could not run. See suite-report.json.');return 2

if __name__=='__main__':sys.exit(main())
