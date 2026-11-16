"""Dependency-free single read-query guard shared by standalone CI bundles."""
import re
TOKEN=re.compile(r"--[^\n]*|/\*|N?'(?:''|[^'])*'|\[(?:\]\]|[^\]])*\]|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`",re.I)
def mask_sql(sql):
    parts=[];pos=0
    while pos<len(sql):
        match=TOKEN.search(sql,pos)
        if not match:parts.append(sql[pos:]);break
        parts.append(sql[pos:match.start()]);end=match.end()
        if match.group()=='/*':
            depth=1
            while depth and end<len(sql):
                marker=re.search(r'/\*|\*/',sql[end:])
                if not marker:end=len(sql);break
                start=end+marker.start();end=end+marker.end();depth+=1 if sql[start:end]=='/*' else -1
        parts.append(''.join('\n' if c=='\n' else ' ' for c in sql[match.start():end]));pos=end
    return ''.join(parts)
def read_query(sql):
    if not isinstance(sql,str) or not sql.strip() or len(sql)>20000:raise ValueError('Supply a SELECT query up to 20,000 characters.')
    masked=mask_sql(sql).strip().rstrip(';').strip()
    if ';' in masked or not re.match(r'^(SELECT|WITH)\b',masked,re.I):raise ValueError('Use one SELECT/WITH read query; multiple statements are not supported.')
    if re.search(r'\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|COPY|CALL|EXEC|GRANT|REVOKE|INTO|PUT|REMOVE)\b',masked,re.I):raise ValueError('Validation accepts read queries only.')
    return sql
