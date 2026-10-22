"""
主窗口：左侧菜单 + 三页结构。

  1) 订单查询（首页）：输入订单ID（≤200）+ 状态筛选 → 查询 → 表格展示
  2) 批量匹配：Excel 批量三重校验（原有流程）
  3) 配置：领星 API / 浏览器 / 回写 API / 输出路径
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import Qt, QThread
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from api.lingxing_api_client import LingxingApiClient, LingxingApiError
from config.settings import AppSettings, get_settings, reload_settings
from datasource.excel_reader import read_excel_orders, ExcelOrderRow
from gui.order_query_page import OrderQueryPage
from gui.worker import OrderProcessWorker, WorkerResult, start_worker


PRE_RUN_WARNING_API = (
    "⚠️ 运行前请确认：\n\n"
    "1) amzvideohelper 包装接口服务可用；\n"
    "2) 已选择含'订单号'列的 Excel 文件；\n"
    "3) 若配置了状态回写 API，请确认 API 服务可达。"
)

PRE_RUN_WARNING_BROWSER = (
    "⚠️ 运行前请确认：\n\n"
    "1) 已在 Chrome 中登录领星 ERP（首次使用工具会自动弹出 Chrome 让你登录）；\n"
    "2) Chrome 已打开 https://erp.lingxing.com/erp/orderList 订单列表页；\n"
    "3) 已选择含'订单号'列的 Excel 文件；\n"
    "4) 当前店铺已切换到你要处理的那个店铺。"
)


# ======================================================================
# 配置页（原 ConfigDialog 改为嵌入页面）
# ======================================================================
class ConfigPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._build()
        self._load()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)

        # ====== API 模式配置（amzvideohelper 包装接口，零 Token）======
        self.api_box = QGroupBox("API 模式（通过 amzvideohelper 包装接口查询，服务端持有 Token，客户端零配置）")
        api_fmt = QFormLayout(self.api_box)
        api_fmt.setContentsMargins(8, 20, 8, 8)

        api_info = QLabel(
            "✅ 领星 ERP Token 已由 amzvideohelper 服务端统一管理\n"
            "   Token 过期后只需服务端更新，所有客户端自动生效\n\n"
            "内置接口地址（无需修改）：\n"
            "  订单查询  https://amzvideohelper.top/api/erp/lingxing/order/list\n"
            "  连通测试  https://amzvideohelper.top/api/erp/lingxing/test\n"
        )
        api_info.setStyleSheet("color:#2e8b57;font-size:12px;font-weight:bold;")
        api_info.setWordWrap(True)
        api_fmt.addRow(api_info)

        self.api_batch_edit = QSpinBox(); self.api_batch_edit.setRange(1, 200)
        api_fmt.addRow("每批查询数量:", self.api_batch_edit)

        self.api_test_btn = QPushButton("🔗 测试 amzvideohelper 接口连通性")
        self.api_test_btn.clicked.connect(self._test_lingxing)
        api_fmt.addRow("", self.api_test_btn)

        # ====== 浏览器配置 ======
        self.br_box = QGroupBox("浏览器自动化配置")
        br_layout = QFormLayout(self.br_box)
        self.br_cdp_port = QSpinBox(); self.br_cdp_port.setRange(1024, 65535)
        self.br_exe_edit = QLineEdit(); self.br_exe_edit.setPlaceholderText("留空自动探测 chrome.exe")
        self.br_userdir_edit = QLineEdit()
        self.br_userdir_edit.setPlaceholderText("留空使用 config\\chrome_profile（登录态跟 exe 走）")
        self.br_url_edit = QLineEdit()

        browse_btn = QPushButton("浏览 ...")
        browse_btn.clicked.connect(lambda: self._pick_file(self.br_exe_edit, "可执行文件 (*.exe)"))
        exe_row = QHBoxLayout(); exe_row.addWidget(self.br_exe_edit, 1); exe_row.addWidget(browse_btn)
        exe_wrap = QWidget(); exe_wrap.setLayout(exe_row)

        userdir_btn = QPushButton("选择 ...")
        userdir_btn.clicked.connect(lambda: self._pick_dir(self.br_userdir_edit))
        ud_row = QHBoxLayout(); ud_row.addWidget(self.br_userdir_edit, 1); ud_row.addWidget(userdir_btn)
        ud_wrap = QWidget(); ud_wrap.setLayout(ud_row)

        br_layout.addRow("CDP 调试端口:", self.br_cdp_port)
        br_layout.addRow("chrome.exe:", exe_wrap)
        br_layout.addRow("Chrome 用户目录:", ud_wrap)
        br_layout.addRow("领星订单页 URL:", self.br_url_edit)

        # ---- 模式 Tab 切换（当前选中 Tab = 生效查询模式）----
        self.mode_tabs = QTabWidget()
        api_tab = QWidget()
        api_lay = QVBoxLayout(api_tab)
        api_lay.setContentsMargins(0, 4, 0, 0)
        api_lay.addWidget(self.api_box)
        api_lay.addStretch()
        browser_tab = QWidget()
        br_lay = QVBoxLayout(browser_tab)
        br_lay.setContentsMargins(0, 4, 0, 0)
        br_lay.addWidget(self.br_box)
        br_lay.addStretch()
        self.mode_tabs.addTab(api_tab, "⚡ 直接 API（快、批量、无需 Token）")
        self.mode_tabs.addTab(browser_tab, "🌐 浏览器自动化（需先登录 Chrome）")

        # ====== 回写 API ======
        api_box = QGroupBox("订单状态回写 API（可选，两种模式共用）")
        api_layout = QFormLayout(api_box)
        self.api_base_edit = QLineEdit(); self.api_base_edit.setPlaceholderText("例: https://your-server.com/api")
        self.api_token_edit = QLineEdit(); self.api_token_edit.setEchoMode(QLineEdit.Password)
        self.api_update_edit = QLineEdit(); self.api_update_edit.setPlaceholderText("默认 /orders/status")
        api_layout.addRow("Base URL:", self.api_base_edit)
        api_layout.addRow("Token:", self.api_token_edit)
        api_layout.addRow("更新状态路径:", self.api_update_edit)

        # ====== 输出路径 ======
        out_box = QGroupBox("不匹配订单输出")
        out_layout = QFormLayout(out_box)
        self.out_path_edit = QLineEdit()
        self.out_path_edit.setPlaceholderText("留空=源Excel同目录，自动加_不匹配_时间戳")
        out_browse = QPushButton("浏览 ...")
        out_browse.clicked.connect(lambda: self._pick_save_file(self.out_path_edit))
        out_row = QHBoxLayout(); out_row.addWidget(self.out_path_edit, 1); out_row.addWidget(out_browse)
        out_wrap = QWidget(); out_wrap.setLayout(out_row)
        out_layout.addRow("输出路径:", out_wrap)

        # ---- 保存按钮 ----
        save_row = QHBoxLayout()
        save_row.addStretch()
        save_btn = QPushButton("💾  保存配置")
        save_btn.setStyleSheet("background:#2e8b57;color:white;font-weight:bold;padding:6px 32px")
        save_btn.clicked.connect(self._save)
        save_row.addWidget(save_btn)

        root.addWidget(self.mode_tabs)
        root.addWidget(api_box)
        root.addWidget(out_box)
        root.addStretch()
        root.addLayout(save_row)

    # ------------------------------------------------------------------
    def _pick_file(self, target: QLineEdit, filter_str: str) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "选择文件", "", filter_str)
        if p: target.setText(p)

    def _pick_dir(self, target: QLineEdit) -> None:
        p = QFileDialog.getExistingDirectory(self, "选择目录")
        if p: target.setText(p)

    def _pick_save_file(self, target: QLineEdit) -> None:
        p, _ = QFileDialog.getSaveFileName(self, "选择输出路径", "", "Excel 文件 (*.xlsx)")
        if p: target.setText(p)

    # ------------------------------------------------------------------
    def _load(self) -> None:
        s = get_settings()
        self.mode_tabs.setCurrentIndex(0 if s.mode == "api" else 1)
        self.api_batch_edit.setValue(s.lingxing_batch_size)
        self.br_cdp_port.setValue(s.cdp_port)
        self.br_exe_edit.setText(s.chrome_executable)
        self.br_userdir_edit.setText(s.chrome_user_data_dir)
        self.br_url_edit.setText(s.lingxing_order_page_url)
        self.api_base_edit.setText(s.api_base_url)
        self.api_token_edit.setText(s.api_token)
        self.api_update_edit.setText(s.api_update_path)
        self.out_path_edit.setText(s.output_excel_path)

    def _save(self) -> None:
        s = get_settings()
        s.mode = "api" if self.mode_tabs.currentIndex() == 0 else "browser"
        s.lingxing_batch_size = self.api_batch_edit.value()
        s.cdp_port = self.br_cdp_port.value()
        s.chrome_executable = self.br_exe_edit.text().strip()
        s.chrome_user_data_dir = self.br_userdir_edit.text().strip()
        s.lingxing_order_page_url = self.br_url_edit.text().strip() or "https://erp.lingxing.com/erp/orderList"
        s.api_base_url = self.api_base_edit.text().strip()
        s.api_token = self.api_token_edit.text()
        s.api_update_path = self.api_update_edit.text().strip() or "/orders/status"
        s.output_excel_path = self.out_path_edit.text().strip()
        s.save()
        reload_settings()
        QMessageBox.information(self, "保存", "配置已保存 ✓")

    def _test_lingxing(self) -> None:
        s = get_settings()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            msg = LingxingApiClient(s).test_connection()
            QMessageBox.information(self, "连接测试", msg)
        except LingxingApiError as exc:
            QMessageBox.critical(self, "连接失败", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "错误", f"未知异常: {exc}")
        finally:
            QApplication.restoreOverrideCursor()


# ======================================================================
# 批量匹配页（原有主界面内容）
# ======================================================================
class BatchMatchPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._thread: Optional[QThread] = None
        self._worker: Optional[OrderProcessWorker] = None
        self._order_rows: list[ExcelOrderRow] = []
        self._build()
        self._apply_style()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        self._settings = get_settings()

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("模式:"))
        self.mode_badge = QLabel()
        self.mode_badge.setStyleSheet(
            "padding:3px 12px;border-radius:10px;background:#e8e8e8;color:#333;font-weight:bold;")
        top_row.addWidget(self.mode_badge)
        top_row.addSpacing(20)
        top_row.addWidget(QLabel("Excel:"))
        self.file_edit = QLineEdit()
        self.file_edit.setPlaceholderText("选择含'订单号/sku或asin/下单实付金额'列的 .xlsx/.xls/.csv 文件")
        self.file_edit.setText(self._settings.last_excel_path)
        browse_btn = QPushButton("浏览 ...")
        browse_btn.clicked.connect(self._pick_excel)
        self.preview_label = QLabel("尚未读取"); self.preview_label.setStyleSheet("color:#666")
        top_row.addWidget(self.file_edit, 1)
        top_row.addWidget(browse_btn)
        top_row.addWidget(self.preview_label)

        btn_row = QHBoxLayout()
        self.test_btn = QPushButton("🔗  测试连接")
        self.test_btn.clicked.connect(self._test_connection)
        self.start_btn = QPushButton("▶  开始")
        self.start_btn.setStyleSheet("background:#2e8b57;color:white;font-weight:bold;padding:6px 18px")
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton("⏹  停止"); self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet("color:#b22222")
        self.stop_btn.clicked.connect(self._stop)
        btn_row.addWidget(self.test_btn)
        btn_row.addStretch(); btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)

        self.progress_bar = QProgressBar(); self.progress_bar.setRange(0, 100)
        self.stat_label = QLabel("就绪"); self.stat_label.setStyleSheet("padding:4px;color:#333")

        self.log_view = QPlainTextEdit(); self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.log_view.setPlaceholderText("日志输出区 ...")
        self.log_view.document().setMaximumBlockCount(5000)

        root.addLayout(top_row); root.addLayout(btn_row)
        root.addWidget(self.progress_bar); root.addWidget(self.stat_label)
        root.addWidget(QLabel("日志:")); root.addWidget(self.log_view, 1)
        self._update_mode_badge()

    def _apply_style(self) -> None:
        self.setStyleSheet("""
            QPushButton { padding: 4px 12px; min-width: 80px; }
            QPlainTextEdit { font-family: Consolas, Monaco, monospace; font-size: 12px; }
        """)

    def _update_mode_badge(self) -> None:
        self._settings = get_settings()
        if self._settings.mode == "api":
            self.mode_badge.setText("⚡ 直接 API")
            self.mode_badge.setStyleSheet(
                "padding:3px 12px;border-radius:10px;background:#d4edda;color:#155724;font-weight:bold;")
            self.test_btn.setVisible(True)
        else:
            self.mode_badge.setText("🌐 浏览器自动化")
            self.mode_badge.setStyleSheet(
                "padding:3px 12px;border-radius:10px;background:#cce5ff;color:#004085;font-weight:bold;")
            self.test_btn.setVisible(False)

    # ==================================================================
    def _pick_excel(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择数据文件",
            self._settings.last_excel_path or str(Path.home()),
            "数据文件 (*.xlsx *.xlsm *.xls *.csv);;所有文件 (*.*)")
        if not path: return
        self.file_edit.setText(path)
        self._settings.last_excel_path = path; self._settings.save()
        self._reload_excel()

    def _reload_excel(self) -> None:
        path = self.file_edit.text().strip()
        if not path:
            self._order_rows = []; self.preview_label.setText("尚未读取"); return
        try:
            self._order_rows = read_excel_orders(path)
            n = len(self._order_rows)
            has_asin = sum(1 for o in self._order_rows if o.asin)
            has_amt = sum(1 for o in self._order_rows if o.amount > 0)
            self.preview_label.setText(f"共 {n} 条 | ASIN:{has_asin} | 金额:{has_amt}")
            self._append_log(f"[Excel] 读取成功: {n} 条订单, {has_asin} 条有ASIN, {has_amt} 条有金额")
        except Exception as exc:
            QMessageBox.critical(self, "错误", f"读取 Excel 失败:\n{exc}")
            self._order_rows = []; self.preview_label.setText("读取失败")

    def _test_connection(self) -> None:
        if get_settings().mode != "api":
            QMessageBox.information(self, "提示", "浏览器模式请点【▶ 开始】自动连接 Chrome。")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            msg = LingxingApiClient(get_settings()).test_connection()
            self._append_log(f"[测试] {msg}")
            QMessageBox.information(self, "连接测试", msg)
        except LingxingApiError as exc:
            self._append_log(f"[测试失败] {exc}")
            QMessageBox.critical(self, "连接失败", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "错误", str(exc))
        finally:
            QApplication.restoreOverrideCursor()

    def _start(self) -> None:
        settings = get_settings()
        if not self._order_rows or self.file_edit.text().strip() != self._settings.last_excel_path:
            self._reload_excel()
        if not self._order_rows:
            QMessageBox.warning(self, "提示", "请先选择有效的 Excel 文件。"); return

        mode = settings.mode
        if mode == "browser" and not settings.chrome_executable_resolved():
            QMessageBox.warning(self, "提示", "未检测到 Chrome。请到【配置】页指定 chrome.exe 路径。"); return

        warn = PRE_RUN_WARNING_API if mode == "api" else PRE_RUN_WARNING_BROWSER
        if QMessageBox.question(self, "运行前确认", warn, QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Cancel) != QMessageBox.Ok:
            return

        self.progress_bar.setValue(0)
        self._update_stat(total=len(self._order_rows), all_match=0, not_found=0,
                          asin_mismatch=0, amount_mismatch=0, errors=0,
                          api_updated=0, api_failed=0)

        self._reap_thread()   # 上一次的线程若还在收尾，先等它退出
        thread, worker = start_worker(
            self._order_rows, settings, excel_path=self.file_edit.text().strip())
        self._thread = thread; self._worker = worker
        worker.log.connect(self._append_log)
        worker.progress.connect(self._on_progress)
        worker.item_result.connect(self._on_item_result)
        worker.failed.connect(self._on_failed)
        worker.finished_ok.connect(self._on_finished)

        self.start_btn.setEnabled(False); self.test_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        thread.start()
        self._append_log(f"========== 开始处理 {len(self._order_rows)} 条订单 ==========")
        self._append_log(f"[模式] {'直接 API 查询' if mode == 'api' else '浏览器自动化'}")

    def _stop(self) -> None:
        if self._worker is None: return
        if QMessageBox.question(self, "确认", "确定要停止当前任务吗？") != QMessageBox.Yes: return
        self._worker.request_stop()

    # ==================================================================
    def _on_progress(self, current: int, total: int) -> None:
        if total <= 0: return
        pct = int(current * 100 / total)
        self.progress_bar.setValue(pct)
        self.progress_bar.setFormat(f"{current} / {total}  ({pct}%)")

    def _on_item_result(self, idx: int, order_id: str, status: str, api_ok: bool) -> None:
        tag = "✅" if status.startswith("✅") else "❌"
        api_tag = "(API ✓)" if api_ok else "(API ✗)"
        self._append_log(f"[{idx:>5}] {tag} {order_id} → {status} {api_tag}")

    def _on_finished(self, result: WorkerResult) -> None:
        self._reap_thread()   # 等线程彻底退出再清理引用，避免销毁竞态崩溃
        self._append_log("========== 全部处理完成 ==========")
        self._update_stat(total=result.total, all_match=result.all_match,
                          not_found=result.not_found, asin_mismatch=result.asin_mismatch,
                          amount_mismatch=result.amount_mismatch, errors=result.errors,
                          api_updated=result.api_updated, api_failed=result.api_failed)
        if result.mismatch_excel_path:
            self._append_log(f"[输出] 不匹配 Excel: {result.mismatch_excel_path}")
        self._reset_buttons()
        extra = ""
        if result.mismatch_excel_path:
            extra = f"\n\n📄 不匹配Excel已保存:\n{result.mismatch_excel_path}"
        elif result.mismatch_failed:
            extra = "\n\n⚠ 不匹配Excel生成失败，请查看日志"
        QMessageBox.information(
            self, "完成",
            f"处理完毕 ✅\n\n总计: {result.total}\n"
            f"  ✅ 全部匹配: {result.all_match}\n"
            f"  ❌ 订单不存在: {result.not_found}\n"
            f"  ⚠ ASIN不匹配: {result.asin_mismatch}\n"
            f"  ⚠ 金额不匹配: {result.amount_mismatch}\n"
            f"  ⚠ 查询错误: {result.errors}\n"
            f"  📡 API回写成功: {result.api_updated}\n"
            f"  📡 API回写失败: {result.api_failed}"
            + extra)

    def _on_failed(self, err: str) -> None:
        self._append_log(f"[致命错误] {err}")
        self._reap_thread()
        self._reset_buttons()
        QMessageBox.critical(self, "执行失败", err)

    def _reap_thread(self) -> None:
        """等待工作线程完全退出后释放引用（顺序确定，无销毁竞态）。"""
        if self._thread is not None:
            try:
                if self._thread.isRunning():
                    self._thread.quit()
                self._thread.wait(5000)
            except RuntimeError:
                pass  # C++ 对象已被销毁（理论上不会再发生）
        self._thread = None
        self._worker = None

    def _reset_buttons(self) -> None:
        self.start_btn.setEnabled(True)
        self.test_btn.setEnabled(get_settings().mode == "api")
        self.stop_btn.setEnabled(False)

    def _update_stat(self, **kwargs: int) -> None:
        self.stat_label.setText(
            f"总 {kwargs.get('total', 0)} | ✅匹配 {kwargs.get('all_match', 0)} | "
            f"❌不存在 {kwargs.get('not_found', 0)} | ⚠ASIN {kwargs.get('asin_mismatch', 0)} | "
            f"⚠金额 {kwargs.get('amount_mismatch', 0)} | ⚠错误 {kwargs.get('errors', 0)} | "
            f"📡API {kwargs.get('api_updated', 0)}/{kwargs.get('api_failed', 0)}")

    def _append_log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{ts}] {msg}")
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def shutdown(self) -> None:
        if self._worker is not None:
            self._worker.request_stop()
        self._reap_thread()


# ======================================================================
# 主窗口：左侧菜单 + 堆叠页面
# ======================================================================
class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("领星 ERP 订单批量查询工具")
        self.resize(1000, 660)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- 左侧菜单 ----
        self.menu = QListWidget()
        self.menu.setFixedWidth(140)
        self.menu.setStyleSheet("""
            QListWidget {
                background:#2c3e50; color:#ecf0f1; border:none;
                font-size:14px; outline:none;
            }
            QListWidget::item { padding: 14px 8px; }
            QListWidget::item:selected {
                background:#3498db; color:white; border-left:4px solid #2980b9;
            }
            QListWidget::item:hover { background:#34495e; }
        """)
        for name in ("🔍  订单查询", "📊  批量匹配", "⚙  配置"):
            QListWidgetItem(name, self.menu)

        # ---- 右侧页面 ----
        self.stack = QStackedWidget()

        self.order_query_page = OrderQueryPage()
        self.batch_page = BatchMatchPage()
        self.config_page = ConfigPage()

        self.stack.addWidget(self.order_query_page)   # 0 首页
        self.stack.addWidget(self.batch_page)          # 1
        self.stack.addWidget(self.config_page)         # 2

        self.menu.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.menu.setCurrentRow(0)

        # 分隔线
        line = QFrame()
        line.setFrameShape(QFrame.VLine)
        line.setStyleSheet("color:#bdc3c7;max-width:1px;")

        layout.addWidget(self.menu)
        layout.addWidget(line)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:
        self.batch_page.shutdown()
        try:
            get_settings().save()
        except Exception:
            pass
        super().closeEvent(event)


def run_app() -> int:
    app = QApplication(sys.argv)
    w = MainWindow(); w.show()
    return app.exec_()
