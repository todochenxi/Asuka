"""M85 / I-6：**"只能由工厂构造"必须是一条机制，不是一句愿望。**

--------------------------------------------------------------------------
起因：一句话和它的实施对不上

`packages/agent_domain/intelligence/observation.py` 的 I-6 唯一入口写着：

    "I-6：来自执行的 Observation 只能由这个工厂构造，必须绑定真实 Execution。"

但 `Observation` 是 `@dataclass(frozen=True)` —— `__init__` 是**公开**的，
而 `execution_id: str | None = None` 这个**默认值**本身就是漏洞的形状。

`probe84.py` 实测四扇门全开：

    直接构造 EXECUTION_RESULT 且不带 execution_id   → 成功
    execution_id=""（空串，同样没绑定）              → 成功
    非执行来源凭空挂 execution_id="exec_FAKE"        → 成功
    attempt_no=0 / -1（工厂拦得住，__init__ 拦不住）  → 成功

而 `from_execution_result` 里那两条校验**只对走工厂的人生效**。
**绕过的人当然不会走工厂。** 这是同义反复 ——
把锁挂在门上，却不装门框。

--------------------------------------------------------------------------
危害：账本的可解释性（不是洁癖）

破坏的是"这条结论有没有实证"这个问题。判据是**宁可拒绝，不许编造**：

    · 一条声称"我来自某次执行"的 Observation 可以完全没有 execution_id
      → 下游问"它是哪次执行的产出"时**问不出答案**
    · 一条 `human_input` 可以顺手挂上 execution_id
      → 于是"有 execution_id"**不再能推出**"它真的来自执行"，
        而 `reducer` / 审计查询正是靠这个字段分流的

第二条尤其隐蔽：它不是"多了一个非法值"，而是**让一个信号失去意义**。

--------------------------------------------------------------------------
修法：判据搬到 `__post_init__`

`__post_init__` 是**每一条构造路径都必须经过**的地方（工厂也走它），
所以它是这类不变量的正确落点（项目里已有先例：I-5 的 `content` 冻结、
I-7 的大对象检查都在这里）。

判据是**双向**的，缺一不可：

    来源是 EXECUTION_RESULT  ⟹  必须绑定 execution_id + attempt_no >= 1
    来源不是 EXECUTION_RESULT ⟹  不许绑定 execution_id

第二句看着严，但正是它让第一句有意义 —— 否则"有 execution_id"
就不再能推出"它来自执行"（见上面第 2 条危害）。

--------------------------------------------------------------------------
一个刻意的留口：`attempt_no` 对非执行来源仍然可选

`attempt_no` 是"这是第几次尝试"这种过程性注记（如超时提示），
单独出现不冒充实证。而 `execution_id` 是**指向实证的指针** ——
不能空着，也不能乱指。两者不是同一种东西，所以不一起收紧。

用 `probe84.py` 复验：四扇门全部拦住，而 1287 条既有测试**一条不红**
（说明真实代码本来就全部走工厂 —— 保证一直是"碰巧成立"，
不是"被机制守住"；这一轮补的正是把它变成后者）。
"""
from __future__ import annotations

import unittest

from packages.agent_domain.errors import InvariantViolation
from packages.agent_domain.intelligence.observation import (
    Observation,
    ObservationSource,
)

from .helpers import new_run

#: 三个"不是执行产出"的来源 —— 它们都**不许**挂 execution_id。
NON_EXECUTION_SOURCES = (
    ObservationSource.EXTERNAL_EVENT,
    ObservationSource.HUMAN_INPUT,
    ObservationSource.SYSTEM,
)


class ExecutionResultMustBeBoundTest(unittest.TestCase):
    """I-6 的正向：声称来自执行，就必须**指向**一次执行。"""

    def setUp(self) -> None:
        self.run_id = new_run()

    def _raw(self, **over):
        """绕过工厂直接构造 —— 这正是这一轮要治的那条路。"""
        kwargs = dict(
            run_id=self.run_id,
            source=ObservationSource.EXECUTION_RESULT,
            kind="tool_result",
            summary="raw construction",
        )
        kwargs.update(over)
        return Observation(**kwargs)

    # -------------------------------------------------- ★ 门 1：完全不绑
    def test_i6_raw_construction_without_execution_id_is_refused(self):
        """★ 主断言：不走工厂、也不带 execution_id 的 EXECUTION_RESULT 挡得住。

        改坏法（变红验证 M1）：删掉 `if not self.execution_id:` 那个分支
        → 这条必须红。

        ⚠️ `attempt_no` 必须给足：不给的话，拒绝理由可能是"attempt_no 缺失"
        而不是"没有 execution_id" —— 那样这条断言就没隔离到它声称守的那件事
        （见下面 `test_i6_empty_string_execution_id_is_refused_too` 记的教训）。
        """
        with self.assertRaises(InvariantViolation) as ctx:
            self._raw(attempt_no=1)
        self.assertIn("I-6", str(ctx.exception))
        self.assertIn("execution_id", str(ctx.exception))

    # -------------------------------------------------- ★ 门 2：空串绕过
    def test_i6_empty_string_execution_id_is_refused_too(self):
        """★ 空串与 None 同罪 —— 都表示"没有指向任何一次真实执行"。

        ------------------------------------------------------------------
        ⚠️ 这条测试**差一点就是假的** —— 记下这次教训

        第一版写成 `self._raw(execution_id="")`（**不带** `attempt_no`）。
        变红验证 M2（把 `if not self.execution_id` 改成
        `if self.execution_id is None`）**没红**。

        根因不是实现对了，而是**测试守错了东西**：
        空串在 M2 下会通过 execution_id 那道检查，但紧接着撞上
        **`attempt_no is None`** 那道（`attempt_no` 默认是 None）——
        于是它**照样抛** `InvariantViolation`，测试**照样绿**。

        也就是说：那条断言真正依赖的是"attempt_no 那道也拦住了它"，
        而不是它**声称**守的"空串本身被拦"。两道检查叠在一起，
        把它伪装成了一条有效的控制组 —— 项目里的老话：
        **变异没红先怀疑测试；问"我这条断言真能区分改坏前/后吗"。**

        修法：把 `attempt_no` **给足**（=1），让"空串"成为**唯一**可能的
        拒绝理由。这样 M2 下它必定放行 → 测试必红 → 断言是真的。

        这与 §119.5（M82 变红 M4 没红）是同一族错：**断言没隔离到它声称的那件事。**
        """
        # attempt_no 给足 —— 否则拒绝理由可能是 attempt_no 而不是空串
        # （给足了才能证明"是空串本身被拦"）
        with self.assertRaises(InvariantViolation) as ctx:
            self._raw(execution_id="", attempt_no=1)
        # 拒绝理由必须**点名 execution_id**，不能是碰巧被 attempt_no 拦下
        self.assertIn("execution_id", str(ctx.exception))

    # -------------------------------------------------- ★ 门 4：attempt 边界
    def test_i6_raw_construction_with_attempt_below_one_is_refused(self):
        """★ 工厂拦得住的，`__init__` 也必须拦得住（否则绕过工厂就绕过了它）。

        `probe84` case 4 实测：`attempt_no=0` / `-1` 走直构原样通过。

        ⚠️ `execution_id` 给足（=1 个真 id）：这样"attempt_no 非法"是
        **唯一**可能的拒绝理由 —— 否则它可能被"没绑 execution_id"那道拦下，
        断言就没隔离到 attempt_no 这道（同门 2 的教训）。
        """
        for bad in (0, -1):
            with self.subTest(attempt_no=bad):
                with self.assertRaises(InvariantViolation) as ctx:
                    self._raw(execution_id="exec_abc", attempt_no=bad)
                self.assertIn("attempt_no", str(ctx.exception))

    def test_i6_missing_attempt_no_is_refused(self):
        """绑了 execution_id 却没给 attempt_no —— 同样是"绑不全"。"""
        with self.assertRaises(InvariantViolation) as ctx:
            self._raw(execution_id="exec_abc")
        self.assertIn("attempt_no", str(ctx.exception))

    # -------------------------------------------------- 控制组：合法的要能过
    def test_the_control_a_fully_bound_one_is_allowed(self):
        """控制组：绑齐了的直构必须**放行**。

        少了这条，上面全部断言都可能被"一律拒绝"满足 ——
        那样的"修法"是假的（它会让工厂自己也构造不出来，
        但工厂那条路径被别的测试覆盖着，这里补一个正对的）。
        """
        obs = self._raw(execution_id="exec_abc", attempt_no=1)
        self.assertEqual(obs.execution_id, "exec_abc")
        self.assertEqual(obs.attempt_no, 1)
        self.assertIs(obs.source, ObservationSource.EXECUTION_RESULT)

    def test_the_control_the_factory_still_works(self):
        """控制组：工厂那条路**必须**照旧可用（修法不许把正门也堵上）。"""
        obs = Observation.from_execution_result(
            run_id=self.run_id,
            execution_id="exec_abc",
            attempt_no=1,
            kind="tool_result",
            summary="from the factory",
        )
        self.assertIs(obs.source, ObservationSource.EXECUTION_RESULT)
        self.assertEqual(obs.execution_id, "exec_abc")


class OnlyExecutionResultMayPointAtAnExecutionTest(unittest.TestCase):
    """I-6 的反向：**不许**让非执行来源指向一次执行。

    这一组守的不是"多了一个非法值"，而是**一个信号的意义** ——
    如果谁都能挂 execution_id，那"有 execution_id"就不再能推出
    "它来自执行"，而 reducer / 审计查询正是靠这个分流。
    """

    def setUp(self) -> None:
        self.run_id = new_run()

    def _raw(self, source, **over):
        kwargs = dict(
            run_id=self.run_id,
            source=source,
            kind="whatever",
            summary="not from an execution",
        )
        kwargs.update(over)
        return Observation(**kwargs)

    def test_i6_non_execution_sources_cannot_carry_an_execution_id(self):
        """★ 主断言：三个非执行来源，一个都不许挂 execution_id。

        改坏法（变红验证 M2）：删掉 `elif self.execution_id is not None:`
        那个分支 → 这条必须红（3 个子用例）。
        """
        for source in NON_EXECUTION_SOURCES:
            with self.subTest(source=source.value):
                with self.assertRaises(InvariantViolation) as ctx:
                    self._raw(source, execution_id="exec_FAKE")
                self.assertIn("I-6", str(ctx.exception))
                self.assertIn("EXECUTION_RESULT", str(ctx.exception))

    def test_i6_the_refusal_names_the_offending_source(self):
        """拒绝时要**点名是谁** —— 否则运维拿着报错也不知道改哪一行。"""
        with self.assertRaises(InvariantViolation) as ctx:
            self._raw(ObservationSource.HUMAN_INPUT, execution_id="exec_FAKE")
        self.assertIn("human_input", str(ctx.exception))

    # -------------------------------------------------- 控制组
    def test_the_control_non_execution_sources_without_an_id_are_fine(self):
        """控制组：不带 execution_id 时，三个非执行来源都必须**放行**。

        少了这条，上面的断言会被"一律拒绝非执行来源"满足 ——
        而那会把整个 SYSTEM / HUMAN_INPUT 通道堵死（13 处真实构造点全在这里）。
        """
        for source in NON_EXECUTION_SOURCES:
            with self.subTest(source=source.value):
                obs = self._raw(source)
                self.assertIsNone(obs.execution_id)
                self.assertIs(obs.source, source)

    def test_the_control_a_non_execution_source_may_still_carry_attempt_no(self):
        """★ 刻意留的口：`attempt_no` 对非执行来源**仍然可选**。

        它不是"指向实证的指针"，而是"这是第几次尝试"这种过程性注记 ——
        单独出现不冒充实证。所以两者不一起收紧。

        这条既是控制组，也是**设计决定的可执行说明**：
        将来若有人想顺手把 attempt_no 也一起禁掉，这条会红，
        逼他先读完上面那段理由。
        """
        obs = self._raw(ObservationSource.SYSTEM, attempt_no=3)
        self.assertEqual(obs.attempt_no, 3)
        self.assertIsNone(obs.execution_id)


class TheInvariantIsReachableFromEveryPathTest(unittest.TestCase):
    """判据住在哪一层，就要在哪一层验证它。

    I-6 的判据现在在 `__post_init__`。这一组直接断言
    **"每一条构造路径都经过 `__post_init__`"** 这件事本身 ——
    用一条不依赖任何具体字段的探针：
    造一个违法的直构，看它是否**无论从哪个入口**都被拦。
    """

    def test_both_entrances_hit_the_same_check(self):
        """★ 工厂与直构走的是同一道判据 —— 这是这一轮修法的核心。

        `from_execution_result` 因为先做了自己的校验，会先抛；
        所以要证明"同一道判据"必须**造一个工厂放行、判据拦住**的输入。
        找不到这种输入（工厂的校验 ⊇ 判据的校验）本身就是结论：
        工厂没有后门，判据才是底盘。
        """
        run_id = new_run()

        # 工厂对空 execution_id 先抛（它自己也有这条）
        with self.assertRaises(InvariantViolation):
            Observation.from_execution_result(
                run_id=run_id, execution_id="", attempt_no=1, kind="k", summary="s"
            )
        # 直构也被拦 —— 两条路都到不了"造出一条没绑定的执行观察"
        with self.assertRaises(InvariantViolation):
            Observation(
                run_id=run_id,
                source=ObservationSource.EXECUTION_RESULT,
                kind="k",
                summary="s",
            )

    def test_the_restore_roundtrip_stays_valid(self):
        """快照恢复那条路：合法 Observation 存-取之后**仍然合法**。

        `snapshot.py` 会逐字段重建 Observation。如果判据太严
        （比如要求一个恢复不出来的字段），恢复就会炸 ——
        那是个会在**重启时**才暴露的故障，值得在这里钉住。
        """
        run_id = new_run()
        obs = Observation.from_execution_result(
            run_id=run_id,
            execution_id="exec_abc",
            attempt_no=2,
            kind="tool_result",
            summary="round trip me",
        )
        # 模拟 snapshot 的重建方式（逐字段直构）
        rebuilt = Observation(
            observation_id=obs.observation_id,
            run_id=obs.run_id,
            source=obs.source,
            kind=obs.kind,
            summary=obs.summary,
            content=dict(obs.content),
            artifact_refs=obs.artifact_refs,
            execution_id=obs.execution_id,
            attempt_no=obs.attempt_no,
            created_at=obs.created_at,
        )
        self.assertEqual(rebuilt.execution_id, "exec_abc")
        self.assertEqual(rebuilt.attempt_no, 2)
        self.assertIs(rebuilt.source, ObservationSource.EXECUTION_RESULT)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
