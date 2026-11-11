"""Automatic repairs applied around the conversion engine.

before_conversion(): fixes SQL Server scripts that SQL Server tolerates but the
converter cannot parse.
after_conversion(): fixes converted Databricks SQL that Databricks rejects.
Every change is reported so it can be reviewed; nothing is changed silently.
"""
import re

TABLE_CONSTRAINT = re.compile(r'(?:CONSTRAINT|PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY|CHECK)\b', re.I)
CREATE_TSQL_TABLE = re.compile(r'\bCREATE\s+TABLE\b', re.I)
CREATE_DBX_TABLE = re.compile(r'\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMPORARY\s+|EXTERNAL\s+)?TABLE\b', re.I)


def _mask(text, brackets=False):
    """Blank comment and string contents (keeping length and newlines). Optionally blank [identifiers]."""
    out, i, n = list(text), 0, len(text)
    while i < n:
        two = text[i:i + 2]
        if two == '--':
            j = text.find('\n', i); j = n if j < 0 else j
        elif two == '/*':
            j = text.find('*/', i + 2); j = n if j < 0 else j + 2
        elif text[i] in ("'", '"', '`') or (brackets and text[i] == '['):
            close = ']' if text[i] == '[' else text[i]
            j = i + 1
            while j < n:
                if text[j] == close:
                    if text[j + 1:j + 2] == close:
                        j += 2; continue
                    break
                j += 1
            j = min(n, j + 1)
            # keep the delimiters so identifiers stay visible as tokens
            for k in range(i + 1, j - 1):
                if out[k] != '\n':
                    out[k] = 'x' if close == ']' else ' '
            i = j
            continue
        else:
            i += 1; continue
        for k in range(i, j):
            if out[k] != '\n':
                out[k] = ' '
        i = j
    return ''.join(out)


def _matching(masked, open_at):
    depth = 0
    for k in range(open_at, len(masked)):
        if masked[k] == '(':
            depth += 1
        elif masked[k] == ')':
            depth -= 1
            if depth == 0:
                return k
    return -1


def _column_list(masked, match):
    """Return (open, close) of the column list after CREATE TABLE <name>, or None for CTAS/unparseable."""
    open_at = masked.find('(', match.end())
    if open_at < 0 or re.search(r'\b(?:AS|SELECT)\b', masked[match.end():open_at], re.I):
        return None
    close_at = _matching(masked, open_at)
    return None if close_at < 0 else (open_at, close_at)


def _items(masked, open_at, close_at):
    """Top-level comma-separated items inside a column list as (start, end) offsets."""
    items, depth, start = [], 0, open_at + 1
    for k in range(open_at + 1, close_at + 1):
        ch = masked[k]
        if ch == '(':
            depth += 1
        elif ch == ')' and k < close_at:
            depth -= 1
        elif depth == 0 and (ch == ',' or k == close_at):
            items.append((start, k))
            start = k + 1
    return items


def _name(token):
    token = token.strip()
    if token[:1] in '["`' and len(token) > 1:
        token = token[1:-1]
    return token.lower()


def _table_name(text, masked, match, open_at):
    return text[match.end():open_at].strip() or 'table'


def _fix_missing_commas(text, notes, label):
    """Insert the comma SQL Server lets you omit before a table-level constraint."""
    masked = _mask(text, brackets=True)
    inserts = []
    for match in CREATE_TSQL_TABLE.finditer(masked):
        span = _column_list(masked, match)
        if not span:
            continue
        open_at, close_at = span
        depth = 0
        k = open_at + 1
        while k < close_at:
            ch = masked[k]
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            elif depth == 0 and (ch.isalpha()) and (k == 0 or not (masked[k - 1].isalnum() or masked[k - 1] == '_')):
                m = TABLE_CONSTRAINT.match(masked, k)
                if m:
                    p = k - 1
                    while p > open_at and masked[p].isspace():
                        p -= 1
                    # CHECK/UNIQUE may legally follow a column type as a column constraint;
                    # only treat them as table constraints when followed by "(" or after CONSTRAINT/PRIMARY/FOREIGN.
                    word = m.group(0).split()[0].upper()
                    header = _starts_table_constraint(masked, k) if word in ('CONSTRAINT', 'PRIMARY', 'FOREIGN') else 0
                    if header and masked[p] not in ',(':
                        inserts.append(p + 1)
                        notes.append(f'{label}: added the missing comma before a table constraint in CREATE TABLE {_table_name(text, masked, match, open_at)}.')
                    # skip the whole constraint header so "CONSTRAINT x PRIMARY KEY" is handled once
                    k = k + header - 1 if header else m.end()
                    continue
            k += 1
    for pos in reversed(inserts):
        text = text[:pos] + ',' + text[pos:]
    return text


def _starts_table_constraint(masked, k):
    """Length of a table-level constraint header at k (up to and including its "("), or 0."""
    m = re.match(r'(?:CONSTRAINT\s+\S+\s+)?(?:PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY|CHECK)(?:\s+(?:CLUSTERED|NONCLUSTERED))?\s*\(', masked[k:], re.I)
    return m.end() if m else 0


def _fix_key_columns(text, notes, label):
    """Repair PRIMARY KEY/UNIQUE constraints that list a column the table does not define."""
    masked = _mask(text, brackets=True)
    edits = []  # (start, end, replacement)
    for match in CREATE_TSQL_TABLE.finditer(masked):
        span = _column_list(masked, match)
        if not span:
            continue
        open_at, close_at = span
        items = _items(masked, open_at, close_at)
        columns = {}
        constraints = []
        for start, end in items:
            seg = masked[start:end]
            stripped = seg.lstrip()
            if not stripped:
                continue
            if _starts_table_constraint(masked, start + len(seg) - len(stripped)):
                constraints.append((start, end))
                continue
            lead = start + len(seg) - len(stripped)
            tok = re.match(r'\[[^\]]*\]|"[^"]*"|[A-Za-z_#@][\w#@$]*', text[lead:end])
            if tok:
                columns[_name(tok.group(0))] = tok.group(0)
        table = _table_name(text, masked, match, open_at)
        for start, end in constraints:
            seg = masked[start:end]
            m = re.search(r'(?:PRIMARY\s+KEY|UNIQUE)(?:\s+(?:CLUSTERED|NONCLUSTERED))?\s*\(', seg, re.I)
            if not m:
                continue
            list_open = start + m.end() - 1
            list_close = _matching(masked, list_open)
            if list_close < 0 or list_close > end:
                continue
            for tok in re.finditer(r'\[[^\]]*\]|"[^"]*"|[A-Za-z_#@][\w#@$]*', text[list_open + 1:list_close]):
                if tok.group(0).upper() in ('ASC', 'DESC'):
                    continue
                name = _name(tok.group(0))
                if name in columns:
                    continue
                candidates = [c for c in columns if name.endswith(c) or c.endswith(name)]
                if len(candidates) == 1:
                    a = list_open + 1 + tok.start()
                    edits.append((a, a + len(tok.group(0)), columns[candidates[0]]))
                    notes.append(f'{label}: key column {tok.group(0)} does not exist in {table}; used {columns[candidates[0]]} instead.')
                else:
                    # Drop the whole constraint item, including its leading comma.
                    a = start - 1 if text[start - 1] == ',' else start
                    edits.append((a, end, ''))
                    notes.append(f'{label}: removed a key constraint on {table} that lists unknown column {tok.group(0)}. '
                                 'Databricks primary keys are informational, so no data rule is lost.')
                    break
    for a, b, new in sorted(edits, reverse=True):
        text = text[:a] + new + text[b:]
    return text


def before_conversion(name, sql):
    """Return (fixed_sql, notes) for a SQL Server source file."""
    notes = []
    fixed = _fix_missing_commas(sql, notes, name)
    fixed = _fix_key_columns(fixed, notes, name)
    return fixed, notes


def after_conversion(name, sql):
    """Return (fixed_sql, notes) for converted Databricks SQL."""
    from deploy import strip_null_constraints
    masked = _mask(sql)
    starts = [m.start() for m in CREATE_DBX_TABLE.finditer(masked)]
    total = 0
    for i in reversed(range(len(starts))):
        a = starts[i]
        b = starts[i + 1] if i + 1 < len(starts) else len(sql)
        piece, count = strip_null_constraints(sql[a:b])
        if count:
            sql = sql[:a] + piece + sql[b:]
            total += count
    notes = [f'{name}: removed {total} SQL Server style NULL column constraint(s); Databricks columns are nullable by default.'] if total else []
    return sql, notes
