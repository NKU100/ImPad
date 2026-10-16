import unittest
from scripts.ci.local_qq_runtime import image_configuration, validate_device


class LocalQqRuntimeTest(unittest.TestCase):
    def test_versions_use_independent_avds_and_evidence(self):
        old, current = image_configuration('30'), image_configuration('37.2')
        self.assertEqual('system-images;android-30;google_apis;arm64-v8a', old['package'])
        self.assertEqual('system-images;android-37.2;google_apis_ps16k;arm64-v8a', current['package'])
        self.assertNotEqual(old['avd'], current['avd'])
        self.assertNotEqual(old['output'], current['output'])

    def test_wrong_android_version_is_rejected_before_install(self):
        with self.assertRaisesRegex(ValueError, 'API'):
            validate_device('30', '37', 'arm64-v8a', '1')

    def test_physical_device_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'emulator'):
            validate_device('30', '30', 'arm64-v8a', '0')

    def test_api30_arm64_is_accepted(self):
        validate_device('30', '30', 'arm64-v8a', '1')
