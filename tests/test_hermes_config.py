"""Synthetic native Hermes consumers, isolated child, no provider replacement."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import signal
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
SOURCE=Path(os.environ.get('W2HLINK_TEST_HERMES_ROOT', Path(sys.prefix).parent))
PYTHON=SOURCE/'venv/bin/python'


def run_native(mode='clean'):
    with tempfile.TemporaryDirectory(prefix='.test-',dir=ROOT) as directory:
        env={k:directory for k in ('HOME','HERMES_HOME','HERMES_MANAGED_DIR','XDG_CONFIG_HOME','XDG_CACHE_HOME','XDG_DATA_HOME','TMPDIR')}
        env['HERMES_MANAGED_DIR']=str(Path(directory)/'managed')
        env.update(PATH='/usr/bin:/bin',HERMES_SAFE_MODE='1',HERMES_SKIP_CHMOD='1',PYTHONDONTWRITEBYTECODE='1',NO_PROXY='*')
        env['W2HLINK_TEST_HERMES_ROOT'] = str(SOURCE)
        child=subprocess.run([str(PYTHON),'-I','-B','-S',str(Path(__file__).resolve()),'--child',directory,mode],
                             env=env,cwd=directory,capture_output=True,text=True,timeout=90)
        try: report=json.loads(child.stdout)
        except ValueError: report={'status':'error','stage':'output'}
        report['exit_ok']=child.returncode==0
        return report


class Denied(BaseException): pass


class Guard:
    def __init__(self,root,command):
        self.root=root; self.command=command; self.helper=False; self.denied=0; self.optional=0; self.events=[]
        self.reads=[SOURCE.resolve(),Path(sys.base_prefix).resolve(),Path('/System/Library'),Path('/usr/lib')]
    def path(self,p,write=False):
        if isinstance(p,int): return
        p=Path(p).resolve()
        if p==self.root or self.root in p.parents: return
        if not write and p in (ROOT/'wblink.py',ROOT/'w2hlink_cli.py',ROOT/'w2hlink',ROOT/'tests/test_cli.py',ROOT/'config.example.yaml',Path(__file__).resolve()): return
        if not write and p.name not in ('.env','.op.env','auth.json','config.yaml') and not p.name.startswith('.env.') and p.suffix!='.info' and any(p==x or x in p.parents for x in self.reads): return
        self.denied+=1
        self.events.append({'kind':'write' if write else 'read','project':ROOT in p.parents,'source':SOURCE in p.parents})
        raise Denied('write' if write else 'read')
    def __call__(self,event,args):
        if event=='open':
            p,mode,flags=args; write=bool(flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_APPEND|os.O_TRUNC))
            if p=='/dev/null' and flags in (os.O_RDWR,os.O_RDWR|os.O_CLOEXEC):
                if self.helper: return
                self.optional+=1; raise PermissionError('probe')
            if p=='/proc/version' and not write: self.optional+=1; raise PermissionError('probe')
            self.path(p,write)
        elif event in ('os.listdir','os.scandir'): self.path(args[0])
        elif event in ('os.mkdir','os.remove','os.rmdir','os.chmod','os.utime','os.chown'): self.path(args[0],True)
        elif event in ('os.rename','os.link','os.symlink'): self.path(args[0],True); self.path(args[1],True)
        elif event.startswith('socket.') and event not in ('socket.__new__','socket.gethostname'):
            if event=='socket.bind' and args[0].family==socket.AF_INET6 and args[1]==('::1',0):
                self.optional+=1; raise PermissionError('probe')
            self.denied+=1; self.events.append({'kind':event}); raise Denied('network')
        elif event=='subprocess.Popen':
            if self.helper and args[0]=='/bin/sh' and args[1]==['/bin/sh','-c',self.command]: return
            self.denied+=1; raise Denied('subprocess')
        elif event in ('os.system','os.posix_spawn','os.exec','os.fork'):
            self.denied+=1; raise Denied('subprocess')


EXPECTED_MODELS = {
    'hy3': 262144, 'hy4-preview': 1048576,
    'deepseek-v4.1-flash': 1048576, 'glm-5.3-flash': 1048576,
}


def references_preserved(config):
    entry = config['providers']['workbuddy']
    return (entry.get('api_key') == '${WBLINK_ACCESS_TOKEN}' and 'key_env' not in entry
            and entry['extra_headers']['X-User-Id'] == '${WBLINK_USER_ID_SECRET}'
            and entry['extra_headers']['X-Domain'] == '${WBLINK_DOMAIN_SECRET}'
            and entry['extra_headers'].get('X-IDE-Name') == '${WBLINK_CLIENT_NAME}')


def native_child(directory, mode='clean'):
    root = Path(directory).resolve()
    if root.parent != ROOT or not root.name.startswith('.test-') or mode not in ('clean', 'stale'):
        return {'status': 'error', 'category': 'input'}
    sys.path.insert(0, str(ROOT))
    import wblink
    sys.path.remove(str(ROOT))
    auth = wblink.Auth('synthetic-native-token', 'synthetic-native.invalid', 'synthetic-native-uid')
    wblink.save_snapshot(auth, root/'.private/auth.json')
    if mode == 'stale':
        (root/'.env').write_text(''.join(key+'=synthetic-old\n' for key in (*wblink.KEYS, 'WBLINK_CLIENT_NAME')))
        (root/'.env').chmod(0o600)
    base = shlex.join([str(PYTHON),'-I','-B',str(ROOT/'wblink.py'),'env'])
    command = base+' --snapshot '+shlex.quote(str(root/'.private/auth.json'))
    guard = Guard(root, command)
    sys.addaudithook(guard)
    def deadline(*args):
        raise Denied('deadline')
    signal.signal(signal.SIGALRM, deadline)
    signal.setitimer(signal.ITIMER_REAL, 30)
    report = {'status': 'error', 'simulated': True}
    agent = None
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            sys.path[:0] = [str(SOURCE), str(SOURCE/'venv/lib/python3.11/site-packages')]
            from hermes_cli import __version__
            expected_name = 'HermesAgent v' + __version__
            expected_headers = {'X-User-Id': auth.uid, 'X-Domain': auth.domain, 'X-Product': 'SaaS',
                                'X-IDE-Type': 'Hermes', 'X-IDE-Name': expected_name}
            import yaml
            config = yaml.safe_load((ROOT/'config.example.yaml').read_text())
            entry = config['providers']['workbuddy']
            report['template_models'] = entry['models'] == {'hy3': {'context_length': 262144}}
            report['template_scope'] = set(config) == {'providers', 'secrets'} and set(config['providers']) == {'workbuddy'}
            report['template_references'] = references_preserved(config)
            report['machine_isolated'] = config['secrets']['command']['command'] == '\"<HERMES_PYTHON>\" -I -B \"<INSTALL_ROOT>/wblink.py\" env'
            report['discovery_disabled'] = entry['discover_models'] is False
            config['secrets']['command']['command'] = command
            # Explicit synthetic fixture, not additional release defaults.
            config['providers']['workbuddy']['models'] = {name:{'context_length':size} for name,size in EXPECTED_MODELS.items()}
            # Provider metadata stays unchanged; only runtime side effects are scoped.
            config['model'] = {'provider': 'workbuddy', 'default': 'hy3', 'streaming': True}
            config['agent'] = {'environment_probe': False}
            config['plugins'] = {'enabled': False}
            (root/'config.yaml').write_text(json.dumps(config))
            (root/'config.yaml').chmod(0o600)
            from hermes_constants import set_hermes_home_override
            from agent.secret_scope import set_multiplex_active, set_secret_scope
            set_multiplex_active(True)
            set_hermes_home_override(root)
            set_secret_scope({})
            from agent.redact import register_vault_redaction_value
            for value in (auth.token, auth.uid, auth.domain):
                register_vault_redaction_value(value)
            from hermes_cli.env_loader import hydrate_profile_secret_sources
            guard.helper = True
            try:
                values = hydrate_profile_secret_sources(root)
            finally:
                guard.helper = False
            expected = dict(zip(wblink.KEYS, (auth.token, auth.uid, auth.domain)))
            expected['WBLINK_CLIENT_NAME'] = expected_name
            report['snapshot_loaded'] = values == expected
            from agent.secret_sources.registry import apply_all
            existing = {**dict.fromkeys((*wblink.KEYS, 'WBLINK_CLIENT_NAME'), 'synthetic-old'), 'UNRELATED_SYNTHETIC': 'kept'}
            guard.helper = True
            try:
                apply_all(config['secrets'], root, environ=existing)
            finally:
                guard.helper = False
            report['override_existing'] = existing == {**expected, 'UNRELATED_SYNTHETIC': 'kept'}
            set_secret_scope(values)
            from hermes_cli.config import load_config, save_config
            from hermes_cli.runtime_provider import resolve_runtime_provider
            from run_agent import AIAgent
            from providers import get_provider_profile
            loaded = load_config()
            report['expanded_snapshot'] = (
                loaded['providers']['workbuddy']['api_key'] == auth.token
                and loaded['providers']['workbuddy']['extra_headers'] == expected_headers)
            report['load_keeps_references'] = references_preserved(yaml.safe_load((root/'config.yaml').read_text()))
            save_config(loaded)
            text = (root/'config.yaml').read_text()
            report['save_keeps_references'] = references_preserved(yaml.safe_load(text))
            report['yaml_no_secret_values'] = not any(value in text for value in (auth.token, auth.uid, auth.domain))
            loaded = load_config()
            report['reload_snapshot'] = loaded['providers']['workbuddy']['api_key'] == auth.token
            report['native_models'] = []
            for model, context in EXPECTED_MODELS.items():
                runtime = resolve_runtime_provider(requested='workbuddy', target_model=model)
                route_ok = (runtime['provider'] == 'custom'
                            and runtime['base_url'] == 'https://copilot.tencent.com/v2'
                            and runtime['api_key'] == auth.token
                            and runtime['api_mode'] == 'chat_completions'
                            and runtime['extra_headers'] == expected_headers)
                agent = AIAgent(provider=runtime['provider'], requested_provider=runtime.get('requested_provider'),
                    model=model, api_key=runtime['api_key'], base_url=runtime['base_url'], api_mode=runtime['api_mode'],
                    request_overrides=runtime.get('request_overrides'), enabled_toolsets=[], max_tokens=128,
                    skip_memory=True, skip_context_files=True, skip_background_review=True,
                    load_soul_identity=False, save_trajectories=False, session_db=None,
                    credential_pool=None, fallback_model=[], checkpoints_enabled=False, quiet_mode=True)
                refresh = agent._try_refresh_env_client_credentials()
                # Exercise the native request client without sending any model request.
                client = agent._create_request_openai_client(reason='config_regression', api_kwargs={})
                try:
                    key_ok = client.api_key == auth.token
                    header_ok = all(client.default_headers.get(k) == v for k, v in expected_headers.items())
                finally:
                    client.close()
                report['native_models'].append({
                    'model': agent.model, 'context': agent.context_compressor.context_length,
                    'route_ok': route_ok and get_provider_profile(agent.provider).name == 'custom',
                    'key_ok': key_ok, 'header_ok': header_ok, 'refresh_adopted': bool(refresh),
                    'context_ok': agent.context_compressor.context_length == context})
                agent.close()
                agent = None
            report['status'] = 'success'
        except BaseException as exc:
            report['status'] = 'error'
            report['category'] = type(exc).__name__ if type(exc).__name__ in ('Denied','TypeError','AttributeError','ValueError') else 'operation_failed'
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            if agent is not None:
                try:
                    agent.close()
                except BaseException:
                    report['status'] = 'error'
            report['hard_denials'] = guard.denied
            report['optional_probe_denials'] = guard.optional
    return report


@unittest.skipUnless((SOURCE/'hermes_cli/config.py').is_file() and PYTHON.is_file(), 'Native tests require W2HLINK_TEST_HERMES_ROOT')
class MainConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = run_native()

    def test_final_template_contract(self):
        r = self.report
        self.assertTrue(r['exit_ok'], r)
        for key in ('template_models', 'template_scope', 'template_references', 'machine_isolated', 'discovery_disabled'):
            self.assertTrue(r.get(key), r)

    def test_native_snapshot_and_reference_roundtrip(self):
        r = self.report
        self.assertEqual(r['status'], 'success', r)
        self.assertEqual(r['hard_denials'], 0, r)
        for key in ('snapshot_loaded', 'override_existing', 'expanded_snapshot', 'load_keeps_references',
                    'save_keeps_references', 'yaml_no_secret_values', 'reload_snapshot'):
            self.assertTrue(r.get(key), r)

    def test_literal_models_and_native_context(self):
        r = self.report
        self.assertEqual(r['status'], 'success', r)
        self.assertEqual([x['model'] for x in r['native_models']], list(EXPECTED_MODELS), r)
        for item in r['native_models']:
            self.assertTrue(item['route_ok'] and item['context_ok'] and item['key_ok'] and item['header_ok'], r)

    def test_stale_dotenv_cannot_replace_snapshot(self):
        r = run_native('stale')
        self.assertTrue(r['exit_ok'], r)
        self.assertEqual(r['status'], 'success', r)
        self.assertEqual(r['hard_denials'], 0, r)
        self.assertTrue(r['yaml_no_secret_values'] and r['save_keeps_references'], r)
        for item in r['native_models']:
            self.assertTrue(item['key_ok'] and item['header_ok'], r)
            self.assertFalse(item['refresh_adopted'], r)


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--child':
        print(json.dumps(native_child(sys.argv[2], sys.argv[3])))
    else:
        unittest.main()
