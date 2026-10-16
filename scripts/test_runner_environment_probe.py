import unittest
from scripts.ci import runner_environment_probe as probe


CATALOG = '''<repository>
<remotePackage path="emulator"><revision><major>99</major></revision><channelRef ref="channel-2"/>
<archives><archive><host-os>linux</host-os><host-arch>x64</host-arch><complete><url>preview.zip</url><checksum type="sha1">preview</checksum></complete></archive></archives></remotePackage>
<remotePackage path="emulator"><revision><major>37</major><minor>2</minor><micro>12</micro></revision><channelRef ref="channel-0"/>
<archives>
<archive><host-os>linux</host-os><host-arch>x64</host-arch><complete><url>linux.zip</url><checksum type="sha1">linux</checksum></complete></archive>
<archive><host-os>macosx</host-os><host-arch>aarch64</host-arch><complete><url>mac-arm.zip</url><checksum type="sha1">mac</checksum></complete></archive>
</archives></remotePackage></repository>'''


class RunnerEnvironmentProbeTest(unittest.TestCase):
    def test_selects_native_stable_archive_instead_of_preview_or_cross_arch(self):
        selected = probe.select_emulator(CATALOG, 'Linux', 'x86_64')
        self.assertEqual('linux.zip', selected['url'])
        self.assertEqual('37.2.12', selected['version'])
        self.assertEqual('mac-arm.zip', probe.select_emulator(CATALOG, 'Darwin', 'arm64')['url'])
        self.assertIsNone(probe.select_emulator(CATALOG, 'Linux', 'aarch64'))
        self.assertIsNone(probe.select_emulator(CATALOG, 'Windows', 'ARM64'))

    def test_acceleration_requires_success_exit_and_explicit_usable_backend(self):
        text = 'accel:\n0\nKVM (version 12) is installed and usable.\naccel\n'
        self.assertEqual('AVAILABLE', probe.acceleration_status(0, text))
        self.assertEqual('UNAVAILABLE', probe.acceleration_status(1, text))
        self.assertEqual('UNAVAILABLE', probe.acceleration_status(0, 'WHPX is not installed'))
        self.assertEqual('UNKNOWN', probe.acceleration_status(0, 'emulator help'))

    def test_api31_image_architectures_are_taken_from_official_packages(self):
        catalog = '''<repository>
<remotePackage path="system-images;android-31;google_apis;x86_64"/>
<remotePackage path="system-images;android-31;google_apis;arm64-v8a"/>
<remotePackage path="system-images;android-30;google_apis;x86_64"/>
</repository>'''
        self.assertEqual(['arm64-v8a', 'x86_64'], probe.api31_images(catalog))
