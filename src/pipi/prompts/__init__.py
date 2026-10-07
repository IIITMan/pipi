"""Prompt 模板加载。

用极简的 {{var}} 替换（不引 jinja2 依赖），
模板文件放同目录 *.jinja。
"""

from __future__ import annotations

import re
from pathlib import Path

_DIR = Path(__file__).parent
_CACHE: dict[str, str] = {}
_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def load(name: str) -> str:
    """加载模板文本（带缓存）。"""
    if name not in _CACHE:
        path = _DIR / name
        if not path.exists():
            raise FileNotFoundError(f"未找到模板：{path}")
        _CACHE[name] = path.read_text(encoding="utf-8")
    return _CACHE[name]


def render(name: str, **kwargs) -> str:
    """渲染模板：{{var}} 替换。未提供的变量保持原样（便于调试）。"""
    text = load(name)

    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key in kwargs:
            val = kwargs[key]
            return "" if val is None else str(val)
        return m.group(0)

    return _VAR.sub(repl, text)


def clear_cache() -> None:
    _CACHE.clear()


__all__ = ["load", "render", "clear_cache"]
