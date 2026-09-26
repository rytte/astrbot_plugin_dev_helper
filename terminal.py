"""Run native shell commands, retaining only each caller's working directory."""

from __future__ import annotations

import asyncio
import base64
import locale
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic

import psutil
from astrbot.core.db import BaseDatabase
from astrbot.core.workspace import (
    default_workspace_root,
    resolve_workspace_root_for_umo,
)

from .display import redact
from .pictures import PictureBlock, PictureDocument


async def session_workspace(context, umo: str) -> Path:
    """Use the same workspace resolution and fallback as AstrBot's local tools."""
    db = getattr(context, "_db", None)
    if isinstance(db, BaseDatabase):
        try:
            return await resolve_workspace_root_for_umo(umo, db)
        except Exception:
            pass
    return default_workspace_root(umo)


@dataclass
class TerminalResult:
    command: str
    cwd: Path
    next_cwd: Path
    shell: str
    output: str
    exit_code: int
    elapsed: float
    timeout: bool = False
    truncated: bool = False

    def summary(self) -> str:
        lines = [
            f"终端：{self.shell}",
            f"执行目录：{self.cwd}",
            f"当前目录：{self.next_cwd}",
            f"退出码：{self.exit_code} · 耗时：{self.elapsed:.2f} 秒",
        ]
        if self.timeout:
            lines.append("执行超时，进程已终止；以下是终止前收集的输出。")
        if self.truncated:
            lines.append("输出达到容量上限，进程已终止，结果已截断。")
        return redact("\n".join(lines))

    def document(self, max_pages: int) -> PictureDocument:
        return PictureDocument(
            "终端执行结果",
            self.summary(),
            (
                PictureBlock(redact(f"> {self.command}\n\n")),
                PictureBlock(redact(self.output) or "（无输出）"),
            ),
            max_pages=max_pages,
        )

    def transcript(self) -> str:
        return redact(
            f"{self.summary()}\n\n> {self.command}\n\n{self.output or '（无输出）'}\n"
        )


@dataclass
class TerminalSession:
    root: Path
    cwd: Path
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class TerminalRunner:
    def __init__(self, timeout: int = 60, max_bytes: int = 1048576) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.sessions: dict[tuple[str, str], TerminalSession] = {}
        self.processes: set[asyncio.subprocess.Process] = set()
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        for process in tuple(self.processes):
            await self._stop(process)
        self.sessions.clear()

    async def execute(
        self, key: tuple[str, str], root: Path, command: str
    ) -> TerminalResult:
        if self.closed:
            raise RuntimeError("终端已关闭。")
        session = self.sessions.setdefault(key, TerminalSession(root, root))
        async with session.lock:
            if self.closed:
                raise RuntimeError("终端已关闭。")
            if session.root != root:
                session.root = session.cwd = root
            if session.cwd == root:
                await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
            result = await self._execute(command, session.cwd)
            session.cwd = result.next_cwd
            return result

    def _shell_args(self, command: str, temporary: Path) -> tuple[str, list[str]]:
        """Keep commands in a script file, without quoting them as shell arguments."""
        location = temporary / "cwd.txt"
        if os.name == "nt":
            shell = shutil.which("pwsh") or shutil.which("powershell")
            if not shell:
                raise RuntimeError("未找到 PowerShell，请安装或将其加入 PATH。")
            script = temporary / "command.ps1"
            # Dot-sourcing reports success for a script even when its last native
            # command failed. Capture $? inside that script before returning.
            script.write_text(
                command
                + "\n$devHelperSucceeded = $?\n"
                + "if (-not $devHelperSucceeded) {\n"
                + "    $devHelperExitCode = if ($LASTEXITCODE) { $LASTEXITCODE } else { 1 }\n"
                + "}\n",
                encoding="utf-8-sig",
            )

            def quote(path: Path) -> str:
                return "'" + str(path).replace("'", "''") + "'"

            wrapper = f"""
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$global:LASTEXITCODE = 0
$devHelperExitCode = 0
$devHelperSucceeded = $null
try {{
    . {quote(script)}
    if ($null -eq $devHelperSucceeded) {{ $devHelperExitCode = $LASTEXITCODE }}
}} catch {{
    Write-Error -ErrorRecord $_
    $devHelperExitCode = 1
}} finally {{
    if ((Get-Location).Provider.Name -eq 'FileSystem') {{
        [System.IO.File]::WriteAllText({quote(location)}, (Get-Location).ProviderPath)
    }}
}}
exit $devHelperExitCode
"""
            encoded = base64.b64encode(wrapper.encode("utf-16-le")).decode("ascii")
            return Path(shell).name, [
                shell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-OutputFormat",
                "Text",
                "-EncodedCommand",
                encoded,
            ]
        # POSIX sh is the default command shell, matching subprocess(shell=True).
        shell = "/bin/sh"
        script = temporary / "command.sh"
        script.write_text(command, encoding="utf-8")
        save_location = f"pwd -P > {shlex.quote(str(location))}"
        wrapper = (
            f"trap {shlex.quote(save_location)} EXIT\n. {shlex.quote(str(script))}\n"
        )
        return shell, [shell, "-c", wrapper]

    async def _stop(self, process: asyncio.subprocess.Process) -> None:
        """Terminate the command tree, including children holding output pipes open."""
        if os.name == "nt":
            if process.returncode is not None:
                return

            def kill_tree() -> None:
                try:
                    parent = psutil.Process(process.pid)
                    children = parent.children(recursive=True)
                except psutil.NoSuchProcess:
                    return
                for child in reversed(children):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                try:
                    parent.kill()
                except psutil.NoSuchProcess:
                    pass

            await asyncio.to_thread(kill_tree)
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def _execute(self, command: str, cwd: Path) -> TerminalResult:
        if self.closed:
            raise RuntimeError("终端已关闭。")
        started = monotonic()
        data = bytearray()
        timed_out = truncated = False
        with tempfile.TemporaryDirectory(prefix="astrbot-terminal-") as folder:
            temporary = Path(folder)
            shell, args = await asyncio.to_thread(self._shell_args, command, temporary)
            options = (
                {
                    "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.CREATE_NO_WINDOW
                }
                if os.name == "nt"
                else {"start_new_session": True}
            )
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(cwd),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **options,
            )
            self.processes.add(process)
            if self.closed:
                await self._stop(process)

            async def collect() -> None:
                nonlocal truncated
                assert process.stdout is not None
                while chunk := await process.stdout.read(8192):
                    remaining = self.max_bytes - len(data)
                    data.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        truncated = True
                        await self._stop(process)
                        return
                await process.wait()

            try:
                await asyncio.wait_for(collect(), timeout=self.timeout)
            except asyncio.TimeoutError:
                timed_out = True
            finally:
                await self._stop(process)

                # Drain after killing: asyncio waits for pipes as well as process exit.
                async def drain() -> None:
                    assert process.stdout is not None
                    while await process.stdout.read(8192):
                        pass

                if process.stdout is not None:
                    try:
                        await asyncio.wait_for(drain(), timeout=5)
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        pass
                self.processes.discard(process)

            def read_location() -> Path:
                location = temporary / "cwd.txt"
                try:
                    if location.is_file():
                        candidate = Path(
                            location.read_text(encoding="utf-8-sig").rstrip("\r\n")
                        )
                        if candidate.is_absolute() and candidate.is_dir():
                            return candidate
                except OSError:
                    pass
                return cwd

            next_cwd = await asyncio.to_thread(read_location)
        try:
            output = data.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            if truncated and error.reason == "unexpected end of data":
                output = data.decode("utf-8-sig", errors="replace")
            else:
                output = data.decode(
                    locale.getpreferredencoding(False), errors="replace"
                )
        return TerminalResult(
            command,
            cwd,
            next_cwd,
            shell,
            output,
            process.returncode or 0,
            monotonic() - started,
            timed_out,
            truncated,
        )
