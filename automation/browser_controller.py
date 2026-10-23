﻿"""
Playwright CDP 浏览器控制器。

设计目标：
- 用户只需要一台装了 Chrome 的机器，首次登录领星 ERP 后 Cookie 自动持久化
- 完全 DOM 级操作，**不受浏览器窗口大小/分辨率影响**
- 可运行时被 Pause / Resume / Stop
- 每个关键步骤都有可观测的日志回调，方便调试 & 固化 selector
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PwTimeoutError,
    sync_playwright,
)

from .selectors import LINGXING_SELECTORS, SelectorSet
from config.settings import AppSettings, get_settings


LogFn = Callable[[str], None]


# ----------------------------------------------------------------------
class BrowserError(Exception):
    """浏览器操作异常。"""


@dataclass
class SearchResult:
    """单次订单搜索结果。"""
    order_id: str
    found: bool                         # True=订单存在
    status: str                         # 订单真实状态 或 "订单不存在"
    row_text: str = ""                  # 命中行的原始文本片段（调试用）


# ----------------------------------------------------------------------
class BrowserController:
    """
    连接本机 Chrome（CDP 端口），在领星 ERP 订单列表页做订单查询。

    使用方式::

        ctl = BrowserController(settings, log_cb=print)
        ctl.connect()
        ctl.ensure_on_order_page()
        for oid in order_ids:
            result = ctl.search_order(oid)
            # 处理 result ...
        ctl.close()

    支持通过 :meth:`pause` / :meth:`resume` / :meth:`stop` 打断执行。
    """

    def __init__(
        self,
        settings: AppSettings,
        log_cb: Optional[LogFn] = None,
    ) -> None:
        self.settings = settings
        self._log: LogFn = log_cb or (lambda m: None)

        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._chrome_proc: Optional[subprocess.Popen] = None

        # 运行时控制
        self._pause_evt = threading.Event()
        self._stop_evt = threading.Event()

        # 运行时缓存的 selector 命中记录（命中一次后锁定，加速后续查询）
        self._locked_selectors: dict[str, str] = {}

    # ==================================================================
    # 生命周期
    # ==================================================================
    def connect(self) -> None:
        """
        连接 CDP，必要时自动启动 Chrome。连接成功后 page 指向
        ``settings.lingxing_order_page_url`` 的页面（或自动导航过去）。
        """
        self._log("[连接] 正在初始化 Playwright ...")
        self._pw = sync_playwright().start()

        cdp_url = f"http://127.0.0.1:{self.settings.cdp_port}"

        if self._cdp_port_in_use():
            self._log(f"[连接] 检测到 CDP 端口 {self.settings.cdp_port} 已有 Chrome，直接连接 ...")
            self._connect_existing(cdp_url)
        else:
            self._log(f"[连接] 端口 {self.settings.cdp_port} 空闲，自动启动 Chrome ...")
            self._launch_chrome_with_cdp()
            time.sleep(2)  # 等待 Chrome 监听端口
            self._connect_existing(cdp_url)

        assert self._page is not None
        url = self._page.url or ""
        if self.settings.lingxing_order_page_url not in url:
            self._log(f"[连接] 当前页面 {url!r}，导航到订单列表页 ...")
            self._page.goto(self.settings.lingxing_order_page_url, wait_until="domcontentloaded", timeout=30000)

        self._wait_page_stable()
        self._log(f"[连接] 当前页面: {self._page.url}")
        self._dump_selector_status()  # 首次连接后尝试各关键元素，锁定稳定 selector

    def close(self, close_chrome: bool = False) -> None:
        """释放 Playwright 资源；默认 **不关闭** Chrome（保留登录态给下次用）。"""
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._context = None
        self._page = None
        self._pw = None

        if close_chrome and self._chrome_proc is not None:
            try:
                self._chrome_proc.terminate()
            except Exception:
                pass
            self._chrome_proc = None

    # ==================================================================
    # 运行时控制
    # ==================================================================
    def pause(self) -> None:
        self._pause_evt.set()
        self._log("[控制] 已暂停，当前操作完成后停止推进。")

    def resume(self) -> None:
        self._pause_evt.clear()
        self._log("[控制] 已恢复。")

    def stop(self) -> None:
        self._stop_evt.set()
        self._pause_evt.clear()
        self._log("[控制] 请求停止 ...")

    def _check_stop(self) -> None:
        if self._stop_evt.is_set():
            raise BrowserError("执行被用户停止")
        if self._pause_evt.is_set():
            self._log("[控制] 暂停中，等待恢复 ...")
            while self._pause_evt.is_set():
                if self._stop_evt.is_set():
                    raise BrowserError("执行被用户停止")
                time.sleep(0.3)

    # ==================================================================
    # 公共查询
    # ==================================================================
    def ensure_on_order_page(self) -> None:
        """若当前不在订单列表页则自动跳转。"""
        assert self._page is not None
        if self.settings.lingxing_order_page_url not in self._page.url:
            self._log(f"[页面] 不在订单列表，当前 {self._page.url}，跳转 ...")
            self._page.goto(self.settings.lingxing_order_page_url, wait_until="domcontentloaded", timeout=30000)
            self._wait_page_stable()
        # 简单登录态探测：如果出现登录相关元素，提醒用户
        if self._page.locator('input[type="password"]').count() > 0:
            raise BrowserError(
                "检测到登录页，请先在 Chrome 中手动登录领星 ERP，然后回到本工具点击'继续'。"
            )

    def search_order(self, order_id: str) -> SearchResult:
        """
        用订单号在搜索栏查询，返回结果。

        :returns: SearchResult （found=False 时 status 固定为 "订单不存在"）
        """
        self._check_stop()
        assert self._page is not None
        order_id = order_id.strip()
        self._log(f"[查询] 订单号: {order_id}")

        # 1. 定位搜索框
        input_sel = self._resolve_selector("order_id_input")
        if input_sel is None:
            raise BrowserError("未能定位到订单号搜索框，请检查是否在正确的页面。")
        inp = self._page.locator(input_sel).first
        inp.click()
        inp.fill("")
        inp.fill(order_id)

        # 2. 点击搜索按钮
        btn_sel = self._resolve_selector("search_button")
        if btn_sel is None:
            raise BrowserError("未能定位到搜索按钮。")
        self._page.locator(btn_sel).first.click()

        # 3. 智能等待结果渲染（轮询最多 15s，检测表格行或空状态）
        self._wait_search_result()
        self._wait_page_stable()

        # 4. 判断是否空状态
        if self._is_empty_state():
            self._log(f"[查询] {order_id} → 订单不存在")
            return SearchResult(order_id=order_id, found=False, status="订单不存在")

        # 5. 在结果行中找匹配订单号的行
        status, row_text = self._extract_status_from_table(order_id)
        if status:
            self._log(f"[查询] {order_id} → 状态: {status}")
            return SearchResult(order_id=order_id, found=True, status=status, row_text=row_text)

        # 表格有数据但没找到这一行（可能是多页，也可能是部分匹配）
        self._log(f"[查询] {order_id} → 表格有数据但未定位到该行，判定为订单不存在")
        return SearchResult(order_id=order_id, found=False, status="订单不存在")

    # ==================================================================
    # 私有辅助
    # ==================================================================
    def _cdp_port_in_use(self) -> bool:
        import urllib.request
        import urllib.error
        url = f"http://127.0.0.1:{self.settings.cdp_port}/json/version"
        try:
            with urllib.request.urlopen(url, timeout=1.0) as r:
                return r.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def _launch_chrome_with_cdp(self) -> None:
        chrome_exe = self.settings.chrome_executable_resolved()
        if not chrome_exe:
            raise BrowserError(
                "未找到 Chrome 可执行文件。请先安装 Google Chrome，或在配置里指定 chrome.exe 路径。"
            )
        user_data = self.settings.chrome_user_data_dir
        Path(user_data).mkdir(parents=True, exist_ok=True)
        args = [
            chrome_exe,
            f"--remote-debugging-port={self.settings.cdp_port}",
            f"--user-data-dir={user_data}",
            "--no-first-run",
            "--no-default-browser-check",
            self.settings.lingxing_order_page_url,
        ]
        self._log(f"[Chrome] 启动参数: {' '.join(args)}")
        try:
            self._chrome_proc = subprocess.Popen(args)
        except OSError as exc:
            raise BrowserError(f"启动 Chrome 失败: {exc}") from exc

    def _connect_existing(self, cdp_url: str) -> None:
        assert self._pw is not None
        try:
            self._browser = self._pw.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:
            raise BrowserError(f"连接 Chrome CDP 失败: {exc}") from exc

        contexts = self._browser.contexts
        if not contexts:
            # 没 context 就创建一个（罕见）
            self._context = self._browser.new_context()
            self._page = self._context.new_page()
        else:
            self._context = contexts[0]
            pages = self._context.pages
            if pages:
                self._page = pages[-1]
            else:
                self._page = self._context.new_page()

        # 设置超时
        timeout_ms = self.settings.request_timeout_ms
        self._page.set_default_timeout(timeout_ms)
        self._page.set_default_navigation_timeout(timeout_ms)

    # ------------------------------------------------------------------
    def _resolve_selector(self, key: str) -> Optional[str]:
        """
        依次尝试 SelectorSet 中的 selector，命中则 **锁定**（后续查询不再尝试备选）。
        """
        if key in self._locked_selectors:
            return self._locked_selectors[key]

        sel_set: SelectorSet = LINGXING_SELECTORS[key]
        assert self._page is not None
        for candidate in sel_set.all():
            try:
                loc = self._page.locator(candidate).first
                if loc.count() > 0 and loc.is_visible():
                    self._locked_selectors[key] = candidate
                    # 运行时写回 SelectorSet.locked，保持一致
                    sel_set.locked = candidate
                    self._log(f"[选择器] 锁定 {key} → {candidate!r}")
                    return candidate
            except PwTimeoutError:
                continue
            except Exception as exc:
                self._log(f"[选择器] {candidate!r} 异常: {exc}")
                continue
        return None

    def _dump_selector_status(self) -> None:
        """首次连接后，对关键元素跑一次探测，加速后续查询。"""
        for key in ("order_id_input", "search_button", "table_body", "empty_state"):
            try:
                self._resolve_selector(key)
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _wait_page_stable(self) -> None:
        """简单等一下网络空闲或无新 DOM 变化，避免过早读取。"""
        assert self._page is not None
        try:
            self._page.wait_for_load_state("networkidle", timeout=5000)
        except PwTimeoutError:
            pass
        self._page.wait_for_timeout(300)

    def _wait_search_result(self, timeout_ms: int = 15000) -> None:
        """
        点击搜索按钮后轮询等待，直到出现表格行 或 空状态 之一（超时则保守返回）。

        比固定 page_load_wait_ms 更稳：ERP 页面响应慢时不会过早读。
        """
        assert self._page is not None
        table_row_key = "table_row"
        empty_key = "empty_state"

        # 初始先等一个很短的时间，让 DOM 开始变化
        self._page.wait_for_timeout(300)

        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            self._check_stop()
            # 先尝试 table_row —— 有可见行 = 有数据
            row_sel = self._resolve_selector(table_row_key)
            if row_sel:
                try:
                    rows = self._page.locator(row_sel)
                    if rows.count() > 0 and rows.first.is_visible():
                        return  # 有行，渲染完成
                except Exception:
                    pass
            # 再尝试 empty_state —— 空状态也说明渲染完成
            empty_sel = self._resolve_selector(empty_key)
            if empty_sel:
                try:
                    es = self._page.locator(empty_sel).first
                    if es.count() > 0 and es.is_visible():
                        return
                except Exception:
                    pass
            time.sleep(0.2)
        # 超时返回，让上层继续做最终判断
        self._log(f"[等待] 搜索结果等待 {timeout_ms}ms 超时，继续执行后续逻辑")

    def _is_empty_state(self) -> bool:
        """
        判断表格是否显示"暂无数据"。

        逻辑（修复版）：
        1. 先看 table_row 有没有可见行 → 有行 = False（有数据）
        2. 再看 empty_state 是否可见 → 可见 = True
        3. 二者都没定位到 → 保守返回 True（避免误把错误页当成有数据）
        """
        assert self._page is not None

        # --- 1. 优先看有没有数据行 ---
        row_sel = self._resolve_selector("table_row")
        if row_sel:
            try:
                rows = self._page.locator(row_sel)
                n = rows.count()
                if n > 0:
                    # 至少有一行可见才算"有数据"
                    for i in range(min(n, 3)):  # 只检查前 3 行，加快速度
                        try:
                            if rows.nth(i).is_visible():
                                return False
                        except Exception:
                            continue
            except Exception:
                pass

        # --- 2. 再看有没有空状态提示 ---
        empty_sel = self._resolve_selector("empty_state")
        if empty_sel:
            try:
                es = self._page.locator(empty_sel).first
                if es.count() > 0 and es.is_visible():
                    return True
            except Exception:
                pass

        # --- 3. 兜底：table_body 是否存在（可见但无行也视为空）---
        body_sel = self._resolve_selector("table_body")
        if body_sel:
            try:
                tb = self._page.locator(body_sel).first
                if tb.count() > 0 and tb.is_visible():
                    # body 可见但前面没找到行，说明真的是空
                    return True
            except Exception:
                pass

        return True

    # ------------------------------------------------------------------
    def _extract_status_from_table(self, order_id: str) -> tuple[str, str]:
        """
        在当前结果表格中找到包含 order_id 的行，返回该行的状态列文本。

        策略：
        1. 从 thead 找到"订单状态"列索引
        2. 遍历所有 tbody tr，用 inner_text 判断是否包含 order_id
        3. 从命中行取对应索引的 td 文本
        """
        assert self._page is not None

        # 1. 定位状态列
        status_col_idx = self._find_status_column_index()
        if status_col_idx is None:
            self._log("[表格] 未找到'订单状态'列，尝试倒数第 2 列 ...")
            # ant-design 默认状态列常在倒数位置，回退方案
            status_col_idx = -2

        # 2. 遍历行
        row_sel = self._resolve_selector("table_row")
        if row_sel is None:
            return "", ""

        rows = self._page.locator(row_sel)
        row_count = rows.count()
        self._log(f"[表格] 共 {row_count} 行待扫描")

        for i in range(row_count):
            row = rows.nth(i)
            try:
                row_text = row.inner_text()
            except Exception:
                continue
            if order_id not in row_text:
                continue
            # 找到匹配行
            try:
                cells = row.locator("> td")
                n_cells = cells.count()
                # 跳过可能的 checkbox 列
                col = status_col_idx if status_col_idx >= 0 else (n_cells + status_col_idx)
                if 0 <= col < n_cells:
                    status_text = cells.nth(col).inner_text().strip()
                else:
                    status_text = row_text.split()[-2].strip()
                return status_text, row_text
            except Exception as exc:
                self._log(f"[表格] 读行 {i} 异常: {exc}")
                continue

        return "", ""

    def _find_status_column_index(self) -> Optional[int]:
        """从 thead 中定位"订单状态"/"状态"列的索引（考虑前面可能有 checkbox 列）。"""
        assert self._page is not None
        try:
            headers = self._page.locator(".ant-table-thead tr th")
            count = headers.count()
            for i in range(count):
                txt = headers.nth(i).inner_text().strip()
                if "状态" in txt or "state" in txt.lower():
                    return i
        except Exception:
            pass
        return None

    # ==================================================================
    # 调试工具
    # ==================================================================
    def dump_page_snippet(self, max_len: int = 2000) -> str:
        """返回当前页面的 HTML 片段（调试 selector 用）。"""
        assert self._page is not None
        html = self._page.content()
        return html[:max_len]
