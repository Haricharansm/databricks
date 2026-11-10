"""Install the official Lakebridge BladeBridge engine in a project-local venv."""
import os
from pathlib import Path
import subprocess
import venv
root=Path(__file__).parent/'engine'/'bladebridge'
if not root.exists():venv.EnvBuilder(with_pip=True).create(root)
python=root/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
subprocess.run([str(python),'-m','pip','install','--disable-pip-version-check','databricks-bb-plugin==0.3.0'],check=True)
print('BladeBridge 0.3.0 installed. Restart server.py to enable experimental SSIS notebook conversion.')
