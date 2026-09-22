"""AgentOS · apps（进程层）

`packages/` 里的一切都是**库**：它们有方法，但没有生命周期。
`apps/` 里的一切都是**进程**：有启动、有停止、有退避、有健康检查。

    _runtime.py            所有进程共用的骨架（PR-1 / PR-2 / PR-7 / PR-8）
    worker/                认领 → 执行 Task（生产型：唯一制造副作用的进程）
    api/                   FastAPI 绑定（PR-22：框架绑定只有这一处）
    outbox_publisher/      Outbox → Kafka（投递型：需要领地）
    child_run_consumer/    Kafka → 唤醒父 Run（消费型：offset 只在事务提交后推进）
    recovery_controller/   Lease 过期 → STALE → Recovery（收敛型）
    wakeup_controller/     Timer / Approval 唤醒（收敛型）
    cancellation_sweeper/  取消意图 → 终态收敛（收敛型）

这一层的判断标准很简单：**一行"它现在该不该继续跑"的代码，放在 packages/ 里
就是越界。** 库回答"这一步怎么做"，进程回答"要不要做下一步、做多久、什么时候停"。

只有 `outbox_publisher` 需要领地（PR-12）：判据不是"要不要多副本"，
而是"这个动作重复做会不会改变结果" —— 投两次就有两封，取消两次还是一次。
`child_run_consumer` 不需要领地表，但它的 offset 提交**排在事务提交之后**
（`ProcessRuntime.on_commit`）：那不是领地，是"不许在落库之前说消费完了"。
"""
