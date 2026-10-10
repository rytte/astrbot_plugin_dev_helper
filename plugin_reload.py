"""Discard source-backed bytecode belonging to one verified plugin directory."""

import importlib
from pathlib import Path


def clear_plugin_bytecode(store_path: str, root_dir_name: str) -> None:
    """Remove plugin bytecode without following paths outside its directory.

    Args:
        store_path: AstrBot's user or built-in plugin storage directory.
        root_dir_name: Registered plugin directory name.

    Raises:
        RuntimeError: A plugin or cache path escapes the expected directory.
        OSError: A plugin directory cannot be read or a cache cannot be removed.
    """
    store = Path(store_path).resolve(strict=True)
    root = (store / root_dir_name).resolve(strict=True)
    if root.parent != store:
        raise RuntimeError("插件目录不是插件存储目录的直接子目录，拒绝清理缓存。")
    caches = set()
    for source in root.rglob("*.py"):
        if not source.resolve(strict=True).is_relative_to(root):
            raise RuntimeError("插件源文件指向插件目录之外，拒绝清理缓存。")
        cache_dir = source.parent / "__pycache__"
        if not cache_dir.exists():
            continue
        if not cache_dir.resolve(strict=True).is_relative_to(root):
            raise RuntimeError("插件字节码缓存目录指向插件目录之外，拒绝清理缓存。")
        for cache in cache_dir.iterdir():
            if cache.name.startswith(source.stem + ".") and cache.suffix == ".pyc":
                if not cache.resolve(strict=True).is_relative_to(root):
                    raise RuntimeError("插件字节码缓存指向插件目录之外，拒绝清理缓存。")
                caches.add(cache)
    for cache in caches:
        cache.unlink()
    importlib.invalidate_caches()
