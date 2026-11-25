"""业务路由汇总：专用扩展路由先注册，通用资源路由后兜底。"""
from __future__ import annotations

from app.routers import reconcile_extra, resource

ROUTERS = [reconcile_extra, resource]
