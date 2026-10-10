"""
订单查询页（首页）：输入订单ID（≤200）+ 可选状态筛选 → 查询 → 结果表格。
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import QEvent, Qt, QThread, pyqtSignal
from PyQt5.QtGui import QStandardItem, QStandardItemModel
from PyQt5.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListView,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from api.lingxing_api_client import LingxingApiClient, LingxingApiError, LingxingOrder
from config.settings import AppSettings, get_settings


MAX_QUERY = 200  # 单次最多查询条数

# 订单状态枚举（领星 OrderList 接口 orderStatus 取值）
ORDER_STATUS_CHOICES = [
    "Canceled",
    "Pending",
    "PendingAvailability",
    "Unshipped",
    "PartiallyShipped",
    "Shipped",
    "InvoiceUnconfirmed",
    "Unfulfillable",
]


class MultiSelectComboBox(QComboBox):
    """可多选的状态下拉框：勾选多个状态，查询时用英文逗号拼接传给 API。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._updating = False

        self.setEditable(True)
        self.lineEdit().setReadOnly(True)
        self.lineEdit().setStyleSheet("background:transparent;border:none;")
        self.setModel(QStandardItemModel(self))
        self.setView(QListView(self))
        self.view().setSelectionMode(self.view().NoSelection)

        # 第 0 项：全部（与其他状态互斥）
        self._all_item = QStandardItem("全部（不过滤）")
        self._all_item.setCheckable(True)
        self._all_item.setCheckState(Qt.Checked)
        self.model().appendRow(self._all_item)

        self._status_items: list[QStandardItem] = []
        for st in ORDER_STATUS_CHOICES:
            it = QStandardItem(st)
            it.setCheckable(True)
            it.setCheckState(Qt.Unchecked)
            self.model().appendRow(it)
            self._status_items.append(it)

        self.model().itemChanged.connect(self._on_item_changed)
        self.view().viewport().installEventFilter(self)
        self._sync_text()

    # ------------------------------------------------------------------
    def eventFilter(self, obj, event):
        # 吞掉弹层上的鼠标点击：手动切换勾选，且弹层不关闭
        if obj is self.view().viewport() and event.type() == QEvent.MouseButtonRelease:
            idx = self.view().indexAt(event.pos())
            item = self.model().itemFromIndex(idx)
            if item is not None and item.isEnabled():
                item.setCheckState(
                    Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked
                )
            return True
        return super().eventFilter(obj, event)

    def _on_item_changed(self, item: QStandardItem) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            if item is self._all_item:
                if item.checkState() == Qt.Checked:
                    for it in self._status_items:
                        it.setCheckState(Qt.Unchecked)
                elif all(it.checkState() == Qt.Unchecked for it in self._status_items):
                    item.setCheckState(Qt.Checked)  # 全部取消时保持"全部"勾选
            else:
                if item.checkState() == Qt.Checked:
                    self._all_item.setCheckState(Qt.Unchecked)
                elif all(it.checkState() == Qt.Unchecked for it in self._status_items):
                    self._all_item.setCheckState(Qt.Checked)
            self._sync_text()
        finally:
            self._updating = False

    def _sync_text(self) -> None:
        checked = self._checked()
        self.lineEdit().setText(", ".join(checked) if checked else "全部（不过滤）")

    def _checked(self) -> list[str]:
        return [it.text() for it in self._status_items if it.checkState() == Qt.Checked]

    def status_value(self) -> str:
        """返回 API 的 orderStatus 参数，如 'Canceled,Pending'；全部=空串。"""
        return ",".join(self._checked())


def _split_order_ids(text: str) -> list[str]:
    """支持换行 / 逗号 / 空格 / 分号分隔，去重保序。"""
    raw = re.split(r"[\s,;，；]+", text)
    seen: set[str] = set()
    ids: list[str] = []
    for r in raw:
        r = r.strip()
        if r and r not in seen:
            seen.add(r)
            ids.append(r)
    return ids


class QueryThread(QThread):
    """订单查询线程（不阻塞 UI）。"""
    log = pyqtSignal(str)
    done = pyqtSignal(object, list)   # found: dict[oid, LingxingOrder], not_found: list[str]
    error = pyqtSignal(str)

    def __init__(self, settings: AppSettings, order_ids: list[str], order_status: str) -> None:
        super().__init__()
        self._settings = settings
        self._order_ids = order_ids
        self._order_status = order_status

    def run(self) -> None:
        try:
            client = LingxingApiClient(self._settings)
            found = client.query_orders(self._order_ids, self._order_status)
            not_found = [oid for oid in self._order_ids if oid not in found]
            self.done.emit(found, not_found)
        except LingxingApiError as exc:
            self.error.emit(str(exc))
        except Exception as exc:
            self.error.emit(f"{type(exc).__name__}: {exc}")


class OrderQueryPage(QWidget):
    """订单查询首页。"""

    def __init__(self) -> None:
        super().__init__()
        self._thread: QueryThread | None = None
        self._result_rows: list[tuple[str, str, str, str]] = []  # 导出用
        self._build()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)

        # ---- 输入区 ----
        root.addWidget(QLabel("订单ID（每行一个，也可用逗号/空格分隔，最多 200 条）:"))
        self.ids_edit = QPlainTextEdit()
        self.ids_edit.setPlaceholderText(
            "114-7080172-7829841\n112-7784120-3840257\n111-8324820-8011451\n..."
        )
        self.ids_edit.textChanged.connect(self._update_count)
        root.addWidget(self.ids_edit)

        input_row = QHBoxLayout()
        self.count_label = QLabel("已输入 0 / 200 条")
        input_row.addWidget(self.count_label)
        input_row.addSpacing(20)
        input_row.addWidget(QLabel("订单状态筛选（可多选）:"))
        self.status_combo = MultiSelectComboBox()
        self.status_combo.setFixedWidth(240)
        input_row.addWidget(self.status_combo)
        self.query_btn = QPushButton("🔍 查询")
        self.query_btn.setStyleSheet("background:#2e8b57;color:white;font-weight:bold;padding:6px 24px")
        self.query_btn.clicked.connect(self._do_query)
        input_row.addWidget(self.query_btn)
        self.export_btn = QPushButton("📥 导出结果")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._export)
        input_row.addWidget(self.export_btn)
        root.addLayout(input_row)

        # ---- 结果表 ----
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["订单ID", "ASIN", "金额", "订单状态"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        root.addWidget(self.table, 1)

        self.stat_label = QLabel("")
        self.stat_label.setStyleSheet("color:#333;padding:2px")
        root.addWidget(self.stat_label)

    # ------------------------------------------------------------------
    def _update_count(self) -> None:
        n = len(_split_order_ids(self.ids_edit.toPlainText()))
        self.count_label.setText(f"已输入 {n} / 200 条")
        if n > MAX_QUERY:
            self.count_label.setText(f"⚠ 已输入 {n} 条，超过 200 条上限，查询时只取前 200 条！")
            self.count_label.setStyleSheet("color:#b22222;font-weight:bold;")
        else:
            self.count_label.setStyleSheet("color:#333;")

    # ------------------------------------------------------------------
    def _do_query(self) -> None:
        if self._thread is not None:
            try:
                if self._thread.isRunning():
                    QMessageBox.information(self, "提示", "查询进行中，请稍候。")
                    return
            except RuntimeError:
                # C++ 对象已被 deleteLater 销毁，Python 引用还在 → 清理掉重建
                self._thread = None

        settings: AppSettings = get_settings()
        if not settings.lingxing_auth_token:
            QMessageBox.warning(self, "提示", "未配置领星 Token，请到【配置】页填写。")
            return

        ids = _split_order_ids(self.ids_edit.toPlainText())[:MAX_QUERY]
        if not ids:
            QMessageBox.warning(self, "提示", "请先输入订单ID。")
            return

        status_filter = self.status_combo.status_value()
        self.table.setRowCount(0)
        self.stat_label.setText("查询中 ...")
        self.query_btn.setEnabled(False)
        self.export_btn.setEnabled(False)

        self._thread = QueryThread(settings, ids, status_filter)
        self._thread.done.connect(self._on_done)
        self._thread.error.connect(self._on_error)
        self._thread.finished.connect(self._reap_query_thread)
        self._thread.start()

    def _reap_query_thread(self) -> None:
        """线程结束后等待其完全终止再释放引用，避免销毁竞态崩溃。"""
        t = self._thread
        if t is not None:
            try:
                t.wait(5000)
            except RuntimeError:
                pass
        self._thread = None

    def _on_done(self, found: dict, not_found: list) -> None:
        self.query_btn.setEnabled(True)
        self._result_rows = []
        for lo in found.values():
            self._result_rows.append(
                (lo.order_id, lo.asin or "-", f"{lo.order_total_amount:.2f}", lo.order_status)
            )
        for oid in not_found:
            self._result_rows.append((oid, "-", "-", "订单不存在"))

        # 订单不存在放最后
        self._fill_table()
        self.export_btn.setEnabled(bool(self._result_rows))
        self.stat_label.setText(
            f"查询完成：找到 {len(found)} 条，未找到 {len(not_found)} 条"
        )

    def _on_error(self, err: str) -> None:
        self.query_btn.setEnabled(True)
        self.stat_label.setText(f"查询失败: {err}")
        self.stat_label.setStyleSheet("color:#b22222;font-weight:bold;")
        QMessageBox.critical(self, "查询失败", err)

    # ------------------------------------------------------------------
    def _fill_table(self) -> None:
        self.table.setRowCount(len(self._result_rows))
        for r, (oid, asin, amt, status) in enumerate(self._result_rows):
            for c, val in enumerate((oid, asin, amt, status)):
                item = QTableWidgetItem(val)
                if status == "订单不存在":
                    item.setForeground(Qt.red)
                elif status == "Canceled":
                    item.setForeground(Qt.darkYellow)
                self.table.setItem(r, c, item)

    # ------------------------------------------------------------------
    def _export(self) -> None:
        if not self._result_rows:
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出查询结果", str(Path.home() / f"订单查询_{ts}.xlsx"),
            "Excel 文件 (*.xlsx)")
        if not path:
            return
        try:
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = "查询结果"
            ws.append(["订单ID", "ASIN", "金额", "订单状态"])
            for row in self._result_rows:
                ws.append(list(row))
            for col_idx, width in enumerate((20, 14, 10, 16)):
                ws.column_dimensions[chr(65 + col_idx)].width = width
            wb.save(path)
            QMessageBox.information(self, "完成", f"已导出 {len(self._result_rows)} 条:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
