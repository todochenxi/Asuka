"""与框架无关的 handler（M18）。

**A-7：这里没有一行 FastAPI。**

每个函数签名是 `(control_plane, ...) -> ApiResponse`，
路由层（FastAPI / Flask / 内部 RPC）只做一行绑定。
业务不长进路由函数，所以换框架不动业务。

```python
# apps/api/main.py（未来）
@app.post("/agents/{agent_id}/runs")
def start(agent_id: str, body: dict, idem: str = Header(default="")):
    resp = api.start_run(cp, {**body, "agent_id": agent_id}, idempotency_key=idem)
    return JSONResponse(resp.body, status_code=resp.status)
```

**A-1：handler 只做校验 / 编排 / 翻译。**
"这个动作该不该做"是 Harness 的事，"怎么驱动"是 Runtime 的事 ——
这里一概不判断。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .dto import DecisionRequest, StartRunRequest
from .errors import ApiError, Conflict, map_domain_error
from .ports import ControlPlane, require


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: Mapping[str, Any]


def _ok(body: Mapping[str, Any], status: int = 200) -> ApiResponse:
    return ApiResponse(status=status, body=dict(body))


def _fail(err: ApiError) -> ApiResponse:
    return ApiResponse(status=err.http_status, body=err.to_body())


def guard(fn):
    """把领域异常翻成 HTTP 语义（A-2）。未知异常落到 500 —— 那是 bug。"""

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as err:                          # noqa: BLE001
            return _fail(map_domain_error(err))

    wrapper.__name__ = getattr(fn, "__name__", "handler")
    wrapper.__doc__ = fn.__doc__
    return wrapper


@guard
def start_run(
    cp: ControlPlane,
    body: Mapping[str, Any],
    *,
    idempotency_key: str = "",
) -> ApiResponse:
    """`POST /agents/{agent_id}/runs` —— §44 的第一行。"""
    request = StartRunRequest(
        agent_id=str(require(body, "agent_id")),
        user_request=str(require(body, "user_request")),
        idempotency_key=idempotency_key,
    )
    view = cp.start_run(request)
    # A-3：幂等命中 → 200（早就有了），新建 → 201
    return _ok(view.to_dict(), status=200 if view.replayed else 201)


@guard
def get_run(cp: ControlPlane, run_id: str) -> ApiResponse:
    """`GET /runs/{run_id}`。"""
    return _ok(cp.get_run(run_id).to_dict())


@guard
def step_run(cp: ControlPlane, run_id: str, body: Mapping[str, Any]) -> ApiResponse:
    """`POST /runs/{run_id}/step` —— 走一步（F-1）。

    终态 Run 再推进 → 409 `RUN_TERMINAL`（F-2）。这个判断在
    `InProcessControlPlane._assert_advancable`，handler 不自己判 ——
    "哪些状态是终态"是领域的知识（B-7）。
    """
    _ = body                                              # 形状上没有必填字段
    return _ok(cp.step_run(run_id).to_dict())


@guard
def drive_run(cp: ControlPlane, run_id: str, body: Mapping[str, Any]) -> ApiResponse:
    """`POST /runs/{run_id}/run` —— 一路走到停下来（F-1）。

    刻意**不接受** `max_steps`：上界归 `Goal.budget`（Intelligence），
    停止条件归 `AgentLoop.run()`（Runtime）。让 HTTP 调用方再传一个，
    "这个 Run 能走几步"就有三个互相不一致的定义（B-7）。
    """
    _ = body
    return _ok(cp.drive_run(run_id).to_dict())


@guard
def cancel_run(
    cp: ControlPlane,
    run_id: str,
    body: Mapping[str, Any],
    *,
    idempotency_key: str = "",
) -> ApiResponse:
    """`POST /runs/{run_id}/cancel` —— 把一条 Run 叫停（B-8 / 空洞 221）。

    M33 之前没有这个端点：一条通过 API 开出来的 Run 只能被推进，不能被叫停。
    想停它的唯一办法是杀进程 —— 于是"把这条 Run 停下来"的实现者是运维，不是系统。

    `reason` 与 `by` 都必填（A-8 同款）：
    审计要回答"谁叫停的、为什么"，一条查不到是谁的 `cancelled` 等于没发生过。

    A-3 / 空洞 223：它也吃 `Idempotency-Key`。
    取消是一次写操作，而"点停止之后网络超时再点一次"是最常见的客户端行为 ——
    没有键，第二次拿到的是 409 `RUN_TERMINAL`，
    一次**成功**的取消被报成了失败（D-35）。
    """
    return _ok(
        cp.cancel_run(
            run_id,
            reason=str(require(body, "reason")),
            by=str(require(body, "by")),
            idempotency_key=idempotency_key,
        ).to_dict()
    )


@guard
def get_trace(cp: ControlPlane, run_id: str) -> ApiResponse:
    """`GET /runs/{run_id}/trace` —— 账本（F-4）。

    它必须是一个独立端点而不是塞进 `RunView`：账本是**只增**的，
    而 `RunView` 会被轮询。把只增的东西塞进一个会被频繁读取的视图，
    等于让每次"这个 Run 现在怎么样了"都背上一整段历史。
    """
    return _ok(cp.get_trace(run_id).to_dict())


@guard
def get_answer(cp: ControlPlane, run_id: str) -> ApiResponse:
    """`GET /runs/{run_id}/answer` —— 这条 Run 最近一次 LLM 回答。

    回答是**只读的派生事实**：它存在 State 的 Observation 里，
    没有独立存储。查询器缺席时返回空串而不是 404 —— 与
    `list_run_executions` 同一条判据：查询语义，空集不是错误。
    """
    answer_of = getattr(cp, "answer", None)
    answer = str(answer_of(run_id)) if answer_of is not None else ""
    return _ok({"run_id": run_id, "answer": answer})


#: 拼进 prompt 的历史轮数上限。它只是防"会话长到把窗口撑爆"，
#: 不是业务语义 —— 真要长期记忆该走 Memory，不是把 transcript 无限拉长。
_MAX_HISTORY_TURNS = 20


def _conversation_prompt(history: Any, message: str) -> str:
    """把多轮对话拼成一条 `user_request`。

    AgentOS 的一条 Run 只认一条 `user_request`（§44），**没有会话对象** ——
    所以"多轮"是**客户端的事实**：把前几轮连同这一句一起交给模型。
    这里把它拼清楚，而不是让前端各自拼一份（B-7：同一个事实一处定义）。

    没有历史时**原样返回**这一句，不添任何前缀 —— 单轮请求的 prompt
    与 M93 之前逐字相同，避免"加了多轮"顺手改掉单轮的行为。
    """
    turns: list[tuple[str, str]] = []
    if isinstance(history, Sequence):
        for item in history[-_MAX_HISTORY_TURNS:]:
            if not isinstance(item, Mapping):
                continue
            role = str(item.get("role") or "").strip()
            content = str(item.get("content") or "").strip()
            if role and content:
                turns.append((role, content))
    if not turns:
        return message
    lines = ["以下是此前的对话，请结合它回答最后一句：", ""]
    for role, content in turns:
        lines.append(f"{'用户' if role == 'user' else '助手'}：{content}")
    lines.append(f"用户：{message}")
    return "\n".join(lines)


@guard
def chat(
    cp: ControlPlane,
    body: Mapping[str, Any],
    *,
    idempotency_key: str = "",
) -> ApiResponse:
    """`POST /chat` —— 单轮 / 多轮问答：开一条 Run、推到停下来，带上模型回答。

    它不是新的运行语义：`start_run` + `drive_run` 两步，和页面/CLI 手动
    做的是同一件事。多出来的只有两样：

        · 多轮：把 `history`（`[{role, content}]`）拼进这一次的 `user_request`
          —— 运行时的每一轮仍是**独立的一条 Run**，会话是客户端的事实；
        · 回答：从 State 的 Observation 里读出来（`ControlPlane.answer`）。

    ⚠️ 它**不**保证一定答得出来：如果那条 Run 停在治理闸门上（`suspended`），
    就没有回答，`answer` 是空串而 `status` 说真话。拿状态冒充答案
    （或反过来）正是这一层最该避免的那类错。
    """
    message = str(require(body, "message"))
    request = StartRunRequest(
        agent_id=str(require(body, "agent_id")),
        user_request=_conversation_prompt(body.get("history"), message),
        idempotency_key=idempotency_key,
    )
    view = cp.start_run(request)
    try:
        view = cp.drive_run(view.run_id)
    except Conflict:
        # 幂等键回放了一条**终态** Run（`RUN_TERMINAL`）：没必要再推，
        # 也用不着报错 —— 调用方要的是那条 Run 的答案，它已经在那儿了。
        pass
    answer_of = getattr(cp, "answer", None)
    answer = str(answer_of(view.run_id)) if answer_of is not None else ""
    return _ok({**view.to_dict(), "answer": answer})


@guard
def list_approvals(cp: ControlPlane, run_id: str | None = None) -> ApiResponse:
    """`GET /approvals` / `GET /runs/{id}/approvals`。"""
    return _ok({"items": [a.to_dict() for a in cp.list_approvals(run_id)]})


@guard
def list_run_executions(cp: ControlPlane, run_id: str) -> ApiResponse:
    """`GET /runs/{run_id}/executions` —— M65 / 空洞 234 的出口。

    M48 给 `executions` 加了 `cancellation_reason` / `cancellation_by` 并真的写入了，
    但**没有任何地方读它们** —— 那两列是死数据。
    按"交付的判据是被用上"（M64 冻过这条），这里给它们一个出口。

    没有查询器时返回**空列表**，不是 404：
    "这条 Run 手上有哪些 Execution"是查询语义，空集不是错误
    （与 `list_approvals` 同一条判据）。
    """
    lookup = getattr(cp, "executions", None)
    items = lookup(run_id) if lookup is not None else []
    return _ok({"run_id": run_id, "items": list(items)})


@guard
def decide_approval(
    cp: ControlPlane,
    run_id: str,
    approval_id: str,
    body: Mapping[str, Any],
) -> ApiResponse:
    """`POST /runs/{run_id}/approvals/{approval_id}/decision` —— HITL 回调。

    A-8：`by` 必填。匿名审批进不了审计 ——
    "谁批的"是这条记录唯一的价值所在。
    """
    request = DecisionRequest(
        approval_id=approval_id,
        decision=str(require(body, "decision")),
        by=str(require(body, "by")),
        comment=str(body.get("comment") or ""),
    )
    return _ok(cp.decide(run_id, request).to_dict())
