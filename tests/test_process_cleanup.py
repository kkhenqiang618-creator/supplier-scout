"""Real isolated process fixtures; no browser, model or external network."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import server

WORKER='''import json,subprocess,sys,time
from pathlib import Path
root=Path(__file__).parent
if '--parent-gate' in sys.argv[5:]:
    assert sys.stdin.readline().strip()=='ready'
if sys.argv[1]=='tree':
    subprocess.Popen([sys.executable,str(root/'child.py')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    print(json.dumps({'kind':'log','message':'tree ready'}),flush=True)
else:
    print(json.dumps({'kind':'result','data':{'contact_status':'blocked','contacts':{}}}),flush=True)
time.sleep(30)
'''
class ProcessCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'collection_worker.py').write_text(WORKER)
        (self.root/'child.py').write_text("import time\nfrom pathlib import Path\np=Path(__file__).with_name('heartbeat')\nwhile True:\n p.write_text(str(time.time()))\n time.sleep(.05)\n")
        self.root_patch=patch.object(server,'ROOT',self.root);self.root_patch.start();self.addCleanup(self.root_patch.stop)
    def test_result_is_preserved_with_original_blocked_status(self):
        result=server.collect_site('result',lambda _:None,threading.Event())
        self.assertEqual(result,{'contact_status':'blocked','contacts':{}})
    @unittest.skipIf(os.name=='nt','POSIX cleanup fault injection; Windows native tree test runs separately')
    def test_cleanup_failure_does_not_discard_result(self):
        children=[];popen=subprocess.Popen
        def launch(*args,**kwargs):
            process=popen(*args,**kwargs);children.append(process);return process
        try:
            with patch.object(server.subprocess,'Popen',side_effect=launch),patch.object(server.os,'killpg',side_effect=OSError('fixture denied')):
                logs=[];started=time.monotonic();result=server.collect_site('result',logs.append,threading.Event())
                self.assertLess(time.monotonic()-started,3,'stdout close must not wait for a stuck reader')
            self.assertEqual(result['contact_status'],'blocked')
            self.assertTrue(any('清理' in line for line in logs))
        finally:
            for child in children:
                if child.poll() is None:child.kill();child.wait(timeout=2)
    def run_tree(self,timeout=False):
        stop=threading.Event();errors=[]
        def run():
            try:server.collect_site('tree',lambda _:None,stop)
            except Exception as error:errors.append(error)
        with patch.dict(server.SITE_HARD_TIMEOUT,{'all':.5 if timeout else 30}):
            thread=threading.Thread(target=run);thread.start()
            try:
                deadline=time.monotonic()+5
                while not (self.root/'heartbeat').exists() and thread.is_alive() and time.monotonic()<deadline:time.sleep(.02)
                self.assertTrue((self.root/'heartbeat').exists(),'grandchild must be alive before stopping')
                started=time.monotonic()
                if not timeout:stop.set()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive(),'collection must stop within 3 seconds')
                self.assertLess(time.monotonic()-started,3)
                self.assertIsInstance(errors[0],ValueError if timeout else server.CollectionCancelled)
                stamp=(self.root/'heartbeat').read_text();time.sleep(.2)
                self.assertEqual((self.root/'heartbeat').read_text(),stamp,'grandchild must also stop')
            finally:
                stop.set();thread.join(timeout=4)
    def test_stop_ends_real_grandchild(self):self.run_tree()
    def test_timeout_ends_real_grandchild(self):self.run_tree(timeout=True)

    def test_stop_during_result_grace_is_prompt(self):
        stop=threading.Event();timer=threading.Timer(.3,stop.set);timer.start()
        self.addCleanup(timer.cancel)
        started=time.monotonic()
        result=server.collect_site('result',lambda _:None,stop)
        self.assertEqual(result['contact_status'],'blocked')
        self.assertLess(time.monotonic()-started,1.5)

    def test_exited_parent_does_not_leave_descendants(self):
        script=WORKER.replace("if sys.argv[1]=='tree':", "if True:").replace("time.sleep(30)", "print(json.dumps({'kind':'result','data':{'contact_status':'blocked','contacts':{}}}),flush=True)")
        # The grandchild must exist before the parent returns and exits.
        script=script.replace("    print(json.dumps({'kind':'log','message':'tree ready'}),flush=True)", "    while not (root/'heartbeat').exists():time.sleep(.01)")
        (self.root/'collection_worker.py').write_text(script)
        result=server.collect_site('tree',lambda _:None,threading.Event())
        self.assertEqual(result['contact_status'],'blocked')
        time.sleep(.1);stamp=(self.root/'heartbeat').read_text();time.sleep(.2)
        self.assertEqual((self.root/'heartbeat').read_text(),stamp)

    def test_exited_parent_with_inherited_stdout_fails_promptly(self):
        script=WORKER.replace('stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL','')
        script=script.replace("    print(json.dumps({'kind':'log','message':'tree ready'}),flush=True)","    while not (root/'heartbeat').exists():time.sleep(.01)")
        script=script.replace('time.sleep(30)','')
        (self.root/'collection_worker.py').write_text(script)
        started=time.monotonic()
        with patch.dict(server.SITE_HARD_TIMEOUT,{'all':2}):
            with self.assertRaisesRegex(ValueError,'采集进程意外结束'):
                server.collect_site('tree',lambda _:None,threading.Event())
        self.assertLess(time.monotonic()-started,1.5)
