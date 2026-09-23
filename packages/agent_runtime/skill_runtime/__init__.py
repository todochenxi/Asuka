"""Skill Runtime（M3）—— 技能的**定义与注册表**。

```text
ActionType.SKILL_CALL  →  SkillRegistry.resolve(name)
                       →  派生一条子 SkillRun（M25 的 _suspend_for_child）
                       →  复用同一套 Execution Kernel
```

**边界**：本包只回答"技能是什么、有哪些"。技能的**执行**是"派生一条子 Run"，
那由 `AgentLoop._suspend_for_child()` 接走（`SuspensionReason.CHILD_SKILL`），
Kernel 负责跑 —— 本包不 import `execution_kernel`，也不碰 Loop。

`Worker` 侧对 `TaskType.SKILL` 的归宿仍是 `SkillExecutor` 的**诚实拒绝**
（一条 Execution 装不下一条子 Run）；那不是洞，是正确行为。
"""
from .spec import SkillKind, SkillNotFoundError, SkillRegistry, SkillSpec

__all__ = [
    "SkillKind",
    "SkillNotFoundError",
    "SkillRegistry",
    "SkillSpec",
]
