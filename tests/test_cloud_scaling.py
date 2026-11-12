import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from cloud_scaling import REQUEST_TOKEN,token,set_instances

class ScalingTests(unittest.TestCase):
    def test_request_and_rolling_file_credentials_are_not_cached(self):
        with patch.dict(os.environ,{},clear=True),tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'oidc';path.write_text('first')
            with patch.dict(os.environ,{'VERCEL_OIDC_TOKEN_FILE':str(path),'VERCEL_OIDC_TOKEN':'old-build-token'}):
                self.assertEqual(token(),'first');path.write_text('second');self.assertEqual(token(),'second')
                context=REQUEST_TOKEN.set('request-token')
                try:self.assertEqual(token(),'request-token')
                finally:REQUEST_TOKEN.reset(context)
    def test_region_override_is_used_for_scaling(self):
        import cloud_scaling
        seen=[]
        class Response:
            def __enter__(self):return self
            def __exit__(self,*a):return False
            def read(self):return b'{"ok":true}'
        class Opener:
            def open(self,request,timeout=None):seen.append(request);return Response()
        env={'SCOUT_VERCEL_SCALER_TOKEN':'t','SCOUT_VERCEL_PROJECT_ID':'p','SCOUT_VERCEL_TEAM_ID':'team'}
        self.assertEqual(cloud_scaling.DAEMON_REGION,'sin1')
        with patch.dict(os.environ,env,clear=True),patch.object(cloud_scaling,'DAEMON_REGION','hkg1'),patch('urllib.request.build_opener',return_value=Opener()):self.assertTrue(set_instances(1))
        self.assertEqual(json.loads(seen[0].data),{'hkg1':1})
    def test_scales_only_fixed_project_region_and_does_not_leak_credentials(self):
        seen=[]
        class Opener:
            def open(self,request,timeout):seen.append(request);return contextlib.closing(io.BytesIO(b'{"ok":true}'))
        env={'SCOUT_VERCEL_SCALER_TOKEN':'fixture-secret','SCOUT_VERCEL_PROJECT_ID':'prj_fixture','SCOUT_VERCEL_TEAM_ID':'team_fixture'}
        with patch.dict(os.environ,env,clear=True),patch('urllib.request.build_opener',return_value=Opener()):self.assertTrue(set_instances(1))
        self.assertEqual(seen[0].method,'PUT');self.assertEqual(json.loads(seen[0].data),{'sin1':1});self.assertIn('/prj_fixture/daemons/browser-worker/instances',seen[0].full_url)
        with patch.dict(os.environ,env,clear=True),patch('urllib.request.OpenerDirector.open',side_effect=urllib.error.HTTPError('https://api.vercel.com',403,'Forbidden',{},None)):
            with self.assertRaises(ValueError) as raised:set_instances(0)
        self.assertNotIn('fixture-secret',str(raised.exception))

if __name__=='__main__':unittest.main()
