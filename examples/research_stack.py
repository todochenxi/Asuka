"""企业「研究与尽调」Agent 的示例栈 —— `AGENTOS_STACK_PROVIDER` 的一个真实行业实现。

--------------------------------------------------------------------------
它跟 `demo_stack.py` 的区别

`demo_stack.py` 是**给内核看**的：两步走完，用来证明 Kernel / Harness /
Runtime 那条链路是通的。它不假装会思考，也不假装在做某件事。

本文件是**给行业看**的：同一个装配路径（`assemble_runtime_stack`），
换成"研究一家公司"这件事真正需要的形状：

    1. 一个目标 = 四个主题（工商 / 财务 / 舆情 / 风险）
    2. 每个主题一次检索，检索结果**带来源**
    3. 最后一步写报告 —— 它是 WRITE，所以被治理层拦下等人签字（I-9）
    4. 报告**不许凭空生成**：任一主题没有证据就拒绝出稿

--------------------------------------------------------------------------
为什么尽调这件事特别吃 AgentOS

尽调的失败方式跟"模型不够聪明"基本无关，而跟下面这些有关 ——
它们恰好是 AgentOS 建的那些能力：

    · 结论要能追溯（每个说法挂得出来源）  → 只增账本 + 证据
    · 报告要发出去（外部副作用，且不可逆）→ S-8 声明补偿 + 治理闸门
    · 跑一半成本失控 / 方向跑偏          → 随时叫停（B-8/B-9）
    · 长跑（多源、多步、几分钟到几小时）  → 检查点 + 续跑
    · 数据源调用要花钱，重试不能重复付    → 幂等键落 PG

--------------------------------------------------------------------------
语料是内置的、确定性的 —— 这一条是刻意的

真实部署把 `CORPUS` 换成工商 / 财报 / 专利 / 新闻的 API 适配器，
`build_tool_runtime()` 那三行注册表之外**一行都不用改**。

这里**不接真的网络**：一个会静默失败的检索比一个本地语料危险得多 ——
前者会让它拿着"没查到"当成"查过了，没有"，而后者至少明说自己是语料。
这也正是本项目一贯的立场：不假装。
"""
from __future__ import annotations

from typing import Any, Mapping

from packages.agent_runtime.model_gateway import (
    CompletionRequest,
    CompletionResponse,
    Deployment,
    FunctionProvider,
    Model,
    ModelGateway,
    ModelRouter,
    ok_response,
)
from packages.agent_runtime.tool_runtime import (
    FunctionInvoker,
    SideEffect,
    ToolRuntime,
    ToolSpec,
)
from packages.agent_runtime.tool_runtime import ToolRegistry as RuntimeToolRegistry

# 四个尽调主题。真实部署里它们各自对应一条子 Run（委派），
# 这里为了在一个进程里演示，排成顺序的四个 Step。
TOPICS: tuple[str, ...] = ("工商", "财务", "舆情", "风险")

#: 内置语料。**刻意离线、确定性** —— 见本模块 docstring 最后一节。
CORPUS: dict[str, dict[str, list[dict[str, str]]]] = {
    "云澜科技": {
        "工商": [
            {"source": "国家企业信用信息公示系统", "fact": "成立于 2016-04，注册资本 5000 万元，存续"},
            {"source": "企查查", "fact": "实控人 陈澜，持股 41.2%；三家全资子公司"},
        ],
        "财务": [
            {"source": "2024 年报（审计：天衡）", "fact": "营收 3.7 亿，同比 +18%；净利润 -2100 万"},
            {"source": "2024 年报", "fact": "经营性现金流 -4300 万；应收账款周转 121 天"},
        ],
        "舆情": [
            {"source": "裁判文书网 2025-03", "fact": "劳动合同纠纷 2 起，均已结案，合计赔付 31 万"},
            {"source": "行业媒体 2025-06", "fact": "主要客户 A 集团母公司被列为被执行人"},
        ],
        "风险": [
            {"source": "央行征信（授权查询）", "fact": "无不良信贷记录；对外担保 1200 万"},
            {"source": "实地走访", "fact": "总部办公地在租，租期至 2027-09"},
        ],
    },
}

#: 已经发出去的报告。撤销要用到它（S-2：一次执行一条记录）。
_SENT: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


class NoEvidence(Exception):
    """证据不足 —— 报告不许出稿。"""


def _search(args: Mapping[str, Any]) -> Mapping[str, Any]:
    """检索一个主题。**每条结论都带 `source`**，没有来源的不算证据。"""
    company = str(args.get("company", "")).strip()
    topic = str(args.get("topic", "")).strip()
    hits = CORPUS.get(company, {}).get(topic, [])
    return {
        "company": company,
        "topic": topic,
        "hits": hits,
        "hit_count": len(hits),
        # 查不到必须明说"查不到"，不许让下游把它当成"查过了，没有"。
        "status": "found" if hits else "no_data",
    }


def _write_report(args: Mapping[str, Any], *, idempotency_key: str = "") -> Mapping[str, Any]:
    """写一版尽职调查报告草稿（WRITE）。

    ⚠️ 两道硬校验，任何一道不过就**拒绝出稿**而不是"先写着"：

        1. `idempotency_key` 由 ToolRuntime 注入（T-2），没有就是配置错了；
        2. **四个主题每一个都必须有证据**。任一主题 `hit_count == 0`
           ⟹ 抛错并点名是哪个主题。

    第 2 条是尽调的命门：一份"看起来完整"但某个维度其实没查到的报告，
    比一份明说"这个维度没查到"的报告危险得多 —— 前者会被当成查过了。
    """
    if not idempotency_key:
        raise NoEvidence(
            "T-2: report.write 是 WRITE 工具，必须带 idempotency_key "
            "（重复投递不得产生第二份报告）"
        )
    company = str(args.get("company", "")).strip()
    topics = list(args.get("topics") or TOPICS)

    missing: list[str] = []
    sections: list[dict[str, Any]] = []
    for topic in topics:
        hits = CORPUS.get(company, {}).get(topic, [])
        if not hits:
            missing.append(topic)
            continue
        sections.append({"topic": topic, "evidence": hits})

    if missing:
        raise NoEvidence(
            f"EVIDENCE_MISSING: 这些主题没有查到任何证据，报告不许出稿：{missing}。"
            f"（尽调报告里'没查到'必须被看见，不能被一份完整的排版盖住）"
        )

    report_id = f"rpt_{abs(hash((company, idempotency_key))) % 10**8:08d}"
    _SENT[report_id] = {"company": company, "sections": sections}
    return {
        "report_id": report_id,
        "company": company,
        "status": "draft",
        "section_count": len(sections),
        "idempotency_key": idempotency_key,
    }


def _withdraw_report(args: Mapping[str, Any], *, idempotency_key: str = "") -> Mapping[str, Any]:
    """撤销一份已发出的报告（S-2 / S-3）。"""
    report_id = str(args.get("report_id", ""))
    existed = _SENT.pop(report_id, None)
    return {"report_id": report_id, "withdrawn": existed is not None}


def build_tool_runtime() -> ToolRuntime:
    registry = RuntimeToolRegistry()
    registry.register(
        ToolSpec(name="corpus.search", version="1.0.0", description="检索一个尽调主题，返回带来源的证据",
                 side_effect=SideEffect.READ,
                 input_schema={"required": ["company", "topic"]}),
        FunctionInvoker(_search),
    )
    # WRITE：T-2 要求调用方给出去重键，否则 ToolRuntime 直接拒绝。
    registry.register(
        ToolSpec(name="report.write", version="1.0.0", description="生成尽调报告草稿（证据不足则拒绝出稿）",
                 side_effect=SideEffect.WRITE,
                 input_schema={"required": ["company", "topics"]}),
        FunctionInvoker(_write_report, pass_idempotency_key=True),
    )
    registry.register(
        ToolSpec(name="report.withdraw", version="1.0.0", description="撤销一份已发出的报告",
                 side_effect=SideEffect.WRITE),
        FunctionInvoker(_withdraw_report),
    )
    return ToolRuntime(registry)


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------

_MODEL = Model(model_id="research-1", name="research")
_DEPLOYMENT = Deployment(
    deployment_id="research-1@in-process",
    model_id="research-1",
    provider="research",
    endpoint="in-process",
)


def _research_complete(deployment: Deployment, request: CompletionRequest) -> CompletionResponse:
    """确定性"模型"。真实部署换成接 LLM 的 HTTP 适配器，**其他一行都不改**。"""
    text = f"research-1 synthesized {len(request.prompt)} chars"
    return ok_response(
        deployment, request, text=text,
        prompt_tokens=max(1, len(request.prompt) // 4),
        completion_tokens=max(1, len(text) // 4),
        metadata={"prompt": request.prompt},
    )


def build_model_gateway() -> ModelGateway:
    return ModelGateway(
        ModelRouter([_MODEL], [_DEPLOYMENT]),
        {"research": FunctionProvider("research", _research_complete)},
        max_fallbacks=0,
        default_model_id=_MODEL.model_id,
    )


# ---------------------------------------------------------------------------
# 智能体栈
# ---------------------------------------------------------------------------


def _company_of(user_request: str) -> str:
    """从"研究 XX 公司"里取出公司名。

    真实部署换成**实体识别 + 消歧**（同名公司、简称、英文名）——
    这里用最笨的办法，因为笨办法的失败方式是**看得见的**：
    认不出来就认不出来，后面会因为"查不到证据"被拒绝出稿，
    而不是拿一个猜错的公司去查。
    """
    text = user_request.strip()
    for name in CORPUS:
        if name in text:
            return name
    for verb in ("研究", "尽调", "调查", "查一下", "查"):
        if text.startswith(verb):
            text = text[len(verb):].strip()
            break
    return text.strip(" 的公司") or text


class ResearchInterpreter:
    """把"研究 XX 公司"变成一个 Goal。

    成功判据里刻意写了两条：证据齐 **且** 报告草稿已生成并待批。
    少了第二条，这个 Run 会在"查完了"的时候就宣称成功 ——
    而尽调的交付物是报告，不是"我查过了"。

    公司名进 `metadata` 而不是藏在引擎里：
    引擎是**每 Run 一个**的，而"这次要研究谁"是 Goal 的事实。
    """

    def interpret(self, user_request: str, context: Mapping[str, Any]) -> Any:
        from packages.agent_domain.intelligence.goal import Budget, Goal

        return Goal(
            run_id=str(context.get("run_id", "")),
            objective=user_request,
            success_criteria=(
                "四个主题（工商/财务/舆情/风险）都有带来源的证据",
                "报告草稿已生成，且已提交人工审批",
            ),
            budget=Budget(max_steps=len(TOPICS) + 3),
            metadata={"topics": list(TOPICS), "company": _company_of(user_request)},
        )


class ResearchPlanner:
    """四个主题拆成四个节点。"""

    def plan(self, state: Any) -> Any:
        from packages.agent_domain.intelligence.plan import Plan, PlanNode

        return Plan(
            run_id=state.run_id,
            nodes=tuple(
                PlanNode(node_id=f"t{i}", name=f"尽调-{topic}")
                for i, topic in enumerate(TOPICS, start=1)
            ),
        )


class ResearchDecisionEngine:
    """先逐个主题检索，然后写报告（会被治理层拦下），最后收尾。

    第 `len(TOPICS) + 1` 步是 `report.write`，**风险等级 HIGH**：

        它会产生一份对外的、可能会被直接拿去决策的东西。
        所以它不是"Agent 请求签字"，而是**治理层说这一步必须有人签字**（I-9）。
    """

    def __init__(self, *, company: str = "") -> None:
        self.calls = 0
        # 空串 = "听 Goal 的"。显式传值只用于测试里想固定一家公司的时候。
        self.company = company

    def decide(self, state: Any) -> Any:
        company = self.company or str(
            (getattr(state.goal, "metadata", None) or {}).get("company", "")
        )
        from packages.agent_domain.business.compensation import CompensationSpec
        from packages.agent_domain.intelligence.action import (
            Action,
            ActionType,
            RiskLevel,
        )
        from packages.agent_domain.intelligence.decision import Decision

        self.calls += 1
        n = len(TOPICS)

        if self.calls <= n:
            topic = TOPICS[self.calls - 1]
            action = Action(
                run_id=state.run_id,
                action_type=ActionType.TOOL_CALL,
                payload={"tool": "corpus.search", "args": {"company": company, "topic": topic}},
                rationale=f"检索{topic}维度的证据",
            )
        elif self.calls == n + 1:
            action = Action(
                run_id=state.run_id,
                action_type=ActionType.TOOL_CALL,
                payload={
                    "tool": "report.write",
                    "args": {"company": company, "topics": list(TOPICS)},
                },
                # 治理层拦下它的唯一理由：这份东西会被拿去做决策。
                risk_level=RiskLevel.HIGH,
                rationale="汇总四路证据，生成报告草稿",
                # S-8：撤销参数必须**随正向动作一起提出**，不许事后发明。
                compensation=CompensationSpec(
                    tool="report.withdraw",
                    args={"company": company},
                    # 报告 id 只有正向跑完才存在 —— 从结果信封里取。
                    result_keys=("result.report_id",),
                    description="撤回已发出的尽职调查报告",
                ),
            )
        else:
            action = Action(run_id=state.run_id, action_type=ActionType.FINISH)

        return Decision(run_id=state.run_id, selected_action=action, rationale="research")


def build_stack_factory(
    config: Any = None,
    *,
    kernel: Any = None,
    clock: Any = None,
    child_registry: Any = None,
    snapshots: Any = None,
    compensations: Any = None,
    cancellations: Any = None,
    company: str = "",
) -> Any:
    """`AGENTOS_STACK_PROVIDER=examples.research_stack:build_stack_factory`。

    参数与 `demo_stack.build_stack_factory` **完全一致** ——
    这就是"Agent 怎么想属于 Intelligence，不属于基础设施"的意思：
    换一个行业实现，装配路径一行不改。

    `kernel` / `snapshots` / `compensations` / `cancellations` 必须照样接住，
    漏掉不会报错，只会**永不落库**（M29 / M34 记着这个坑）。
    """
    from packages.agent_runtime.assembly import assemble_runtime_stack
    from packages.agent_runtime.delegation import (
        ChildRunRegistry,
        InProcessChildRunSpawner,
    )

    gateway = build_model_gateway()
    tool_runtime = build_tool_runtime()
    interpreter = ResearchInterpreter()
    planner = ResearchPlanner()
    registry = child_registry if child_registry is not None else ChildRunRegistry()

    def make_stack(agent_id: str, approvals: Any) -> Any:
        spawner = InProcessChildRunSpawner(
            factory=make_stack, approvals=approvals, registry=registry
        )
        return assemble_runtime_stack(
            agent_id=agent_id,
            interpreter=interpreter,
            planner=planner,
            # 每个 Run 一个引擎：它是**有状态的**（第几步了）。
            decision_engine=ResearchDecisionEngine(company=company),
            gateway=gateway,
            tool_runtime=tool_runtime,
            kernel=kernel,
            clock=clock,
            approval_store=approvals,
            snapshots=snapshots,
            compensations=compensations,
            cancellations=cancellations,
            spawner=spawner,
        )

    return make_stack


__all__ = [
    "TOPICS",
    "build_model_gateway",
    "build_stack_factory",
    "build_tool_runtime",
]
