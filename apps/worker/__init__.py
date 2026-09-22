"""worker 进程（M22）。

唯一真正执行 Task 的部署单元。没有它，系统产出为零 ——
M21 那四个后台进程只是在打扫一间从来不会脏的房间。
"""
from .app import WorkerApp, WorkerProcessConfig

__all__ = ["WorkerApp", "WorkerProcessConfig"]
