"""Fetch the pinned official Morpheus artifact. Requires Java 21+ to run it."""
import hashlib
import os
from pathlib import Path
import urllib.request
VERSION='0.10.0'
EXPECTED='befab886308f7ccdc4775c9045f584209b6bd993a7eb2a65206be24edd5732d3'
URL=f'https://repo.maven.apache.org/maven2/com/databricks/labs/databricks-morph-plugin/{VERSION}/databricks-morph-plugin-{VERSION}.jar'
root=Path(__file__).parent/'engine';root.mkdir(exist_ok=True)
target=root/'morpheus.jar'
if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest()==EXPECTED:
    print('Morpheus 0.10.0 is already installed and checksum verified.')
else:
    temporary=root/'morpheus.jar.download'
    try:
        print('Downloading official Morpheus 0.10.0 (about 68 MB)…',flush=True)
        with urllib.request.urlopen(URL,timeout=60) as response,temporary.open('wb') as stream:
            while chunk:=response.read(1024*1024):stream.write(chunk)
        if hashlib.sha256(temporary.read_bytes()).hexdigest()!=EXPECTED:raise RuntimeError('Artifact checksum mismatch.')
        os.replace(temporary,target)
        print('Morpheus installed and SHA-256 checksum verified. Run python server.py.')
    finally:
        temporary.unlink(missing_ok=True)
