"""Tests for the daemon single-instance guard (D27).

flock is tied to the open file description, so a *second* open+lock of the same
file — even in this one test process — is denied, which is exactly the stray
double-launch D27 guards against.
"""

import pytest

from salutebot.daemon import DaemonAlreadyRunningError, single_instance_lock


def test_lock_blocks_a_second_holder(tmp_path):
    lock = str(tmp_path / "salute-bot.lock")
    with (
        single_instance_lock(lock),
        pytest.raises(DaemonAlreadyRunningError),
        single_instance_lock(lock),
    ):
        pass


def test_lock_is_released_after_the_block(tmp_path):
    lock = str(tmp_path / "salute-bot.lock")
    with single_instance_lock(lock):
        pass
    # Re-acquiring after the first holder exits must succeed (auto-release, D27).
    with single_instance_lock(lock):
        pass


def test_distinct_lockfiles_do_not_contend(tmp_path):
    with single_instance_lock(str(tmp_path / "a.lock")), single_instance_lock(str(tmp_path / "b.lock")):
        pass
