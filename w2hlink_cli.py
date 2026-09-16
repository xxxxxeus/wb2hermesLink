"""macOS installation and profile-local management for wb2hermesLink."""
import argparse
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import signal
import stat
import sys
import tempfile
import unicodedata

VERSION = '0.1.0'
PUBLIC_RUNTIME = ('w2hlink', 'w2hlink_cli.py', 'wblink.py')
HERE = Path(__file__).resolve().parent


class Error(Exception):
    pass


def safe_path(path):
    path = Path(path).expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise Error('symlink_path')
    return path


def digest(path):
    path = safe_path(path)
    if not path.exists(): return None
    if not path.is_file() or path.stat().st_nlink != 1: raise Error('not_regular_file')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def model_id(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(unicodedata.category(c).startswith('C') for c in value):
        raise Error('invalid_model_id')
    return value


def atomic_json(path, value):
    safe_path(path)
    fd, temporary = tempfile.mkstemp(prefix='.w2h-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as target:
            os.fchmod(target.fileno(), 0o600)
            json.dump(value, target, ensure_ascii=True)
            target.flush(); os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


class Manager:
    def __init__(self, hermes_root, hermes_home):
        if platform.system() != 'Darwin': raise Error('unsupported_platform_macos_only')
        self.source = Path(hermes_root).expanduser().absolute()
        if not (self.source/'hermes_cli/config.py').is_file() or not (self.source/'venv/bin/python').is_file():
            raise Error('hermes_installation_not_found_use_hermes_root')
        self.home = safe_path(hermes_home)
        self.config = self.home/'config.yaml'
        self.root = self.home/'integrations/w2hlink'
        self.receipt = self.root/'receipt.json'
        self.journal = self.root/'pending.json'
        self.lock = self.home/'.w2hlink.lock'

    def native(self):
        if str(self.source) not in sys.path: sys.path.insert(0, str(self.source))
        from hermes_constants import set_hermes_home_override
        from agent.secret_scope import set_multiplex_active, set_secret_scope
        set_multiplex_active(True); set_hermes_home_override(self.home); set_secret_scope({})
        from hermes_cli.managed_scope import get_managed_dir
        # Refuse managed scopes without reading administrator config or secrets.
        if get_managed_dir() is not None or (self.home/'.managed').exists():
            raise Error('managed_configuration_unsupported')
        from hermes_cli import config
        if config.is_managed(): raise Error('managed_configuration_unsupported')
        return config

    def read(self):
        self.native()
        safe_path(self.config)
        signature = digest(self.config)
        if signature is None: return {}, signature
        if self.config.stat().st_size > 1048576: raise Error('config_too_large')
        import yaml
        class Strict(yaml.SafeLoader): pass
        def mapping(loader, node):
            result = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=True)
                if not isinstance(key, str) or key in result: raise Error('invalid_config')
                result[key] = loader.construct_object(value_node, deep=True)
            return result
        Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
        try:
            value = yaml.load(self.config.read_text(), Loader=Strict)
            if value is None: value = {}
            if not isinstance(value, dict): raise Error('invalid_config')
            json.dumps(value)  # Reject recursive/non-JSON YAML before native fallback.
        except (yaml.YAMLError, ValueError, TypeError, RecursionError): raise Error('invalid_config') from None
        for key in ('providers', 'secrets'):
            if key in value and not isinstance(value[key], dict): raise Error('invalid_config')
        native = self.native().read_raw_config()
        if native != value or digest(self.config) != signature: raise Error('config_changed')
        return value, signature

    def _save(self, value, signature):
        native = self.native()
        issues = native.validate_config_structure(value)
        if any(getattr(item, 'severity', '') == 'error' for item in issues): raise Error('native_config_invalid')
        if digest(self.config) != signature: raise Error('config_changed')
        # Hermes' own atomic YAML writer preserves unrelated raw settings without
        # canonicalizing user data or expanding secret references.
        from utils import atomic_yaml_write
        atomic_yaml_write(self.config, value)
        self.config.chmod(0o600)
        actual, _ = self.read()
        if actual != value: raise Error('config_write_mismatch')

    def fragment(self):
        command = shlex.join([str(self.source/'venv/bin/python'), '-I', '-B', str(self.root/'wblink.py'), 'env'])
        return {
            'provider': {'name': 'WorkBuddy', 'base_url': 'https://copilot.tencent.com/v2',
                'transport': 'chat_completions', 'default_model': 'hy3', 'api_key': '${WBLINK_ACCESS_TOKEN}',
                'extra_headers': {'X-User-Id': '${WBLINK_USER_ID_SECRET}', 'X-Domain': '${WBLINK_DOMAIN_SECRET}',
                                  'X-Product': 'SaaS', 'X-IDE-Type': 'Hermes', 'X-IDE-Name': '${WBLINK_CLIENT_NAME}'},
                'discover_models': False, 'models': {'hy3': {'context_length': 262144}}},
            'command': {'enabled': True, 'command': command, 'override_existing': True, 'helper_timeout_seconds': 5}}

    @staticmethod
    def owned(config):
        return {'provider': config.get('providers', {}).get('workbuddy'),
                'command': config.get('secrets', {}).get('command')}

    @staticmethod
    def put(config, owned, empty_parents=()):
        result = copy.deepcopy(config)
        for parent, key, part in (('providers', 'workbuddy', 'provider'), ('secrets', 'command', 'command')):
            if owned[part] is None:
                if parent in result: result[parent].pop(key, None)
                if parent in empty_parents and not result.get(parent): result.pop(parent, None)
            else: result.setdefault(parent, {})[key] = copy.deepcopy(owned[part])
        return result

    def _record(self, path):
        if digest(path) is None: return None
        if path.stat().st_size > 262144 or path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) != 0o600: raise Error('receipt_invalid')
        try:
            value = json.loads(path.read_text())
        except (ValueError, UnicodeError): raise Error('receipt_invalid') from None
        if not isinstance(value, dict) or value.get('version') != VERSION: raise Error('receipt_invalid')
        return value

    def _write_receipt(self, receipt):
        atomic_json(self.receipt, receipt)

    def recover(self):
        journal = self._record(self.journal)
        if journal is None: return
        cfg, signature = self.read()
        if self.owned(cfg) not in (journal['before'], journal['after']): raise Error('pending_config_conflict')
        self._save(self.put(cfg, journal['before'], journal['empty_parents']), signature)
        if journal['previous'] is None:
            if self.receipt.exists(): self.receipt.unlink()
            for name, checksum in journal.get('created_files', {}).items():
                if name not in PUBLIC_RUNTIME: raise Error('receipt_invalid')
                actual = digest(self.root/name)
                if actual is not None:
                    if actual != checksum: raise Error('runtime_file_drift')
                    (self.root/name).unlink()
        else: self._write_receipt(journal['previous'])
        self.journal.unlink()

    @contextlib.contextmanager
    def locked(self):
        self.native()
        safe_path(self.home); self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        safe_path(self.lock)
        try: fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError: raise Error('operation_locked_remove_stale_lock_only_after_confirming_no_running_operation') from None
        try:
            self.recover()
            yield
        finally:
            os.close(fd); self.lock.unlink()

    def transaction(self, cfg, signature, desired, previous, receipt):
        journal = {'version': VERSION, 'before': self.owned(cfg), 'after': self.owned(desired),
                   'previous': previous, 'empty_parents': [k for k in ('providers','secrets') if k not in cfg],
                   'created_files': receipt['files'] if previous is None else {}}
        atomic_json(self.journal, journal)
        try:
            self._save(desired, signature)
            self._write_receipt(receipt)
            self.journal.unlink()
        except Exception:
            self.recover()
            raise

    def verify_owned(self, cfg):
        receipt = self._record(self.receipt)
        if receipt is None or receipt.get('state') != 'installed': raise Error('not_owned_manual_install_conflict')
        if self.owned(cfg) != receipt['owned']: raise Error('configuration_drift')
        if receipt.get('hermes_root') != str(self.source): raise Error('hermes_root_mismatch')
        if set(receipt.get('files', {})) != set(PUBLIC_RUNTIME): raise Error('receipt_invalid')
        for name, checksum in receipt['files'].items():
            if digest(self.root/name) != checksum: raise Error('runtime_file_drift')
        return receipt

    def install(self):
        with self.locked():
            cfg, signature = self.read()
            if self.receipt.exists():
                self.verify_owned(cfg)
                return {'status': 'already_installed'}
            if 'workbuddy' in cfg.get('providers', {}) or 'command' in cfg.get('secrets', {}): raise Error('configuration_conflict')
            safe_path(self.root)
            if self.root.exists() and any(p.name != '.private' for p in self.root.iterdir()): raise Error('installation_directory_conflict')
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            files = {name: digest(HERE/name) for name in PUBLIC_RUNTIME}
            if not all(files.values()): raise Error('distribution_incomplete')
            owned = self.fragment()
            receipt = {'version': VERSION, 'state': 'installed', 'owned': owned, 'files': files,
                       'hermes_root': str(self.source), 'empty_parents': [k for k in ('providers','secrets') if k not in cfg]}
            atomic_json(self.journal, {'version':VERSION,'before':self.owned(cfg),'after':owned,'previous':None,
                                      'empty_parents':receipt['empty_parents'],'created_files':files})
            try:
                for name in PUBLIC_RUNTIME:
                    target = self.root/name
                    safe_path(target)
                    # Atomic copying leaves either a full verified file or no target.
                    fd, temporary = tempfile.mkstemp(prefix='.runtime-', dir=self.root)
                    try:
                        with os.fdopen(fd, 'wb') as output:
                            output.write((HERE/name).read_bytes())
                            output.flush(); os.fsync(output.fileno())
                        os.chmod(temporary, 0o755 if name == 'w2hlink' else 0o644)
                        os.replace(temporary, target)
                    finally:
                        if os.path.exists(temporary): os.unlink(temporary)
                self.transaction(cfg, signature, self.put(cfg, owned), None, receipt)
            except Exception:
                self.recover()
                raise
            return {'status': 'installed', 'restart_hermes': True, 'login_performed': False}

    def change_model(self, action, name, context=None):
        model_id(name)
        with self.locked():
            cfg, signature = self.read(); receipt = self.verify_owned(cfg)
            desired = copy.deepcopy(cfg); entry = desired['providers']['workbuddy']; models = entry['models']
            if action in ('add', 'set'):
                if type(context) is not int or context < 64000: raise Error('context_length_minimum_64000')
                if (action == 'add') == (name in models): raise Error('model_exists' if action == 'add' else 'model_missing')
                models[name] = {'context_length': context}
            elif action == 'default':
                if name not in models: raise Error('model_missing')
                entry['default_model'] = name
            elif action == 'remove':
                if name not in models: raise Error('model_missing')
                if name == entry['default_model'] or self.referenced(cfg, name): raise Error('model_referenced')
                del models[name]
            else: raise Error('arguments')
            after = {**receipt, 'owned': self.owned(desired)}
            self.transaction(cfg, signature, desired, receipt, after)
            return {'status': 'updated', 'restart_hermes': True}

    def referenced(self, cfg, model=None):
        other = self.put(cfg, {'provider': None, 'command': None})
        def contains(value):
            if isinstance(value, dict): return any(contains(v) for v in value.values())
            if isinstance(value, list): return any(contains(v) for v in value)
            return isinstance(value, str) and (value == model or 'workbuddy' in value.lower())
        return contains(other)

    def uninstall(self):
        with self.locked():
            cfg, signature = self.read(); receipt = self._record(self.receipt)
            if receipt and receipt.get('state') == 'removed':
                if self.owned(cfg) != {'provider':None,'command':None} or set(receipt.get('files',{})) != set(PUBLIC_RUNTIME): raise Error('configuration_drift')
            else:
                receipt = self.verify_owned(cfg)
                if self.referenced(cfg): raise Error('provider_referenced')
                desired = self.put(cfg, {'provider': None, 'command': None}, receipt['empty_parents'])
                self.transaction(cfg, signature, desired, receipt, {**receipt, 'state': 'removed'})
            for name in PUBLIC_RUNTIME:
                actual = digest(self.root/name)
                if actual is not None:
                    if actual != receipt['files'][name]: raise Error('runtime_file_drift')
                    (self.root/name).unlink()
            self.receipt.unlink()
            return {'status': 'uninstalled', 'credentials_retained': True, 'restart_hermes': True}

    def check(self):
        if self.journal.exists() or self.journal.is_symlink(): raise Error('pending_transaction_requires_management_recovery')
        cfg, _ = self.read(); entry = cfg.get('providers', {}).get('workbuddy')
        owned = self.receipt.exists() or self.receipt.is_symlink()
        if owned: self.verify_owned(cfg)
        if not isinstance(entry, dict): raise Error('provider_missing')
        for key in ('api_key', 'extra_headers', 'base_url', 'transport', 'discover_models'):
            if entry.get(key) != self.fragment()['provider'][key]: raise Error('provider_contract_mismatch')
        models = entry.get('models')
        if not isinstance(models, dict) or not models or entry.get('default_model') not in models: raise Error('models_invalid')
        for name, settings in models.items():
            model_id(name)
            if not isinstance(settings, dict) or type(settings.get('context_length')) is not int or settings['context_length'] < 64000: raise Error('context_invalid')
        command = cfg.get('secrets', {}).get('command')
        if not isinstance(command, dict) or command.get('enabled') is not True or command.get('override_existing') is not True: raise Error('secret_source_invalid')
        try: argv = shlex.split(command.get('command', ''))
        except ValueError: raise Error('secret_source_invalid') from None
        if len(argv) != 5 or argv[0] != str(self.source/'venv/bin/python') or argv[1:3] != ['-I','-B'] or argv[4] != 'env': raise Error('secret_source_not_exact_helper')
        helper = safe_path(argv[3])
        if helper.name != 'wblink.py' or digest(helper) != digest(HERE/'wblink.py'): raise Error('helper_version_mismatch')
        if any(getattr(i, 'severity', '') == 'error' for i in self.native().validate_config_structure(cfg)): raise Error('native_config_invalid')
        return {'status': 'ready', 'config_valid': True, 'owned': owned, 'server_validity_known': False}


def parser():
    p = argparse.ArgumentParser(prog='w2hlink', description='wb2hermesLink: macOS only; native Windows is unsupported.')
    p.add_argument('--version', action='version', version='wb2hermesLink '+VERSION)
    p.add_argument('--hermes-root', default=os.environ.get('W2HLINK_HERMES_ROOT', str(Path.home()/'.hermes/hermes-agent')))
    p.add_argument('--hermes-home', default=os.environ.get('HERMES_HOME', str(Path.home()/'.hermes')))
    commands = p.add_subparsers(dest='command', required=True)
    for name in ('install','uninstall'):
        sub = commands.add_parser(name); sub.add_argument('--yes', action='store_true')
    commands.add_parser('status'); commands.add_parser('login')
    models = commands.add_parser('models').add_subparsers(dest='action', required=True)
    models.add_parser('list')
    for name in ('add','set','default','remove'):
        sub = models.add_parser(name); sub.add_argument('model')
        if name in ('add','set'): sub.add_argument('--context-length', type=int, required=True)
    check = commands.add_parser('check').add_subparsers(dest='kind', required=True)
    check.add_parser('config')
    sub = check.add_parser('model', help='One real request; may incur upstream charges.')
    sub.add_argument('--model')
    return p


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ['--version']:
        print('wb2hermesLink '+VERSION); return 0
    if not args or args == ['--help']:
        parser().print_help(); return 0
    if platform.system() != 'Darwin':
        print(json.dumps({'status':'error','error_category':'unsupported_platform_macos_only'})); return 1
    try:
        ns = parser().parse_args(args)
        manager = Manager(ns.hermes_root, ns.hermes_home)
        if ns.command in ('install','uninstall') and not ns.yes:
            print('Only providers.workbuddy, its dedicated secrets.command and owned integration files will change. Default/auxiliary and credentials are preserved.')
            if input('Continue? [y/N] ').strip().lower() != 'y': raise Error('cancelled')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            if ns.command == 'install': result = manager.install()
            elif ns.command == 'uninstall': result = manager.uninstall()
            elif ns.command == 'models':
                if ns.action == 'list':
                    cfg, _ = manager.read(); entry = cfg.get('providers', {}).get('workbuddy', {})
                    result = {'status':'ok','models':entry.get('models', {}),'provider_default':entry.get('default_model')}
                else: result = manager.change_model(ns.action, ns.model, getattr(ns,'context_length',None))
            elif ns.command == 'check' and ns.kind == 'config': result = manager.check()
            elif ns.command == 'check': result = check_model(manager, ns.model)
            elif ns.command == 'login':
                cfg, _ = manager.read(); manager.verify_owned(cfg)
                import importlib.util
                spec = importlib.util.spec_from_file_location('w2hlink_installed_helper', manager.root/'wblink.py')
                module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module; spec.loader.exec_module(module)
                auth = module.authorize(); module.save_snapshot(auth)
                result = {'status':'authorized','restart_hermes':True}
            else:
                cfg, _ = manager.read()
                result = {'status':'local','provider_present':'workbuddy' in cfg.get('providers', {}),
                          'owned':manager.receipt.exists(),'server_validity_known':False}
                if manager.receipt.exists(): manager.verify_owned(cfg)
                import wblink
                snapshot = manager.root/'.private/auth.json'
                present = snapshot.exists() or snapshot.is_symlink()
                result['credential_present'] = present
                result['credential_structure_permissions_valid'] = False
                if present: wblink.load_snapshot(snapshot); result['credential_structure_permissions_valid'] = True
        print(json.dumps(result, ensure_ascii=True)); return 0
    except (Exception, KeyboardInterrupt) as exc:
        category = str(exc) if isinstance(exc, Error) else 'cancelled' if isinstance(exc, KeyboardInterrupt) else 'operation_failed'
        print(json.dumps({'status':'error','error_category':category})); return 1


def load_command_auth(manager, cfg):
    from agent.secret_sources.command import CommandSource
    from agent.secret_sources.base import set_source_environment, reset_source_environment
    # The verified absolute command needs no inherited profile or secret sources.
    scope = set_source_environment({'PATH':'/usr/bin:/bin','HOME':str(manager.home)})
    try:
        result = CommandSource().fetch(cfg['secrets']['command'], manager.home)
    finally:
        reset_source_environment(scope)
    keys = {'WBLINK_ACCESS_TOKEN','WBLINK_USER_ID_SECRET','WBLINK_DOMAIN_SECRET','WBLINK_CLIENT_NAME'}
    if result.error or set(result.secrets) != keys or any(not isinstance(v,str) or not v for v in result.secrets.values()):
        raise Error('saved_authentication_unavailable_login_manually')
    return result.secrets


def prepare_model_client(manager, model=None):
    manager.check()
    cfg, _ = manager.read(); entry = cfg['providers']['workbuddy']
    model = model or entry['default_model']
    if model not in entry['models']: raise Error('model_missing')
    from agent.secret_scope import set_secret_scope
    values = load_command_auth(manager, cfg)
    from agent.redact import register_vault_redaction_value
    for key in ('WBLINK_ACCESS_TOKEN','WBLINK_USER_ID_SECRET','WBLINK_DOMAIN_SECRET'):
        value = values.get(key)
        if not isinstance(value, str) or not value: raise Error('saved_authentication_unavailable_login_manually')
        register_vault_redaction_value(value)
    set_secret_scope(values)
    from hermes_cli.runtime_provider import _get_named_custom_provider
    from agent.auxiliary_client import resolve_provider_client
    named = _get_named_custom_provider('custom:workbuddy')
    headers = {'X-User-Id':values['WBLINK_USER_ID_SECRET'], 'X-Domain':values['WBLINK_DOMAIN_SECRET'],
               'X-Product':'SaaS', 'X-IDE-Type':'Hermes', 'X-IDE-Name':values['WBLINK_CLIENT_NAME']}
    if (not named or named.get('base_url') != entry['base_url']
            or named.get('api_key') != values['WBLINK_ACCESS_TOKEN']
            or named.get('api_mode') != 'chat_completions'
            or named.get('extra_headers') != headers): raise Error('runtime_route_mismatch')
    # Explicit credentials select the native named-custom branch without the
    # generic runtime resolver's credential-pool discovery or persistence.
    client, resolved = resolve_provider_client('custom:workbuddy', model=model, async_mode=False,
        explicit_api_key=values['WBLINK_ACCESS_TOKEN'], api_mode='chat_completions')
    from openai import OpenAI
    if (not isinstance(client, OpenAI) or resolved != model
            or str(client.base_url).rstrip('/') != entry['base_url']
            or client.api_key != values['WBLINK_ACCESS_TOKEN']):
        if client is not None: client.close()
        raise Error('runtime_client_mismatch')
    client = client.with_options(default_headers=named['extra_headers'], max_retries=0, timeout=45)
    client._client.follow_redirects = False
    if any(client.default_headers.get(k) != v for k,v in headers.items()):
        client.close(); raise Error('runtime_client_mismatch')
    return client, model, headers


def check_model(manager, model=None):
    client, model, headers = prepare_model_client(manager, model)
    old_handler = signal.getsignal(signal.SIGALRM)
    if signal.getitimer(signal.ITIMER_REAL) != (0.0,0.0): raise Error('timer_busy')
    def expire(*args): raise Error('model_timeout')
    signal.signal(signal.SIGALRM, expire); signal.alarm(60)
    try:
        text = ''; finish = None
        stream = client.chat.completions.create(model=model, messages=[{'role':'user','content':'Reply only OK.'}],
            stream=True, max_tokens=1024, extra_headers=headers)
        try:
            for chunk in stream:
                for choice in chunk.choices:
                    text += choice.delta.content or ''
                    if len(text) > 16384: raise Error('response_size')
                    if choice.finish_reason: finish = choice.finish_reason
        finally: stream.close()
        if finish != 'stop' or not text.strip(): raise Error('incomplete_model_response')
        return {'status':'success','model':model,'request_attempts':1,'normal_final_text':True,'reply_is_OK':text.strip()=='OK'}
    except Exception as exc:
        if isinstance(exc, Error): raise
        code = getattr(exc, 'status_code', None)
        raise Error('authentication_rejected_login_manually' if code in (401,403) else 'model_request_failed') from None
    finally:
        signal.alarm(0); signal.signal(signal.SIGALRM, old_handler); client.close()


if __name__ == '__main__':
    sys.path.insert(0, str(HERE))
    sys.exit(main())
