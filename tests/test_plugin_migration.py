"""Plugin lifecycle commands belong exclusively to the plugin manager."""


async def test_development_helper_no_longer_registers_plugin_management(env):
    assert not hasattr(env.main.Main, "plugin_reload")
    assert not hasattr(env.main.Main, "plugin_remove")
    assert hasattr(env.main.Main, "dev")
    assert not hasattr(env.main.Main, "_dev")
    assert not hasattr(env.main, "DEV_USAGE")
    assert not hasattr(env.catalog, "command_entries")
    assert not hasattr(env.catalog, "tool_entries")
    assert not hasattr(env.plugin, "_plugin_removals")
    assert all(
        handler.handler_name not in {"plugin_reload", "plugin_remove"}
        for handler in env.registry
    )
