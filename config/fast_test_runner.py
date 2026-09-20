"""Test-only runner: the full suite, with a fast password hasher and operator alerting silenced.

Django's default PBKDF2 hasher costs ~0.5 s per hash on the development machine (1,000,000 iterations), and the
suite creates thousands of users with passwords in ``setUp``: hashing, not the code under test, dominated the run.
This runner swaps in Django's ``MD5PasswordHasher`` for the duration of a test run only -- in the main process and
in every ``--parallel`` worker (Windows spawns workers, so they must be configured again). It is the test-settings
technique Django's own documentation recommends for speeding up tests.

It also empties ``ADMINS`` for the duration of a run. ``settings.ADMINS`` is derived from ``SUPERADMIN_EMAILS``,
which has a default, so it is populated on a developer machine too -- and tests run with ``DEBUG`` False, so the
``require_debug_false`` filter on the operator email handler stops nothing. Without this, any test that logs an
ERROR from ``gemiapp.ingestion.client`` or ``gemiapp.services`` would queue an operator email and change
``mail.outbox`` under an unrelated assertion. A test that needs the routing asserts it explicitly with
``override_settings(ADMINS=...)``.

It never affects the running product: ``TEST_RUNNER`` is consulted only by ``manage.py test``, and the settings
module keeps Django's default hashers and its real ``ADMINS``. Tests that assert something about the production hash format opt back in
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


def silence_operator_alerts():
    """No test may notify an operator: with no ``ADMINS``, ``AdminEmailHandler`` returns before it builds a
    message, whatever ``SUPERADMIN_EMAILS`` the environment happens to define."""
    from django.conf import settings

    settings.ADMINS = []


def disable_request_metrics():
    """G6 request metrics are off for the suite by default. They write a row per outbound GEMI attempt, and
    most client tests are ``SimpleTestCase`` with no database -- the write would only exercise the fail-open
    path and fill the output with its error line. The tests that measure the instrumentation turn it on with
    ``override_settings(GEMI_REQUEST_METRICS_ENABLED=True)``; production keeps it on."""
    from django.conf import settings

    settings.GEMI_REQUEST_METRICS_ENABLED = False


def _process_setup(*args):
    """Runs first in every spawned ``--parallel`` worker (before ``django.setup()``)."""
    _process_setup_stub(*args)
    use_fast_test_hashers()
    silence_operator_alerts()
    disable_request_metrics()


class FastHasherParallelTestSuite(ParallelTestSuite):
    process_setup = _process_setup


class FastHasherTestRunner(DiscoverRunner):
    parallel_test_suite = FastHasherParallelTestSuite

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        use_fast_test_hashers()
        silence_operator_alerts()
        disable_request_metrics()
