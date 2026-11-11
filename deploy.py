"""Deploy converted SQL artifacts to a Databricks SQL warehouse.

Runs each statement of a run's converted .sql files, in order, through the
Databricks SQL Statement Execution API, using the saved warehouse and the saved
default catalog/schema. Execution stops at the first failed statement.
Notebooks, workflow templates, plans and reports are never executed.
"""
from datetime import datetime, timezone
import re
import threading
import time
import uuid

from databricks_settings import DatabricksError

NON_DEPLOYABLE_KINDS = {'coverage_report', 'migration_plan', 'workflow_template', 'notebook', 'engine_artifact'}
MAX_STATEMENTS = 2000
STATEMENT_TIMEOUT = 15 * 60  # seconds per statement before it is cancelled
POLL_SECONDS = 2
MAX_ERROR_TEXT = 4000
# END followed by one of these closes an IF/WHILE/... block, which the splitter does not count as an opener.
NON_COUNTED_END = {'IF', 'WHILE', 'LOOP', 'REPEAT', 'FOR'}
WORD = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def split_statements(text):
    """Split SQL text on top-level semicolons.

    Semicolons inside quotes, backtick identifiers, comments, $$ bodies and
    BEGIN ... END compound blocks (Databricks SQL scripting) do not split.
    Returns statements without the trailing semicolon; comment-only pieces are dropped.
    """
    statements, start, depth, i, n = [], 0, 0, 0, len(text)
    has_code = False

    def finish(end):
        nonlocal start, has_code
        piece = text[start:end].strip()
        if piece and has_code:
            statements.append(piece)
        start, has_code = end + 1, False

    while i < n:
        ch = text[i]
        two = text[i:i + 2]
        if two == '--':
            j = text.find('\n', i)
            i = n if j < 0 else j + 1
            continue
        if two == '/*':
            j = text.find('*/', i + 2)
            i = n if j < 0 else j + 2
            continue
        if two == '$$':
            j = text.find('$$', i + 2)
            i = n if j < 0 else j + 2
            has_code = True
            continue
        if ch in ("'", '"', '`'):
            j = i + 1
            while j < n:
                if text[j] == '\\' and ch != '`':
                    j += 2
                    continue
                if text[j] == ch:
                    if text[j + 1:j + 2] == ch:  # doubled quote escape
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            has_code = True
            continue
        if ch.isalpha() or ch == '_':
            m = WORD.match(text, i)
            word = m.group(0).upper()
            if word in ('BEGIN', 'CASE'):
                depth += 1
            elif word == 'END':
                nxt = WORD.match(text, _skip_space(text, m.end()))
                if not (nxt and nxt.group(0).upper() in NON_COUNTED_END):
                    depth = max(0, depth - 1)
            has_code = True
            i = m.end()
            continue
        if ch == ';' and depth == 0:
            finish(i)
        elif not ch.isspace():
            has_code = True
        i += 1
    finish(n)
    return statements


def _skip_space(text, i):
    while i < len(text) and text[i].isspace():
        i += 1
    return i


def _mask(text):
    """Blank out string, identifier and comment contents (same length) so keyword searches ignore them."""
    out, i, n = list(text), 0, len(text)
    while i < n:
        two = text[i:i + 2]
        if two == '--':
            j = text.find('\n', i); j = n if j < 0 else j
        elif two == '/*':
            j = text.find('*/', i + 2); j = n if j < 0 else j + 2
        elif text[i] in ("'", '"', '`'):
            q, j = text[i], i + 1
            while j < n:
                if text[j] == '\\' and q != '`':
                    j += 2; continue
                if text[j] == q:
                    if text[j + 1:j + 2] == q:
                        j += 2; continue
                    break
                j += 1
            j = min(n, j + 1)
        else:
            i += 1; continue
        for k in range(i, j):
            if out[k] != '\n':
                out[k] = ' '
        i = j
    return ''.join(out)


CREATE_TABLE = re.compile(r'^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMPORARY\s+|EXTERNAL\s+)?TABLE\b', re.I)
NULL_KEEP_BEFORE = {'NOT', 'DEFAULT', 'IS', 'AS', 'THEN', 'ELSE', 'WHEN', 'AND', 'OR', 'IN', 'SELECT', 'RETURN'}


def strip_null_constraints(statement):
    """Remove SQL Server style bare `NULL` column constraints from a CREATE TABLE column list.

    Databricks accepts only `NOT NULL`; columns are nullable by default. Strings, comments,
    `NOT NULL`, `DEFAULT NULL` and CHECK/CONSTRAINT clauses are left untouched.
    Returns (statement, number_of_removals).
    """
    masked = _mask(statement)
    if not CREATE_TABLE.match(masked):
        return statement, 0
    head_end = CREATE_TABLE.match(masked).end()
    open_at = masked.find('(', head_end)
    if open_at < 0 or re.search(r'\b(?:AS|SELECT)\b', masked[head_end:open_at], re.I):
        return statement, 0
    if re.search(r'\bAS\s*$', masked[:open_at], re.I):  # CREATE TABLE t AS (SELECT ...)
        return statement, 0
    depth, close_at = 0, -1
    for k in range(open_at, len(masked)):
        if masked[k] == '(':
            depth += 1
        elif masked[k] == ')':
            depth -= 1
            if depth == 0:
                close_at = k
                break
    if close_at < 0:
        return statement, 0
    # Walk column definitions separated by top-level commas inside the list.
    removals, depth, start = [], 0, open_at + 1
    for k in range(open_at + 1, close_at + 1):
        ch = masked[k]
        if ch == '(':
            depth += 1
            continue
        if ch == ')' and depth > 0:
            depth -= 1
            continue
        if depth == 0 and ch in ',)':
            segment = masked[start:k]
            head = segment.strip().split(None, 1)[0].upper() if segment.strip() else ''
            if head not in ('CONSTRAINT', 'CHECK', 'PRIMARY', 'FOREIGN', 'UNIQUE'):
                inner = 0
                for m in re.finditer(r'\(|\)|\bNULL\b', segment, re.I):
                    tok = m.group(0)
                    if tok == '(':
                        inner += 1
                    elif tok == ')':
                        inner -= 1
                    elif inner == 0:
                        before = re.findall(r'[A-Za-z_]+', segment[:m.start()])
                        if before and before[-1].upper() not in NULL_KEEP_BEFORE:
                            a = start + m.start()
                            while a > start and statement[a - 1] in ' \t':
                                a -= 1
                            removals.append((a, start + m.end()))
            start = k + 1
    for a, b in reversed(removals):
        statement = statement[:a] + statement[b:]
    return statement, len(removals)


OBJECT_HEAD = re.compile(r'\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:EXTERNAL\s+|MATERIALIZED\s+|STREAMING\s+)?'
                         r'(?:TABLE|VIEW|FUNCTION|PROCEDURE)\b', re.I)
IF_NOT_EXISTS = re.compile(r'\s*IF\s+NOT\s+EXISTS\b', re.I)
NAME_PART = re.compile(r'`(?:``|[^`])+`|[A-Za-z_][A-Za-z0-9_]*')


def target_schema(statement):
    """Schema an object is created in, as written (`dbo` or `cat`.`dbo`), or None when unqualified."""
    masked = _mask(statement)
    head = OBJECT_HEAD.match(masked)
    if not head:
        return None
    i = head.end()
    skip = IF_NOT_EXISTS.match(statement, i)
    if skip:
        i = skip.end()
    while i < len(statement) and statement[i].isspace():
        i += 1
    parts = []
    while True:
        m = NAME_PART.match(statement, i)
        if not m:
            break
        parts.append(m.group(0))
        j = m.end()
        while j < len(statement) and statement[j] in ' \t':
            j += 1
        if statement[j:j + 1] != '.':
            break
        i = j + 1
        while i < len(statement) and statement[i] in ' \t':
            i += 1
    if len(parts) < 2:
        return None
    return '.'.join(parts[:-1])


EXISTING_MODES = ('skip', 'replace', 'fail')
PLAIN_CREATE = re.compile(r'(\s*)CREATE(\s+)(?:(TEMPORARY|EXTERNAL)\s+)?(TABLE|VIEW)\b', re.I)


def handle_existing(statement, mode):
    """Rewrite a plain CREATE TABLE/VIEW so re-running a deployment does not fail.

    skip    -> CREATE TABLE IF NOT EXISTS (existing object and data are kept)
    replace -> CREATE OR REPLACE TABLE (existing table is rebuilt; its data is deleted)
    Statements that already say OR REPLACE / IF NOT EXISTS, and temporary objects, are unchanged.
    Returns (statement, note or None).
    """
    if mode not in ('skip', 'replace'):
        return statement, None
    masked = _mask(statement)
    m = PLAIN_CREATE.match(masked)
    if not m or (m.group(3) or '').upper() == 'TEMPORARY':
        return statement, None
    if IF_NOT_EXISTS.match(masked, m.end()):
        return statement, None
    kind = m.group(4).upper()
    if mode == 'skip':
        fixed = statement[:m.end()] + ' IF NOT EXISTS' + statement[m.end():]
        return fixed, f'Added IF NOT EXISTS: an existing {kind.lower()} is kept unchanged.'
    a = m.start() + len(m.group(1))
    fixed = statement[:a] + 'CREATE OR REPLACE' + statement[a + len('CREATE'):]
    note = ('Changed to CREATE OR REPLACE: an existing table is rebuilt and ALL ITS DATA IS DELETED.' if kind == 'TABLE'
            else 'Changed to CREATE OR REPLACE: an existing view is redefined.')
    return fixed, note


def deployable_files(conversion):
    files = []
    for item in (conversion or {}).get('files', []):
        if not isinstance(item, dict) or not isinstance(item.get('sql'), str):
            continue
        if item.get('kind') in NON_DEPLOYABLE_KINDS or not str(item.get('name', '')).lower().endswith('.sql'):
            continue
        files.append({'name': item['name'], 'sql': item['sql']})
    return files


class DeployService:
    def __init__(self, settings, runs, sleep=time.sleep, clock=time.monotonic):
        self.settings, self.runs = settings, runs
        self.sleep, self.clock = sleep, clock
        self.lock = threading.Lock()
        self.jobs = {}
        self.active = None

    # ---- planning -------------------------------------------------------
    def _plan(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('Invalid deploy request.')
        run_id = payload.get('run_id')
        if not isinstance(run_id, str) or not re.fullmatch(r'[a-f0-9]{32}', run_id):
            raise ValueError('Open a converted run before deploying.')
        run = self.runs.get(run_id)
        files = deployable_files(run.get('conversion'))
        if not files:
            raise ValueError('This run has no converted .sql files to deploy. Notebooks and reports are not executed.')
        selected = payload.get('files')
        if selected is not None:
            if not isinstance(selected, list) or not all(isinstance(s, str) for s in selected):
                raise ValueError('Invalid file selection.')
            wanted = set(selected)
            files = [f for f in files if f['name'] in wanted]
            if not files:
                raise ValueError('Select at least one converted .sql file.')
        plan = []
        for f in files:
            for number, statement in enumerate(split_statements(f['sql']), 1):
                fixed, count = strip_null_constraints(statement)
                note = f'Removed {count} SQL Server style NULL column constraint(s); Databricks columns are nullable by default.' if count else None
                plan.append({'file': f['name'], 'number': number, 'statement': fixed, 'note': note})
        if not plan:
            raise ValueError('The selected files contain no SQL statements.')
        mode = payload.get('existing', 'skip')
        if mode not in EXISTING_MODES:
            raise ValueError('Choose how to handle tables that already exist.')
        for p in plan:
            p['statement'], note = handle_existing(p['statement'], mode)
            if note:
                p['note'] = (p['note'] + ' ' if p['note'] else '') + note
        if payload.get('create_schemas', True):
            schemas, seen = [], set()
            for p in plan:
                schema = target_schema(p['statement'])
                if schema and schema.replace('`', '').lower() not in seen:
                    seen.add(schema.replace('`', '').lower())
                    schemas.append({'file': '(automatic)', 'number': len(schemas) + 1,
                                    'statement': f'CREATE SCHEMA IF NOT EXISTS {schema}',
                                    'note': f'Added automatically: the SQL creates objects in schema {schema}. Does nothing if it already exists.'})
            plan = schemas + plan
        if len(plan) > MAX_STATEMENTS:
            raise ValueError(f'Too many statements ({len(plan)}). Deploy at most {MAX_STATEMENTS} at a time by selecting fewer files.')
        return run_id, [f['name'] for f in files], plan

    def preview(self, payload):
        run_id, names, plan = self._plan(payload)
        public = self.settings.public()
        return {'run_id': run_id, 'files': names, 'statement_count': len(plan),
                'statements': [{'file': p['file'], 'number': p['number'], 'preview': p['statement'][:400], 'note': p['note']} for p in plan],
                'adjusted': sum(1 for p in plan if p['note']),
                'target': {'warehouse_id': public['warehouse_id'], 'catalog': public['catalog'] or '(warehouse default)',
                           'schema': public['schema'] or '(warehouse default)'},
                'message': 'Preview only. Nothing was sent to Databricks.'}

    # ---- execution ------------------------------------------------------
    def start(self, payload):
        if not isinstance(payload, dict) or payload.get('allow_execution') is not True:
            raise ValueError('Confirm that this deployment may create or change objects and use warehouse compute.')
        run_id, names, plan = self._plan(payload)
        keep_going = payload.get('on_error', 'stop') == 'continue'
        config, token, _ = self.settings._credentials(payload)
        if not config['warehouse_id']:
            raise ValueError('Select and save a SQL warehouse in Databricks settings first.')
        with self.lock:
            if self.active and self.jobs[self.active]['status'] in ('queued', 'running'):
                raise ValueError('Another deployment is still running. Wait for it or cancel it.')
            job_id = uuid.uuid4().hex
            job = {'id': job_id, 'run_id': run_id, 'status': 'queued', 'started_at': stamp(), 'finished_at': None,
                   'target': {'warehouse_id': config['warehouse_id'], 'catalog': config['catalog'], 'schema': config['schema']},
                   'files': names, 'total': len(plan), 'completed': 0, 'failed': 0, 'continue_on_error': keep_going,
                   'cancel_requested': False, 'message': 'Queued.',
                   'statements': [{'file': p['file'], 'number': p['number'], 'preview': p['statement'][:400], 'note': p['note'],
                                   'status': 'pending', 'statement_id': None, 'error': None, 'seconds': None} for p in plan]}
            self.jobs[job_id] = job
            self.active = job_id
        self._event(run_id, f'Deployment started: {len(plan)} statements to warehouse {config["warehouse_id"]}'
                            f' (catalog {config["catalog"] or "default"}, schema {config["schema"] or "default"}).')
        thread = threading.Thread(target=self._execute, args=(job_id, config, token, plan, keep_going), daemon=True, name='deploy')
        thread.start()
        return self.status(job_id)

    def status(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError('Deployment not found. Deployment progress is kept only until the server restarts.')
            return {k: (v if k != 'statements' else [dict(s) for s in v]) for k, v in job.items()}

    def latest(self):
        with self.lock:
            return self.status(self.active) if self.active else None

    def cancel(self, payload):
        job_id = payload.get('id') if isinstance(payload, dict) else None
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError('Deployment not found.')
            if job['status'] in ('queued', 'running'):
                job['cancel_requested'] = True
                job['message'] = 'Cancellation requested; stopping after cancelling the current statement.'
        return self.status(job_id)

    def _update(self, job_id, **changes):
        with self.lock:
            self.jobs[job_id].update(changes)

    def _set_statement(self, job_id, index, **changes):
        with self.lock:
            self.jobs[job_id]['statements'][index].update(changes)

    def _cancelled(self, job_id):
        with self.lock:
            return self.jobs[job_id]['cancel_requested']

    def _event(self, run_id, message):
        try:
            with self.runs.db() as db:
                self.runs.event(db, run_id, message[:1000])
        except Exception:
            pass

    def _call(self, config, token, method, path, payload=None):
        return self.settings.transport(config['workspace_url'], token, method, path, payload)

    def _execute(self, job_id, config, token, plan, keep_going=False):
        self._update(job_id, status='running', message='Running statements in order.')
        final, summary, failures = 'succeeded', None, []
        try:
            for index, item in enumerate(plan):
                if self._cancelled(job_id):
                    final, summary = 'cancelled', f'Cancelled before statement {index + 1} of {len(plan)}.'
                    break
                self._set_statement(job_id, index, status='running')
                begun = self.clock()
                state, statement_id, error = self._run_statement(job_id, config, token, item['statement'])
                self._set_statement(job_id, index, status=state, statement_id=statement_id, error=error,
                                    seconds=round(self.clock() - begun, 1))
                if state == 'succeeded':
                    with self.lock:
                        self.jobs[job_id]['completed'] += 1
                    continue
                where = f'{item["file"]} statement {item["number"]}'
                if state == 'cancelled':
                    final, summary = 'cancelled', f'Cancelled during {where}.'
                    break
                with self.lock:
                    self.jobs[job_id]['failed'] += 1
                if keep_going:
                    failures.append(where)
                    continue
                final, summary = 'failed', f'Stopped at {where}: {error or "statement failed"}'
                break
        except Exception as exc:  # unexpected failure: report it, never crash the server
            final, summary = 'failed', f'Deployment stopped: {str(exc)[:MAX_ERROR_TEXT]}'
        with self.lock:
            job = self.jobs[job_id]
            done = job['completed']
            if final == 'succeeded' and failures:
                final = 'failed'
                summary = (f'{len(failures)} statement(s) failed and the rest ran: ' + ', '.join(failures[:10])
                           + (' …' if len(failures) > 10 else '') + '. See the errors below.')
            elif final == 'succeeded':
                summary = f'All {done} statements ran successfully.'
            for s in job['statements']:
                if s['status'] == 'pending':
                    s['status'] = 'skipped'
            job.update(status=final, finished_at=stamp(), message=summary)
            run_id = job['run_id']
        self._event(run_id, f'Deployment {final}: {done} of {len(plan)} statements succeeded. {summary}')

    def _run_statement(self, job_id, config, token, statement):
        body = {'warehouse_id': config['warehouse_id'], 'statement': statement, 'wait_timeout': '30s',
                'on_wait_timeout': 'CONTINUE', 'disposition': 'INLINE', 'format': 'JSON_ARRAY', 'row_limit': 1}
        for key in ('catalog', 'schema'):
            if config[key]:
                body[key] = config[key]
        try:
            response = self._call(config, token, 'POST', '/api/2.0/sql/statements', body)
        except DatabricksError as exc:
            return 'failed', None, str(exc)
        statement_id = response.get('statement_id') if isinstance(response, dict) else None
        if not (isinstance(statement_id, str) and re.fullmatch(r'[A-Za-z0-9-]{1,64}', statement_id)):
            statement_id = None
        deadline = self.clock() + STATEMENT_TIMEOUT
        while True:
            status = response.get('status', {}) if isinstance(response, dict) else {}
            state = status.get('state') if isinstance(status, dict) else None
            if state == 'SUCCEEDED':
                return 'succeeded', statement_id, None
            if state in ('FAILED', 'CANCELED', 'CLOSED'):
                err = status.get('error') if isinstance(status, dict) else None
                text = err.get('message') if isinstance(err, dict) else None
                label = 'failed' if state == 'FAILED' else 'cancelled' if state == 'CANCELED' else 'failed'
                return label, statement_id, (str(text)[:MAX_ERROR_TEXT] if text else f'Statement ended with state {state}.')
            if state not in ('PENDING', 'RUNNING') or not statement_id:
                return 'failed', statement_id, 'Databricks returned an unexpected statement status.'
            if self._cancelled(job_id) or self.clock() >= deadline:
                timed_out = not self._cancelled(job_id)
                try:
                    self._call(config, token, 'POST', f'/api/2.0/sql/statements/{statement_id}/cancel', {})
                except DatabricksError:
                    pass
                if timed_out:
                    return 'failed', statement_id, f'Statement exceeded {STATEMENT_TIMEOUT // 60} minutes and was cancelled.'
                return 'cancelled', statement_id, 'Cancelled by user.'
            self.sleep(POLL_SECONDS)
            try:
                response = self._call(config, token, 'GET', f'/api/2.0/sql/statements/{statement_id}')
            except DatabricksError as exc:
                return 'failed', statement_id, f'Lost track of the statement: {exc} Check the warehouse query history.'
