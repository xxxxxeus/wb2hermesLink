"""Synthetic native-config transactions. Never uses the real profile or account."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import w2hlink_cli as cli
import wblink

ACTIVE_GUARD = None
def audit(event, args):
    if ACTIVE_GUARD is not None: ACTIVE_GUARD(event, args)
sys.addaudithook(audit)


@unittest.skipUnless(os.environ.get('W2HLINK_TEST_HERMES_ROOT'), 'Set W2HLINK_TEST_HERMES_ROOT for native synthetic transactions')
class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.test-', dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)/'profile space 中文'
        self.home.mkdir(mode=0o700)
        environment = patch.dict(os.environ, {'HERMES_MANAGED_DIR':str(Path(self.temp.name)/'absent-managed'), 'HERMES_MANAGED':'false', 'HERMES_SKIP_CHMOD':'1'})
        environment.start(); self.addCleanup(environment.stop)
        self.source = Path(os.environ['W2HLINK_TEST_HERMES_ROOT'])
        self.manager = cli.Manager(self.source, self.home)
        self.original = {'model': {'provider': 'other', 'default': 'keep'},
                         'auxiliary': {'monitor': {'provider': 'auto'}},
                         'secrets': {'bitwarden': {'enabled': False}}, 'custom_user_setting': ['keep']}
        (self.home/'config.yaml').write_text(json.dumps(self.original))
        from test_hermes_config import Guard
        original_path = list(sys.path)
        sys.path[:] = [p for p in sys.path if p and Path(p).is_absolute() and Path(p).resolve() not in (ROOT, ROOT/'tests')]
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), original_path))
        global ACTIVE_GUARD
        ACTIVE_GUARD = Guard(Path(self.temp.name), '')
        self.addCleanup(self.clear_guard)

    @staticmethod
    def clear_guard():
        global ACTIVE_GUARD
        ACTIVE_GUARD = None

    def read(self):
        return self.manager.read()[0]

    def test_native_install_models_check_uninstall(self):
        self.manager.install()
        self.manager.change_model('add', 'Example.v1', 131072)
        self.manager.change_model('default', 'Example.v1')
        self.manager.change_model('set', 'hy3', 524288)
        self.manager.install()
        cfg = self.read()
        self.assertEqual(cfg['providers']['workbuddy']['models']['hy3']['context_length'], 524288)
        self.assertEqual(cfg['providers']['workbuddy']['default_model'], 'Example.v1')
        self.assertTrue(self.manager.check()['config_valid'])
        for key, value in self.original.items():
            if key != 'secrets': self.assertEqual(cfg[key], value)
        self.assertEqual(cfg['secrets']['bitwarden'], self.original['secrets']['bitwarden'])
        with self.assertRaises(cli.Error): self.manager.change_model('remove', 'Example.v1')
        self.manager.change_model('default', 'hy3')
        self.manager.change_model('remove', 'Example.v1')
        private = self.manager.root/'.private'
        private.mkdir(mode=0o700)
        (private/'auth.json').write_text('synthetic-preserved')
        self.manager.uninstall()
        self.assertEqual(self.read(), self.original)
        self.assertEqual((private/'auth.json').read_text(), 'synthetic-preserved')

    def test_invalid_yaml_conflict_and_drift(self):
        path = self.home/'config.yaml'
        for raw in ('bad: [', 'providers: 4', 'model: 1\nmodel: 2\n'):
            path.write_text(raw)
            with self.assertRaises(cli.Error): self.manager.install()
            self.assertEqual(path.read_text(), raw)
        path.write_text(json.dumps({**self.original, 'providers': {'workbuddy': {}}}))
        with self.assertRaises(cli.Error): self.manager.install()
        path.write_text(json.dumps(self.original))
        self.manager.install()
        cfg = self.read(); cfg['providers']['workbuddy']['default_model'] = 'manual-edit'
        path.write_text(json.dumps(cfg))
        with self.assertRaises(cli.Error): self.manager.uninstall()
        self.assertEqual(self.read()['providers']['workbuddy']['default_model'], 'manual-edit')

    def test_symlink_and_shared_command_rejected(self):
        path = self.home/'config.yaml'
        path.write_text(json.dumps({**self.original,'secrets':{'command':{'command':'unrelated-helper'}}}))
        before = path.read_bytes()
        with self.assertRaises(cli.Error): self.manager.install()
        self.assertEqual(path.read_bytes(), before)
        path.unlink(); path.symlink_to(self.home/'elsewhere')
        with self.assertRaises(cli.Error): self.manager.install()

    def test_failed_install_rolls_back_and_recovers(self):
        with patch.object(self.manager, '_write_receipt', side_effect=OSError()):
            with self.assertRaises(OSError): self.manager.install()
        self.assertEqual(self.read(), self.original)
        self.assertFalse(self.manager.receipt.exists())
        self.assertTrue(all(not (self.manager.root/n).exists() for n in cli.PUBLIC_RUNTIME))
        self.manager.install()
        self.manager.uninstall()

    def test_reference_protection_and_uninstall_resume(self):
        self.manager.install()
        cfg = self.read(); cfg['fallback_model'] = [{'provider':'workbuddy','model':'hy3'}]
        (self.home/'config.yaml').write_text(json.dumps(cfg))
        with self.assertRaises(cli.Error): self.manager.uninstall()
        del cfg['fallback_model']; (self.home/'config.yaml').write_text(json.dumps(cfg))
        receipt = self.manager.verify_owned(cfg)
        empty = self.manager.put(cfg, {'provider':None,'command':None}, receipt['empty_parents'])
        self.manager.transaction(cfg, self.manager.read()[1], empty, receipt, {**receipt,'state':'removed'})
        (self.manager.root/'w2hlink').unlink()
        self.manager.uninstall()
        self.assertEqual(self.read(), self.original)

    def test_interruption_recovery_and_concurrent_edit(self):
        self.manager.install()
        before = copy.deepcopy(self.read())
        with patch.object(self.manager, '_write_receipt', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.manager.change_model('add', 'new.id', 64000)
        self.manager.recover()
        self.assertEqual(self.read(), before)
        cfg, signature = self.manager.read()
        (self.home/'config.yaml').write_text(json.dumps({**cfg, 'concurrent': True}))
        with self.assertRaises(cli.Error): self.manager._save(cfg, signature)
        self.assertTrue(self.read()['concurrent'])

    def test_platform_gate_and_help(self):
        with patch.object(cli.platform, 'system', return_value='Windows'), patch.object(cli, 'Manager', side_effect=AssertionError):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['status']), 1)
                self.assertEqual(cli.main(['--version']), 0)

    def test_managed_policy_refused_without_environment_override(self):
        managed = Path(self.temp.name)/'managed'; managed.mkdir()
        before = (self.home/'config.yaml').read_bytes()
        with patch.dict(os.environ, {'HERMES_MANAGED_DIR':str(managed)}):
            with self.assertRaises(cli.Error): self.manager.install()
            self.assertEqual(os.environ['HERMES_MANAGED_DIR'],str(managed))
            with self.assertRaises(cli.Error): self.manager.check()
        with patch.dict(os.environ, {'HERMES_MANAGED':'nix'}):
            with self.assertRaises(cli.Error): self.manager._save(self.original,cli.digest(self.home/'config.yaml'))
        (self.home/'.managed').write_text('synthetic marker, never parsed')
        with self.assertRaises(cli.Error): self.manager.install()
        self.assertEqual((self.home/'config.yaml').read_bytes(),before)

    def test_check_rejects_drift_and_pending_read_only(self):
        self.manager.install()
        path = self.manager.root/'w2hlink_cli.py'; original = path.read_bytes()
        path.write_bytes(original+b'\n# synthetic drift\n')
        with self.assertRaises(cli.Error): self.manager.check()
        path.write_bytes(original)
        cfg = self.read(); changed = copy.deepcopy(cfg)
        changed['providers']['workbuddy']['models']['hy3']['context_length'] = 524288
        (self.home/'config.yaml').write_text(json.dumps(changed))
        with self.assertRaises(cli.Error): self.manager.check()
        (self.home/'config.yaml').write_text(json.dumps(cfg))
        receipt = self.manager.receipt.read_bytes()
        self.manager.receipt.write_text('{}')
        with self.assertRaises(cli.Error): self.manager.check()
        self.manager.receipt.write_bytes(receipt)
        self.manager.journal.write_text('{}')
        for receipt_present in (True,False):
            if not receipt_present: self.manager.receipt.unlink()
            with self.assertRaises(cli.Error): self.manager.check()
            self.assertEqual(self.manager.journal.read_text(),'{}')
        self.manager.journal.unlink()
        self.assertFalse(self.manager.check()['owned'])

    def test_only_command_source_loads_four_snapshot_fields(self):
        self.manager.install()
        cfg = self.read(); cfg['secrets']['bitwarden']['enabled'] = True
        (self.home/'config.yaml').write_text(json.dumps(cfg))
        auth = wblink.Auth('synthetic-command-token','synthetic-command.invalid','synthetic-command-uid')
        private = self.manager.root/'.private'; private.mkdir(mode=0o700)
        snapshot = private/'auth.json'
        snapshot.write_text(json.dumps({'schema_version':1,'token':auth.token,'domain':auth.domain,'uid':auth.uid}))
        snapshot.chmod(0o600)
        ACTIVE_GUARD.command = cfg['secrets']['command']['command']; ACTIVE_GUARD.helper = True
        try:
            with patch('hermes_cli.env_loader.hydrate_profile_secret_sources',side_effect=AssertionError('other_source')), patch('agent.secret_sources.registry.apply_all',side_effect=AssertionError('other_source')):
                values = cli.load_command_auth(self.manager,cfg)
        finally: ACTIVE_GUARD.helper = False
        self.assertEqual(set(values),set(wblink.KEYS)|{'WBLINK_CLIENT_NAME'})
        self.assertEqual([values[k] for k in wblink.KEYS],[auth.token,auth.uid,auth.domain])
        self.assertEqual(values['WBLINK_CLIENT_NAME'],wblink.client_name())
        # Exercise the same native client preparation used by check model. A
        # BaseException sentinel cannot be swallowed as a provider fallback.
        class ForbiddenCredentialAccess(BaseException): pass
        names = (
            'hermes_cli.auth._load_auth_store', 'hermes_cli.auth._load_global_auth_store',
            'hermes_cli.auth._save_auth_store', 'hermes_cli.auth.read_credential_pool',
            'hermes_cli.auth.write_credential_pool', 'agent.credential_pool.load_pool',
            'agent.credential_pool.persist_pool_entries',
            'hermes_cli.env_loader.hydrate_profile_secret_sources',
            'agent.secret_sources.registry.apply_all',
        )
        from agent.secret_sources.command import CommandSource
        native_fetch = CommandSource.fetch
        fetch_count = []
        def observed_fetch(source, *args, **kwargs):
            fetch_count.append(True)
            ACTIVE_GUARD.helper = True
            try: return native_fetch(source, *args, **kwargs)
            finally: ACTIVE_GUARD.helper = False
        client = None
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(CommandSource,'fetch',new=observed_fetch))
                sentinels = [stack.enter_context(patch(name,side_effect=ForbiddenCredentialAccess)) for name in names]
                client, model, headers = cli.prepare_model_client(self.manager,'hy3')
                self.assertEqual(model,'hy3')
                self.assertEqual(str(client.base_url),'https://copilot.tencent.com/v2/')
                self.assertEqual(client.api_key,auth.token)
                expected_headers = {'X-User-Id':auth.uid,'X-Domain':auth.domain,'X-Product':'SaaS',
                                    'X-IDE-Type':'Hermes','X-IDE-Name':values['WBLINK_CLIENT_NAME']}
                self.assertEqual(headers,expected_headers)
                for key,value in expected_headers.items(): self.assertEqual(client.default_headers[key],value)
                self.assertEqual(client.max_retries,0)
                self.assertTrue(all(s.call_count == 0 for s in sentinels))
                self.assertEqual(len(fetch_count),1)
        finally:
            if client is not None: client.close()
            ACTIVE_GUARD.helper = False


if __name__ == '__main__': unittest.main()
