"""Run the real container supervisor with a sleep stub, never real mining."""
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

root=Path(__file__).resolve().parents[2]
def run(*args):
 return subprocess.run(args, capture_output=True, text=True, check=True, timeout=30)

def main():
 with tempfile.TemporaryDirectory(prefix='pb-mining-lease-test-') as tmp:
  base=Path(tmp);base.chmod(0o755)
  lease=base/'lease';lease.mkdir(mode=0o755)
  stub=base/'miner';stub.write_text('#!/bin/sh\necho test-worker-started\nexec sleep 600\n');stub.chmod(0o755)
  for scenario in ('expiry','disable','revoked','missing'):
   name='pb-mining-test-'+uuid.uuid4().hex[:10]
   (lease/'disabled').unlink(missing_ok=True)
   (lease/'until').unlink(missing_ok=True)
   if scenario!='missing':
    (lease/'until').write_text(str(int(time.clock_gettime(time.CLOCK_BOOTTIME))+3 if scenario=='expiry' else 0 if scenario=='revoked' else int(time.clock_gettime(time.CLOCK_BOOTTIME))+25)+'\n')
   try:
    run('docker','run','-d','--name',name,'--network=none','--read-only','--cap-drop=ALL',
        '--security-opt=no-new-privileges','--pids-limit=16',
        '--mount',f'type=bind,src={lease},dst=/lease,readonly',
        '--mount',f'type=bind,src={stub},dst=/usr/local/bin/miner,readonly',
        '--mount',f'type=bind,src={root}/lumaris_agent/mining/supervise.sh,dst=/usr/local/bin/supervise,readonly',
        'petabyte-idle-miner:test-20260914')
    if scenario=='disable':
     time.sleep(1)
     (lease/'disabled').touch()
    started=time.monotonic()
    result=run('docker','wait',name)
    assert time.monotonic()-started<6,(scenario,'did not stop promptly')
    logs=run('docker','logs',name).stdout
    if scenario in ('missing','revoked'):assert 'test-worker-started' not in logs
    else:assert 'test-worker-started' in logs
    print('PASS',scenario,'exit',result.stdout.strip())
   finally:
    run('docker','rm','-f',name)

if __name__ == "__main__":
 main()
