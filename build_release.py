"""
一键发版脚本：递增版本号 → PyInstaller 打包 → 生成分发目录 → 压缩 zip。

用法（在项目根目录执行）：
    python build_release.py                # 自动递增次版本号，如 1.0 → 1.1
    python build_release.py --set 2.0      # 指定版本号

产物：
    Order Search Sys-V{版本}/LingxingOrderTool.exe
    Order Search Sys-V{版本}/config/config.json     （干净模板，无任何领星凭证）
    Order Search Sys-V{版本}/使用说明.md
    Order Search Sys-V{版本}.zip
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION_FILE = ROOT / "version.py"
SPEC_FILE = ROOT / "lingxing_order_tool.spec"
EXE_PATH = ROOT / "dist" / "LingxingOrderTool.exe"
README_PATH = ROOT / "使用说明.md"
PREFIX = "Order Search Sys-V"


def read_version() -> str:
    text = VERSION_FILE.read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not m:
        raise SystemExit("version.py 中未找到 __version__")
    return m.group(1)


def write_version(version: str) -> None:
    text = VERSION_FILE.read_text(encoding="utf-8")
    text = re.sub(r'__version__\s*=\s*"[^"]+"', f'__version__ = "{version}"', text)
    VERSION_FILE.write_text(text, encoding="utf-8")


def bump(version: str) -> str:
    """次版本号 +1：1.0 → 1.1；主版本号不变。"""
    parts = version.split(".")
    major = int(parts[0])
    minor = int(parts[1]) if len(parts) > 1 else 0
    return f"{major}.{minor + 1}"


def clean_config() -> dict:
    """生成干净配置模板：不含任何领星原生凭证，只保留默认运行参数。"""
    return {
        "mode": "api",
        "lingxing_batch_size": 100,
        "lingxing_test_url": "https://amzvideohelper.top/api/erp/lingxing/test",
        "lingxing_order_url": "https://amzvideohelper.top/api/erp/lingxing/order/list",
        "lingxing_api_key": "avk_ecc3094da949cbc71bef8ca502d48068",
        "cdp_port": 9222,
        "chrome_executable": "",
        "chrome_user_data_dir": "",
        "lingxing_order_page_url": "https://erp.lingxing.com/erp/orderList",
        "api_base_url": "",
        "api_token": "",
        "api_update_path": "/orders/status",
        "request_timeout_sec": 30.0,
        "batch_delay_sec": 0.5,
        "last_excel_path": "",
        "output_excel_path": "",
    }


def build_exe() -> None:
    print("[1/4] PyInstaller 打包中 ...")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(SPEC_FILE), "--clean", "--noconfirm"],
        cwd=ROOT,
        check=True,
    )
    if not EXE_PATH.is_file():
        raise SystemExit(f"打包失败：未生成 {EXE_PATH}")


def make_release_folder(version: str) -> Path:
    print(f"[2/4] 生成分发目录 Order Search Sys-V{version}")
    release_dir = ROOT / f"{PREFIX}{version}"
    if release_dir.exists():
        shutil.rmtree(release_dir)
    (release_dir / "config").mkdir(parents=True, exist_ok=True)

    shutil.copy2(EXE_PATH, release_dir / "LingxingOrderTool.exe")
    if README_PATH.is_file():
        shutil.copy2(README_PATH, release_dir / "使用说明.md")
    (release_dir / "config" / "config.json").write_text(
        json.dumps(clean_config(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return release_dir


def make_zip(release_dir: Path) -> Path:
    print(f"[3/4] 压缩 {release_dir.name}.zip")
    zip_path = ROOT / f"{release_dir.name}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in release_dir.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(ROOT))
    return zip_path


def main() -> None:
    args = sys.argv[1:]
    current = read_version()
    if "--set" in args:
        idx = args.index("--set")
        try:
            version = args[idx + 1]
        except IndexError:
            raise SystemExit("--set 后需要跟版本号，如 --set 2.0")
        if not re.fullmatch(r"\d+\.\d+", version):
            raise SystemExit("版本号格式应为 x.y，如 2.0")
    else:
        version = bump(current)

    print(f"版本号：{current} → {version}")
    write_version(version)

    build_exe()
    release_dir = make_release_folder(version)
    zip_path = make_zip(release_dir)

    size_mb = zip_path.stat().st_size / 1024 / 1024
    print("[4/4] 完成")
    print(f"  目录：{release_dir}")
    print(f"  压缩包：{zip_path}  ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
