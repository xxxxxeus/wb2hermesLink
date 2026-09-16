"""Distribution tests: no account access, no network, no installed dependencies."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_builder', ROOT/'scripts/build_release.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class ReleaseTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('W2HLINK_ZIP_FLOW') == '1', 'Explicit one fresh ZIP workflow')
    def test_fresh_zip_public_workflow(self):
        source = Path(os.environ['W2HLINK_TEST_HERMES_ROOT']).resolve()
        archive = ROOT/'dist'/f'{builder.PREFIX}.zip'
        with tempfile.TemporaryDirectory(prefix='.test-', dir=ROOT) as temporary:
            root = Path(temporary)
            download = root/'download space 中文'
            with zipfile.ZipFile(archive) as z:
                z.extractall(download)
                for entry in z.infolist():
                    (download/entry.filename).chmod((entry.external_attr >> 16) & 0o777)
            release = download/builder.PREFIX
            profile = root/'profile space 中文'; profile.mkdir()
            original = {'model': {'provider':'other','default':'kept'},
                        'auxiliary':{'monitor':{'provider':'other'}},
                        'secrets':{'bitwarden':{'enabled':False}}, 'unrelated':42}
            (profile/'config.yaml').write_text(json.dumps(original))
            env = {'PATH':'/usr/bin:/bin','HOME':str(root),'TMPDIR':str(root),
                   'PYTHONDONTWRITEBYTECODE':'1'}
            def run(*args, expected=0):
                result = subprocess.run([str(release/'w2hlink'),'--hermes-root',str(source),
                    '--hermes-home',str(profile),*args], cwd=root, env=env,
                    capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,expected,result.stdout)
                return json.loads(result.stdout)
            run('install','--yes')
            installed = profile/'integrations/w2hlink'
            # Exercise storage with synthetic data only; never authorize a browser.
            code = ('import sys;sys.path.insert(0,sys.argv[1]);import wblink;'
                    'wblink.save_snapshot(wblink.Auth("synthetic-zip-token",'
                    '"synthetic.invalid","synthetic-zip-uid"))')
            result = subprocess.run([str(source/'venv/bin/python'),'-I','-B','-c',code,str(installed)],
                                    env=env,cwd=root,capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0)
            snapshot = installed/'.private/auth.json'; before = snapshot.read_bytes()
            run('models','add','example.model-v1','--context-length','262144')
            run('models','set','example.model-v1','--context-length','524288')
            run('models','default','example.model-v1')
            run('models','remove','example.model-v1',expected=1)
            self.assertEqual(run('models','list')['provider_default'],'example.model-v1')
            run('install','--yes'); run('check','config')
            self.assertTrue(run('status')['credential_structure_permissions_valid'])
            run('models','default','hy3'); run('models','remove','example.model-v1')
            run('uninstall','--yes')
            self.assertEqual(snapshot.read_bytes(),before)
            self.assertTrue(all(not (installed/name).exists() for name in ('w2hlink','w2hlink_cli.py','wblink.py')))
            code = 'import yaml,sys,json;print(json.dumps(yaml.safe_load(open(sys.argv[1]))))'
            result = subprocess.run([str(source/'venv/bin/python'),'-I','-B','-c',code,str(profile/'config.yaml')],
                                    env=env,cwd=root,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0)
            self.assertEqual(json.loads(result.stdout),original)

    def test_public_allowlist_and_reproducible_zip(self):
        with tempfile.TemporaryDirectory(prefix='.test-', dir=ROOT) as temporary:
            archive, first = builder.build(ROOT, Path(temporary)/'a')
            other, second = builder.build(ROOT, Path(temporary)/'b')
            self.assertEqual(first, second)
            self.assertEqual(archive.read_bytes(), other.read_bytes())
            with zipfile.ZipFile(archive) as z:
                self.assertEqual(set(z.namelist()), {builder.PREFIX+'/'+p for p in builder.PACKAGE_FILES})
                self.assertIsNone(z.testzip())
                for item in z.infolist():
                    self.assertTrue(stat.S_ISREG(item.external_attr >> 16))
                    self.assertNotIn('..', Path(item.filename).parts)

    def test_rejects_symlink_and_personal_path(self):
        with tempfile.TemporaryDirectory(prefix='.test-', dir=ROOT) as temporary:
            root = Path(temporary)
            (root/'sample').write_text('/'+'Users'+'/example/private')
            with self.assertRaises(ValueError): builder.validate(root, ('sample',))
            (root/'sample').unlink(); (root/'sample').symlink_to(ROOT/'LICENSE')
            with self.assertRaises(ValueError): builder.validate(root, ('sample',))
            with self.assertRaises(ValueError): builder.validate(root, ('../outside',))

    def test_launcher_help_and_version(self):
        for option in ('--help','--version'):
            result = subprocess.run([str(ROOT/'w2hlink'),option], cwd=ROOT.parent,
                                    env={'PATH':'/usr/bin:/bin','HOME':'/nonexistent'}, capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('wb2hermesLink',result.stdout)


if __name__ == '__main__': unittest.main()
