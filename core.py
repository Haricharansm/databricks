"""SQL Server assessment and a fail-closed Lakebridge CLI adapter."""
from __future__ import annotations
import os
from bisect import bisect_right
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading

MAX_FILES = 50
MAX_BYTES = 4 * 1024 * 1024
ENGINE_LOCK = threading.BoundedSemaphore(1)

# A lexer masks comments and literals so text inside them is not assessed as SQL.
BLOCK_MARKER = re.compile(r'/\*|\*/')
TOKEN = re.compile(r"--[^\n]*|/\*|N?'(?:''|[^'])*'|\[(?:\]\]|[^\]])*\]|\"(?:\"\"|[^\"])*\"", re.I)

def mask_sql(sql):
    parts, pos = [], 0
    while pos < len(sql):
        match = TOKEN.search(sql, pos)
        if not match:
            parts.append(sql[pos:]); break
        parts.append(sql[pos:match.start()])
        end = match.end()
        if match.group() == '/*':
            depth = 1
            while depth and end < len(sql):
                marker = BLOCK_MARKER.search(sql, end)
                if not marker:
                    end = len(sql); break
                end = marker.end()
                depth += 1 if marker.group() == '/*' else -1
        chunk = sql[match.start():end]
        parts.append(''.join('\n' if c == '\n' else ' ' for c in chunk))
        pos = end
    return ''.join(parts)

RULES = [
    ('dynamic-sql', r'\b(?:EXEC(?:UTE)?\s*\(|SP_EXECUTESQL)\b|\bEXEC(?:UTE)?\s*\(', 'high', 'Dynamic SQL', 'Extract generated SQL and review runtime dependencies; static analysis cannot resolve it.'),
    ('procedure', r'\b(?:CREATE|ALTER)\s+(?:OR\s+ALTER\s+)?PROC(?:EDURE)?\b', 'high', 'Stored procedure', 'Review control flow, result sets, parameter modes, and Databricks SQL scripting support.'),
    ('cursor', r'\b(?:CURSOR|FETCH\s+NEXT)\b', 'high', 'Cursor processing', 'Consider set-based transformations and verify stateful behavior.'),
    ('transaction', r'\b(?:BEGIN\s+TRAN(?:SACTION)?|COMMIT|ROLLBACK|SAVE\s+TRAN(?:SACTION)?)\b', 'high', 'Transaction semantics', 'Review atomicity and multi-statement transaction requirements for the target runtime.'),
    ('temp-table', r'(?<![\w])##?[A-Za-z_]\w*', 'medium', 'Temporary table', 'Review table scope, lifecycle, and supported target temporary objects.'),
    ('table-variable', r'\bDECLARE\s+@\w+\s+TABLE\b', 'high', 'Table variable', 'Rewrite or map table variables and verify their lifetime.'),
    ('hint', r'\bWITH\s*\(\s*(?:NOLOCK|INDEX|FORCESEEK|ROWLOCK|TABLOCK)|\bOPTION\s*\(', 'medium', 'SQL Server query hint', 'Review isolation and performance intent; hints may require a different target design.'),
    ('linked-server', r'\b(?:OPENQUERY|OPENROWSET|OPENDATASOURCE)\b|\b\w+\.\w+\.\w+\.\w+\b', 'high', 'External dependency', 'Replace linked-server access with an approved Databricks connection or ingestion path.'),
    ('identity', r'\bIDENTITY\s*\(', 'medium', 'Identity column', 'Check generated-key behavior and downstream key dependencies.'),
    ('merge', r'\bMERGE\b', 'medium', 'MERGE statement', 'Check duplicate-match handling, predicates, and target Delta table requirements.'),
    ('output', r'\bOUTPUT\b', 'high', 'OUTPUT clause', 'Review returned DML rows and downstream use.'),
    ('error-handling', r'\b(?:TRY|CATCH|RAISERROR|THROW)\b', 'medium', 'Error handling', 'Map exception behavior and retry semantics to the Databricks runtime.'),
    ('function', r'\b(?:GETDATE|GETUTCDATE|ISNULL|CONVERT|TRY_CONVERT|DATEADD|DATEDIFF|CHARINDEX|LEN|IIF)\s*\(', 'low', 'Dialect-specific function', 'Review argument order, null handling, types, and time-zone semantics after conversion.'),
    ('top', r'\bTOP\s*(?:\(|\d)', 'low', 'TOP row limit', 'Verify ordering, ties, and percentage behavior in converted SQL.'),
    ('collation', r'\bCOLLATE\b', 'medium', 'Explicit collation', 'Verify case sensitivity, comparison, and sort behavior.'),
    ('sqlserver-setting', r'\b(?:USE\s+|SET\s+(?:ANSI_NULLS|QUOTED_IDENTIFIER|NOCOUNT|XACT_ABORT))', 'medium', 'SQL Server session setting', 'Review catalog/schema mappings and session behavior.'),
]

def validate_files(payload):
    from sources import normalize_files
    return normalize_files(payload)


def assess_file(item):
    if item['name'].lower().endswith('.dtsx'):
        return assess_ssis(item)
    sql = item['sql']; masked = mask_sql(sql)
    findings = []
    newlines = [m.start() for m in re.finditer('\n', masked)]
    for code, pattern, severity, title, action in RULES:
        count = 0; line_numbers = set()
        for match in re.finditer(pattern, masked, re.I):
            count += 1
            if len(line_numbers) < 30:
                line_numbers.add(bisect_right(newlines, match.start()) + 1)
        if count:
            findings.append({'code': code, 'severity': severity, 'title': title, 'action': action,
                             'count': count, 'lines': sorted(line_numbers)})
    high = sum(f['severity'] == 'high' for f in findings)
    medium = sum(f['severity'] == 'medium' for f in findings)
    from legacy import sql_inventory
    metadata = sql_inventory(sql)
    outputs=sum(p['output'] for proc in metadata['procedures'] for p in proc['parameters'])
    if outputs:
        findings.append({'code':'procedure-output-parameters','severity':'high','title':'Procedure output parameters',
            'action':'Verify IN/OUT parameter modes, caller bindings, data types, and target-runtime support.',
            'count':outputs,'lines':[p['line'] for p in metadata['procedures'] if any(v['output'] for v in p['parameters'])]})
        high+=1
    complexity = 'High' if high else 'Medium' if medium else 'Low'
    return {**metadata, 'name': item['name'], 'lines': len(sql.splitlines()),
            'statement_estimate': len([s for s in re.split(r';|(?im:^\s*GO\s*$)', masked) if s.strip()]),
            'complexity': complexity, 'review_required': bool(findings), 'findings': findings,
            'status': 'Manual review' if high else 'Review findings' if findings else 'No rules matched'}

def assess_ssis(item):
    from ssis import inventory
    info=inventory(item);findings=list(info['findings'])
    for i,statement in enumerate(info['embedded_sql']):
        if statement['source_type'] not in ('','0'):continue
        scan=assess_file({'name':'embedded.sql','sql':statement['sql']})
        for finding in scan['findings']:
            findings.append({**finding,'code':f"embedded-{i}-"+finding['code'],
                'title':f"Embedded SQL {i+1}: "+finding['title'],
                'location':'Embedded SQL line numbers; not XML line numbers'})
    high=any(f['severity']=='high' for f in findings)
    return {'name':item['name'],'object_type':'ssis_package','lines':len(item['sql'].splitlines()),
            'complexity':'High' if high else 'Medium','review_required':True,
            'status':'Experimental conversion — manual review','findings':findings,
            'package':{k:v for k,v in info.items() if k not in ('findings','embedded_sql')},
            'embedded_sql_count':len(info['embedded_sql'])}


def assess(files):
    results = [assess_file(f) for f in files]
    return {'assessment_method': 'Heuristic SQL/procedure scan and SSIS XML inventory; not Lakebridge Analyzer or a live database assessment.',
            'limitations': 'No database inventory, data volumes, query history, resolved dependency graph, or execution validation. No matching rules does not establish migration readiness.',
            'summary': {'files': len(results), 'ssis_packages':sum(r.get('object_type')=='ssis_package' for r in results), 'stored_procedures':sum(len(r.get('procedures',[])) for r in results), 'lines': sum(r['lines'] for r in results),
                        'high_complexity': sum(r['complexity'] == 'High' for r in results),
                        'findings': sum(len(r['findings']) for r in results)}, 'files': results}

def sql_engine_status():
    cli = shutil.which('databricks')
    config = os.environ.get('LAKEBRIDGE_TRANSPILER_CONFIG', '')
    configured = bool(config and Path(config).is_file())
    jar = Path(os.environ.get('MORPHEUS_JAR', str(Path(__file__).parent / 'engine' / 'morpheus.jar')))
    direct = bool(shutil.which('java') and jar.is_file())
    if direct and not (cli and configured):
        return {'engine': 'Lakebridge / Morpheus 0.10.0', 'available': True, 'mode': 'lsp',
                'jar': str(jar.resolve()), 'cli_installed': bool(cli), 'config_present': configured,
                'message': 'Morpheus is installed. Conversion runs locally without workspace credentials.', 'target_validation': False}
    return {'engine': 'Lakebridge / Morpheus', 'available': bool(cli and configured),
            'cli_installed': bool(cli), 'config_present': configured,
            'message': 'Configured; conversion will verify the engine at runtime.' if cli and configured else
                       'Conversion needs the Databricks CLI, Lakebridge with Morpheus installed, and LAKEBRIDGE_TRANSPILER_CONFIG set to its config file.',
            'target_validation': False}

def convert_sql(files):
    """Repair known SQL Server quirks, convert, then repair known Databricks incompatibilities.

    Uploaded sources are not modified; every automatic change is listed in the diagnostics.
    """
    from sqlfix import before_conversion, after_conversion
    notes, prepared = [], []
    for item in files:
        fixed, changes = before_conversion(item['name'], item['sql'])
        notes.extend('Before conversion: ' + c for c in changes)
        prepared.append({**item, 'sql': fixed})
    result = _convert_sql_engine(prepared)
    for out in result.get('files', []):
        if isinstance(out.get('sql'), str) and out['name'].lower().endswith('.sql') and not out.get('kind'):
            out['sql'], changes = after_conversion(out['name'], out['sql'])
            notes.extend('After conversion: ' + c for c in changes)
    result['auto_fixes'] = notes
    if notes:
        result['diagnostics'] = (result.get('diagnostics') or '') + '\n\nAutomatic fixes applied (review these):\n' + '\n'.join('- ' + n for n in notes)
    return result


def _convert_sql_engine(files):
    state = engine_status()
    if not state['available']:
        raise RuntimeError(state['message'])
    if not ENGINE_LOCK.acquire(blocking=False):
        raise RuntimeError('Another conversion is running. Try again after it finishes.')
    try:
        if state.get('mode') == 'lsp':
            from morpheus import convert_with_morpheus
            return convert_with_morpheus(files, state['jar'])
        with tempfile.TemporaryDirectory(prefix='sql-accelerator-') as scratch:
            root = Path(scratch); source = root / 'input'; output = root / 'output'
            source.mkdir(); output.mkdir()
            for item in files:
                (source / item['name']).write_text(item['sql'], encoding='utf-8')
            errors = root / 'conversion-errors.log'
            command = [shutil.which('databricks'), 'labs', 'lakebridge', 'transpile',
                       '--transpiler-config-path', str(Path(os.environ['LAKEBRIDGE_TRANSPILER_CONFIG']).resolve()),
                       '--input-source', str(source), '--source-dialect', 'mssql',
                       '--output-folder', str(output), '--skip-validation', 'true',
                       '--error-file-path', str(errors)]
            try:
                # Spool engine logs to disk rather than buffering unlimited subprocess output in RAM.
                with (root / 'engine.log').open('w+') as log:
                    result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                            timeout=180, stdin=subprocess.DEVNULL, check=False)
                    log.seek(0); diagnostics = log.read(16000)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError('Lakebridge exceeded the 180-second limit. Try fewer files.') from exc
            if result.returncode:
                raise RuntimeError('Lakebridge conversion failed. Verify the CLI and Morpheus installation, then review these diagnostics: ' + diagnostics[-8000:])
            converted = []
            total_bytes = 0
            for path in sorted(output.rglob('*.sql')):
                if path.is_symlink():
                    continue
                total_bytes += path.stat().st_size
                if total_bytes > 16 * 1024 * 1024 or len(converted) >= 200:
                    raise RuntimeError('Converted output exceeds the download limit. Try fewer files.')
                converted.append({'name': str(path.relative_to(output)), 'sql': path.read_text(encoding='utf-8')})
            if not converted:
                raise RuntimeError('Lakebridge returned no SQL files. Review the transpiler configuration and source syntax. ' + diagnostics[-4000:])
            engine_errors = errors.read_text(encoding='utf-8', errors='replace')[:16000] if errors.exists() else ''
            return {'engine': 'Lakebridge / Morpheus', 'files': converted, 'diagnostics': diagnostics,
                    'conversion_errors': engine_errors,
                    'status': 'Review engine errors' if engine_errors.strip() else 'Generated — validation required',
                    'validated_on_databricks': False,
                    'warning': 'Generated SQL has not been executed or validated on Databricks. Review all engine diagnostics and reconcile results before use. Output file count can differ from input; inspect coverage.'}
    finally:
        ENGINE_LOCK.release()


def engine_status():
    from bladebridge import status
    return {**sql_engine_status(),'ssis':status()}

def convert(files):
    # Retain direct SQL API behavior; mixed uploads add explicit coverage and SSIS artifacts.
    if not any(f['name'].lower().endswith('.dtsx') for f in files):
        return convert_sql(files)
    from legacy import convert_mixed
    return convert_mixed(files)
