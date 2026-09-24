"""Control Plane 的进程内实现（M18）。

`apps/api` 真正上线时这里是 HTTP → 服务 → DB；但**契约是一样的**，
所以路由层现在就能被测试，不用等基础设施。

**A-3：`POST /runs` 必须幂等，且幂等键不能放 Redis。**

`IdempotencyStore`（execution_kernel 的端口）语义明确写着：

> `get()` 返回 None 的含义是 **UNKNOWN**，不是"没执行过"。

这个语义对**外部副作用**是对的（缓存丢了就回查下游）。
但 Run 创建不一样 —— Redis 里的键丢了，我们没有"下游"可以回查，
结果就是**开出第二个 Run**：同一个用户请求跑两遍，花两份钱，
而且两个 Run 都可能产生外部副作用。

所以 Run 创建的幂等键必须与"创建 Run"在**同一个事务**里落 PG。
这里用 `InMemoryIdempotencyStore` 只是把契约钉住，真实实现换 PG 即可 ——
但换的时候不能换成 Redis。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from packages.agent_domain.errors import IllegalTransition
from packages.agent_harness.approval import (
    ApprovalStatus,
    ApprovalStore,
    InMemoryApprovalStore,
)
from packages.agent_runtime.assembly import RuntimeStack
from packages.agent_runtime.driving import snapshot_reason
from packages.agent_runtime.recovery import (
    RunRecovery,
    RunSnapshotStore,
    trace_entries_to_dicts,
)
from packages.execution_kernel.inmemory import InMemoryIdempotencyStore
from packages.execution_kernel.ports import IdempotencyStore

from . import idempotency
from .dto import (
    ApprovalView,
    DecisionRequest,
    RunView,
    StartRunRequest,
    TraceView,
)
from .errors import Conflict, Gone, NotFound


def _outcome_str(outcome: Any) -> str:
    """StepOutcome → 字符串。`None`（还没走过一步）必须是 `""` 而不是 `"None"`。

    后者会在页面上显示成一步叫 "None" 的动作 —— 一个凭空冒出来的状态，
    比留空更难解释。
    """
    if outcome is None:
        return ""
    return str(getattr(outcome, "value", outcome))


def _status_value(obj: Any) -> str:
    """枚举取 `.value`，其余取 `str`。避免页面看到 `AgentRunStatus.COMPLETED`。"""
    if obj is None:
        return ""
    return str(getattr(obj, "value", obj))


def layers_of(stack: Any, entries: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """M109：把一本次账按 AgentOS 的**层**摊开。

    账本本身是"只增的一串"（`trace.entries`），排障时人问的却是
    "**这一层**发生了什么"。两件事都留着：串是事实源，分层是视图。

    能摊出什么，取决于那一层往外说了多少：

        goal / plan      Intelligence（`state.goal` / `state.current_plan`）
        actions          `task.submitted`（ActionType → 交给 Kernel 的那一步）
        executions        `execution.observed`（Kernel 的执行）
        harness          `context.built` / `approval.requested` / `guardrail.rejected`
        state            终态与步数

    ⚠️ 摊不出 Goal / Plan 时**不补一个空壳** —— 缺就是缺（那是"这一层没登记"，
    不是"这一层是空的"）。
    """
    loop = stack.loop
    state = getattr(loop, "state", None)
    layers: dict[str, Any] = {}

    goal = getattr(state, "goal", None)
    if goal is not None:
        budget = getattr(goal, "budget", None)
        layers["goal"] = {
            "objective": str(getattr(goal, "objective", "")),
            "success_criteria": [str(x) for x in (getattr(goal, "success_criteria", ()) or ())],
            "max_steps": getattr(budget, "max_steps", None) if budget is not None else None,
        }

    plan = getattr(state, "current_plan", None)
    if plan is not None:
        layers["plan"] = {
            "nodes": [
                {
                    "node_id": str(getattr(node, "node_id", "")),
                    "name": str(getattr(node, "name", "")),
                    "kind": _status_value(getattr(node, "kind", None)),
                }
                for node in (getattr(plan, "nodes", ()) or ())
            ]
        }

    actions: list[dict[str, Any]] = []
    executions: list[dict[str, Any]] = []
    context: list[dict[str, Any]] = []
    approvals: list[int] = []
    guardrails: list[int] = []
    seen_exec: set[str] = set()
    for entry in entries:
        kind = str(entry.get("kind") or "")
        payload = dict(entry.get("payload") or {})
        if kind == "task.submitted":
            actions.append(
                {
                    "seq": entry.get("seq"),
                    "step_id": entry.get("step_id") or "",
                    "task_id": entry.get("task_id") or "",
                    "execution_id": entry.get("execution_id") or "",
                    "attempt_no": entry.get("attempt_no") or 0,
                    "action_type": str(payload.get("action_type") or ""),
                }
            )
        elif kind == "execution.observed":
            execution_id = str(entry.get("execution_id") or "")
            if execution_id and execution_id not in seen_exec:
                seen_exec.add(execution_id)
                executions.append(
                    {
                        "execution_id": execution_id,
                        "task_id": entry.get("task_id") or "",
                        "attempt_no": entry.get("attempt_no") or 0,
                        "status": str(payload.get("status") or ""),
                    }
                )
        elif kind == "context.built":
            context.append(
                {
                    "seq": entry.get("seq"),
                    "total_tokens": payload.get("total_tokens"),
                    "model_id": payload.get("model_id"),
                }
            )
        elif kind == "approval.requested":
            approvals.append(entry.get("seq"))
        elif kind == "guardrail.rejected":
            guardrails.append(entry.get("seq"))

    layers["actions"] = actions
    layers["executions"] = executions
    layers["harness"] = {
        "context": context,
        "approval_seqs": approvals,
        "guardrail_seqs": guardrails,
    }

    if state is not None:
        run = getattr(loop, "agent_run", None)
        layers["state"] = {
            "runtime_status": _status_value(getattr(state, "runtime_status", "")),
            "run_status": _status_value(getattr(run, "status", None)),
            "steps": len(getattr(loop, "steps_of_run", ()) or ()),
        }
    return layers


def last_llm_answer(state: Any) -> str:
    """这条 Run 最近一次 LLM 执行的输出文本；没有就返回空串。

    回答存在 Observation 的 `content["result"]["response"]["text"]` 里 ——
    它既不在 trace（trace 只记 `status` / `outcome`），也不在 `RunView`。
    聊天页需要它，所以这里把它读出来。

    读不到就**如实返回空串**，不拿别的字段冒充：`""`（没答）与
    "答了一句空话"是两件事，混起来会让聊天页把工具调用的输出当成回答。
    """
    observations = getattr(state, "observations", None) or ()
    for observation in reversed(tuple(observations)):
        content = getattr(observation, "content", None)
        if not isinstance(content, Mapping):
            continue
        result = content.get("result")
        if not isinstance(result, Mapping):
            continue
        response = result.get("response")
        if isinstance(response, Mapping) and response.get("text"):
            return str(response["text"])
    return ""


@dataclass
class InProcessControlPlane:
    """把 `assemble_runtime_stack()` 包成可被 HTTP 调用的服务。"""

    #: agent_id + 共享审批存储 → 一套 RuntimeStack 的工厂。
    #: 审批存储**由 ControlPlane 持有**并传下去：它不是某个 Run 的私有状态，
    #: 而是跨 Run、跨进程的共享事实（A-10）。
    factory: Callable[[str, ApprovalStore], RuntimeStack]
    approvals: ApprovalStore = field(default_factory=InMemoryApprovalStore)
    #: M20 / R-5：Run 快照存储。给了它，服务重启后 `decide()` 才能把 Run 装载回来 ——
    #: 只持久化审批（M19）解决的是"看得见"，这个解决的是"点得动"。
    snapshots: RunSnapshotStore | None = None
    idempotency: IdempotencyStore = field(default_factory=InMemoryIdempotencyStore)
    #: M34 / 空洞 222：Run 级取消意图。**跨进程取消的唯一通道** ——
    #: 一条跑在别的进程里的 Run，这条进程手上既没有它的 stack，
    #: 也（可能）没有它的快照，唯一能做的事就是在这里留一句话。
    cancellations: Any = None
    #: M61 / 空洞 231：agent 注册表。**`None` = 不校验**（维持现状，见 `start_run`）。
    registry: Any = None
    #: M65 / 空洞 234：Execution 查询器（`(run_id) -> list[dict]`）。
    #
    # 由**组合根**注入 —— service 层不 import 任何数据库客户端。
    # `None` 时端点返回空列表（不是 404：查询语义，空集不是错误）。
    executions: Any = None
    #: run_id → RuntimeStack。真实实现里这里是"从 DB 恢复"，不是内存字典
    runs: dict[str, RuntimeStack] = field(default_factory=dict)

    # ------------------------------------------------------------ Run
    def start_run(self, request: StartRunRequest) -> RunView:
        # 空洞 231：agent_id 存不存在，第一次有了答案。
        #
        # ⚠️ 只在**配了注册表**时校验。没配就维持现状 —— 各处的即席 agent id
        # （`agent-it` / `agent-api` / `agent-math` …）都不是登记过的名字，
        # 强制校验会把"没登记"和"写错了"混成一种错，顺带打红一堆无关用例。
        #
        # 于是"要不要强校验"交给部署决定：不配 = 透传（现状）；配了 = 真的生效。
        if self.registry is not None and not self.registry.knows(request.agent_id):
            raise NotFound(
                f"no agent {request.agent_id!r} in the registry",
                code="AGENT_NOT_FOUND",
                agent_id=request.agent_id,
            )
        if request.idempotency_key:
            cached = self.idempotency.get(
                idempotency.scoped(request.idempotency_key, namespace=idempotency.NAMESPACE_RUN)
            )
            if cached is not None:
                # M44：同一个键、不同的请求体 ⟹ 点名拒绝。
                # 这一条 `start_run` 原本是缺的（见 `idempotency.refuse_if_reused`）：
                # 只存了 run_id，于是换一个 `user_request` 重发同一个键，
                # 会静默拿回第一条 Run —— 用户改了需求，拿到的是旧 Run 的视图。
                idempotency.refuse_if_reused(
                    cached,
                    key=request.idempotency_key,
                    namespace=idempotency.NAMESPACE_RUN,
                    fingerprint=idempotency.fingerprint(
                        {
                            "agent_id": request.agent_id,
                            "user_request": request.user_request,
                        }
                    ),
                )
                # A-3：同一个键第二次来 → 返回第一次的结果，**绝不开第二个 Run**。
                #
                # 这里曾经有一处破法：只在内存 `runs` 里找，找不到就顺着往下走 ——
                # 于是重启后（内存空、PG 里的键还在）同一个键会开出第二个 Run，
                # 而键仍然指向第一个，第二个从此谁也查不到，却照样在花钱、
                # 照样产生外部副作用。
                # 幂等键落对存储只是解决了"键不丢"；**命中之后找不到**这一支
                # 同样不许退化成"当没执行过"。
                run_id = str(idempotency.answer_of(cached)["run_id"])
                existing = self.runs.get(run_id) or self._recover(run_id)
                if existing is None:
                    # 记录说它执行过，但结果装载不回来 —— 只能如实报，不能猜。
                    raise Conflict(
                        f"run {run_id!r} was created for this idempotency key but "
                        f"cannot be reloaded; refusing to create a second run",
                        code="RUN_NOT_RELOADABLE",
                        run_id=run_id,
                        hint="retry with the SAME key; never re-send it as a new request",
                    )
                self.runs[run_id] = existing
                view = self._view(existing)
                return RunView(**{**view.__dict__, "replayed": True})

        stack = self.factory(request.agent_id, self.approvals)
        stack.start(request.user_request)
        self.runs[stack.run_id] = stack

        if request.idempotency_key:
            # 这里存的仍然是一个**指针**（run_id），不是答案。
            # 与 `cancel_run` 相反，见那里 D-36 那段：
            # 开 Run 的答案里含"这条 Run 现在长什么样"，而它会变 ——
            # 所以回放必须重新装载，装载不回来就喊（RUN_NOT_RELOADABLE）。
            self.idempotency.put(
                idempotency.scoped(
                    request.idempotency_key, namespace=idempotency.NAMESPACE_RUN
                ),
                idempotency.envelope(
                    fingerprint=idempotency.fingerprint(
                        {
                            "agent_id": request.agent_id,
                            "user_request": request.user_request,
                        }
                    ),
                    answer={"run_id": stack.run_id},
                ),
            )
        return self._view(stack)

    def get_run(self, run_id: str) -> RunView:
        """查一条 Run。

        M74：终态 Run **查得到**，只是推不动。

        `_recover()` 让 R-3（终态不可恢复）一路抛上来 —— 那对
        **推进 / 取消**是对的：它们要 409，意思是"这条已经结束了，别再动它"。

        但对 `GET` 不是。查一条已经完成的 Run 得到 409，
        表达的是"这个操作不被允许"，而调用方问的只是"它现在是什么状态" ——
        于是人以为这条 Run 出了事，去查一个根本没问题的地方。

        尤其在 Kubernetes 里：Pod 重启之后内存里没有这条 Run，
        每一次"查我昨天跑完的那条"都会撞上这条分支。
        """
        stack = self.runs.get(run_id)
        if stack is None:
            try:
                stack = self._must_stack(run_id)
            except IllegalTransition:
                view = self._terminal_view(run_id)
                if view is None:
                    raise
                return view
        return self._view(stack)

    def _terminal_view(self, run_id: str) -> RunView | None:
        """从快照读出一条**终态** Run 的样子（M74）。

        终态 Run 装载不回来（R-3），但它的**事实**还在快照里：
        run_id、agent_id、status、走了几步。
        把它们说出来，比报一个"不允许"更接近调用方问的事。

        刻意**不**编造 `waiting_for` / `pending_approval`：
        终态 Run 什么都没在等，说它在等谁就是谎报。
        """
        if self.snapshots is None:
            return None
        snapshot = self.snapshots.latest(run_id)
        if snapshot is None:
            return None
        return RunView(
            run_id=snapshot.run_id,
            agent_id=snapshot.agent_id,
            status=snapshot.status,
            step_count=snapshot.step_count,
            last_outcome="",
            waiting_for=None,
            pending_approval=None,
            cancel_requested=False,
        )

    def answer(self, run_id: str) -> str:
        """这条 Run 最近一次 LLM 执行的回答（聊天页 `POST /chat` 用）。

        只回答**还活在这一进程里**的 Run —— 聊天页刚开的那条一定在。
        一条已经不在手上的 Run 返回空串，而不是去猜：
        回答不落快照，猜出来的东西没有第二种查证方式。
        """
        stack = self.runs.get(run_id)
        if stack is None:
            return ""
        return last_llm_answer(getattr(stack.loop, "state", None))

    # ------------------------------------------------------------ 推进（F-1）
    def step_run(self, run_id: str) -> RunView:
        """F-1：走**一步**。

        为什么必须有它：M28 之前契约层只有 `start_run`，而它调的是
        `stack.start()` —— 那只做初始化（interpret → goal → state），不跑。
        于是通过 API 开出来的 Run 永远停在 `created`：
        不产生审批、不做工具调用、永不完成。
        **一个开得出却跑不动的 API，比没有 API 更危险** ——
        它看起来是通的。

        B-7 的落点：这里**不**声明终态。终态由 Runtime（`AgentLoop`）在
        `step()` 内部按自己的判据声明，契约层只是把它读出来。
        """
        stack = self._must_stack(run_id)
        self._assert_advancable(stack)
        outcome = stack.loop.step()
        self._persist_after_advance(stack, outcome=outcome)
        return self._view(stack, last_outcome=_outcome_str(outcome))

    def drive_run(self, run_id: str) -> RunView:
        """F-1：一路走到**停下来**为止（挂起 / 终态 / 预算耗尽）。

        与 `step_run` 的区别不是"批量"，是**语义**：
        `run()` 会在遇到闸门时**自己停下**，而连续调 `step()` 若干次
        等于由调用方重新实现一遍"什么时候该停" ——
        那样"一个 Run 什么时候停下来"就有了第二个定义（B-7）。

        **刻意不提供 `max_steps` 参数。** 它看起来是个无害的保险丝，
        实际上是在给"一个 Run 能走几步"开第三个定义：
        第一个是 `Goal.budget.max_steps`（Intelligence），
        第二个是 `AgentLoop.run()` 的停止集合（Runtime），
        再让 HTTP 调用方传一个，三者不一致时没人说得清以哪个为准。
        真正需要上界时，改的是 Goal 的预算 —— 那是它该待的地方。
        """
        stack = self._must_stack(run_id)
        self._assert_advancable(stack)
        loop = stack.loop
        # `run()` 返回的是 `State`，不是"最后一步是什么"（`step()` 才返回后者）。
        # 所以这里读 `loop.last_outcome` —— 那正是 F-1 把结果记在 Runtime 侧的原因。
        loop.run()
        self._persist_after_advance(stack, outcome=loop.last_outcome)
        return self._view(stack, last_outcome=_outcome_str(loop.last_outcome))

    # ------------------------------------------------------------ 叫停（B-8）
    def cancel_run(
        self,
        run_id: str,
        *,
        reason: str,
        by: str,
        idempotency_key: str = "",
    ) -> RunView:
        """B-8：把一条 Run 叫停（空洞 221）。

        ------------------------------------------------------------------
        D-35：取消是**写操作**，所以它也吃幂等键（A-3 / 空洞 223）

        M44 之前 `cancel_run` 没有键。于是客户端一次普通的重试
        （点"停止"之后网络超时，再点一次）拿到的是：

            第一次  200  取消成功
            第二次  409  RUN_TERMINAL

        它如实报了 —— 那条 Run 确实终态了。但报的方式是**一个错误码**，
        于是 UI 只能显示"停止失败"，而事实上停止成功了。更糟的是
        409 分不出"是被我叫停的"还是"它自己跑完了"：
        客户端要么把它显示成失败，要么**猜**它是成功。

        A-3 说写操作要支持幂等键，要的正是这一件：
        **重试拿到第一次的答案**，而不是拿到一个错误码去猜。

        ------------------------------------------------------------------
        D-36：记录里存的是**答案**，不是一个指向活对象的指针

        `start_run` 存的是 `{"run_id": ...}`（指针），所以它的回放必须
        重新装载，装载不回来就得喊（`RUN_NOT_RELOADABLE`）。

        `cancel_run` 反着来：存第一次给出的那份 `RunView`。
        因为取消的答案里最要紧的那一半 —— "这次叫停被受理了没有"
        （`cancel_requested`） —— 恰恰**装载不回来**：
        跨进程取消那条路上，这条进程手上既没有 stack 也没有快照，
        它能给出的答案只有"我留了一句话"（`status="unknown"`）。

        若照抄 `start_run` 的写法，那一次成功的跨进程取消在重试时
        会因为"装载不回来"而被报成 409 ——
        **把一次已经发生的取消报成失败**，正是这一轮要治的病。

        两个方向的判据相反，不是矛盾，是它们保护的东西不同：
        start 的键保护"不要开第二条 Run"（装载不回来 ⟹ 宁可喊）；
        cancel 的键保护"不要把已经发生的取消报成失败"
        （装载不回来 ⟹ 照样给当时给过的那份答案）。

        ------------------------------------------------------------------
        失败**不**占键

        幂等键的语义是"这次写已经发生了，别再做一次"。
        一次失败的写没有发生，不应当占住这个键 ——
        否则一次瞬时故障会把这个键永久作废，重试再也修不好。
        `guard` 把异常翻成响应，所以这里只要把 `put` 放在成功之后即可。

        ------------------------------------------------------------------

        M33 之前这条不存在 —— 一条 Run 只能被推进，不能被叫停。
        而一个能派生子 Run、能挂起等人审批的平台，缺了"算了别跑了"，
        就等于把"停"这个动作的实现权交给了 kill -9。

        B-7：这里**不**声明终态，也不复制一份"终态长什么样"。
        取消的落点在 `AgentLoop.cancel()`（Runtime），
        这里只是把它暴露成一个能被 HTTP 调用的动作。

        ------------------------------------------------------------------
        装载不了 ≠ 不存在（空洞 222 的下半）

        一条 Run 不在这个进程的内存里，且**没有快照**（R-1：快照只在挂起时拍），
        那它多半正跑在**别的进程**里。`_must_stack()` 会把它报成
        404 RUN_NOT_FOUND —— 而它明明活着。

        页面上的后果很直接：控制台看得到这条 Run（另一个接口给的），
        点"叫停"却得到"没有这条 Run"。于是"停"这个动作在跨进程下
        只能由运维来做 —— 又回到 M33 之前那个形状。

        所以装载不了时改走**意图**：落一条持久的取消请求，
        由那条 Run 自己的安全点或 Sweeper 认领，并如实把
        `cancel_requested=True` 交回调用方（不是谎报"已取消"）。
        """
        scoped = ""
        mark = idempotency.fingerprint({"run_id": run_id, "reason": reason, "by": by})
        if idempotency_key:
            scoped = idempotency.scoped(
                idempotency_key, namespace=idempotency.NAMESPACE_CANCEL
            )
            cached = self.idempotency.get(scoped)
            if cached is not None:
                idempotency.refuse_if_reused(
                    cached,
                    key=idempotency_key,
                    namespace=idempotency.NAMESPACE_CANCEL,
                    fingerprint=mark,
                )
                # D-36：回放**第一次给出的那份答案**，不重新推导。
                #
                # 重新推导会让同一个键在两个时刻给出两个答案 —— 那就不叫幂等了。
                # "这条 Run 现在怎么样了"是 `GET /runs/{id}` 的职责，
                # 不是幂等键的职责。
                first = RunView.from_dict(idempotency.answer_of(cached))
                return RunView(**{**first.__dict__, "replayed": True})

        view = self._cancel_now(run_id, reason=reason, by=by)

        if scoped:
            # 成功之后才记 —— 失败不占键（见 docstring 最后一段）。
            self.idempotency.put(
                scoped,
                idempotency.envelope(fingerprint=mark, answer=view.to_dict()),
            )
        return view

    def _cancel_now(self, run_id: str, *, reason: str, by: str) -> RunView:
        """真的去叫停（被 `cancel_run` 包着，键的处理在那一层）。"""
        stack = self._try_stack(run_id)
        if stack is None and self.cancellations is None:
            # 没有持久通道：只能叫停装载得回来的那条 Run。
            # 装载不了就让它按老样子如实报（404 RUN_NOT_FOUND / 409 RUN_TERMINAL），
            # 不另发明一个错 —— "这条 Run 不存在"与"这条进程叫不动它"
            # 在没有通道时**无法区分**，而猜一个更糟。
            # 生产一定有通道（组合根 `_REQUIRED_EXTRAS["cancellations"]`）。
            stack = self._must_stack(run_id)        # 必然抛
        if stack is None:
            return self._request_cancellation(run_id, reason=reason, by=by)
        self._assert_cancellable(stack)
        outcome = stack.loop.cancel(reason=reason, by=by)
        self._persist_after_advance(stack, outcome=outcome)
        return self._view(stack, last_outcome=_outcome_str(outcome))

    def _request_cancellation(self, run_id: str, *, reason: str, by: str) -> RunView:
        """跨进程：留一句话，而不是谎报一个结果。"""
        if self.cancellations is None:
            raise NotFound(
                f"run {run_id!r} is not driven by this process and no "
                f"RunCancellationStore is configured, so there is no way to tell "
                f"it to stop — refusing to report a cancellation that reached "
                f"nobody",
                code="RUN_NOT_CANCELLABLE",
                run_id=run_id,
            )
        self.cancellations.request(run_id, reason=reason, by=by)
        snapshot = self.snapshots.latest(run_id) if self.snapshots else None
        return RunView(
            run_id=run_id,
            agent_id=snapshot.agent_id if snapshot is not None else "",
            # 有快照就用快照上的状态；没有就如实说"不知道"。
            # 猜一个 `running` 会让人以为系统看得见它，而事实上看不见。
            status=snapshot.status if snapshot is not None else "unknown",
            cancel_requested=True,
        )

    def _persist_after_advance(self, stack: RuntimeStack, *, outcome: Any) -> None:
        """M73：推进（或叫停）之后必须落一个可恢复点。

        这个方法是被一次真实部署逼出来的。此前 HTTP 推进路径
        （`step_run` / `drive_run` / `cancel_run`）只在**内存**里改状态，
        从不落快照；而后台进程走的 `RunDriver.drive()` 每次都落。

        单机长跑时看不出区别 —— 内存一直在，查什么都对。
        放进 Kubernetes 之后区别是立刻的、而且方向很坏：

            一条 Run 通过 API 推进到 completed
            → api Pod 重启（滚动更新 / 探活重启 / 节点漂移）
            → 内存没了，`_try_stack` 从 `run_snapshots` 装载
            → 装载到的是**推进之前**那张快照
            → 那条 Run 显示为 `created`，还挂着一个已经 approved 的审批

        也就是说：**持久化只在后台路径上成立**，而线上绝大多数 Run
        恰恰是通过 API 推进的。这个洞在没有 Pod 重启的环境里
        永远不会被发现 —— 它只在"进程真的会死"的地方出现。

        `snapshots is None` 时什么都不做：没有快照存储就没有"恢复"这回事，
        那时它是纯内存的控制面（测试与演示），落一次只会得到
        一个写不进去的报错。
        """
        if self.snapshots is None:
            return
        self.snapshots.save(stack.loop.capture(reason=snapshot_reason(outcome)))

    def _assert_cancellable(self, stack: RuntimeStack) -> None:
        """B-10：终态 Run 不可取消 —— 与 `_assert_advancable` 是**同一条判据**。

        "终态之后不许再动它"只有一个定义（B-7），所以这里不另列终态集合，
        判据在 `AgentRun.is_terminal`（领域对象）。

        静默返回成功的后果比推进更糟：调用方以为自己**按停了**一条 Run，
        而那条 Run 其实早就停了 —— 而且可能是 COMPLETED。
        """
        run = stack.loop.agent_run
        if run is not None and run.is_terminal:
            raise Conflict(
                f"run {run.run_id!r} is already {run.status.value}; "
                f"a terminal run cannot be cancelled",
                code="RUN_TERMINAL",
                run_id=run.run_id,
            )

    def _assert_advancable(self, stack: RuntimeStack) -> None:
        """F-2：终态之后再推进 → 明说，不静默成功。

        静默成功有两种后果，都很难查：
          1. 调用方以为还有下一步，一直轮询；
          2. `step()` 会在账本里再记一条 `run.finished`（M28 探针实测过 3 条重复），
             于是"这个 Run 完成了几次"变成一个有歧义的问题。

        判据放在 `AgentRun.is_terminal`（领域对象）而不是让契约层自己列终态集合 ——
        "哪些状态是终态"是领域的知识，契约层复制一份就会有第二个版本（B-7）。
        """
        run = stack.loop.agent_run
        if run is not None and run.is_terminal:
            raise Conflict(
                f"run {run.run_id!r} is already {run.status.value}; "
                f"a terminal run cannot be advanced",
                code="RUN_TERMINAL",
                run_id=run.run_id,
            )

    def get_trace(self, run_id: str) -> TraceView:
        """F-4：账本。排障与审计的入口，也是页面"看得见流"的数据源。

        M109：同一本账**按层再摊一份**（`layers`）—— 串是事实源，分层是视图。
        """
        stack = self._must_stack(run_id)
        entries = trace_entries_to_dicts(stack.loop.trace)
        return TraceView(
            run_id=run_id,
            entries=entries,
            step_count=len(stack.loop.steps_of_run),
            layers=layers_of(stack, entries),
        )

    # ------------------------------------------------------------ 审批
    def list_approvals(self, run_id: str | None = None) -> Sequence[ApprovalView]:
        """A-10：待审批列表查**存储**，不查内存里的 Loop。

        这不只是"更干净"—— 审批必须能活过进程重启（它被写在 Kernel 里那条
        SUSPENDED 的 Execution 上）。如果这里读 `loop.pending_approval`，
        那么服务一重启，列表就空了，而 Run 还实实在在挂着等人批：
        **界面上什么都没有，系统里全在等**。这是最难排查的一类故障。

        注意这里**不**先查 Run 在不在内存里：列表是**过滤语义**，空集不是错误。
        如果先 `_must_stack()`，那么服务重启后（Run 还没被重新装载）
        列表会直接 404 —— 恰好在最需要它的时候看不见东西。
        """
        return tuple(
            self._approval_view(a) for a in self.approvals.pending(run_id)
        )

    def decide(self, run_id: str, request: DecisionRequest) -> RunView:
        """A-4：审批状态的**唯一**改变入口，且必须经 `AgentLoop.approve()`。

        不能让 HTTP 层直接改 `ApprovalRequest.status` ——
        那样 Kernel 里那条 SUSPENDED 的 Execution 不会被唤醒，
        Run 就永远挂在等一个"已经批过了"的审批上。
        """
        stack = self._must_stack(run_id)
        loop = stack.loop

        # A-5 第一步：从**审批存储**里查，不是从 Loop 的内存字段里查。
        # 审批必须能活过进程重启（它被写在 Kernel 里那条 SUSPENDED 的 Execution 上），
        # 所以"它存不存在 / 有没有决定过"要以存储为准。
        stored = self.approvals.get(request.approval_id)
        # 归属校验用 404 而不是 403 —— 不泄漏"另一个 Run 里有这条审批"这个事实。
        if stored is None or stored.run_id != run_id:
            raise NotFound(
                f"no approval {request.approval_id!r} for run {run_id!r}",
                code="APPROVAL_NOT_FOUND",
            )
        if stored.status is ApprovalStatus.EXPIRED:
            # A-9：没人决定过它，是时间判死的。报 409 会让人继续等一个不会来的结果。
            raise Gone(
                f"approval {request.approval_id} expired at "
                f"{stored.expires_at.isoformat() if stored.expires_at else '?'}",
                code="APPROVAL_EXPIRED",
            )
        if stored.status is ApprovalStatus.CANCELLED:
            raise Conflict(
                f"approval {request.approval_id} was cancelled",
                code="APPROVAL_CANCELLED",
                cancelled_by=stored.decided_by or "system",
            )
        if stored.status is not ApprovalStatus.PENDING:
            # A-5 第二步：已决定的审批再回调 → 409，不能静默成功。
            # 静默成功意味着审计记录被第二次调用覆盖 —— "谁批的"就说不清了。
            raise Conflict(
                f"approval {request.approval_id} already {stored.status.value}",
                code="APPROVAL_ALREADY_DECIDED",
                decided_by=stored.decided_by or "",
            )
        if stored.expired_at(stack.clock.now()):
            # H-8：存储里还写着 PENDING，但时间已经过了 ——
            # 只差一次 `expire_due()` 没扫。**这里只读地报，不推进状态**：
            # 把状态写成 EXPIRED 是 sweeper 的职责（跟 Cancellation 的 sweep 同构），
            # API 顺手改掉的话，"这条审批是被人判死的还是被时间判死的"就混在一起了。
            raise Gone(
                f"approval {request.approval_id} expired but is still PENDING; "
                "run expire_due() before deciding",
                code="APPROVAL_EXPIRED",
                pending=True,
            )
        approval = loop.pending_approval
        if approval is None or approval.approval_id != request.approval_id:
            # 存储里是 PENDING，但当前 Loop 没在等它 —— 状态不一致，不能硬放行
            raise Conflict(
                f"approval {request.approval_id} is not gating run {run_id!r}",
                code="APPROVAL_NOT_GATING",
            )

        outcome = (
            loop.approve(by=request.by, comment=request.comment)
            if request.approved
            else loop.reject(by=request.by, comment=request.comment)
        )
        # F-1：放行不是"什么都没发生"，它**执行了**被挂起的那一步。
        # 不把它报出来，页面上这一步就显示为空白 —— 而事实上副作用已经发生了。
        return self._view(stack, last_outcome=_outcome_str(outcome))

    # ------------------------------------------------------------ 内部
    def _must_stack(self, run_id: str) -> RuntimeStack:
        stack = self._try_stack(run_id)
        if stack is None:
            raise NotFound(f"run {run_id!r} not found", code="RUN_NOT_FOUND")
        return stack

    def _try_stack(self, run_id: str) -> RuntimeStack | None:
        """装载一条 Run；装载不了返回 `None`，**不**替调用方决定那意味着什么。

        "装载不了"至少有三种含义，而它们的正确处置完全不同：

            真的没有这条 Run      → 404（推进 / 查询）
            终态 Run（R-3）       → 409（推进 / 取消）
            它在**别的进程**里跑  → 留一条取消意图（取消）

        所以判据交给调用方 —— 只有它知道问的是哪一种。
        """
        stack = self.runs.get(run_id)
        if stack is None:
            # 内存里没有 ≠ 这个 Run 不存在 —— 它可能是**重启之前**建的。
            stack = self._recover(run_id)
            if stack is None:
                return None
            self.runs[run_id] = stack
        return stack

    def _recover(self, run_id: str) -> RuntimeStack | None:
        """R-5：怎么重建由 Runtime 说了算，这里只负责调用。"""
        if self.snapshots is None:
            return None
        recovery = RunRecovery(
            snapshots=self.snapshots,
            factory=self.factory,
            approvals=self.approvals,
        )
        try:
            return recovery.rebuild(run_id)
        except LookupError:
            # 没有快照 = 没有可恢复点，那就是真没这个 Run
            return None
        # IllegalTransition（终态 Run，R-3）故意不接 —— 它是 409，不是 404

    def _view(self, stack: RuntimeStack, *, last_outcome: str = "") -> RunView:
        loop = stack.loop
        run = loop.agent_run
        assert run is not None
        # F-3：挂起时在等谁 —— 审批 id 或子 Run id（R-6）。
        # 少了它，一个挂起的 Run 在界面上只是"卡住了"，
        # 而不是"在等 X"，于是没人知道该去点哪一个按钮。
        waiting_for = None
        if loop.pending_approval is not None:
            waiting_for = loop.pending_approval.approval_id
        elif loop.pending_child is not None:
            waiting_for = loop.pending_child.child_run_id
        return RunView(
            run_id=run.run_id,
            agent_id=run.agent_id,
            status=run.status.value,
            step_count=len(loop.steps_of_run),
            pending_approval=(
                self._approval_view(loop.pending_approval)
                if loop.pending_approval is not None
                else None
            ),
            last_outcome=last_outcome,
            waiting_for=waiting_for,
        )

    def _approval_view(self, approval) -> ApprovalView:
        question = ""
        if approval.action is not None:
            question = str(approval.action.payload.get("question") or approval.reason)
        return ApprovalView(
            approval_id=approval.approval_id,
            run_id=approval.run_id,
            status=approval.status.value,
            question=question,
            requested_at=approval.requested_at,
            expires_at=approval.expires_at,
            execution_id=approval.execution_id or "",
        )
