"""WorkBuddy authorization helper. Default/help never read credentials or use network."""
import ast
import contextlib
from dataclasses import dataclass
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlencode, urlsplit

HOST = 'copilot.tencent.com'
ROOT = Path(__file__).resolve().parent
SNAPSHOT = ROOT / '.private/auth.json'
LIMIT = 65536
KEYS = ('WBLINK_ACCESS_TOKEN', 'WBLINK_USER_ID_SECRET', 'WBLINK_DOMAIN_SECRET')


class Error(Exception):
    def __init__(self, category):
        self.category = category
        super().__init__(category)


@dataclass(frozen=True, repr=False)
class Auth:
    token: str
    domain: str
    uid: str


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise Error('schema')
            result[key] = value
        return result
    def invalid(value): raise Error('schema')
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError):
        raise Error('schema') from None


def field(value):
    # Machine source parsing strips whitespace and surrounding quotes; reject
    # ambiguous values instead of changing an authorization snapshot in transit.
    if (not isinstance(value, str) or not 0 < len(value) <= 16384
            or any(ord(c) < 33 or ord(c) > 126 or c in "\"'" for c in value)):
        raise Error('schema')
    return value


def validate(value):
    if (not isinstance(value, dict) or set(value) != {'schema_version','token','domain','uid'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1):
        raise Error('schema')
    return Auth(*(field(value[k]) for k in ('token','domain','uid')))


def snapshot(auth):
    value = dict(schema_version=1, token=auth.token, domain=auth.domain, uid=auth.uid)
    validate(value)
    return value


def client_name():
    # The configured helper runs in Hermes' own venv. Read only its version
    # literal: never import Hermes or invoke its CLI during secret hydration.
    source = Path(sys.prefix).parent / 'hermes_cli/__init__.py'
    try:
        with source.open('rb') as stream:
            raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            return 'HermesAgent'
        tree = ast.parse(raw.decode('utf-8'))
        for node in tree.body:
            if (isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == '__version__' for t in node.targets)
                    and isinstance(node.value, ast.Constant)):
                version = node.value.value
                if (isinstance(version, str) and len(version) <= 128
                        and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?', version)):
                    return 'HermesAgent v' + version
                break
    except (OSError, UnicodeError, SyntaxError, ValueError, RecursionError):
        pass
    return 'HermesAgent'


def environment(auth):
    snapshot(auth)
    return (''.join(k+'='+v+'\n' for k,v in zip(KEYS,(auth.token,auth.uid,auth.domain)))
            + 'WBLINK_CLIENT_NAME=' + client_name() + '\n')


def private_dir(path, create=False):
    path = Path(path).absolute()
    for component in (path, *path.parents):
        if component.is_symlink(): raise Error('storage_permissions')
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    try:
        meta = path.lstat()
        if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid() or stat.S_IMODE(meta.st_mode) != 0o700:
            raise Error('storage_permissions')
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise Error('storage_permissions') from None


def check_file(meta):
    if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
            or stat.S_IMODE(meta.st_mode) != 0o600 or meta.st_nlink != 1):
        raise Error('storage_permissions')
    if meta.st_size > LIMIT: raise Error('response_size')


def load_snapshot(path=SNAPSHOT):
    path = Path(path)
    directory = private_dir(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as source:
            check_file(os.fstat(source.fileno()))
            raw = source.read(LIMIT+1)
            if len(raw)>LIMIT: raise Error('response_size')
        return validate(strict_json(raw))
    except OSError:
        raise Error('storage_read') from None
    finally:
        os.close(directory)


def save_snapshot(auth, path=SNAPSHOT):
    raw = json.dumps(snapshot(auth), ensure_ascii=True).encode('ascii')
    if len(raw)>LIMIT: raise Error('response_size')
    path = Path(path)
    directory = private_dir(path.parent, create=True)
    temporary = None
    try:
        try: check_file(os.stat(path.name, dir_fd=directory, follow_symlinks=False))
        except FileNotFoundError: pass
        # The directory is private; openat and same-directory replace preserve
        # the previous snapshot if serialization or writing fails.
        import secrets
        temporary = '.auth-' + secrets.token_hex(12)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, 'wb') as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(raw); target.flush(); os.fsync(target.fileno())
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        temporary = None
    except OSError:
        raise Error('storage_write') from None
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError): os.unlink(temporary, dir_fd=directory)
        os.close(directory)


def validate_url(url):
    if not isinstance(url,str) or len(url)>16384 or '\\' in url or any(ord(c)<33 or ord(c)>126 for c in url):
        raise Error('auth_url')
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.hostname != HOST or parsed.port not in (None,443)
                or parsed.username is not None or parsed.password is not None): raise ValueError()
    except ValueError: raise Error('auth_url') from None
    return url


def remaining(deadline):
    value = deadline-time.monotonic()
    if value<=0: raise Error('timeout')
    return value


def open_browser(url, deadline):
    validate_url(url)
    script = "ObjC.import('AppKit'); Boolean($.NSWorkspace.sharedWorkspace.openURL($.NSURL.URLWithString("+json.dumps(url)+")));"
    try:
        child = subprocess.run(['/usr/bin/osascript','-l','JavaScript'], input=script,
                               text=True,capture_output=True,timeout=min(10,remaining(deadline)))
        if child.returncode or child.stdout.strip()!='true': raise Error('browser')
    except (OSError,subprocess.SubprocessError): raise Error('browser') from None
    remaining(deadline)


def auth_http(stage, deadline, state=None, auth=None):
    if stage not in ('state','token','account'): raise Error('arguments')
    if stage!='state': field(state)
    if stage=='account': snapshot(auth)
    if threading.current_thread() is not threading.main_thread() or signal.getitimer(signal.ITIMER_REAL)!=(0.0,0.0):
        raise Error('timer')
    end = min(deadline,time.monotonic()+30)
    path = '/v2/plugin/auth/state?platform=workbuddy' if stage=='state' else '/v2/plugin/'+('auth/token' if stage=='token' else 'login/account')+'?'+urlencode({'state':state})
    headers = {'X-Product':'SaaS','X-Domain':HOST,'Accept':'application/json','Accept-Encoding':'identity','Connection':'close'}
    if stage=='account': headers.update({'Authorization':'Bearer '+auth.token,'X-Domain':auth.domain})
    if stage=='state': headers['Content-Type']='application/json'
    connection = response = None
    old = signal.getsignal(signal.SIGALRM)
    def expire(*args): raise Error('timeout')
    signal.signal(signal.SIGALRM,expire)
    try:
        timeout=min(10,remaining(end)); signal.setitimer(signal.ITIMER_REAL,timeout)
        connection=http.client.HTTPSConnection(HOST,443,timeout=timeout,context=ssl.create_default_context())
        connection.connect()
        signal.setitimer(signal.ITIMER_REAL,remaining(end)); connection.sock.settimeout(remaining(end))
        connection.request('POST' if stage=='state' else 'GET',path,body=b'{}' if stage=='state' else None,headers=headers)
        response=connection.getresponse()
        if 300<=response.status<400: raise Error('redirect')
        if response.status in (401,403): raise Error('authentication')
        lengths=response.headers.get_all('Content-Length',[])
        size=LIMIT-sum(len(k)+len(v)+4 for k,v in response.getheaders())-32
        if (size<0 or len(lengths)>1 or lengths and response.getheader('Transfer-Encoding')
                or response.getheader('Content-Encoding','identity').lower()!='identity'
                or response.getheader('Content-Type','').split(';')[0].strip().lower()!='application/json'):
            raise Error('response_headers')
        if lengths and (not lengths[0].isdigit() or len(lengths[0])>8 or int(lengths[0])>size): raise Error('response_size')
        raw=response.read(size+1)
        if len(raw)>size: raise Error('response_size')
        value=strict_json(raw)
        if not isinstance(value,dict) or type(value.get('code')) is not int: raise Error('schema')
        pending={'token':11217,'account':12151}.get(stage)
        if pending is not None and value['code']==pending and response.status in (200,400): return None
        if response.status!=200: raise Error('http_error')
        if value['code']!=0: raise Error('business_error')
        if not isinstance(value.get('data'),dict): raise Error('schema')
        remaining(end)
        return value['data']
    except (socket.timeout,TimeoutError): raise Error('timeout') from None
    except ssl.SSLError: raise Error('tls') from None
    except (OSError,http.client.HTTPException): raise Error('network') from None
    finally:
        signal.setitimer(signal.ITIMER_REAL,0); signal.signal(signal.SIGALRM,old)
        if response is not None: response.close()
        if connection is not None: connection.close()


def authorize(*, request=auth_http, opener=open_browser, register=lambda value:None, stats=None):
    stats = stats if stats is not None else {}
    stats.update(state_attempts=0,poll_attempts=0,browser_opened=False)
    deadline=time.monotonic()+300
    stats['state_attempts']=1
    data=request('state',deadline)
    if not isinstance(data,dict): raise Error('schema')
    state=field(data.get('state')); url=validate_url(data.get('authUrl'))
    register(state); register(url)
    opener(url,deadline); stats['browser_opened']=True
    previous=None; auth=None
    for stage in ('token','account'):
        while True:
            if stats['poll_attempts']>=60: raise Error('auth_budget')
            if previous is not None:
                delay=max(0,previous+5-time.monotonic())
                if delay>=remaining(deadline): raise Error('timeout')
                time.sleep(delay)
            remaining(deadline); stats['poll_attempts']+=1
            data=request(stage,deadline,state=state,auth=auth)
            previous=time.monotonic()
            if data is not None: break
        if not isinstance(data,dict): raise Error('schema')
        if stage=='token':
            auth=Auth(field(data.get('accessToken')),field(data.get('domain')),'pending')
            register(auth.token); register(auth.domain)
        else:
            auth=Auth(auth.token,auth.domain,field(data.get('uid'))); register(auth.uid)
    remaining(deadline)
    return auth


def validate_processes(text):
    if not isinstance(text,str) or not text.strip(): raise Error('process_gate')
    seen=set()
    names={'workbuddy','codebuddy','codebuddy-headless','workbuddy-sidecar','workbuddy-server','daemon-app-server'}
    for line in text.splitlines():
        parts=line.split(None,2)
        if len(parts)!=3 or not parts[0].isdigit() or not parts[1].isdigit(): raise Error('process_gate')
        pid,ppid=int(parts[0]),int(parts[1]); command=parts[2]
        if pid<=0 or pid in seen or pid==ppid or any(ord(c)<32 for c in command): raise Error('process_gate')
        seen.add(pid)
        components=command.lower().split('/')
        if components[-1] in names or any(x in ('workbuddy.app','codebuddy.app') for x in components):
            raise Error('process_gate')


def check_process_gate():
    try:
        result=subprocess.run(['/bin/ps','-axo','pid=,ppid=,comm='],capture_output=True,text=True,timeout=5)
        if result.returncode or len(result.stdout)>1048576: raise Error('process_gate')
        validate_processes(result.stdout)
    except (OSError,subprocess.SubprocessError): raise Error('process_gate') from None


def main(argv=None):
    args=list(sys.argv[1:] if argv is None else argv)
    try:
        if not args or args==['--help']:
            print(json.dumps({'status':'not_run','commands':['login [--memory-only]','status','env (Hermes machine only)']})); return 0
        command=args.pop(0); path=SNAPSHOT; memory=False
        if args==['--memory-only'] and command=='login': memory=True; args=[]
        if len(args)==2 and args[0]=='--snapshot' and command in ('env','status'):
            path=Path(args[1]); args=[]
        if args: raise Error('arguments')
        if command=='env':
            if 'HERMES_SECRET_KEY' not in os.environ or sys.stdout.isatty(): raise Error('machine_only')
            sys.stdout.write(environment(load_snapshot(path))); return 0
        if command=='status':
            present=path.exists() or path.is_symlink()
            if present: load_snapshot(path)
            print(json.dumps({'status':'ready' if present else 'absent','present':present,
                              'permissions_ok':present,'server_validity_known':False})); return 0
        if command=='login':
            stats={}; auth=authorize(stats=stats)
            if not memory: save_snapshot(auth)
            print(json.dumps({'status':'authorized','persisted':not memory,'authorization':stats})); return 0
        raise Error('arguments')
    except BaseException as exc:
        category=exc.category if isinstance(exc,Error) else 'cancelled' if isinstance(exc,KeyboardInterrupt) else 'operation_failed'
        print(json.dumps({'status':'error','error_category':category}),file=sys.stderr)
        return 1


if __name__=='__main__': sys.exit(main())
