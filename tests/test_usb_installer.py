"""Tests for the USB-stick / SD-card installer (venus-data-mppt-monitor.tar.gz).

Venus OS unpacks venus-data*.tar.gz into /data at boot and then runs rc/post-hook.sh from the
archive (update-data.sh in Victron's meta-victronenergy). These tests build the archive with
make-usb-installer.sh and check it has exactly that shape.
"""

import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest

from support import REPO


@unittest.skipUnless(shutil.which('sh') and shutil.which('tar'), 'needs sh and tar')
class UsbInstallerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for name in ('dbus-mppt-monitor', 'usb'):
            shutil.copytree(os.path.join(REPO, name), os.path.join(self.tmp, name),
                            ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy(os.path.join(REPO, 'make-usb-installer.sh'), self.tmp)
        # Files the driver makes while it runs must never be packed (they'd overwrite the Cerbo's).
        open(os.path.join(self.tmp, 'dbus-mppt-monitor', 'ui.json'), 'w').close()
        subprocess.run(['sh', os.path.join(self.tmp, 'make-usb-installer.sh')], check=True,
                       stdout=subprocess.DEVNULL)
        with tarfile.open(os.path.join(self.tmp, 'venus-data-mppt-monitor.tar.gz')) as t:
            self.names = {n.rstrip('/') for n in t.getnames()}
            self.hook = t.extractfile('rc/post-hook.sh').read().decode()

    def test_unpacks_into_the_folder_install_sh_expects(self):
        # /data/<this> must be /data/dbus-mppt-monitor, the path install.sh and service/run use
        for f in ('dbus-mppt-monitor/dbus-mppt-monitor.py', 'dbus-mppt-monitor/install.sh',
                  'dbus-mppt-monitor/config.ini', 'dbus-mppt-monitor/service/run'):
            self.assertIn(f, self.names)
        tops = {n.split('/')[0] for n in self.names}
        self.assertEqual(tops, {'dbus-mppt-monitor', 'rc'})

    def test_leaves_out_run_time_files(self):
        self.assertFalse(any(n.endswith(('ui.json', 'state.json')) for n in self.names))

    def test_post_hook_installs_only_after_a_good_unpack(self):
        self.assertIn('rc/post-hook.sh', self.names)
        self.assertNotIn('\r', self.hook)                     # Unix line endings, or sh fails
        self.assertIn('[ "$1" = "success" ] || exit 0', self.hook)
        self.assertIn('sh /data/dbus-mppt-monitor/install.sh', self.hook)

    def test_installs_once_by_renaming_the_file_on_the_stick(self):
        # Venus OS only unpacks venus-data*.tar.gz; the renamed file is left alone on later boots
        self.assertIn('/media/*/venus-data-mppt-monitor.tar.gz', self.hook)
        self.assertIn('mv -f "$f" "$f.installed"', self.hook)
        self.assertTrue(re.fullmatch(r'venus-data-[a-z0-9-]+\.tar\.gz', 'venus-data-mppt-monitor.tar.gz'))


if __name__ == '__main__':
    unittest.main()
