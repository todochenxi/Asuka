"""Tool Registry：Resolution + Version Resolution（§24 流程的前两环）。

**T-5：版本必须显式解析，"latest" 不能是个会漂移的魔法值。**

```text
resolve("query_database")              → 默认版本（显式登记过的那个）
resolve("query_database", "2.1.0")     → 指定版本
```

为什么默认版本要**显式登记**而不是"取最新注册的"：
工具升级是业务事件，不是部署细节。今天注册顺序变了就换版本，
等于让部署顺序决定 Agent 的行为 —— 这种 bug 极难复现。

所以：第一次注册自动成为默认，之后换默认必须 `make_default=True` 明说。
"""
from __future__ import annotations

from typing import Mapping

from .protocols import ToolInvoker
from .spec import ToolSpec


class ToolNotFoundError(Exception):
    """工具不存在 / 版本不存在。PERMANENT —— 重试一百年也不会有。"""

    def __init__(self, name: str, version: str | None = None) -> None:
        wanted = f"{name}@{version}" if version else name
        super().__init__(f"no such tool: {wanted}")
        self.name = name
        self.version = version


class ToolRegistry:
    """name@version → (spec, invoker)。"""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], tuple[ToolSpec, ToolInvoker]] = {}
        self._defaults: dict[str, str] = {}

    # ------------------------------------------------------------ 注册
    def register(
        self, spec: ToolSpec, invoker: ToolInvoker, *, make_default: bool = False
    ) -> None:
        key = (spec.name, spec.version)
        if key in self._rows and not make_default:
            # 重复注册同一版本很容易在热加载时无声覆盖 —— 拒绝比静默好
            raise ValueError(f"tool {spec.qualified_name} already registered")
        self._rows[key] = (spec, invoker)
        if make_default or spec.name not in self._defaults:
            self._defaults[spec.name] = spec.version

    def set_default(self, name: str, version: str) -> None:
        """显式切换默认版本（工具升级要走这条路，让它在代码里留痕）。"""
        if (name, version) not in self._rows:
            raise ToolNotFoundError(name, version)
        self._defaults[name] = version

    # ------------------------------------------------------------ 解析
    def resolve(self, name: str, version: str | None = None) -> ToolSpec:
        if version is None:
            version = self._defaults.get(name)
            if version is None:
                raise ToolNotFoundError(name)
        row = self._rows.get((name, version))
        if row is None:
            raise ToolNotFoundError(name, version)
        return row[0]

    def invoker_for(self, spec: ToolSpec) -> ToolInvoker:
        row = self._rows.get((spec.name, spec.version))
        if row is None:
            raise ToolNotFoundError(spec.name, spec.version)
        return row[1]

    # ------------------------------------------------------------ 视图
    def has(self, name: str, version: str | None = None) -> bool:
        try:
            self.resolve(name, version)
            return True
        except ToolNotFoundError:
            return False

    def versions(self, name: str) -> tuple[str, ...]:
        return tuple(v for (n, v) in self._rows if n == name)

    def default_version(self, name: str) -> str | None:
        return self._defaults.get(name)

    def specs(self) -> Mapping[str, ToolSpec]:
        return {n: self.resolve(n) for n in sorted({n for n, _ in self._rows})}
