"""Verify the detached Desktop restart plan without stopping real processes."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_dev_helper import desktop_restart


class FakeProcess:
    def __init__(self, pid, started=10.0, exe="", kill_order=None):
        self.pid = pid
        self.started = started
        self.path = exe
        self.argv = [exe]
        self.environment = {}
        self.kill_order = kill_order
        self.ancestors = []
        self.descendants = []
        self.killed = []

    def create_time(self):
        return self.started

    def exe(self):
        return self.path

    def cmdline(self):
        return self.argv

    def environ(self):
        return self.environment

    def parents(self):
        return self.ancestors

    def children(self, recursive=False):
        assert recursive
        return self.descendants

    def kill(self):
        self.killed.append(self.pid)
        if self.kill_order is not None:
            self.kill_order.append(self.pid)

    def wait(self, timeout):
        assert timeout == 10


def test_helper_targets_only_verified_desktop_tree(tmp_path, monkeypatch):
    executable = tmp_path / desktop_restart.DESKTOP_EXE_NAME
    executable.touch()
    kill_order = []
    desktop = FakeProcess(100, exe=str(executable), kill_order=kill_order)
    desktop.argv = [str(executable), "--minimized"]
    desktop.environment = {"PATH": "desktop-path"}
    backend = FakeProcess(200, kill_order=kill_order)
    worker = FakeProcess(300, kill_order=kill_order)
    helper = FakeProcess(400)
    backend.ancestors = [desktop]
    desktop.descendants = [backend, worker, helper]
    processes = {100: desktop, 200: backend, 400: helper}
    monkeypatch.setattr(
        desktop_restart.psutil, "Process", lambda pid=None: processes[pid or 400]
    )
    monkeypatch.setattr(
        desktop_restart.psutil,
        "wait_procs",
        lambda pending, timeout: (pending, []),
    )
    monkeypatch.setattr(desktop_restart.time, "sleep", lambda seconds: None)
    launches = []
    monkeypatch.setattr(
        desktop_restart.subprocess,
        "Popen",
        lambda *args, **kwargs: launches.append((args, kwargs)),
    )
    args = SimpleNamespace(
        desktop_pid=100,
        desktop_start=10.0,
        backend_pid=200,
        backend_start=10.0,
        desktop_exe=str(executable),
    )

    desktop_restart._run_restart(args)

    assert worker.killed == [300]
    assert backend.killed == [200]
    assert desktop.killed == [100]
    assert not helper.killed
    assert kill_order[0] == 100
    assert launches[0][0] == ([str(executable), "--minimized"],)
    assert launches[0][1]["env"] == {"PATH": "desktop-path"}


def test_helper_rejects_changed_process_identity(tmp_path, monkeypatch):
    executable = tmp_path / desktop_restart.DESKTOP_EXE_NAME
    executable.touch()
    desktop = FakeProcess(100, started=20.0, exe=str(executable))
    backend = FakeProcess(200)
    monkeypatch.setattr(
        desktop_restart.psutil,
        "Process",
        lambda pid=None: {100: desktop, 200: backend}[pid],
    )
    monkeypatch.setattr(desktop_restart.time, "sleep", lambda seconds: None)
    args = SimpleNamespace(
        desktop_pid=100,
        desktop_start=10.0,
        backend_pid=200,
        backend_start=10.0,
        desktop_exe=str(executable),
    )

    with pytest.raises(RuntimeError, match="身份已变化"):
        desktop_restart._run_restart(args)
    assert not desktop.killed and not backend.killed


@pytest.mark.skipif(os.name != "nt", reason="Desktop helper requires Windows")
def test_schedule_uses_independent_pythonw_and_exact_ancestor(tmp_path, monkeypatch):
    executable = tmp_path / desktop_restart.DESKTOP_EXE_NAME
    executable.touch()
    current = FakeProcess(300)
    backend = FakeProcess(200)
    desktop = FakeProcess(100, exe=str(executable))
    current.ancestors = [backend, desktop]
    desktop.name = lambda: desktop_restart.DESKTOP_EXE_NAME
    backend.name = lambda: "python.exe"
    monkeypatch.setattr(desktop_restart.psutil, "Process", lambda: current)
    monkeypatch.setattr(
        "astrbot.core.utils.astrbot_path.get_astrbot_data_path", lambda: str(tmp_path)
    )
    launches = []
    monkeypatch.setattr(
        desktop_restart.subprocess,
        "Popen",
        lambda command, **kwargs: launches.append((command, kwargs)),
    )

    desktop_restart.schedule_desktop_restart()

    command, options = launches[0]
    assert Path(command[0]).name.casefold() == "pythonw.exe"
    assert command[command.index("--desktop-pid") + 1] == "100"
    assert command[command.index("--backend-pid") + 1] == "200"
    assert options["creationflags"] & desktop_restart.WINDOWS_CREATE_BREAKAWAY_FROM_JOB
