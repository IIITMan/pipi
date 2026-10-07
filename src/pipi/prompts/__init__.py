"""Prompt 模板渲染（Jinja2）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import jinja2
    _ENV = jinja2.Environment(
        loader=jinja2.FileSystemLoader(Path(__file__).parent),
        autoescape=False, keep_trailing_newline=False,
    )
except ImportError:            # jinja2 非核心依赖（fastapi 会带上）
    _ENV = None


def render(template: str, **kwargs: Any) -> str:
    """渲染 prompts/ 下的模板。jinja2 缺失时退回 {var} 直替。"""
    if _ENV is not None:
        return _ENV.get_template(template).render(**kwargs).strip()
    text = (Path(__file__).parent / template).read_text(encoding="utf-8")
    for k, v in kwargs.items():
        text = text.replace("{{ " + k + " }}", str(v)).replace("{{" + k + "}}", str(v))
    return text.strip()
