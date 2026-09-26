"""Verify native shell behavior, bounded execution and actual message delivery."""

import asyncio
import os
import shlex
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.message_components import File, Image
from astrbot_plugin_dev_helper.pictures import PicturePages, RenderError, build_html
from astrbot_plugin_dev_helper.terminal import TerminalRunner, session_workspace


def quote(value):
    if os.name == "nt":
        return "'" + str(value).replace("'", "''") + "'"
    return shlex.quote(str(value))


def python_command(code):
    prefix = "& " if os.name == "nt" else ""
    return f"{prefix}{quote(sys.executable)} -c {quote(code)}"


@pytest.fixture
def terminal_env(env):
    env.context.get_registered_star = lambda name: SimpleNamespace(
        activated=True, star_cls=SimpleNamespace(service=object())
    )
    env.plugin.picture_renderer.render = AsyncMock(
        return_value=PicturePages([b"png"], 1)
    )
    return env


@pytest.fixture(params=["preferred", "windows-powershell"])
def native_shell(request, monkeypatch):
    if request.param == "windows-powershell":
        if os.name != "nt":
            pytest.skip("Windows PowerShell is only available on Windows")
        original_which = shutil.which
        monkeypatch.setattr(
            shutil,
            "which",
            lambda name: None if name == "pwsh" else original_which(name),
        )


async def test_native_cd_ls_cat_git_and_session_isolation(tmp_path, native_shell):
    runner = TerminalRunner()
    root = tmp_path / "工作区 with space's"
    child = root / "子目录 with space"
    child.mkdir(parents=True)
    (child / "hello.txt").write_text("中文 output", encoding="utf-8-sig")
    key = ("session", "admin")
    changed = await runner.execute(key, root, f"cd {quote(child)}")
    assert changed.exit_code == 0 and changed.next_cwd == child
    cat = await runner.execute(key, root, "cat hello.txt")
    assert "中文 output" in cat.output and cat.cwd == child
    listing = await runner.execute(key, root, "ls")
    assert "hello.txt" in listing.output
    git = await runner.execute(key, root, "git --version")
    assert git.exit_code == 0 and "git version" in git.output
    other = await runner.execute(("session", "another-admin"), root, "pwd")
    assert other.cwd == root
    second = await runner.execute(("another-session", "admin"), root, "pwd")
    assert second.cwd == root
    new_root = tmp_path / "new-root"
    moved = await runner.execute(key, new_root, "pwd")
    assert moved.cwd == new_root and new_root.is_dir()
    await runner.close()


async def test_native_pipe_redirection_spaces_multiline_and_error_codes(
    tmp_path, native_shell
):
    runner = TerminalRunner()
    key = ("session", "admin")
    command = (
        "'two  spaces' | Out-File -Encoding utf8 output.txt\ncat output.txt"
        if os.name == "nt"
        else "printf 'two  spaces' > output.txt\ncat output.txt"
    )
    result = await runner.execute(key, tmp_path, command)
    assert "two  spaces" in result.output and result.exit_code == 0
    failed = await runner.execute(
        key,
        tmp_path,
        python_command(
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)"
        ),
    )
    assert "out" in failed.output and "err" in failed.output
    assert failed.exit_code == 7
    missing = await runner.execute(key, tmp_path, "cat missing-file.txt")
    assert missing.exit_code != 0 and "missing-file.txt" in missing.output
    unknown = await runner.execute(key, tmp_path, "dev_helper_nonexistent_command")
    assert unknown.exit_code != 0
    explicit_exit = await runner.execute(key, tmp_path, "exit 9")
    assert explicit_exit.exit_code == 9


async def test_output_capacity_stops_process_and_marks_transcript(tmp_path):
    runner = TerminalRunner(max_bytes=1024)
    result = await runner.execute(
        ("session", "admin"),
        tmp_path,
        python_command(
            "import sys; sys.stdout.write('x' * 1000000); sys.stdout.flush()"
        ),
    )
    assert result.truncated and not result.timeout
    assert len(result.output) == 1024
    assert "已截断" in result.transcript()
    assert not runner.processes


async def test_timeout_kills_child_tree_and_preserves_partial_output(tmp_path):
    runner = TerminalRunner(timeout=1)
    code = "import time; print('before-timeout', flush=True); time.sleep(1); open('late-file', 'w').write('unexpected')"
    result = await runner.execute(("session", "admin"), tmp_path, python_command(code))
    assert result.timeout and "before-timeout" in result.output
    await asyncio.sleep(1.1)
    assert not (tmp_path / "late-file").exists()
    assert "超时" in result.summary() and not runner.processes


async def test_concurrent_commands_keep_cd_order_and_close_stops_execution(
    tmp_path, monkeypatch
):
    runner = TerminalRunner()
    child = tmp_path / "child"
    child.mkdir()
    key = ("session", "admin")
    change, next_command = await asyncio.gather(
        runner.execute(key, tmp_path, f"cd {quote(child)}"),
        runner.execute(key, tmp_path, "pwd"),
    )
    assert change.next_cwd == next_command.cwd == child
    started = asyncio.Event()
    create_process = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        process = await create_process(*args, **kwargs)
        asyncio.get_running_loop().call_soon(started.set)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    running = asyncio.create_task(
        runner.execute(key, tmp_path, python_command("import time; time.sleep(30)"))
    )
    await asyncio.wait_for(started.wait(), 5)
    await runner.close()
    await asyncio.wait_for(running, 5)
    assert not runner.processes and not runner.sessions
    with pytest.raises(RuntimeError, match="关闭"):
        await runner.execute(key, tmp_path, "pwd")


async def test_workspace_matches_astrbot_default(env):
    from astrbot.core.workspace import default_workspace_root

    event = env.event()
    assert await session_workspace(
        env.context, event.unified_msg_origin
    ) == default_workspace_root(event.unified_msg_origin)


@pytest.mark.parametrize(
    "command,user,group,expected",
    [
        ("/term ls", "member", None, "权限"),
        ("/term ls", "admin", "group", "私聊"),
        ("/term", "admin", None, "用法"),
    ],
)
async def test_terminal_rejects_before_execution(
    terminal_env, command, user, group, expected
):
    env = terminal_env
    env.plugin.terminal.execute = AsyncMock()
    event = env.event(command, user=user, group=group, admin=False)
    await env.scheduler.execute(event)
    assert expected in "".join(event.sent)
    env.plugin.terminal.execute.assert_not_awaited()
    env.plugin.picture_renderer.render.assert_not_awaited()
    assert not env.model_calls


async def test_missing_browser_does_not_execute_command(env):
    env.plugin.get_browser_service = lambda: (_ for _ in ()).throw(
        RenderError("浏览器服务不可用")
    )
    env.plugin.terminal.execute = AsyncMock()
    event = env.event("/term ls", admin=False)
    await env.scheduler.execute(event)
    assert "浏览器服务不可用" in "".join(event.sent)
    env.plugin.terminal.execute.assert_not_awaited()


async def test_real_pipeline_preserves_shell_whitespace_and_renders_redacted_result(
    terminal_env,
):
    env = terminal_env
    command = (
        "Write-Output 'two  spaces 中文 api_key=terminal-secret'\nWrite-Output '<script>bad</script>'"
        if os.name == "nt"
        else "printf 'two  spaces 中文 api_key=terminal-secret\\n'\nprintf '<script>bad</script>'"
    )
    event = env.event(f"/term {command}", admin=False)
    await env.scheduler.execute(event)
    document = env.plugin.picture_renderer.render.call_args.args[0]
    assert "two  spaces" in document.blocks[0].text
    assert "\n" in document.blocks[0].text
    assert "two  spaces 中文" in document.blocks[1].text
    assert "terminal-secret" not in build_html(document)
    assert "&lt;script&gt;" in build_html(document)
    assert document.max_pages == 5
    assert len(event.sent_chains) == 1 and isinstance(
        event.sent_chains[0].chain[0], Image
    )
    assert event.is_stopped() and not event.call_llm and not env.model_calls


@pytest.mark.parametrize("render_fails", [False, True])
async def test_long_output_or_render_failure_sends_redacted_file_and_cleans_up(
    terminal_env, render_fails
):
    env = terminal_env
    renderer = env.plugin.picture_renderer.render
    if render_fails:
        renderer.side_effect = RenderError("图片渲染失败")
    else:
        renderer.return_value = PicturePages([b"png"] * 5, 6)
    event = env.event(
        "/term "
        + python_command(
            "print('password=attachment-secret'); print('tail-of-output')"
        ),
        admin=False,
    )
    captured = []
    original_send = event.send

    async def send(chain):
        for component in chain.chain:
            if isinstance(component, File):
                path = Path(component.file)
                text = await asyncio.to_thread(path.read_text, encoding="utf-8")
                captured.append((path, text))
        await original_send(chain)

    event.send = AsyncMock(side_effect=send)
    await env.scheduler.execute(event)
    assert len(captured) == 1
    path, text = captured[0]
    assert "attachment-secret" not in text
    assert "tail-of-output" in text and "[已隐藏]" in text
    assert not path.exists()
    assert event.is_stopped() and not env.model_calls
    assert event.send.await_count == (2 if render_fails else 6)


@pytest.mark.parametrize("method", ["term"])
async def test_direct_call_and_api_role_cannot_execute(terminal_env, method):
    env = terminal_env
    env.plugin.terminal.execute = AsyncMock()
    for event in (env.event(user="member", admin=False), env.event()):
        event.set_extra("_api_key_allow_admin_role", False)
        await env.invoke(method, event, "ls")
        assert "仅限" in "".join(event.sent)
    env.plugin.terminal.execute.assert_not_awaited()
