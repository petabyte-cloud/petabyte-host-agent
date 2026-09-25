"""Real container argv and optional Docker execution for Python Swarm jobs.

SWARM_DOCKER_TEST=1 also executes success, failure and timeout cases using a
locally available python:3.11-slim image; no GPU or cloud account is needed.
"""
import hashlib
import os
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'lumaris_api'))
os.environ.setdefault('PETABYTE_API_URL', 'https://test.local')
os.environ.setdefault('PETABYTE_API_KEY', 'pk_test')
os.environ.setdefault('PETABYTE_SPEC_ID', '1')
for name in ('crypto', 'notebook', 'vm', 'agent_telemetry'):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules['crypto'].sign_proof = lambda p: 'sig'
sys.modules['crypto'].sha256_hex = lambda b: hashlib.sha256(b if isinstance(b, bytes) else str(b).encode()).hexdigest()
sys.modules['notebook'].run_notebook_code = lambda *a, **k: []
sys.modules['vm'].launch_vm_task = lambda *a, **k: None
import task_fetcher as tf
import swarm_python as sp

def task(code, timeout=15):
    p = sp.normalize({'mode': 'python', 'code': code, 'gpu': False, 'args': ['$(echo literal)', 'λ']})
    return dict(task_id=778, **sp.payload(p, 'python:3.11-slim', timeout))

payload = task('print("source-is-data")')
try:
    argv = tf.build_container_cmd(payload, name='pb-python-test')
    assert argv[argv.index('--entrypoint') + 1] == 'python3'
    assert argv[argv.index('--network') + 1] == 'none'
    assert argv[argv.index('--user') + 1] == '65534:65534'
    assert '--read-only' in argv and '--cap-drop' in argv and '--gpus' not in argv
    assert '--privileged' not in argv and '--env-file' in argv
    assert not any('source-is-data' in arg or payload['env']['SWARM_PYTHON_B64'] in arg for arg in argv)
finally:
    tf._remove_env_file(payload)
print('ok Python entrypoint, isolation and source off argv')

if os.getenv('SWARM_DOCKER_TEST') == '1':
    received = []
    tf._post = lambda path, body: received.append(body)
    tf._signed_result = lambda tid, **kw: dict(task_id=tid, **kw)
    tf._set_ui = lambda **kw: None
    tf.report_progress = lambda *a: None
    tf.report_log = lambda *a: None
    code = '''import os, sys
assert os.getuid() == 65534
assert sys.argv[1:] == ["$(echo literal)", "λ"]
assert __name__ == "__main__"
try:
    open('/must-not-write', 'w').close()
    raise AssertionError('rootfs was writable')
except OSError:
    pass
print('python-swarm-ok')
'''
    tf._run_container(task(code))
    assert received[-1]['status'] == 'completed' and 'python-swarm-ok' in received[-1]['result'], received
    print('ok real non-root Python execution with literal arguments and read-only rootfs')
    tf._run_container(task('raise RuntimeError("intentional failure")'))
    assert received[-1]['status'] == 'failed' and received[-1]['failure_cause'] == 'container_exit', received[-1]
    print('ok nonzero exit is reported as failed')
    tf._run_container(task('import time; time.sleep(60)', timeout=2))
    assert received[-1]['status'] == 'failed' and received[-1]['failure_cause'] == 'timeout', received[-1]
    print('ok runtime cap kills container and reports failure')
