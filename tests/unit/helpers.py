"""测试辅助：构造最小合法对象。"""
from __future__ import annotations

from datetime import timedelta

from packages.agent_domain.execution import (
    Execution,
    ExecutionAggregate,
    ExecutorType,
    Task,
    TaskType,
)
from packages.agent_domain.ids import new_execution_id, new_run_id, new_step_id, new_task_id


def new_run() -> str:
    return new_run_id()


def make_task(run_id: str | None = None, **kwargs) -> Task:
    return Task(
        run_id=run_id or new_run(),
        step_id=new_step_id(),
        task_type=kwargs.pop("task_type", TaskType.TOOL_CALL),
        executor_type=kwargs.pop("executor_type", ExecutorType.NATIVE),
        **kwargs,
    )


def make_execution(task: Task | None = None, **kwargs) -> Execution:
    task = task or make_task()
    return Execution(
        execution_id=kwargs.pop("execution_id", new_execution_id()),
        task_id=task.task_id,
        **kwargs,
    )


def make_aggregate(task: Task | None = None, **kwargs) -> ExecutionAggregate:
    return ExecutionAggregate(make_execution(task, **kwargs))


DEFAULT_TTL = timedelta(seconds=30)
