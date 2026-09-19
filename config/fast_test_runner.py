"""Test-only runner: the full suite, with a fast password hasher.

Django's default PBKDF2 hasher costs ~0.5 s per hash on the development machine (1,000,000 iterations), and the
suite creates thousands of users with passwords in ``setUp``: hashing, not the code under test, dominated the run.
This runner swaps in Django's ``MD5PasswordHasher`` for the duration of a test run only -- in the main process and
in every ``--parallel`` worker (Windows spawns workers, so they must be configured again). It is the test-settings
technique Django's own documentation recommends for speeding up tests.

It never affects the running product: ``TEST_RUNNER`` is consulted only by ``manage.py test``, and the settings
module keeps Django's default hashers. Tests that assert something about the production hash format opt back in
with ``override_settings(PASSWORD_HASHERS=...)``. Nothing is skipped, filtered or reordered: discovery, test
population and assertions are exactly those of ``DiscoverRunner``.
"""

from django.test.runner import DiscoverRunner, ParallelTestSuite, _process_setup_stub

FAST_TEST_PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def use_fast_test_hashers():
    """Switch the current process to the test-only hasher and drop any cached hasher instances."""
    from django.conf import settings
    from django.contrib.auth import hashers

    settings.PASSWORD_HASHERS = FAST_TEST_PASSWORD_HASHERS
    hashers.get_hashers.cache_clear()
    hashers.get_hashers_by_algorithm.cache_clear()


def _process_setup(*args):
    """Runs first in every spawned ``--parallel`` worker (before ``django.setup()``)."""
    _process_setup_stub(*args)
    use_fast_test_hashers()


class FastHasherParallelTestSuite(ParallelTestSuite):
    process_setup = _process_setup


class FastHasherTestRunner(DiscoverRunner):
    parallel_test_suite = FastHasherParallelTestSuite

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        use_fast_test_hashers()
