"""Strict public allowlist and reproducible ZIP. Standard library only."""
import hashlib
from pathlib import Path
import re
import stat
import zipfile

VERSION = '0.1.0'
PREFIX = 'wb2hermesLink-v'+VERSION+'-macos'
PACKAGE_FILES = ('w2hlink','w2hlink_cli.py','wblink.py','README.md','IMPLEMENTATION.md',
                 'config.example.yaml','CHANGELOG.md','LICENSE')
SOURCE_FILES = PACKAGE_FILES + ('.gitignore','scripts/build_release.py','.github/workflows/ci.yml',
    'tests/test_wblink.py','tests/test_hermes_config.py','tests/test_cli.py','tests/test_release.py')


def validate(root, names=SOURCE_FILES):
    root = Path(root).absolute()
    result = {}
    for name in names:
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts: raise ValueError('unsafe_path')
        path = root/relative
        if any(p.is_symlink() for p in (path, *path.parents)): raise ValueError('symlink')
        if not stat.S_ISREG(path.stat().st_mode): raise ValueError('not_regular')
        data = path.read_bytes()
        text = data.decode('utf-8')
        patterns = (r'/[U]sers/[^/\s]+', r'/[h]ome/[^/\s]+',
                    r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b')
        if any(re.search(pattern, text) for pattern in patterns): raise ValueError('personal_path_or_session')
        if re.search(r'(?i)\b(?:sk|sess)-[A-Za-z0-9]{20,}', text): raise ValueError('credential_like_literal')
        result[name] = data
    return result


def build(root=None, output=None):
    root = Path(root or Path(__file__).resolve().parents[1])
    data = validate(root)
    output = Path(output or root/'dist')
    if any(p.is_symlink() for p in (output, *output.parents)): raise ValueError('symlink')
    output.mkdir(parents=True, exist_ok=True)
    archive = output/(PREFIX+'.zip')
    checksums = output/'SHA256SUMS'
    if archive.is_symlink() or checksums.is_symlink(): raise ValueError('symlink')
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as bundle:
        for name in sorted(PACKAGE_FILES):
            info = zipfile.ZipInfo(PREFIX+'/'+name, (2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | (0o755 if name == 'w2hlink' else 0o644)) << 16
            bundle.writestr(info, data[name])
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksums.write_text(checksum+'  '+archive.name+'\n', encoding='ascii')
    return archive, checksum


if __name__ == '__main__':
    artifact, checksum = build()
    print(artifact.name+' '+checksum)
