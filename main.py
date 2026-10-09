"""
领星 ERP 订单批量查询工具 —— 入口。

直接运行:
    python main.py

打包:
    pyinstaller lingxing_order_tool.spec
    # 或一键:
    build.bat

打包产物:
    dist/LingxingOrderTool.exe
"""
from __future__ import annotations

import sys
import traceback


def _install_excepthook() -> None:
    """把未捕获异常打到 GUI 上，避免静默崩溃。"""
    import ctypes
    from PyQt5.QtWidgets import QApplication, QMessageBox

    def hook(exc_type, exc, tb):
        app = QApplication.instance()
        msg = "".join(traceback.format_exception(exc_type, exc, tb))
        if app is not None:
            QMessageBox.critical(None, "程序崩溃", msg)
        ctypes.windll.user32.MessageBoxW(None, msg, "程序崩溃", 0x10)

    sys.excepthook = hook


def main() -> int:
    _install_excepthook()
    from gui.main_window import run_app
    return run_app()


if __name__ == "__main__":
    sys.exit(main())
