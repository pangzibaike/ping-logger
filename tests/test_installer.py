"""Run the installer's actual release-selection lines in Bash without installing anything."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('bash'), 'Bash required')
class InstallerTests(unittest.TestCase):
    def installer_selection(self, os_contents, override=None):
        text=(ROOT/'install.sh').read_text()
        assignment=next(line for line in text.splitlines() if line.startswith('PING_LOGGER_RELEASE_VERSION='))
        guard=next(line for line in text.splitlines() if line.startswith('[[ $PING_LOGGER_RELEASE_VERSION =~'))
        download=next(line.strip() for line in text.splitlines() if 'archive/refs/tags/' in line)
        with tempfile.TemporaryDirectory() as tmp:
            release=Path(tmp)/'os-release'
            release.write_text(os_contents)
            script='\n'.join(['set -eu','fail() { echo "$*" >&2; exit 1; }','REPO=pangzibaike/ping-logger',
                              assignment,guard,'source "$1"','TMP=/unused',
                              'curl() { printf "%s\\n" "$@"; }',download])
            env=dict(os.environ)
            env.pop('PING_LOGGER_VERSION',None)
            if override is not None: env['PING_LOGGER_VERSION']=override
            return subprocess.run(['bash','-c',script,'installer-test',str(release)],env=env,text=True,capture_output=True)

    def test_debian_version_cannot_replace_release(self):
        result=self.installer_selection('ID=debian\nVERSION="12 (bookworm)"\nVERSION_ID="12"\n')
        self.assertEqual(result.returncode,0,result.stderr)
        urls=[arg for arg in result.stdout.splitlines() if arg.startswith('https://')]
        self.assertEqual(urls,['https://github.com/pangzibaike/ping-logger/archive/refs/tags/v1.1.1.tar.gz'])

    def test_ubuntu_preserves_requested_release(self):
        result=self.installer_selection('ID=ubuntu\nVERSION="24.04.3 LTS (Noble Numbat)"\n','v1.2.3')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('/v1.2.3.tar.gz',result.stdout)

    def test_invalid_release_rejected_before_download(self):
        for value in ('12 (bookworm)','main','v1.1.1;echo unsafe'):
            result=self.installer_selection('ID=debian\nVERSION="12 (bookworm)"\n',value)
            self.assertNotEqual(result.returncode,0)
            self.assertNotIn('https://',result.stdout)
