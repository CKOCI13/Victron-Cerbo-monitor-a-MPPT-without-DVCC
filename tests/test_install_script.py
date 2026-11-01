"""Tests for dbus-mppt-monitor/install.sh: how it adds itself to /data/rc.local.

Venus OS runs /data/rc.local at the end of every boot, but only when the file is executable
(custom-rc-late.sh in Victron's meta-victronenergy). install.sh puts its own path in there so
the service comes back after every reboot and firmware update. Other add-ons share the file,
and some end it with "exit 0", so a line added after that would never run.

These run install.sh for real against a temporary folder (the MPPTMON_* variables point it there).
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest

from support import REPO


@unittest.skipUnless(shutil.which('sh') and shutil.which('awk'), 'needs sh and awk')
class InstallScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dir = os.path.join(self.tmp, 'data', 'dbus-mppt-monitor')
        shutil.copytree(os.path.join(REPO, 'dbus-mppt-monitor'), self.dir,
                        ignore=shutil.ignore_patterns('__pycache__'))
        self.rc = os.path.join(self.tmp, 'data', 'rc.local')
        self.service = os.path.join(self.tmp, 'service')
        os.mkdir(self.service)
        self.line = os.path.join(self.dir, 'install.sh')

    def install(self):
        env = dict(os.environ, MPPTMON_DIR=self.dir, MPPTMON_RC=self.rc,
                   MPPTMON_SERVICE=self.service)
        subprocess.run(['sh', self.line], env=env, check=True, stdout=subprocess.DEVNULL)
        with open(self.rc) as f:
            return f.read()

    def write_rc(self, text, mode=0o755):
        with open(self.rc, 'w') as f:
            f.write(text)
        os.chmod(self.rc, mode)

    def assert_executable(self):
        self.assertTrue(os.stat(self.rc).st_mode & stat.S_IXUSR, 'rc.local must be executable')

    def test_creates_rc_local(self):
        self.assertEqual(self.install(), '#!/bin/sh\n%s\n' % self.line)
        self.assert_executable()

    def test_links_the_service(self):
        self.install()
        link = os.path.join(self.service, 'dbus-mppt-monitor')
        self.assertEqual(os.readlink(link), os.path.join(self.dir, 'service'))

    def test_running_twice_adds_one_line(self):
        self.install()
        self.assertEqual(self.install().count(self.line), 1)

    def test_goes_before_another_add_ons_exit(self):
        self.write_rc('#!/bin/sh\n/data/other/start.sh\nexit 0\n')
        self.assertEqual(self.install(),
                         '#!/bin/sh\n/data/other/start.sh\n%s\nexit 0\n' % self.line)

    def test_moves_a_line_left_after_exit_by_an_older_version(self):
        self.write_rc('#!/bin/sh\nexit 0\n%s\n' % self.line)
        self.assertEqual(self.install(), '#!/bin/sh\n%s\nexit 0\n' % self.line)

    def test_keeps_an_exit_inside_a_block(self):
        text = '#!/bin/sh\nif [ -f /x ]; then\n    exit 1\nfi\n'
        self.write_rc(text)
        self.assertEqual(self.install(), text + self.line + '\n')

    def test_file_without_a_final_newline(self):
        self.write_rc('#!/bin/sh\n/data/other/start.sh')
        self.assertEqual(self.install(),
                         '#!/bin/sh\n/data/other/start.sh\n%s\n' % self.line)

    def test_makes_an_existing_rc_local_executable(self):
        self.write_rc('#!/bin/sh\n', mode=0o644)
        self.install()
        self.assert_executable()

    def test_leaves_no_temporary_file(self):
        self.install()
        self.install()
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.rc))),
                         ['dbus-mppt-monitor', 'rc.local'])


if __name__ == '__main__':
    unittest.main()
