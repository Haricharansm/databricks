"""Shared standard-library audit notebook; no SQL, parameter values or credentials logged."""
RUNTIME='''# Databricks notebook source
import datetime as _audit_dt
import json as _audit_json
import logging as _audit_logging
import time as _audit_time
import uuid as _audit_uuid

def crosslake_start(application_id, package, task, run_id='', sink='stdout'):
    context={'application_id':application_id,'package':package,'task':task,'run_id':run_id,'correlation_id':_audit_uuid.uuid4().hex,'_start':_audit_time.monotonic(),'_sink':sink}
    crosslake_event(context,'started')
    return context

def crosslake_event(context,event,error_class=None):
    payload={k:context[k] for k in ('application_id','package','task','run_id','correlation_id')}
    payload.update(schema_version=1,event=event,timestamp=_audit_dt.datetime.now(_audit_dt.timezone.utc).isoformat(),duration_ms=round((_audit_time.monotonic()-context['_start'])*1000,3))
    if error_class is not None:payload['error_class']=error_class
    message=_audit_json.dumps(payload,sort_keys=True)
    if context['_sink']=='python_logging':_audit_logging.getLogger('crosslake.audit').warning(message)
    else:print(message)
'''


def instrument(namespace,body,package,task,application_id,sink='stdout'):
    # A result notebook's exit must happen after the completed audit event.
    exit_lines=[line for line in body.splitlines() if line.startswith('dbutils.notebook.exit(')]
    body='\n'.join(line for line in body.splitlines() if not line.startswith('dbutils.notebook.exit('))
    namespace='\n'.join(line for line in namespace.splitlines() if not line.strip().startswith('# COMMAND ----------'))+'\n'
    code='# MAGIC %run ./_crosslake_runtime\n\n# COMMAND ----------\n'
    code+='dbutils.widgets.text("crosslake_application_id", '+repr(application_id)+')\ndbutils.widgets.text("crosslake_job_run_id", "")\n'
    code+='_audit_context = crosslake_start(dbutils.widgets.get("crosslake_application_id"), '+repr(package)+', '+repr(task)+', dbutils.widgets.get("crosslake_job_run_id"), '+repr(sink)+')\ntry:\n'
    code+='\n'.join('    '+line for line in (namespace+body).splitlines())+'\nexcept Exception as _audit_error:\n    crosslake_event(_audit_context,"failed",type(_audit_error).__name__)\n    raise\nelse:\n    crosslake_event(_audit_context,"completed")\n'
    return code+'\n'.join(exit_lines)+'\n'
