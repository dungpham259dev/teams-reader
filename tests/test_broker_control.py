import subprocess
import sys
import time

import pytest

from teams_reader.broker import control

_HOLDER = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0); os.write(fd, str(os.getpid()).encode())
print("ready", flush=True)
time.sleep(60)
"""


@pytest.mark.unit
def test_stop_terminates_the_broker_holding_the_lock(tmp_path):
    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(tmp_path / "broker.lock")],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ready"
        assert control.running_pid(tmp_path) == holder.pid
        assert control.stop(tmp_path, timeout=5) is True
        assert holder.wait(timeout=5) is not None
        assert control.running_pid(tmp_path) is None
    finally:
        holder.kill()


@pytest.mark.unit
def test_stop_is_a_no_op_without_a_running_broker(tmp_path):
    assert control.running_pid(tmp_path) is None
    (tmp_path / "broker.lock").write_text("999999")  # stale pid, nobody holds the lock
    assert control.running_pid(tmp_path) is None
    assert control.stop(tmp_path, timeout=1) is False


@pytest.mark.unit
def test_mcp_server_pids_lists_other_mcp_processes(monkeypatch):
    monkeypatch.setattr(control.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "12\n34\n"})())
    monkeypatch.setattr(control.os, "getpid", lambda: 34)
    assert control.mcp_server_pids() == [12]
