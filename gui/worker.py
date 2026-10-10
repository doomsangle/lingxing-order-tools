"""
后台执行 Worker（QThread）。

三重校验：
  1) 订单是否存在
  2) 订单ID 与 ASIN 是否匹配
  3) 订单金额是否相等（四舍五入2位小数）

两种输出模式（自动切换）：
  - 用户回写 API 可用 → 调 API 更新状态
  - API 未配置/不通   → 写不匹配行到本地 Excel
"""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from PyQt5.QtCore import QObject, QThread, pyqtSignal

from api.lingxing_api_client import LingxingApiClient, LingxingApiError, LingxingOrder
from api.order_api_client import OrderApiClient, OrderApiError
from automation.browser_controller import BrowserController, BrowserError
from config.settings import AppSettings
from datasource.excel_reader import ExcelOrderRow
from datasource.excel_writer import write_mismatch_excel


@dataclass
class WorkerResult:
    total: int = 0
    found: int = 0
    not_found: int = 0
    asin_mismatch: int = 0
    amount_mismatch: int = 0
    all_match: int = 0              # 三项全部通过
    api_updated: int = 0
    api_failed: int = 0
    errors: int = 0
    mismatch_excel_path: str = ""
    mismatch_failed: bool = False   # 不匹配 Excel 写入失败


class OrderProcessWorker(QObject):
    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    item_result = pyqtSignal(int, str, str, bool)  # idx, order_id, 说明, api_ok
    finished_ok = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        orders: list[ExcelOrderRow],
        settings: AppSettings,
        excel_path: str = "",
    ) -> None:
        super().__init__()
        self._orders = orders
        self._settings = settings
        self._excel_path = excel_path   # 当前处理的 Excel 实际路径（用于输出不匹配文件）
        self._wants_stop = False
        self._mismatches: list[tuple[ExcelOrderRow, str]] = []

    def request_stop(self) -> None:
        self._wants_stop = True

    # ------------------------------------------------------------------
    def run(self) -> None:
        mode = self._settings.mode
        try:
            if mode == "api":
                self._run_api()
            elif mode == "browser":
                self._run_browser()
            else:
                self.failed.emit(f"未知模式: {mode}")
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}")
        finally:
            # 在自己线程的事件循环里排队销毁（此时 quit 还未投递，顺序确定），
            # 避免跨线程 delete 引发 "QThread: Destroyed while thread is still running" 崩溃
            self.deleteLater()

    # ==================================================================
    # API 模式
    # ==================================================================
    def _run_api(self) -> None:
        total = len(self._orders)
        result = WorkerResult(total=total)

        self._log("[模式] 直接 API 查询")
        lingxing = LingxingApiClient(self._settings)
        lingxing.test_connection()
        self._log("[初始化] 领星 API 连通 ✓")

        # 用户回写 API（可选）
        api_client = self._init_user_api()

        batch_size = self._settings.lingxing_batch_size
        batch_delay = self._settings.batch_delay_sec

        for start in range(0, total, batch_size):
            if self._wants_stop:
                self._log("[Worker] 收到停止请求，退出。")
                break

            batch = self._orders[start:start + batch_size]
            batch_no = start // batch_size + 1
            total_batches = (total + batch_size - 1) // batch_size
            self._log(f"[批次 {batch_no}/{total_batches}] 查询 {len(batch)} 条 ...")

            batch_ids = [o.order_id for o in batch]
            try:
                found_map = lingxing.query_orders(batch_ids)
            except LingxingApiError as exc:
                result.errors += len(batch)
                self._log(f"[批次 {batch_no}] 查询失败: {exc}")
                for i, o in enumerate(batch, start + 1):
                    self.item_result.emit(i, o.order_id, f"查询错误: {exc}", False)
                    self.progress.emit(i, total)
                continue

            for i, o in enumerate(batch, start + 1):
                if self._wants_stop:
                    break
                desc, api_ok = self._check_and_emit(i, o, found_map, api_client, result)
                self.progress.emit(i, total)

            if not self._wants_stop and start + batch_size < total:
                time.sleep(batch_delay)

        # 写不匹配 Excel
        self._write_mismatches(result)
        self.progress.emit(total, total)
        self.finished_ok.emit(result)

    # ==================================================================
    # 浏览器模式（仍做三重校验，但只能查状态，ASIN/金额由 API 辅助）
    # ==================================================================
    def _run_browser(self) -> None:
        total = len(self._orders)
        result = WorkerResult(total=total)

        self._log("[模式] 浏览器自动化")
        if not self._settings.chrome_executable_resolved():
            raise RuntimeError("未检测到 Chrome，请在配置里指定 chrome.exe 路径。")

        # 浏览器模式下也可用 API 做ASIN/金额校验
        lingxing: LingxingApiClient | None = None
        try:
            lingxing = LingxingApiClient(self._settings)
            lingxing.test_connection()
            self._log("[初始化] 领星 API 也可用，浏览器+API 双通道校验")
        except LingxingApiError:
            self._log("[初始化] 领星 API 不可用，仅浏览器查状态")

        api_client = self._init_user_api()
        ctl = BrowserController(self._settings, log_cb=self._log)
        try:
            ctl.connect()
            ctl.ensure_on_order_page()
            self._log("[初始化] Chrome 已连接 ✓")

            # 如果有 API，批量查 ASIN+金额；浏览器只查状态
            api_map: dict[str, LingxingOrder] = {}
            if lingxing is not None:
                batch_size = self._settings.lingxing_batch_size
                for s in range(0, total, batch_size):
                    if self._wants_stop: break
                    chunk = [o.order_id for o in self._orders[s:s+batch_size]]
                    try:
                        api_map.update(lingxing.query_orders(chunk))
                    except LingxingApiError:
                        pass
                    time.sleep(self._settings.batch_delay_sec)

            for i, o in enumerate(self._orders, 1):
                if self._wants_stop:
                    self._log("[Worker] 收到停止请求，退出。")
                    break
                self.progress.emit(i - 1, total)

                # 浏览器查状态
                sr = None
                last_exc: Exception | None = None
                for attempt in (1, 2):
                    if self._wants_stop: break
                    try:
                        sr = ctl.search_order(o.order_id); last_exc = None; break
                    except BrowserError as exc:
                        last_exc = exc
                        if attempt == 1:
                            self._log(f"[重试] {o.order_id} 失败: {exc}，1s 后重试 ...")
                            time.sleep(1); continue
                    except Exception as exc:
                        last_exc = exc; break

                if last_exc is not None:
                    result.errors += 1
                    self._log(f"[错误] {o.order_id}: {last_exc}")
                    self.item_result.emit(i, o.order_id, f"浏览器错误: {last_exc}", False)
                    continue

                # 构造 found_map 兼容 _check_and_emit
                found_map: dict[str, LingxingOrder] = {}
                if sr.found:
                    lo = LingxingOrder(order_id=o.order_id, order_status=sr.status)
                    if o.order_id in api_map:
                        lo.asin = api_map[o.order_id].asin
                        lo.order_total_amount = api_map[o.order_id].order_total_amount
                    found_map[o.order_id] = lo

                self._check_and_emit(i, o, found_map, api_client, result)
                self.progress.emit(i, total)

            self._write_mismatches(result)
            self.progress.emit(total, total)
            self.finished_ok.emit(result)
        finally:
            ctl.close(close_chrome=False)

    # ==================================================================
    # 三重校验核心
    # ==================================================================
    def _check_and_emit(
        self, idx: int, ex_row: ExcelOrderRow,
        found_map: dict[str, LingxingOrder],
        api_client: OrderApiClient | None,
        result: WorkerResult,
    ) -> tuple[str, bool]:
        """
        三重校验：存在性 / ASIN匹配 / 金额匹配。
        返回 (说明文字, api_ok)。
        """
        oid = ex_row.order_id
        reasons: list[str] = []

        # 1. 存在性
        if oid not in found_map:
            result.not_found += 1
            desc = "订单不存在"
            self._mismatches.append((ex_row, desc))
            if api_client is not None:
                try:
                    api_client.update_status(oid, desc); result.api_updated += 1; api_ok = True
                except OrderApiError:
                    result.api_failed += 1; api_ok = False
            else:
                api_ok = True
            self.item_result.emit(idx, oid, desc, api_ok)
            return desc, api_ok

        lo = found_map[oid]
        result.found += 1

        # 2. ASIN 匹配
        if ex_row.asin and lo.asin and ex_row.asin.strip().upper() != lo.asin.strip().upper():
            result.asin_mismatch += 1
            reasons.append(f"ASIN不匹配(Excel:{ex_row.asin} / 领星:{lo.asin})")

        # 3. 金额匹配（四舍五入2位小数）
        #    Excel/API 金额 ≤ ERP 金额 → 正常；> ERP 金额 → 异常
        if ex_row.amount > 0 and lo.order_total_amount > 0:
            ea = round(ex_row.amount, 2)
            la = round(lo.order_total_amount, 2)
            if ea > la:
                result.amount_mismatch += 1
                reasons.append(f"金额异常(Excel:{ea} > 领星:{la})")

        # 汇总
        if reasons:
            desc = "；".join(reasons)
            self._mismatches.append((ex_row, desc))
        else:
            result.all_match += 1
            desc = f"✅ 匹配 (状态:{lo.order_status})"

        # 回写
        api_ok = False
        if api_client is not None:
            try:
                api_client.update_status(oid, desc); result.api_updated += 1; api_ok = True
            except OrderApiError:
                result.api_failed += 1; api_ok = False
        else:
            api_ok = True

        self.item_result.emit(idx, oid, desc, api_ok)
        return desc, api_ok

    # ==================================================================
    def _init_user_api(self) -> OrderApiClient | None:
        if not self._settings.api_base_url:
            self._log("[初始化] 未配置回写 API，不匹配结果将写入本地 Excel")
            return None
        c = OrderApiClient(
            base_url=self._settings.api_base_url,
            token=self._settings.api_token,
            query_path="/orders",
            update_path=self._settings.api_update_path,
        )
        try:
            c.test_connection()
            self._log("[初始化] 用户回写 API 连通 ✓")
        except OrderApiError as exc:
            self._log(f"[初始化] 回写 API 不可用（将走本地 Excel）: {exc}")
            return None
        return c

    def _write_mismatches(self, result: WorkerResult) -> None:
        """把不匹配行写入 Excel（源文件不可读时自动降级：无表头 / 换输出目录）。"""
        if not self._mismatches:
            self._log("[输出] 全部匹配，无需写 Excel")
            return
        source = self._excel_path or self._settings.last_excel_path
        out = self._settings.output_excel_path or None
        try:
            path = write_mismatch_excel(source, self._mismatches, out)
            result.mismatch_excel_path = path
            self._log(f"[输出] 不匹配 {len(self._mismatches)} 条已写入: {path}")
        except Exception as exc:
            self._log(f"[输出] 写 Excel 失败: {exc}")
            # 兜底：改存到配置目录（exe 同级 config），保证文件一定生成
            try:
                from datetime import datetime
                from config.settings import _config_dir
                fallback = Path(_config_dir()) / (
                    f"不匹配订单_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
                path = write_mismatch_excel(source, self._mismatches, fallback)
                result.mismatch_excel_path = path
                self._log(f"[输出] 已改存到: {path}")
            except Exception as exc2:
                result.mismatch_failed = True
                self._log(f"[输出] 兜底写入也失败: {exc2}（不匹配 {len(self._mismatches)} 条）")

    def _log(self, msg: str) -> None:
        self.log.emit(msg)


def start_worker(
    orders: list[ExcelOrderRow],
    settings: AppSettings,
    excel_path: str = "",
) -> tuple[QThread, OrderProcessWorker]:
    """
    创建工作线程。注意：不做任何 deleteLater —— 线程退出后由 GUI 侧 wait()
    再释放引用，避免 "QThread: Destroyed while thread is still running" 崩溃。
    """
    thread = QThread()
    worker = OrderProcessWorker(orders, settings, excel_path=excel_path)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished_ok.connect(thread.quit)
    worker.failed.connect(thread.quit)
    return thread, worker
