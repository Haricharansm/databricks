"""Dependency-free local web server. Run behind authenticated hosting for team use."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
import zipfile
from core import MAX_BYTES, assess, convert, engine_status, validate_files

from monitoring import RunService, Conflict
from databricks_settings import DatabricksSettings
from deploy import DeployService

ROOT = Path(__file__).parent

class AppServer(ThreadingHTTPServer):
    def __init__(self,address,handler,run_service=None):
        super().__init__(address,handler)
        self.runs=run_service or RunService(os.environ.get("MIGRATION_DB_PATH",str(ROOT/".data"/"migration.sqlite3")))
        self.databricks=DatabricksSettings(self.runs)
        self.deploy=DeployService(self.databricks,self.runs)
    def server_close(self):
        super().server_close()
        if hasattr(self,'databricks'):
            with self.databricks.lock:self.databricks.session_token=None
        if hasattr(self,'runs'):self.runs.close()

class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        super().end_headers()

    def respond(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

    def trusted_api_host(self):
        port=self.server.server_address[1]
        allowed={f'127.0.0.1:{port}',f'localhost:{port}',f'[::1]:{port}'}
        if port==80:allowed.update(('127.0.0.1','localhost','[::1]'))
        allowed.update(value.strip().lower() for value in os.environ.get('ACCELERATOR_ALLOWED_HOSTS','').split(',') if value.strip())
        return self.headers.get('Host','').lower() in allowed

    def do_GET(self):
        path=urlsplit(self.path).path
        if path.startswith('/api/') and not self.trusted_api_host():return self.respond(403,{'error':'Untrusted API Host header.'})
        try:
            if path=='/api/databricks/settings':return self.respond(200,self.server.databricks.public())
            if path=='/api/databricks/deploy/latest':return self.respond(200,{'deployment':self.server.deploy.latest()})
            deployment=re.fullmatch(r'/api/databricks/deploy/([a-f0-9]{32})',path)
            if deployment:return self.respond(200,self.server.deploy.status(deployment.group(1)))
            if path=='/api/runs':
                q=parse_qs(urlsplit(self.path).query)
                return self.respond(200,self.server.runs.listing(limit=q.get('limit',[25])[0],offset=q.get('offset',[0])[0],status=q.get('status',[''])[0],query=q.get('q',[''])[0]))
            validation=re.fullmatch(r'/api/runs/([a-f0-9]{32})/validation(?:/(cases|attempts)/([a-f0-9]{32}))?',path)
            if validation:
                run_id,kind,item_id=validation.groups()
                if kind=='cases':return self.respond(200,self.server.runs.validation_case(run_id,item_id))
                if kind=='attempts':
                    include=parse_qs(urlsplit(self.path).query).get('inputs',['0'])[0]=='1'
                    return self.respond(200,self.server.runs.validation_attempt(run_id,item_id,include))
                return self.respond(200,self.server.runs.validation_cases(run_id))
            match=re.fullmatch(r'/api/runs/([a-f0-9]{32})',path)
            if match:
                summary=parse_qs(urlsplit(self.path).query).get('summary',['0'])[0]=='1'
                return self.respond(200,self.server.runs.get(match.group(1),not summary))
        except KeyError as exc:return self.respond(404,{'error':str(exc)})
        except ValueError as exc:return self.respond(400,{'error':str(exc)})
        if self.path == '/api/health':
            return self.respond(200, engine_status())
        if urlsplit(self.path).path not in ('/', '/index.html', '/app.js', '/monitor.js', '/validation.js', '/databricks.js', '/deploy.js', '/style.css'):
            return self.respond(404, {'error': 'Not found'})
        super().do_GET()

    def do_POST(self):
        if self.path.startswith('/api/') and not self.trusted_api_host():return self.respond(403,{'error':'Untrusted API Host header.'})
        # Fetch uses application/json and a custom header: reject cross-origin form submissions.
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json' or self.headers.get('X-Accelerator-Request') != '1':
            return self.respond(415, {'error': 'Use the application JSON API.'})
        origin = self.headers.get('Origin')
        if origin and urlsplit(origin).netloc != self.headers.get('Host'):
            return self.respond(403, {'error': 'Cross-origin requests are not allowed.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= (24 * 1024 * 1024 if self.path == '/api/export' else MAX_BYTES * 2):
                return self.respond(413, {'error': 'Request exceeds the upload limit.'})
            payload = json.loads(self.rfile.read(length))
            if self.path.startswith('/api/databricks/'):
                actions={'/api/databricks/settings':self.server.databricks.save,
                         '/api/databricks/test-workspace':self.server.databricks.test_workspace,
                         '/api/databricks/warehouses':self.server.databricks.warehouses,
                         '/api/databricks/test-warehouse':self.server.databricks.test_warehouse,
                         '/api/databricks/forget-credential':self.server.databricks.forget,
                         '/api/databricks/deploy/preview':self.server.deploy.preview,
                         '/api/databricks/deploy/start':self.server.deploy.start,
                         '/api/databricks/deploy/cancel':self.server.deploy.cancel}
                if self.path not in actions:return self.respond(404,{'error':'Not found'})
                return self.respond(200,actions[self.path](payload))
            validation=re.fullmatch(r'/api/runs/([a-f0-9]{32})/validation/cases(?:/([a-f0-9]{32})(/compare)?)?',self.path)
            if validation:
                run_id,case_id,action=validation.groups()
                if action:return self.respond(201,self.server.runs.evaluate_case(run_id,case_id,payload))
                return self.respond(200 if case_id else 201,self.server.runs.save_case(run_id,payload,case_id))
            if self.path=='/api/runs':
                return self.respond(202,self.server.runs.create(payload))
            match=re.fullmatch(r'/api/runs/([a-f0-9]{32})/(convert|review|delete)',self.path)
            if match:
                run_id,action=match.groups()
                if action=='convert':return self.respond(202,self.server.runs.start_conversion(run_id))
                if action=='review':return self.respond(200,self.server.runs.review(run_id,payload))
                return self.respond(200,self.server.runs.delete(run_id))
            if self.path == '/api/assess':
                return self.respond(200, assess(validate_files(payload)))
            if self.path == '/api/convert':
                return self.respond(200, convert(validate_files(payload)))
            if self.path == '/api/export':
                if not isinstance(payload, dict) or not isinstance(payload.get('files'), list) or len(payload['files']) > 200:
                    raise ValueError('Invalid export request.')
                buffer = BytesIO()
                with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
                    seen={'report.json'}
                    for i, item in enumerate(payload['files']):
                        if not isinstance(item, dict) or not isinstance(item.get('sql'), str) or not isinstance(item.get('name'), str):
                            raise ValueError('Invalid export file.')
                        # Flat numbered names prevent archive path traversal and collisions.
                        name = Path(item['name'].replace('\\', '/')).name
                        relative = item['name'].replace('\\', '/')
                        from pathlib import PurePosixPath
                        path = PurePosixPath(relative)
                        if path.is_absolute() or '..' in path.parts or any(p.startswith('.') for p in path.parts):
                            raise ValueError('Unsafe export path.')
                        if path.suffix.lower() not in ('.sql', '.py', '.json', '.ipynb', '.txt', '.md'):
                            raise ValueError('Unsupported export artifact type.')
                        if relative.casefold() in seen:
                            raise ValueError('Duplicate export path.')
                        seen.add(relative.casefold())
                        archive.writestr(relative, item['sql'])
                    archive.writestr('report.json', json.dumps(payload.get('report', {}), indent=2))
                body = buffer.getvalue()
                self.send_response(200); self.send_header('Content-Type', 'application/zip')
                self.send_header('Content-Disposition', 'attachment; filename="migration-bundle.zip"')
                self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
                return
            return self.respond(404, {'error': 'Not found'})
        except Conflict as exc:
            return self.respond(409, {'error': str(exc)})
        except KeyError as exc:
            return self.respond(404, {'error': str(exc)})
        except (ValueError, UnicodeError) as exc:
            return self.respond(400, {'error': str(exc)})
        except RuntimeError as exc:
            return self.respond(503, {'error': str(exc)})
        except Exception:
            return self.respond(500, {'error': 'Processing failed. Check the local server console.'})

if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--host', default='127.0.0.1'); parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    server = AppServer((args.host, args.port), partial(Handler, directory=str(ROOT / 'static')))
    print(f'SQL Migration Accelerator: http://{args.host}:{args.port}', flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
