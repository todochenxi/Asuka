"""Harness 的适配器层。

只放基础设施实现，不放业务。`ApprovalStore` / `Budget` 等 Port 的
进程内实现留在使用处（`InMemoryApprovalStore` 在 `approval.py`），
只有"接了真数据库"的才放这里。
"""
