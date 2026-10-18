import os

import pytest

from teams_reader.browser import profile_lock


@pytest.mark.unit
def test_second_holder_fails_fast_with_owner_pid(tmp_path):
    first = profile_lock.acquire(tmp_path, "acme")
    try:
        with pytest.raises(profile_lock.ProfileInUse) as err:
            profile_lock.acquire(tmp_path, "acme")
        message = str(err.value)
        assert "acme" in message and str(os.getpid()) in message
    finally:
        first.release()


@pytest.mark.unit
def test_lock_is_reusable_after_release(tmp_path):
    profile_lock.acquire(tmp_path, "p").release()
    again = profile_lock.acquire(tmp_path, "p")
    again.release()
    again.release()  # idempotent
