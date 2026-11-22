"""Run each unittest module in its own Qt process; retain all failures and skips."""
import json
import os
from pathlib import Path
import subprocess
import sys

root=Path(__file__).resolve().parents[1]
output=root/'tracking_validation'
output.mkdir(exist_ok=True)
results=[]
for path in sorted((root/'tests').glob('test_*.py')):
    command=[sys.executable,'-m','unittest','discover','-s','tests','-p',path.name]
    try:
        run=subprocess.run(command,cwd=root,env=dict(os.environ,QT_QPA_PLATFORM='offscreen',PYTHONPATH=str(root)),
                           capture_output=True,text=True,timeout=120)
        content=run.stdout+run.stderr
        code=run.returncode
    except subprocess.TimeoutExpired as error:
        content='TIMEOUT\n'+str(error.stdout or '')+str(error.stderr or '')
        code=124
    (output/(path.stem+'.log')).write_text(content)
    results.append(dict(module=path.stem,returncode=code,command=command))
    print(path.stem,code,content[-180:].strip(),flush=True)
(output/'results.json').write_text(json.dumps(results,indent=2))
sys.exit(int(any(row['returncode'] for row in results)))
