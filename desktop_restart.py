"""Restart a Windows AstrBot Desktop installation from an independent process."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import psutil

DESKTOP_EXE_NAME = "astrbot-desktop-tauri.exe"
RESTART_DELAY_SECONDS = 3
WINDOWS_DETACHED_PROCESS = 0x00000008
WINDOWS_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def schedule_desktop_restart() -> None:
    """Launch a detached helper for this backend's Desktop parent.

    Raises:
        RuntimeError: The process is not a supported Windows Desktop backend.
        OSError: The independent helper cannot be started.
    """
    if os.name != "nt":
        raise RuntimeError("AstrBot Desktop 脚本重启目前仅支持 Windows。")

    current = psutil.Process()
    parents = current.parents()
    desktop_index = next(
        (
            index
            for index, process in enumerate(parents)
            if process.name().casefold() == DESKTOP_EXE_NAME
        ),
        None,
    )
    if desktop_index is None:
        raise RuntimeError("未找到管理当前 AstrBot 后端的 Desktop 进程。")
    desktop = parents[desktop_index]
    backend_root = current if desktop_index == 0 else parents[desktop_index - 1]
    desktop_exe = Path(desktop.exe()).resolve(strict=True)
    if desktop_exe.name.casefold() != DESKTOP_EXE_NAME:
        raise RuntimeError("AstrBot Desktop 程序路径无效。")

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        raise RuntimeError("未找到桌面后端的 pythonw.exe，无法启动独立重启脚本。")

    from astrbot.core.utils.astrbot_path import get_astrbot_data_path

    log_path = Path(get_astrbot_data_path()) / "logs" / "desktop_restart.log"
    subprocess.Popen(
        [
            str(pythonw),
            str(Path(__file__).resolve()),
            "--desktop-pid",
            str(desktop.pid),
            "--desktop-start",
            repr(desktop.create_time()),
            "--backend-pid",
            str(backend_root.pid),
            "--backend-start",
            repr(backend_root.create_time()),
            "--desktop-exe",
            str(desktop_exe),
            "--log-path",
            str(log_path),
        ],
        cwd=str(desktop_exe.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=(WINDOWS_DETACHED_PROCESS | WINDOWS_CREATE_BREAKAWAY_FROM_JOB),
    )


def _run_restart(args: argparse.Namespace) -> None:
    """Stop the verified Desktop process tree and launch the same executable.

    Args:
        args: Process identities and executable path supplied by the backend.

    Raises:
        RuntimeError: A process identity changed or the old process did not exit.
    """
    time.sleep(RESTART_DELAY_SECONDS)
    desktop = psutil.Process(args.desktop_pid)
    backend = psutil.Process(args.backend_pid)
    desktop_exe = Path(args.desktop_exe).resolve(strict=True)
    if (
        desktop_exe.name.casefold() != DESKTOP_EXE_NAME
        or abs(desktop.create_time() - args.desktop_start) > 0.001
        or abs(backend.create_time() - args.backend_start) > 0.001
        or Path(desktop.exe()).resolve(strict=True) != desktop_exe
        or desktop.pid not in {process.pid for process in backend.parents()}
    ):
        raise RuntimeError("AstrBot Desktop 进程身份已变化，取消脚本重启。")
    desktop_command = [str(desktop_exe), *desktop.cmdline()[1:]]
    desktop_environment = desktop.environ()

    helper = psutil.Process()
    excluded = {
        helper.pid,
        *(process.pid for process in helper.children(recursive=True)),
    }
    descendants = [
        process
        for process in reversed(desktop.children(recursive=True))
        if process.pid not in excluded
    ]
    if backend.pid not in {process.pid for process in descendants}:
        raise RuntimeError("AstrBot 后端已不属于 Desktop 进程树，取消脚本重启。")

    desktop.kill()
    try:
        desktop.wait(timeout=10)
    except psutil.TimeoutExpired as exc:
        raise RuntimeError("AstrBot Desktop 未退出，取消重新启动。") from exc

    for process in descendants:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(descendants, timeout=10)
    if alive:
        raise RuntimeError("部分 AstrBot Desktop 子进程未退出，取消重新启动。")

    subprocess.Popen(
        desktop_command,
        cwd=str(desktop_exe.parent),
        env=desktop_environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=WINDOWS_DETACHED_PROCESS,
    )


def _main() -> None:
    """Parse the independent helper arguments and persist restart failures."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--desktop-pid", type=int, required=True)
    parser.add_argument("--desktop-start", type=float, required=True)
    parser.add_argument("--backend-pid", type=int, required=True)
    parser.add_argument("--backend-start", type=float, required=True)
    parser.add_argument("--desktop-exe", required=True)
    parser.add_argument("--log-path", required=True)
    args = parser.parse_args()

    try:
        _run_restart(args)
    except Exception:
        log_path = Path(args.log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(f"[{datetime.now().isoformat(timespec='seconds')}]\n")
            traceback.print_exc(file=stream)


if __name__ == "__main__":
    _main()
