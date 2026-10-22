"""
领星 ERP 订单查询 API 客户端。

通过 amzvideohelper 平台包装接口查询（服务端持有领星凭证，客户端只需 API Key）：
  订单查询：POST https://amzvideohelper.top/api/erp/lingxing/order/list
  连通测试：POST https://amzvideohelper.top/api/erp/lingxing/test
鉴权：Authorization: Bearer <API Key>
order/list 请求体完全照搬领星原始接口，响应原样透传：
  {code:1, data:{data:[{amazonOrderId, orderStatus, ...}]}, success:true}
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import requests

from config.settings import AppSettings


class LingxingApiError(Exception):
    """领星 API 调用失败。"""


# ----------------------------------------------------------------------
@dataclass
class LingxingOrder:
    """领星 API 返回的单条订单完整数据。"""
    order_id: str
    order_status: str = ""
    asin: str = ""
    order_total_amount: float = 0.0
    raw: dict | None = None


class LingxingApiClient:
    """领星 ERP 订单查询客户端，支持批量查询。"""

    def __init__(self, settings: AppSettings):
        self.settings = settings
        self._req_counter = 0

    # ------------------------------------------------------------------
    def _build_headers(self) -> dict:
        """构建 amzvideohelper 接口请求头（Bearer API Key 鉴权）。"""
        s = self.settings
        if not s.lingxing_api_key:
            raise LingxingApiError("未配置 amzvideohelper API Key。")
        return {
            "Authorization": f"Bearer {s.lingxing_api_key}",
            "Content-Type": "application/json",
        }

    def _build_body(self, order_ids: list[str], order_status: str = "") -> dict:
        """构建请求体。order_status 非空时加入筛选（如 "Canceled,Pending"）。"""
        self._req_counter += 1
        body = {
            "exactSearch": 1,
            "fulfillmentChannel": "",
            "isMcfOrder": "0",
            "isReturn": "",
            "pageNo": 1,
            "pageSize": max(len(order_ids), 20),
            "req_time_sequence": f"/amz-order/api/order/OrderList$${self._req_counter}",
            "searchField": "amazonOrderId",
            "searchValue": ",".join(order_ids),
            "sortField": "purchaseDateLocal",
            "sortType": "desc",
        }
        if order_status:
            body["orderStatus"] = order_status
        return body

    # ------------------------------------------------------------------
    def test_connection(self) -> str:
        """
        测试 amzvideohelper 接口连通性（同时验证服务端领星凭证是否可用）。
        :returns: 成功消息
        :raises LingxingApiError: 失败原因
        """
        url = self.settings.lingxing_test_url
        headers = self._build_headers()
        try:
            resp = requests.post(
                url,
                headers=headers,
                timeout=self.settings.request_timeout_sec,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise LingxingApiError(self._explain_http_error(exc)) from exc
        except ValueError as exc:
            raise LingxingApiError(f"返回非 JSON: {exc}") from exc

        if data.get("ok"):
            return f"连接成功 ✓  {data.get('message') or data.get('msg', '')}"
        msg = data.get("message") or data.get("msg") or "未知错误"
        raise LingxingApiError(f"连接失败: {msg}")

    # ------------------------------------------------------------------
    def query_orders(
        self, order_ids: Iterable[str], order_status: str = "",
    ) -> dict[str, LingxingOrder]:
        """
        批量查询订单（amzvideohelper 透传领星 order/list，服务端持有领星凭证）。

        :param order_ids: 订单号列表（不超过 200 个）
        :param order_status: 可选状态筛选，如 "Canceled,Pending"；空=不过滤
        :returns: {order_id: LingxingOrder}  只有查到的订单才在 dict 中
        :raises LingxingApiError: API 调用失败
        """
        ids = [oid.strip() for oid in order_ids if oid and oid.strip()]
        if not ids:
            return {}

        body = self._build_body(ids, order_status)
        url = self.settings.lingxing_order_url
        headers = self._build_headers()

        try:
            resp = requests.post(
                url,
                json=body,
                headers=headers,
                timeout=self.settings.request_timeout_sec,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise LingxingApiError(self._explain_http_error(exc)) from exc
        except ValueError as exc:
            raise LingxingApiError(f"返回非 JSON: {exc}") from exc

        # 接口原样透传领星响应：{code:1, data:{data:[...]}, success:true}
        # 兼容可能的包装形态：{ok:true, data:<领星原始响应>}
        if isinstance(data, dict) and "ok" in data:
            if not data.get("ok"):
                raise LingxingApiError(f"查询失败: {data.get('message') or data.get('msg') or '未知错误'}")
            data = data.get("data") or {}

        if isinstance(data, dict) and data.get("success") is False:
            raise LingxingApiError(f"领星返回失败: {data.get('msg') or '未知错误'}")

        inner = (data or {}).get("data") or {}
        items = inner.get("data") or []

        result: dict[str, LingxingOrder] = {}
        for item in items:
            oid = (item.get("amazonOrderId") or "").strip()
            if not oid:
                continue
            slist = item.get("slist") or []
            asin = ""
            if slist and isinstance(slist, list):
                asin = (slist[0].get("asin") or "").strip()
            try:
                amt = float(item.get("orderTotalAmount") or 0)
            except (ValueError, TypeError):
                amt = 0.0
            result[oid] = LingxingOrder(
                order_id=oid,
                order_status=(item.get("orderStatus") or "").strip(),
                asin=asin,
                order_total_amount=amt,
                raw=item,
            )
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _explain_http_error(exc: requests.RequestException) -> str:
        """把 HTTP 错误码翻译成用户可读的说明。"""
        resp = getattr(exc, "response", None)
        if resp is None:
            return f"网络请求失败: {exc}"
        code = resp.status_code
        detail = ""
        try:
            body = resp.json()
            detail = body.get("message") or body.get("msg") or ""
        except ValueError:
            detail = (resp.text or "")[:150]
        mapping = {
            400: "服务端尚未配置领星凭证，请在 amzvideohelper 官网填写并保存",
            401: "API Key 缺失或无效，请检查 Authorization 请求头",
            502: "领星网关返回异常（如 auth-token 已过期），需在官网更新凭证",
        }
        reason = mapping.get(code, f"HTTP {code}")
        return f"{reason}" + (f"（{detail}）" if detail else "")
