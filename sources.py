"""Bounded SQL, DTSX, and ISPAC upload normalization; archives never extract to disk."""
import base64
import binascii
from io import BytesIO
from pathlib import PurePosixPath
import re
import zipfile

MAX_FILES=50
MAX_BYTES=4*1024*1024
NAME=re.compile(r'[\w .()-]{1,120}\.(?:sql|dtsx|ispac)',re.I)

def valid_name(name):
    return isinstance(name,str) and not name.startswith('.') and NAME.fullmatch(name)

def normalize_files(payload):
    raw=payload.get('files') if isinstance(payload,dict) else None
    if not isinstance(raw,list) or not 1<=len(raw)<=MAX_FILES:
        raise ValueError('Upload between 1 and 50 SQL files or SSIS packages.')
    result=[];seen=set();total=0
    def add(name,text,project=None,metadata=None):
        nonlocal total
        if not valid_name(name) or name.lower().endswith('.ispac'):
            raise ValueError('Use plain .sql or .dtsx filenames without folders.')
        if name.casefold() in seen:raise ValueError('File names must be unique: '+name)
        if not isinstance(text,str) or not text.strip() or '\0' in text:
            raise ValueError(name+': supply nonempty decoded SQL/XML text.')
        total+=len(text.encode('utf-8'))
        if total>MAX_BYTES or len(result)>=MAX_FILES:
            raise ValueError('Expanded upload exceeds 50 files or 4 MB total.')
        item={'name':name,'sql':text,'kind':'ssis' if name.lower().endswith('.dtsx') else 'sql'}
        if project:item.update(origin_project=project,project_metadata=metadata or [])
        result.append(item);seen.add(name.casefold())
    for item in raw:
        if not isinstance(item,dict) or not valid_name(item.get('name')):raise ValueError('Use plain .sql, .dtsx, or .ispac filenames without folders.')
        name=item['name']
        if not name.lower().endswith('.ispac'):
            add(name,item.get('sql'));continue
        encoded=item.get('base64')
        if not isinstance(encoded,str) or len(encoded)>MAX_BYTES*2:raise ValueError('Invalid or oversized ISPAC upload.')
        try:
            data=base64.b64decode(encoded,validate=True)
            if len(data)>MAX_BYTES:raise ValueError('ISPAC archive exceeds 4 MB.')
            with zipfile.ZipFile(BytesIO(data)) as z:
                entries=z.infolist()
                if len(entries)>200 or sum(e.file_size for e in entries)>MAX_BYTES:
                    raise ValueError('ISPAC expanded contents exceed 4 MB or 200 entries.')
                metadata=[];packages=[]
                for entry in entries:
                    path=PurePosixPath(entry.filename)
                    if '\\' in entry.filename or path.is_absolute() or '..' in path.parts or entry.flag_bits&1:
                        raise ValueError('ISPAC contains an unsafe or encrypted entry.')
                    if entry.filename.lower().endswith('.dtsx'):packages.append(entry)
                    elif not entry.is_dir():metadata.append(entry.filename)
                if not packages:raise ValueError('ISPAC contains no DTSX packages.')
                for entry in packages:
                    text=decode_text(z.read(entry))
                    add(PurePosixPath(entry.filename).name,text,name,metadata)
        except (binascii.Error,zipfile.BadZipFile,UnicodeError,NotImplementedError,RuntimeError) as exc:
            raise ValueError('Invalid, encrypted, or unreadable ISPAC archive.') from exc
    return result

def decode_text(data):
    if data.startswith((b'\xff\xfe',b'\xfe\xff')):return data.decode('utf-16')
    return data.decode('utf-8-sig')
