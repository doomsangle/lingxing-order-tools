"""
应用配置持久化模块。

配置存放位置（便携式，跟 exe 走）：
  打包 exe 运行时 → exe 同级目录下 ``config\\config.json``
  源码运行时     → 项目根目录下 ``config\\config.json``
  目录不可写时   → 回退 ``%APPDATA%\\LingxingOrderTool\\``

老版本配置（%APPDATA%）首次运行会自动迁移到新位置。

两种查询模式（通过 mode 字段切换）：
  - "api"        → 直接 HTTP POST 领星 ERP OrderList 接口（快、无需浏览器）
  - "browser"    → Playwright 驱动已登录的 Chrome，模拟 DOM 操作（需要先手动登录领星）
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


APP_NAME = "LingxingOrderTool"


def _detect_base_dir() -> Path:
    """exe 所在目录（打包）或项目根目录（源码运行）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _legacy_config_dir() -> Path:
    """老版本配置目录（%APPDATA%\\LingxingOrderTool）。"""
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / APP_NAME


def _config_dir() -> Path:
    """
    便携式配置目录：exe/项目根下 config\\。
    目录创建或写入测试失败（如 exe 放在只读位置）时回退 %APPDATA%。
    """
    candidate = _detect_base_dir() / "config"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".write_test"
        probe.touch()
        probe.unlink()
        return candidate
    except OSError:
        fallback = _legacy_config_dir()
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


CONFIG_PATH: Path = _config_dir() / "config.json"
_LEGACY_CONFIG_PATH = _legacy_config_dir() / "config.json"


def _migrate_legacy_config() -> None:
    """一次性迁移：老版本 %APPDATA% 配置 → 新位置（仅当新位置还没有配置时）。"""
    if not CONFIG_PATH.exists() and _LEGACY_CONFIG_PATH.exists():
        try:
            shutil.copy2(_LEGACY_CONFIG_PATH, CONFIG_PATH)
        except OSError:
            pass  # 迁移失败不影响使用（会生成默认配置）


# ----------------------------------------------------------------------
@dataclass
class AppSettings:
    """应用运行时配置。"""

    # ---- 模式 ----
    mode: str = "api"                     # "api" | "browser"

    # ========== 模式 1：直接 API ==========
    lingxing_api_url: str = "https://gw.lingxingerp.com/amz-order/api/order/OrderList"
    lingxing_auth_token: str = ""          # auth-token（会过期，需手动更新）
    lingxing_company_id: str = ""          # X-AK-Company-Id
    lingxing_env_key: str = "SAAS-12"      # X-AK-ENV-KEY
    lingxing_uid: str = ""                 # X-AK-Uid
    lingxing_zid: str = ""                 # X-AK-Zid
    lingxing_version: str = "3.9.3.3.0.040"
    lingxing_batch_size: int = 100
    lingxing_test_url: str = "https://amzvideohelper.top/api/erp/lingxing/test"
    lingxing_order_url: str = "https://amzvideohelper.top/api/erp/lingxing/order/list"

    # ========== 模式 2：浏览器自动化 ==========
    cdp_port: int = 9222
    chrome_executable: str = ""
    chrome_user_data_dir: str = ""         # 空则自动用 %APPDATA%\LingxingOrderTool\chrome_profile
    lingxing_order_page_url: str = "https://erp.lingxing.com/erp/orderList"

    # ========== 用户订单状态回写 API（两种模式共用） ==========
    api_base_url: str = ""
    api_token: str = ""
    api_update_path: str = "/orders/status"

    # ========== 运行参数 ==========
    request_timeout_sec: float = 30.0
    batch_delay_sec: float = 0.5

    # ========== 最近使用 ==========
    last_excel_path: str = ""
    output_excel_path: str = ""             # 不匹配订单输出路径（空=源Excel同目录）

    # ------------------------------------------------------------------
    @classmethod
    def load(cls) -> "AppSettings":
        _migrate_legacy_config()  # 老配置自动搬到新位置
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                known = {k: raw[k] for k in cls.__dataclass_fields__ if k in raw}
                s = cls(**known)
                # 旧版本默认批量 20，残留配置会覆盖新默认值 → 迁移到 100
                if raw.get("lingxing_batch_size") == 20:
                    s.lingxing_batch_size = 100
                # 旧请求超时 15 秒 → 迁移到 30 秒
                if raw.get("request_timeout_sec") == 15.0:
                    s.request_timeout_sec = 30.0
                # lingxing_order_url 之前被误写为浏览器页面 URL 或 amzrover → 迁移到 amzvideohelper
                old = raw.get("lingxing_order_url", "")
                if "erp/orderList" in old or old.startswith("https://erp.lingxing.com") or "amzrover" in old:
                    if "erp/orderList" in old or old.startswith("https://erp.lingxing.com"):
                        # 旧 config 里这个键存的是浏览器订单页 URL，迁移到新字段
                        s.lingxing_order_page_url = old
                    s.lingxing_order_url = cls.lingxing_order_url
                # lingxing_test_url 同理
                old_test = raw.get("lingxing_test_url", "")
                if old_test and ("amzrover" in old_test or "amzvideohelper" not in old_test):
                    s.lingxing_test_url = cls.lingxing_test_url
                return s
            except (json.JSONDecodeError, TypeError):
                backup = CONFIG_PATH.with_suffix(".json.bak")
                CONFIG_PATH.rename(backup)
                return cls()
        s = cls()
        # 默认 Chrome 用户目录
        s.chrome_user_data_dir = str(_config_dir() / "chrome_profile")
        return s

    def save(self) -> None:
        if not self.chrome_user_data_dir:
            self.chrome_user_data_dir = str(_config_dir() / "chrome_profile")
        CONFIG_PATH.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    def chrome_executable_resolved(self) -> str:
        if self.chrome_executable and os.path.isfile(self.chrome_executable):
            return self.chrome_executable
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            str(Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe"),
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
        return ""


_instance: AppSettings | None = None


def get_settings() -> AppSettings:
    global _instance
    if _instance is None:
        _instance = AppSettings.load()
    return _instance


def reload_settings() -> AppSettings:
    global _instance
    _instance = AppSettings.load()
    return _instance
