"""Offline agent probe bounds and preemption; no Docker or hardware required."""
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import template_probe as agent
path=Path(__file__).resolve().parents[1]/'lumaris_api'/'benchmark_runtime.py'
spec=importlib.util.spec_from_file_location('probe_server',path)
server=importlib.util.module_from_spec(spec);spec.loader.exec_module(server)
IMAGE='quay.io/jupyter/pytorch-notebook@sha256:'+'a'*64


class AgentTests(unittest.TestCase):
    def setUp(self):
        # All uploads mocked: these tests must not touch seller credentials or the network.
        self.reports=patch('diagnostics.schedule').start()
        self.addCleanup(patch.stopall)
        self.ch=server.issue(IMAGE);self.ch['template']='pytorch';self.ch['env']={}
        self.answer=dict(self.ch,status='completed',app_ready=True,output_hash=server.expected_hash(self.ch))
    def run_probe(self,runner,prepare=lambda *a,**k:None):
        return agent.run(self.ch,runner,prepare,['--cap-drop','ALL'],['--gpus','all'],42)
    def test_real_image_fixed_command_isolated_and_bounded(self):
        seen=[]
        def run(argv,**kwargs):
            seen.append((argv,kwargs));return SimpleNamespace(returncode=0,stdout=json.dumps(self.answer))
        result=self.run_probe(run)
        self.assertTrue(server.check(self.ch,result)[0]);self.assertTrue(result['app_ready'])
        cmd,options=seen[0]
        self.assertIn(IMAGE,cmd);self.assertIn('--pull=never',cmd)
        self.assertEqual(cmd[cmd.index('--network')+1],'none')
        self.assertNotIn('-v',cmd);self.assertNotIn('-p',cmd)
        self.assertEqual(options['timeout'],180)
    def test_prepare_timeout_and_bad_output_are_inconclusive(self):
        def bad(*a,**kw):raise RuntimeError('registry unreachable')
        self.assertEqual(self.run_probe(None,bad)['failure'],'IMAGE_UNAVAILABLE')
        def timeout(*a,**kw):raise subprocess.TimeoutExpired('docker',180)
        self.assertEqual(self.run_probe(timeout)['failure'],'TIMEOUT')
        out=self.run_probe(lambda *a,**k:SimpleNamespace(returncode=0,stdout='secret\n'*10000))
        self.assertEqual(out['failure'],'CHECK_FAILED')
        self.assertNotIn('secret',str(out))
    def test_docker_failure_evidence_goes_to_private_diagnostics_only(self):
        error=subprocess.CalledProcessError(1,['docker','pull',IMAGE],output='download incomplete',
                                           stderr='registry transport error')
        def bad(*a,**kw):raise error
        answer=self.run_probe(None,bad)
        self.assertEqual(answer['failure'],'IMAGE_UNAVAILABLE')
        self.assertNotIn('transport',str(answer));self.assertNotIn('download',str(answer))
        self.assertEqual(self.reports.call_count,1)
        call=self.reports.call_args
        self.assertIn('prepare IMAGE_UNAVAILABLE',call.args[0])
        self.assertIn('registry transport error',call.kwargs['evidence'])
        self.assertIn('download incomplete',call.kwargs['evidence'])
        self.reports.reset_mock()
        out=self.run_probe(lambda *a,**k:SimpleNamespace(returncode=1,stdout='',stderr='CUDA loader error'))
        self.assertEqual(out['failure'],'CHECK_FAILED')
        self.assertIn('CUDA loader error',self.reports.call_args.kwargs['evidence'])
    def test_diagnostics_failure_never_breaks_signed_answer(self):
        self.reports.side_effect=RuntimeError('collector unavailable')
        def timeout(*a,**kw):raise subprocess.TimeoutExpired('docker pull',120,
                                                               output=b'layer 1 downloading',stderr=b'network stalled')
        answer=self.run_probe(None,timeout)
        self.assertEqual(answer['failure'],'IMAGE_UNAVAILABLE')
        self.assertIn('network stalled',self.reports.call_args.kwargs['evidence'])
    def test_invalid_inputs_never_prepare_an_image(self):
        for key,value in [('image','anything:latest'),('nonce','wrong'),('template','custom'),('env',{'DOCKER_HOST':'evil'})]:
            bad=dict(self.ch,**{key:value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                agent.run(bad,None,lambda *a,**k:self.fail('prepared invalid input'),[],[],42)
    def test_paid_work_cancellation_is_not_a_host_failure(self):
        def run(*a,**kw):
            agent._CANCEL.set();return SimpleNamespace(returncode=0,stdout=json.dumps(self.answer))
        self.assertEqual(self.run_probe(run)['failure'],'CHECK_FAILED')
        self.reports.assert_not_called()
        agent._RUNNING.set()
        with patch.object(subprocess,'run',return_value=SimpleNamespace(stdout='a'*12+'\n--all\n')) as mock:
            agent.yield_to_paid_work()
            self.assertEqual(mock.call_count,2)
            self.assertIn('label=pb.template_probe=1',mock.call_args_list[0].args[0])
            self.assertEqual(mock.call_args_list[1].args[0],['docker','rm','-f','a'*12])
        agent._RUNNING.clear()

    def program(self, name, cuda=True, fallback=False, app=True):
        # Execute the shipped program with tiny CPU adapters for framework APIs. This checks
        # nonce/matrix parity and fallback rejection without claiming real CUDA compatibility.
        class Tensor:
            def __init__(self, values, device='GPU'):
                self.values, self.device = values, device
            def reshape(self, *shape): return self
            def cpu(self): return self
            def numpy(self): return self
            def tolist(self): return self.values
            def __matmul__(self, other):
                n = 64
                return Tensor([sum(self.values[r*n+k]*other.values[k*n+c] for k in range(n))
                               for r in range(n) for c in range(n)], 'CPU' if fallback else 'GPU')
        torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda:cuda), float32='float32',
            tensor=lambda values, **kwargs:Tensor(values))
        tf = SimpleNamespace(config=SimpleNamespace(list_physical_devices=lambda _:['gpu'] if cuda else [],
            set_soft_device_placement=lambda _:None, experimental=SimpleNamespace(set_memory_growth=lambda *a:None)),
            device=lambda _:contextlib.nullcontext(), reshape=lambda a,shape:a,
            constant=lambda values,**kwargs:Tensor(values), float32='float32', matmul=lambda a,b:a@b)
        process = SimpleNamespace(poll=lambda:None,terminate=lambda:None,wait=lambda **kw:None)
        output = io.StringIO()
        with patch.dict(sys.modules, {'torch':torch, 'tensorflow':tf}), \
             patch.object(sys,'argv',['python',name,self.ch['nonce']]), \
             patch('shutil.which',return_value='start-notebook.py' if app else None), \
             patch('subprocess.Popen',return_value=process) as launched, \
             patch('urllib.request.urlopen',return_value=contextlib.nullcontext(SimpleNamespace(
                 status=200,read=lambda:b'{"kernels":0}'))) as health, contextlib.redirect_stdout(output):
            exec(agent.PROGRAM, {'__name__':'__main__'})
        return json.loads(output.getvalue()), launched, health

    def test_shipped_program_parity_and_real_notebook_health_requirement(self):
        for name in ('pytorch','tensorflow','jupyter'):
            with self.subTest(template=name):
                answer, app, health = self.program(name)
                self.assertEqual(answer['output_hash'],server.expected_hash(self.ch),answer)
                self.assertTrue(answer['app_ready']);self.assertEqual(app.call_count,1)
                self.assertIn('/api/status?token='+self.ch['nonce'],health.call_args.args[0])
        answer,app,health=self.program('pytorch',app=False)
        self.assertEqual(answer['failure'],'APP_START_FAILED');self.assertNotIn('app_ready',answer)
        self.assertEqual(app.call_count,0)

    def test_shipped_program_never_passes_cpu_fallback(self):
        for name, cuda, fallback in [('pytorch',False,False),('tensorflow',False,False),('tensorflow',True,True)]:
            with self.subTest(template=name,cuda=cuda,fallback=fallback):
                answer,app,health=self.program(name,cuda,fallback)
                self.assertEqual(answer['failure'],'CUDA_UNAVAILABLE')
                self.assertEqual(app.call_count,0);self.assertEqual(health.call_count,0)


if __name__=='__main__':unittest.main()
