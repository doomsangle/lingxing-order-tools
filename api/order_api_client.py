"""
用户自定义 API 客户端。

协议约定（由调用方可按需扩展）：
- ``query_orders``  GET  {base}{api_query_path}?order_ids=id1,id2,...
  返回 ``[{"order_id":"xxx","status":"..."}, ...]`` 列表
- ``update_status``  PATCH  {base}{api_update_path}
  body JSON: ``{"order_id": "...", "status": "...", "note": "..."}``

若后端契约不同，只改本文件即可，其他模块不受影响。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import requests


class OrderApiError(Exception):
    """API 调用失败时抛出。"""


@dataclass
class OrderRecord:
    order_id: str
    status: str = ""
    raw: dict | None = None


class OrderApiClient:
    """薄封装，带 Token 认证与超时重试。"""

    def __init__(self, base_url: str, token: str, query_path: str, update_path: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.query_path = query_path
        self.update_path = update_path
        self.timeout = timeout

    # ------------------------------------------------------------------
    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    def test_connection(self) -> None:
        """简单探测 API 是否可达（不校验业务）。"""
        if not self.base_url:
            raise OrderApiError("API Base URL 未配置")
        try:
            requests.get(
                f"{self.base_url}{self.query_path}",
                headers=self._headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise OrderApiError(f"API 连接失败: {exc}") from exc

    # ------------------------------------------------------------------
    def query_orders(self, order_ids: Iterable[str]) -> list[OrderRecord]:
        """批量查询订单，返回找到的订单列表。"""
        ids = [o for o in order_ids if o]
        if not ids:
            return []
        url = f"{self.base_url}{self.query_path}"
        try:
            resp = requests.get(
                url,
                params={"order_ids": ",".join(ids)},
                headers=self._headers,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise OrderApiError(f"查询订单失败: {exc}") from exc
        except ValueError as exc:
            raise OrderApiError(f"查询订单返回非 JSON: {exc}") from exc

        records: list[OrderRecord] = []
        # 兼容后端直接返回 list 或 { "data": [...] } 两种包法
        items = data if isinstance(data, list) else data.get("data") or data.get("items") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            oid = item.get("order_id") or item.get("orderId") or item.get("id")
            if not oid:
                continue
            records.append(OrderRecord(
                order_id=str(oid).strip(),
                status=str(item.get("status") or ""),
                raw=item,
            ))
        return records

    # ------------------------------------------------------------------
    def update_status(self, order_id: str, status: str, note: str = "") -> dict:
        """更新单个订单的状态。"""
        url = f"{self.base_url}{self.update_path}"
        payload = {"order_id": order_id, "status": status}
        if note:
            payload["note"] = note
        try:
            resp = requests.patch(url, json=payload, headers=self._headers, timeout=self.timeout)
            resp.raise_for_status()
            try:
                return resp.json()
            except ValueError:
                return {"ok": resp.status_code}
        except requests.RequestException as exc:
            raise OrderApiError(f"更新订单状态失败: {exc}") from exc
