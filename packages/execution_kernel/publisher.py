"""Outbox Publisher：把 Outbox 里的事件投递到 Kafka。

    PG (状态 + Outbox，同一事务)
        ↓  OutboxPublisher.drain()
    Kafka（Durable Event Log）
        ↓  至少一次投递
    Consumer（按 event_id 去重）

投递语义是**至少一次（at-least-once）**，这是刻意选的：

    X-3   状态写入与事件写入在 PG 里同事务 —— 这一跳是可靠的
    X-5   Kafka 是 Event Log，不是 Truth；重放/重复是常态，不是异常

顺序必须是 **先 publish，再 mark_published**：

    publish() 成功 → 进程崩溃 → 没来得及 mark
        ↓
    下一轮 drain 会再发一次（重复）
        ↓
    所以**消费者必须按 event_id 去重**（见 consumers.IdempotentConsumer）

反过来（先 mark 再 publish）会丢事件，那比重复严重得多 ——
丢事件意味着 Read Model / 审计 / Evaluation 永久缺一块，且无从发现。

--------------------------------------------------------------------------
M21：这里只有一个 `drain()`，没有循环。

循环属于进程（`apps/outbox_publisher` + `apps._runtime.ProcessRuntime`），
不属于库。库只负责"一个调度周期内该做的事"，进程的生命周期
（优雅退出 / 退避 / 失败退出 / 健康检查）在 `_runtime.py` 里。

同理，`delivery` 是**必填**的、没有默认值：
多副本部署是默认部署方式，而"不认领就投"在多副本下会把每条事件投 N 遍。
留一个 `delivery=None` 的开关，等于把"多副本安全"做成可选项 ——
那正是 A-10 / S-9 踩过的坑（把该是事实的东西写成断言）。

失败还要分两种（见 `ports.PartialPublishError`）：

    broker 指认得出是哪条坏  → 毒消息  → 就地判死，进程继续
    broker 指认不出          → 系统性故障 → 抛给进程，累计失败后退出（PR-8）

混淆这两种，就会在 broker 挂掉时把整批好事件逐条判死。
--------------------------------------------------------------------------
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Mapping, Sequence

from packages.agent_domain.events.event import Event

from .inmemory import SystemClock
from .outbox_delivery import OutboxDeliveryStore
from .ports import Clock, EventPublisher, OutboxStore, PartialPublishError


@dataclass
class OutboxPublisher:
    """一个独立的部署单元（`outbox_publisher`），不是 Kernel 的一部分。"""

    outbox: OutboxStore
    publisher: EventPublisher
    delivery: OutboxDeliveryStore
    owner: str = "outbox-publisher"
    batch_size: int = 100
    lease: timedelta = timedelta(seconds=30)
    max_attempts: int = 5
    clock: Clock = field(default_factory=SystemClock)
    last_published: list[str] = field(default_factory=list)
    last_failed: dict[str, str] = field(default_factory=dict)
    last_dead: list[str] = field(default_factory=list)

    def drain(self) -> int:
        """认领 → 投递 → 记账。返回**这一轮成功投出去**的条数。

        只有**系统性故障**才抛（broker 不可达之类）；毒消息不抛 ——
        `drain()` 的返回值就是"这一轮干了多少活"，进程据此决定退避多久。
        真正的失败信息在 `last_failed` / `last_dead` 里。
        把毒消息也抛出去，会让进程把"一批里有一条坏事件"误判成"整个进程坏了"。
        """
        now = self.clock.now()
        self.last_published = []
        self.last_failed = {}
        self.last_dead = []

        # PR-5：死信如果不排除出候选集，会一直占着 limit 里的位置
        exclude = list(self.delivery.dead_ids())
        candidates = list(
            self.outbox.pending(limit=self.batch_size, exclude=exclude)
        )

        claimed: list[Event] = []
        for event in candidates:
            # PR-3：原子认领。拿不到 = 别的实例正在投，或已判死
            if self.delivery.claim(event.event_id, self.owner, self.lease, now):
                claimed.append(event)
        if not claimed:
            return 0

        try:
            self.publisher.publish(claimed)
        except PartialPublishError as exc:
            # broker 是好的，坏的是这几条数据 —— 就地判死，不惊动进程
            return self._quarantine(claimed, exc.failures)
        except Exception:
            # PR-5：broker 没收到，无从指认是哪一条，只能逐条隔离重试
            return self._isolate(claimed)

        # X-3：先 publish 后 mark —— 崩在这里只会重复，不会丢
        for event in claimed:
            self._account_sent(event.event_id)
        return len(claimed)

    def _quarantine(self, claimed: Sequence[Event], failures: Mapping[str, str]) -> int:
        """broker 指认出来的毒消息：只有那几条判死，其余照常落账。

        既然 broker 能指认，说明它是通的 —— 这时候把整批回滚再逐条重试
        纯属浪费：好事件会被白重发一遍（at-least-once 的代价），
        坏事件重试多少次还是坏。
        """
        sent = 0
        for event in claimed:
            reason = failures.get(event.event_id)
            if reason is None:
                self._account_sent(event.event_id)
                sent += 1
            else:
                self._account_failed(event.event_id, reason)
        return sent

    def _isolate(self, claimed: Sequence[Event]) -> int:
        """broker 没收到整批 → 逐条重试，把毒消息隔离出来（PR-5）。

        不做这一步会怎样：一条坏事件让整批失败 → 整批 attempts+1 →
        max_attempts 之后**整批**进死信。**一条**坏事件害死 batch_size 条好事件，
        而且是在没人写错代码的情况下发生的。
        """
        sent = 0
        last: Exception | None = None
        for event in claimed:
            try:
                self.publisher.publish([event])
            except PartialPublishError as exc:
                reason = exc.failures.get(event.event_id) or str(exc)
                self._account_failed(event.event_id, reason)
            except Exception as exc:
                last = exc
                self._account_failed(event.event_id, str(exc) or repr(exc))
            else:
                self._account_sent(event.event_id)
                sent += 1

        if sent == 0 and last is not None:
            # 一条都没投出去 = 系统性故障（broker 不可达 / 鉴权挂了），不是毒消息。
            # 必须抛出去：进程要靠它做失败计数并最终退出（PR-8）。
            # 相反，投出去一部分 → 那是隔离出来的毒消息，不抛 ——
            # 否则一条坏事件就能把整个进程拖去重启。
            raise last
        return sent

    def _account_sent(self, event_id: str) -> None:
        self.outbox.mark_published([event_id])
        self.delivery.mark_sent([event_id])
        self.last_published.append(event_id)

    def _account_failed(self, event_id: str, reason: str) -> bool:
        died = self.delivery.mark_failed(
            event_id, reason, max_attempts=self.max_attempts, now=self.clock.now()
        )
        self.last_failed[event_id] = reason
        if died:
            self.last_dead.append(event_id)
        return died

    def summary(self) -> str:
        return (
            f"published={len(self.last_published)} "
            f"failed={len(self.last_failed)} dead={len(self.last_dead)}"
        )
