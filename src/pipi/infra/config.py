"""配置加载 —— 支持 YAML、环境变量覆盖、热重载。

环境变量覆盖规则：`PIPI_` 前缀 + 双下划线表示层级。
例如 PIPI_LLM__API_KEY=sk-xxx  →  config["llm"]["api_key"]
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
    _HAS_YAML = True
except ImportError:  # pragma: no cover
    _HAS_YAML = False


def _deep_merge(base: dict, override: dict) -> dict:
    """深合并：override 覆盖 base，嵌套 dict 递归合并。"""
    out = dict(base)
    for key, val in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def _coerce(value: str) -> Any:
    """把环境变量字符串转成合适的类型。"""
    low = value.strip().lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("none", "null", ""):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _apply_env_overrides(cfg: dict, prefix: str = "PIPI_") -> dict:
    """从环境变量深合并配置。"""
    for env_key, raw in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        path = env_key[len(prefix):].lower().split("__")
        if not path:
            continue
        cursor = cfg
        for part in path[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[path[-1]] = _coerce(raw)
    return cfg


@dataclass
class Config:
    """配置对象。用 .get("a.b.c") 点号取值。"""

    data: dict[str, Any] = field(default_factory=dict)
    _sources: list[Path] = field(default_factory=list, repr=False)
    _mtimes: dict[str, float] = field(default_factory=dict, repr=False)

    def get(self, path: str, default: Any = None) -> Any:
        """点号路径取值：cfg.get("llm.model")。"""
        cursor: Any = self.data
        for part in path.split("."):
            if not isinstance(cursor, dict) or part not in cursor:
                return default
            cursor = cursor[part]
        return cursor

    def set(self, path: str, value: Any) -> None:
        parts = path.split(".")
        cursor = self.data
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value

    def section(self, name: str) -> dict[str, Any]:
        val = self.get(name, {})
        return val if isinstance(val, dict) else {}

    # ---- 热重载 ----

    def changed_on_disk(self) -> bool:
        """检查配置文件是否被外部修改。"""
        for p in self._sources:
            try:
                if p.stat().st_mtime != self._mtimes.get(str(p)):
                    return True
            except OSError:
                continue
        return False

    def reload(self) -> "Config":
        """重新从磁盘加载（保留环境变量覆盖）。"""
        paths = list(self._sources)
        new = Config.load(*paths) if paths else self
        self.data = new.data
        self._mtimes = new._mtimes
        return self

    # ---- 构造 ----

    @classmethod
    def load(cls, *paths: str | Path, extra: dict | None = None) -> "Config":
        """从若干 YAML 文件加载（后面的覆盖前面的），再套环境变量。"""
        merged: dict[str, Any] = {}
        resolved: list[Path] = []

        if not _HAS_YAML:
            # 没装 pyyaml 时退化为空配置（所有值走默认）
            merged = {}

        for raw_path in paths:
            p = Path(raw_path)
            if not p.exists():
                continue
            resolved.append(p)
            if _HAS_YAML:
                try:
                    loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                    if isinstance(loaded, dict):
                        merged = _deep_merge(merged, loaded)
                except Exception as exc:  # 配置坏了不该让程序崩
                    print(f"[config] 解析 {p} 失败：{exc}")

        if extra:
            merged = _deep_merge(merged, extra)

        merged = _apply_env_overrides(merged)

        cfg = cls(data=merged, _sources=resolved)
        cfg._mtimes = {str(p): p.stat().st_mtime for p in resolved if p.exists()}
        return cfg

    @classmethod
    def default(cls) -> "Config":
        """按标准布局加载：configs/default.yaml + configs/degradation.yaml。"""
        root = PROJECT_ROOT
        return cls.load(
            root / "configs" / "default.yaml",
            root / "configs" / "degradation.yaml",
        )


# 项目根目录（src/pipi/infra/config.py → 上溯 4 级）
PROJECT_ROOT = Path(__file__).resolve().parents[3]
