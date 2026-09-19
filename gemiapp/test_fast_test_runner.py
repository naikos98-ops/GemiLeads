"""Guards for the test-only fast password hasher (config/fast_test_runner.py)."""

from django.conf import settings, global_settings
from django.contrib.auth.hashers import get_hasher
from django.test import SimpleTestCase

import config.settings as project_settings
from config.fast_test_runner import FAST_TEST_PASSWORD_HASHERS, FastHasherTestRunner


class FastTestRunnerTests(SimpleTestCase):
    def test_the_product_keeps_djangos_default_password_hashers(self):
        self.assertFalse(hasattr(project_settings, "PASSWORD_HASHERS"))
        self.assertEqual(global_settings.PASSWORD_HASHERS[0], "django.contrib.auth.hashers.PBKDF2PasswordHasher")

    def test_only_the_test_run_uses_the_fast_hasher_in_every_process(self):
        self.assertEqual(settings.TEST_RUNNER, "config.fast_test_runner.FastHasherTestRunner")
        self.assertEqual(settings.PASSWORD_HASHERS, FAST_TEST_PASSWORD_HASHERS)
        self.assertEqual(get_hasher().algorithm, "md5")
        self.assertIsNot(FastHasherTestRunner.parallel_test_suite.process_setup,
                         __import__("django.test.runner", fromlist=["x"]).ParallelTestSuite.process_setup)
