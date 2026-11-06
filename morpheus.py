"""Minimal stdio LSP client for the official Lakebridge Morpheus transpiler.

Protocol verified against Lakebridge's LSPEngine; no workspace credentials required.
"""
import json
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time

class Morpheus:
    def __init__(self, jar, root):
        self.deadline = time.monotonic() + 180
        self.inbox = queue.Queue(maxsize=1000)
        self.next_id = 0
        self.registered = False
        self.encoding = 'utf-16'
        self.log = tempfile.TemporaryFile(mode='w+')
        self.process = subprocess.Popen([shutil.which('java'), '-Xmx768m', '-jar', str(jar)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                                        cwd=str(root))
        self.reader = threading.Thread(target=self._read, daemon=True);self.reader.start()

    def _read(self):
        try:
            while True:
                headers = {}
                while True:
                    line = self.process.stdout.readline()
                    if not line:raise EOFError('Morpheus stopped unexpectedly.')
                    if line == b'\r\n':break
                    key, value = line.decode('ascii').split(':', 1);headers[key.lower()] = value.strip()
                size = int(headers['content-length'])
                if size > 32*1024*1024:raise RuntimeError('Engine response exceeds the 32 MB limit.')
                data = self.process.stdout.read(size)
                self.inbox.put(json.loads(data),timeout=2)
        except Exception as exc:
            try:self.inbox.put(exc,timeout=1)
            except queue.Full:pass

    def send(self, method, params=None, request=False):
        data = {'jsonrpc':'2.0','method':method}
        if params is not None:data['params'] = params
        if request:
            self.next_id += 1;data['id'] = self.next_id
        self.write(data)
        return data.get('id')

    def write(self, data):
        encoded = json.dumps(data).encode('utf-8')
        self.process.stdin.write(f'Content-Length: {len(encoded)}\r\n\r\n'.encode()+encoded)
        self.process.stdin.flush()

    def receive(self):
        if time.monotonic() >= self.deadline:
            raise RuntimeError('Morpheus exceeded the 180-second time limit.')
        try:message = self.inbox.get(timeout=max(.01,self.deadline-time.monotonic()))
        except queue.Empty as exc:raise RuntimeError('Morpheus exceeded the 180-second time limit.') from exc
        if isinstance(message,Exception):raise RuntimeError(str(message))
        if 'method' in message and 'id' in message:
            method = message['method']
            if method == 'client/registerCapability':
                self.registered |= any(r['method']=='document/transpileToDatabricks' for r in message.get('params',{}).get('registrations',[]))
                result = None
            elif method == 'workspace/configuration':result = [{} for _ in message.get('params',{}).get('items',[])]
            elif method == 'workspace/applyEdit':
                result = {'applied':False,'failureReason':'This client only accepts returned document text edits.'}
            else:
                self.write({'jsonrpc':'2.0','id':message['id'],'error':{'code':-32601,'message':'Unsupported client method'}})
                return message
            self.write({'jsonrpc':'2.0','id':message['id'],'result':result})
        return message

    def request(self,method,params=None):
        request_id = self.send(method,params,True)
        while True:
            message = self.receive()
            if message.get('id') == request_id and 'method' not in message:
                if 'error' in message:raise RuntimeError('Morpheus: '+message['error'].get('message','Unknown engine error'))
                return message.get('result')

    def initialize(self,root):
        result = self.request('initialize',{
            'processId':None,'rootUri':Path(root).absolute().as_uri(),
            'capabilities':{'general':{'positionEncodings':['utf-16']},'workspace':{'applyEdit':False}},
            'initializationOptions':{'remorph':{'source-dialect':'mssql'},'options':{},'custom':{}}})
        self.encoding = result.get('capabilities',{}).get('positionEncoding','utf-16')
        if self.encoding not in ('utf-16','utf-8','utf-32'):raise RuntimeError('Unsupported position encoding from engine.')
        self.send('initialized',{})
        while not self.registered:self.receive()

    def transpile(self,path,text):
        uri = Path(path).absolute().as_uri()
        self.send('textDocument/didOpen',{'textDocument':{'uri':uri,'languageId':'sql','version':1,'text':text}})
        result = self.request('document/transpileToDatabricks',{'uri':uri,'languageId':'sql'})
        self.send('textDocument/didClose',{'textDocument':{'uri':uri}})
        edits = result.get('changes',[])
        output = apply_edits(text,edits,self.encoding)
        return output,result.get('diagnostics',[])

    def close(self):
        # Terminate the isolated process even when protocol startup or a request fails.
        if self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        for stream in (self.process.stdin,self.process.stdout):stream.close()
        self.log.close()

def apply_edits(text,edits,encoding='utf-16'):
    lines = text.splitlines(keepends=True)
    if not lines or text.endswith(('\n','\r')):lines.append('')
    def offset(position):
        line, units = position['line'],position['character']
        if line < 0 or line >= len(lines) or units < 0:raise RuntimeError('Engine returned an invalid edit position.')
        fragment = lines[line]
        if encoding == 'utf-32':
            if units > len(fragment):raise RuntimeError('Engine returned an invalid edit offset.')
            prefix = fragment[:units]
        else:
            codec = 'utf-16-le' if encoding=='utf-16' else 'utf-8'
            raw = fragment.encode(codec);size = units*(2 if encoding=='utf-16' else 1)
            if size > len(raw):raise RuntimeError('Engine returned an invalid edit offset.')
            prefix = raw[:size].decode(codec)
        return sum(len(s) for s in lines[:line])+len(prefix)
    replacements=[]
    for edit in edits:
        span = edit['range']
        if span['start'] == {'line': 0, 'character': 0} and span['end']['line'] >= len(text.split('\n')) and span['end']['character'] >= 0:
            replacements.append((0,len(text),edit['newText']))
            continue
        start=offset(edit['range']['start']);end=offset(edit['range']['end'])
        if end < start:raise RuntimeError('Engine returned an invalid edit range.')
        replacements.append((start,end,edit['newText']))
    previous=len(text)+1
    for start,end,new_text in sorted(replacements,reverse=True):
        if end>previous:raise RuntimeError('Engine returned overlapping edits.')
        text=text[:start]+new_text+text[end:];previous=start
    return text

def convert_with_morpheus(files, jar):
    with tempfile.TemporaryDirectory(prefix='morpheus-') as scratch:
        root=Path(scratch);client=Morpheus(jar,root)
        try:
            client.initialize(root)
            converted=[];all_diagnostics=[]
            for item in files:
                path=root/item['name'];path.write_text(item['sql'],encoding='utf-8')
                sql,diagnostics=client.transpile(path,item['sql'])
                converted.append({'name':item['name'],'sql':sql,'diagnostics':diagnostics})
                all_diagnostics.extend({'file':item['name'],**d} for d in diagnostics)
            return {'engine':'Lakebridge / Morpheus 0.10.0 (LSP)', 'files':converted,
                    'diagnostics':json.dumps(all_diagnostics,indent=2),
                    'conversion_errors':json.dumps([d for d in all_diagnostics if d.get('severity')==1],indent=2),
                    'status':'Review engine findings' if all_diagnostics else 'Generated — validation required',
                    'validated_on_databricks':False,
                    'warning':'Generated SQL has not been executed or validated on Databricks. Review engine findings and reconcile results before use.'}
        finally:client.close()
