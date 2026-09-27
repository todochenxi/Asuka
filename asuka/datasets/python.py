"""Python 标准库任务集（人工整理，ground truth 来自官方文档本身）。

与 `redis.py` 同一份纪律（详见那份文件的头注）：

    · `reference_answer` 是**判分的尺子**，所以**人工**从官方文档整理，不用 LLM 生成；
    · 每条声明 `evidence = (unit_id, section)` —— 校验器会核对这一节**真实存在**
      （section 取自 `chunks.jsonl` 的 `attributes.section`），写错就报错。

section 名来自切分器对 RST 转换后的标题路径（见 `asuka/rst.py`）：
`overview` 是模块顶层，其余是文档里真实的小节标题。

三档难度（同 redis）：
    simple  是什么 / 语法 / 返回值 / 边界
    medium  对比与选择：A 与 B 的差别、这种场景用哪个
    hard    设计场景：用这些 API 拼一个方案，并说清**失效边界**
"""
from __future__ import annotations

from . import Dataset, Evidence, RequiredPoint, TaskItem  # noqa: F401  (re-export)

E = Evidence
R = RequiredPoint


def build() -> Dataset:
    items: tuple[TaskItem, ...] = (
        # ---------------------------------------------------------- simple
        TaskItem(
            task_id="p-simple-01",
            question="What does `dataclasses.dataclass` do, and what does the `frozen` option change?",
            reference_answer=(
                "`@dataclass` is a decorator that adds generated special methods "
                "(`__init__`, `__repr__`, and optionally `__eq__`/ordering) to a class "
                "based on its annotated class variables. `frozen=True` makes instances "
                "immutable: assigning to a field after creation raises "
                "`FrozenInstanceError` (a subclass of `AttributeError`)."
            ),
            source_document="python:dataclasses",
            difficulty="simple",
            evidence=(E("dataclasses", "overview"), E("dataclasses", "Frozen instances")),
            required_points=(
                R(label="作用：为类生成 __init__/__repr__ 等方法",
                  any_of=("adds generated special methods", "__init__", "generated special methods")),
                R(label="frozen=True 使实例不可变",
                  any_of=("frozen", "immutable", "FrozenInstanceError")),
            ),
        ),
        TaskItem(
            task_id="p-simple-02",
            question="What does `json.dumps` return, and how do you make its output deterministic?",
            reference_answer=(
                "`json.dumps` returns a JSON **string** (unlike `json.dump`, which writes to "
                "a file). To make the output deterministic, pass `sort_keys=True` so object "
                "keys are sorted; `separators` and `indent` control whitespace formatting."
            ),
            source_document="python:json",
            difficulty="simple",
            evidence=(E("json", "Basic Usage"), E("json", "overview")),
            required_points=(
                R(label="返回字符串（不是写文件）",
                  any_of=("returns a json", "returns a string", "json string representation")),
                R(label="sort_keys=True 保证确定顺序",
                  any_of=("sort_keys", "sorted")),
            ),
        ),
        TaskItem(
            task_id="p-simple-03",
            question="What does a `with` statement do, and what two methods does the context manager protocol require?",
            reference_answer=(
                "The `with` statement ensures setup and guaranteed cleanup: it calls "
                "`__enter__` on the context manager when entering the block and `__exit__` "
                "when leaving, including when an exception is raised. `ExitStack` and "
                "`contextlib.contextmanager` build such managers."
            ),
            source_document="python:contextlib",
            difficulty="simple",
            evidence=(E("contextlib", "overview"), E("contextlib", "Utilities")),
            required_points=(
                R(label="进入调用 __enter__", any_of=("__enter__",)),
                R(label="退出（含异常）调用 __exit__",
                  any_of=("__exit__", "guaranteed", "cleanup")),
            ),
        ),
        # ---------------------------------------------------------- medium
        TaskItem(
            task_id="p-medium-01",
            question="When should you use a mutable default value for a dataclass field instead of a plain default?",
            reference_answer=(
                "A plain default that is a mutable object (list/dict/set) would be **shared** "
                "by all instances, so dataclasses **reject** a mutable default and raise "
                "`ValueError`. Use `field(default_factory=list)` (or another factory) so each "
                "instance gets its own fresh object."
            ),
            source_document="python:dataclasses",
            difficulty="medium",
            evidence=(E("dataclasses", "Mutable default values"), E("dataclasses", "Default factory functions")),
            required_points=(
                R(label="可变默认会被拒绝 / 共享",
                  any_of=("mutable default", "ValueError", "shared")),
                R(label="用 default_factory 生成新对象",
                  any_of=("default_factory",)),
            ),
        ),
        TaskItem(
            task_id="p-medium-02",
            question="What is the difference between `json.dump` and `json.dumps`, and between `json.load` and `json.loads`?",
            reference_answer=(
                "The `-s` variants work with **strings**: `json.dumps` serializes an object to "
                "a string and `json.loads` parses a string. The non-`s` variants work with "
                "**file-like objects**: `json.dump(obj, fp)` writes to a file and `json.load(fp)` "
                "reads from one."
            ),
            source_document="python:json",
            difficulty="medium",
            evidence=(E("json", "Basic Usage"), E("json", "overview")),
            required_points=(
                R(label="s = string", any_of=("string", "dumps")),
                R(label="无 s = 文件对象",
                  any_of=("file", "fp", "file-like")),
            ),
        ),
        TaskItem(
            task_id="p-medium-03",
            question="When would you use `functools.lru_cache`, and what does it assume about the decorated function?",
            reference_answer=(
                "`lru_cache` is a memoization decorator: it caches results keyed by the "
                "arguments and returns the cached result on repeat calls. It assumes the "
                "function is **pure / deterministic** (same args → same result) and that its "
                "arguments are **hashable**, since they are used as dict keys."
            ),
            source_document="python:functools",
            difficulty="medium",
            evidence=(E("functools", "overview"),),
            required_points=(
                R(label="缓存结果（记忆化）", any_of=("memoization", "caches", "cached")),
                R(label="要求参数可哈希",
                  any_of=("hashable",)),
            ),
        ),
        # ---------------------------------------------------------- hard
        TaskItem(
            task_id="p-hard-01",
            question=(
                "Design a helper that turns a generator-based resource setup into a context "
                "manager, and explain how errors during teardown are handled."
            ),
            reference_answer=(
                "Use `contextlib.contextmanager`: decorate a generator that yields exactly once — "
                "code before `yield` is the setup, code after is the teardown, and the value "
                "yielded is bound to the `as` target. If the body raises, the exception is "
                "thrown **into the generator at the `yield` point**, so a `try/finally` around "
                "the `yield` guarantees teardown; the manager can suppress the exception by not "
                "re-raising it."
            ),
            source_document="python:contextlib",
            difficulty="hard",
            evidence=(E("contextlib", "Utilities"), E("contextlib", "Single use, reusable and reentrant context managers")),
            required_points=(
                R(label="用 @contextmanager 包生成器", any_of=("contextmanager",)),
                R(label="yield 之前是 setup、之后是 teardown",
                  any_of=("yield",)),
                R(label="异常会被抛回 yield 处",
                  any_of=("thrown into the generator", "at the yield point")),
            ),
        ),
        TaskItem(
            task_id="p-hard-02",
            question=(
                "You must run N coroutines concurrently and collect their results, but one "
                "failure must not cancel the others. What do you use, and what is the failure boundary?"
            ),
            reference_answer=(
                "Use `asyncio.gather(*aws, return_exceptions=True)`: all awaitables run "
                "concurrently and results come back in order; with `return_exceptions=True` a "
                "failing task returns its exception **in place of** its result instead of "
                "propagating and cancelling the others. Without it, the first exception "
                "propagates and the other awaitables are **not cancelled** but their results "
                "are lost to the caller."
            ),
            source_document="python:asyncio-task",
            difficulty="hard",
            evidence=(E("asyncio-task", "overview"),),
            required_points=(
                R(label="用 asyncio.gather", any_of=("gather",)),
                R(label="return_exceptions=True 隔离失败",
                  any_of=("return_exceptions",)),
                R(label="结果按输入顺序返回", any_of=("in order", "order")),
            ),
        ),
        TaskItem(
            task_id="p-hard-03",
            question=(
                "You need to find all files under a directory tree matching a pattern across "
                "nested folders. What is the idiomatic pathlib approach, and what is its failure boundary?"
            ),
            reference_answer=(
                "`Path.rglob(pattern)` recursively yields matching paths under the directory "
                "(`glob` is non-recursive). It is a **lazy generator**, so nothing is scanned "
                "until you iterate; it follows the filesystem at call time, so files created "
                "after you start iterating may not appear, and on very large trees it can be "
                "slow because it walks directories one by one."
            ),
            source_document="python:pathlib",
            difficulty="hard",
            evidence=(E("pathlib", "Basic use"), E("pathlib", "Comparison to the glob module")),
            required_points=(
                R(label="用 rglob 递归匹配", any_of=("rglob",)),
                R(label="惰性生成器（迭代时才扫）",
                  any_of=("lazy", "generator", "iterate")),
            ),
        ),
    )
    return Dataset(topic="python", version="0.1", items=items)
