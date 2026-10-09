"""
领星 ERP 订单查询 API 客户端。

直接 HTTP POST 调用领星 ERP 后端接口，无需浏览器自动化。
用户需从浏览器 DevTools 抓取 auth-token 等请求头，填入配置即可。

接口：POST https://gw.lingxingerp.com/amz-order/api/order/OrderList
请求体：JSON，searchField="amazonOrderId"，searchValue=逗号分隔的订单号
响应：{code:1, data:{data:[{amazonOrderId, orderStatus, ...}]}, success:true}
"""
from __future__ import annotations

import uuid
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


# ----------------------------------------------------------------------
def parse_lingxing_headers(text: str) -> dict:
    """
    解析从浏览器 DevTools 复制的原始 Request Headers 文本。

    支持：
    - 请求行（POST /path HTTP/1.1）→ 与 Host 拼出完整 API URL
    - 任意空行分隔
    - 值里的反引号自动去除
    - 大小写不敏感

    :raises ValueError: 未找到 auth-token
    :returns: {api_url, auth_token, company_id, uid, zid, env_key, version}
    """
    headers: dict[str, str] = {}
    request_line = ""
    methods = ("GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD")

    for raw in text.splitlines():
        line = raw.strip().replace("`", "")
        if not line:
            continue
        upper = line.upper()
        first = upper.split(" ", 1)[0]
        if first in methods and " HTTP/" in upper:
            request_line = line
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()

    auth = headers.get("auth-token", "")
    if not auth:
        raise ValueError("未找到 auth-token，请确认粘贴的是 OrderList 请求的 Request Headers。")

    api_url = ""
    if request_line and "host" in headers:
        parts = request_line.split()
        if len(parts) >= 2:
            api_url = f"https://{headers['host']}{parts[1]}"

    return {
        "api_url": api_url,
        "auth_token": auth,
        "company_id": headers.get("x-ak-company-id", ""),
        "uid": headers.get("x-ak-uid", ""),
        "zid": headers.get("x-ak-zid", ""),
        "env_key": headers.get("x-ak-env-key", ""),
        "version": headers.get("x-ak-version", ""),
    }


class LingxingApiClient:
    """领星 ERP 订单查询客户端，支持批量查询。"""

    def __init__(self, settings: AppSettings):
        self.settings = settings
        self._req_counter = 0

    # ------------------------------------------------------------------
    def _build_headers(self) -> dict:
        """构建请求头（含用户配置的 auth-token 等）。"""
        s = self.settings
        if not s.lingxing_auth_token:
            raise LingxingApiError("未配置领星 ERP auth-token，请在配置页面填写。")
        return {
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "AK-Client-Type": "web",
            "AK-Origin": "https://erp.lingxing.com",
            "auth-token": s.lingxing_auth_token,
            "X-AK-Company-Id": s.lingxing_company_id,
            "X-AK-ENV-KEY": s.lingxing_env_key,
            "X-AK-Language": "zh",
            "X-AK-PLATFORM": "1",
            "X-AK-Request-Source": "erp",
            "X-AK-Request-Id": str(uuid.uuid4()),
            "X-AK-Uid": s.lingxing_uid,
            "X-AK-Version": s.lingxing_version,
            "X-AK-Zid": s.lingxing_zid,
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
        测试 API 连通性：查一个不存在的订单号，只要返回 success=true 即 Token 有效。
        :returns: 成功消息
        :raises LingxingApiError: 失败原因
        """
        headers = self._build_headers()
        body = self._build_body(["000-0000000-0000000"])
        try:
            resp = requests.post(
                self.settings.lingxing_api_url,
                json=body,
                headers=headers,
                timeout=self.settings.request_timeout_sec,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise LingxingApiError(f"网络请求失败: {exc}") from exc
        except ValueError as exc:
            raise LingxingApiError(f"返回非 JSON: {exc}") from exc

        if data.get("success"):
            return "连接成功！Token 有效 ✓"
        msg = data.get("msg", "未知错误")
        raise LingxingApiError(f"连接失败: {msg}")

    # ------------------------------------------------------------------
    def query_orders(
        self, order_ids: Iterable[str], order_status: str = "",
    ) -> dict[str, LingxingOrder]:
        """
        批量查询订单，返回完整订单数据。

        :param order_ids: 订单号列表（不超过 200 个）
        :param order_status: 可选状态筛选，如 "Canceled,Pending"；空=不过滤
        :returns: {order_id: LingxingOrder}  只有查到的订单才在 dict 中
        :raises LingxingApiError: API 调用失败
        """
        ids = [oid.strip() for oid in order_ids if oid and oid.strip()]
        if not ids:
            return {}

        headers = self._build_headers()
        body = self._build_body(ids, order_status)

        try:
            resp = requests.post(
                self.settings.lingxing_api_url,
                json=body,
                headers=headers,
                timeout=self.settings.request_timeout_sec,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise LingxingApiError(f"查询失败: {exc}") from exc
        except ValueError as exc:
            raise LingxingApiError(f"返回非 JSON: {exc}") from exc

        if not data.get("success"):
            msg = data.get("msg", "未知错误")
            raise LingxingApiError(f"查询失败: {msg}")

        result: dict[str, LingxingOrder] = {}
        items = (data.get("data") or {}).get("data") or []
        for item in items:
            oid = (item.get("amazonOrderId") or "").strip()
            if not oid:
                continue
            # 取第一个 SKU 的 ASIN
            slist = item.get("slist") or []
            asin = ""
            if slist and isinstance(slist, list):
                asin = (slist[0].get("asin") or "").strip()
            # 金额
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
