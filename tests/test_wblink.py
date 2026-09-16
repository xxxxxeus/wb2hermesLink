import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from email.message import Message
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import wblink as w


class ClientNameTests(unittest.TestCase):
    def test_environment_tracks_interpreter_install_without_executing_source(self):
        with tempfile.TemporaryDirectory() as directory:
            install = Path(directory)
            source = install/'hermes_cli/__init__.py'
            source.parent.mkdir()
            auth = w.Auth('synthetic-token', 'synthetic.invalid', 'synthetic-uid')
            with patch.object(w.sys, 'prefix', str(install/'venv')), \
                    patch.object(w.subprocess, 'run', side_effect=AssertionError('no nested CLI')):
                for version in ('0.21.3', '0.22.0'):
                    source.write_text('__version__ = '+repr(version)+'\nraise RuntimeError("must not execute")\n')
                    self.assertEqual(w.environment(auth).splitlines(), [
                        'WBLINK_ACCESS_TOKEN=synthetic-token',
                        'WBLINK_USER_ID_SECRET=synthetic-uid',
                        'WBLINK_DOMAIN_SECRET=synthetic.invalid',
                        'WBLINK_CLIENT_NAME=HermesAgent v'+version])


    def test_version_failures_keep_auth_and_use_plain_name(self):
        auth = w.Auth('synthetic-token', 'synthetic.invalid', 'synthetic-uid')
        cases = [None, 'not python !', '__version__ = None', '__version__ = 123',
                 '__version__ = "0.21.3\\nOTHER=value"', '__version__ = ""',
                 '__version__ = "不明"', '__version__ = unknown()',
                 '__version__ = "0.21.3"\n' + '#' * (w.LIMIT + 1), b'\xff']
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'hermes_cli/__init__.py'
            source.parent.mkdir()
            with patch.object(w.sys, 'prefix', str(Path(directory)/'venv')):
                for case in cases:
                    with self.subTest(source=repr(case)[:80]):
                        if case is not None:
                            source.write_bytes(case if isinstance(case, bytes) else case.encode())
                        try:
                            result = w.environment(auth).splitlines()
                        except Exception as exc:
                            self.fail('version lookup must not break auth: '+type(exc).__name__)
                        self.assertEqual(result, [
                            'WBLINK_ACCESS_TOKEN=synthetic-token',
                            'WBLINK_USER_ID_SECRET=synthetic-uid',
                            'WBLINK_DOMAIN_SECRET=synthetic.invalid',
                            'WBLINK_CLIENT_NAME=HermesAgent'])


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.test-', dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / '.private/auth.json'
        self.auth = w.Auth('synthetic-token', 'synthetic.invalid', 'synthetic-uid')

    def test_roundtrip_machine_and_permissions(self):
        w.save_snapshot(self.auth, self.path)
        self.assertEqual(w.load_snapshot(self.path), self.auth)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(w.environment(self.auth).splitlines()[:3], [
            'WBLINK_ACCESS_TOKEN=synthetic-token', 'WBLINK_USER_ID_SECRET=synthetic-uid',
            'WBLINK_DOMAIN_SECRET=synthetic.invalid'])
        self.assertNotIn(self.auth.token, repr(self.auth))

    def test_bad_fields_and_injection(self):
        for val in ('', ' x', 'x\nY=z', 'x\r', 'x\x00', '\u4e2d', 'x"', "x'"):
            with self.subTest(kind='invalid'), self.assertRaises(w.Error):
                w.validate({'schema_version': 1, 'token': val, 'domain': 'd', 'uid': 'u'})
        for obj in ({}, {'schema_version': True, 'token':'t','domain':'d','uid':'u'},
                    {'schema_version':1,'token':'t','domain':'d','uid':'u','extra':0}):
            with self.assertRaises(w.Error): w.validate(obj)

    def test_symlink_permissions_and_atomic_failure(self):
        w.save_snapshot(self.auth, self.path)
        before = self.path.read_bytes()
        with patch.object(w.os, 'replace', side_effect=OSError()):
            with self.assertRaises(w.Error): w.save_snapshot(self.auth, self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(list(self.path.parent.iterdir())), 1)
        self.path.chmod(0o644)
        with self.assertRaises(w.Error): w.load_snapshot(self.path)
        self.path.unlink(); self.path.symlink_to(self.path.parent / 'absent')
        with self.assertRaises(w.Error): w.save_snapshot(self.auth, self.path)
        with self.assertRaises(w.Error): w.load_snapshot(self.path)

    def test_duplicate_and_size(self):
        w.save_snapshot(self.auth, self.path)
        for raw in ('{"schema_version":1,"schema_version":1}', 'x' * 65537):
            self.path.write_text(raw)
            with self.assertRaises(w.Error): w.load_snapshot(self.path)

    def test_default_help_never_read(self):
        for args in ([], ['--help']):
            with patch.object(w, 'load_snapshot', side_effect=AssertionError()), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(w.main(args), 0)
            self.assertNotIn('synthetic-token', out.getvalue())

    def test_machine_subprocess_and_status(self):
        w.save_snapshot(self.auth, self.path)
        env = {'PATH':'/usr/bin:/bin', 'HERMES_SECRET_KEY':''}
        child = subprocess.run([sys.executable, '-I', '-B', str(ROOT/'wblink.py'), 'env', '--snapshot', str(self.path)],
                               env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(child.returncode, 0)
        self.assertEqual(child.stdout, w.environment(self.auth))
        self.assertEqual(child.stderr, '')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(w.main(['status', '--snapshot', str(self.path)]), 0)
        self.assertNotIn(self.auth.token, out.getvalue())
        self.assertFalse(json.loads(out.getvalue())['server_validity_known'])

    def test_url(self):
        self.assertEqual(w.validate_url('https://copilot.tencent.com/auth?q=x'), 'https://copilot.tencent.com/auth?q=x')
        for url in ('http://copilot.tencent.com', 'https://evil.invalid', 'https://u@copilot.tencent.com',
                    'https://copilot.tencent.com:444', 'https://copilot.tencent.com\\x', 'https://copilot.tencent.com/\nx'):
            with self.assertRaises(w.Error): w.validate_url(url)

    def test_new_state_binding_pending_and_no_persistence(self):
        calls = []; clock = [0.0]
        def request(stage, deadline, state=None, auth=None):
            calls.append((stage, state, auth, clock[0]))
            if stage == 'state': return {'state':'synthetic-state', 'authUrl':'https://copilot.tencent.com/login'}
            self.assertEqual(state, 'synthetic-state')
            if len(calls) == 2: return None
            if stage == 'token': return {'accessToken':self.auth.token,'domain':self.auth.domain}
            self.assertEqual(auth.token, self.auth.token)
            return {'uid':self.auth.uid}
        def sleep(seconds): clock[0] += seconds
        with patch.object(w.time, 'monotonic', side_effect=lambda:clock[0]), patch.object(w.time, 'sleep', side_effect=sleep), patch.object(w, 'save_snapshot', side_effect=AssertionError()):
            result = w.authorize(request=request, opener=lambda url, deadline:None)
        self.assertEqual(result, self.auth)
        self.assertEqual([x[0] for x in calls], ['state','token','token','account'])
        self.assertGreaterEqual(calls[3][3]-calls[2][3], 5)

    def test_pending_budget_cancel_schema(self):
        clock = [0.0]; count = [0]
        def request(stage, deadline, **kw):
            count[0] += 1
            return {'state':'s','authUrl':'https://copilot.tencent.com'} if stage=='state' else None
        with patch.object(w.time, 'monotonic', side_effect=lambda:clock[0]), patch.object(w.time, 'sleep', side_effect=lambda x:clock.__setitem__(0,clock[0]+x)):
            with self.assertRaises(w.Error): w.authorize(request=request, opener=lambda *a:None)
        self.assertLessEqual(count[0], 61)
        with self.assertRaises(KeyboardInterrupt): w.authorize(request=lambda *a,**k: (_ for _ in ()).throw(KeyboardInterrupt()))
        with self.assertRaises(w.Error): w.authorize(request=lambda *a,**k: {'state':'s','authUrl':'https://evil.invalid'})

    def test_opener_url_only_stdin(self):
        with patch.object(w.subprocess, 'run') as run:
            run.return_value = subprocess.CompletedProcess([],0,'true','')
            w.open_browser('https://copilot.tencent.com/?state=synthetic', w.time.monotonic()+10)
            self.assertNotIn('synthetic', repr(run.call_args.args))
            self.assertIn('synthetic', run.call_args.kwargs['input'])

    def test_transport_contract_and_pending_errors(self):
        from unittest.mock import MagicMock
        def response(status, value):
            peer=MagicMock(); peer.status=status; peer.headers=Message()
            peer.headers['Content-Type']='application/json'
            peer.getheaders.return_value=list(peer.headers.items())
            peer.getheader.side_effect=lambda k,d=None:peer.headers.get(k,d)
            peer.read.return_value=json.dumps(value).encode()
            return peer
        for stage, code, pending in [('token',11217,True),('account',12151,True),('token',12151,False),('state',11217,False)]:
            connection=MagicMock(); peer=response(200,{'code':code}); connection.getresponse.return_value=peer
            with patch.object(w.http.client,'HTTPSConnection',return_value=connection):
                if pending: self.assertIsNone(w.auth_http(stage,w.time.monotonic()+30,state='synthetic-state',auth=self.auth))
                else:
                    with self.assertRaises(w.Error): w.auth_http(stage,w.time.monotonic()+30,state='synthetic-state',auth=self.auth)
            self.assertTrue(connection.close.called); self.assertTrue(peer.close.called)
            self.assertEqual(w.signal.getitimer(w.signal.ITIMER_REAL),(0.0,0.0))
        connection=MagicMock(); connection.getresponse.return_value=response(200,{'code':0,'data':{}})
        with patch.object(w.http.client,'HTTPSConnection',return_value=connection) as factory:
            w.auth_http('state',w.time.monotonic()+30)
        self.assertEqual(factory.call_args.args,(w.HOST,443))
        self.assertTrue(factory.call_args.kwargs['context'].check_hostname)
        args=connection.request.call_args
        self.assertEqual(args.args,('POST','/v2/plugin/auth/state?platform=workbuddy'))
        self.assertEqual(args.kwargs['body'],b'{}')
        self.assertNotIn('Authorization',args.kwargs['headers'])
        for status, category in [(302,'redirect'),(401,'authentication'),(403,'authentication')]:
            connection.getresponse.return_value=response(status,{'msg':'synthetic-token'})
            with patch.object(w.http.client,'HTTPSConnection',return_value=connection),self.assertRaises(w.Error) as caught:
                w.auth_http('state',w.time.monotonic()+30)
            self.assertEqual(caught.exception.category,category)

    def test_machine_denied_and_fixed_errors(self):
        with patch.dict(os.environ,{},clear=True),contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(w.main(['env']),1)
        self.assertEqual(json.loads(err.getvalue())['error_category'],'machine_only')
        with patch.object(w,'authorize',side_effect=ValueError(self.auth.token)),contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(w.main(['login','--memory-only']),1)
        self.assertNotIn(self.auth.token,err.getvalue())

    def test_precise_process_gate(self):
        w.validate_processes('1 0 launchd\n2 1 /usr/libexec/SidecarRelay\n3 1 node\n4 1 /tmp/workbuddy-probe/python')
        for command in ('/Applications/WorkBuddy.app/Contents/MacOS/helper','/Applications/CodeBuddy.app/helper',
                        'codebuddy','codebuddy-headless','workbuddy-sidecar','workbuddy-server','daemon-app-server'):
            with self.assertRaises(w.Error): w.validate_processes('2 1 '+command)
        for text in ('','2 1','x 1 node','0 1 node','2 -1 node','2 1 node\n2 1 other'):
            with self.assertRaises(w.Error): w.validate_processes(text)


if __name__ == '__main__': unittest.main()
