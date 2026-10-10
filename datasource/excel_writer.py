"""
Excel 输出：把不匹配的订单行写入新 Excel（复制原始数据 + 末列加"说明"）。

对源文件容错：xlsx/xls/csv 均可读表头；读不到表头时用"列1..列N"占位。
保存失败（目录不可写等）由调用方兜底换目录。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Sequence

from openpyxl import Workbook, load_workbook

from datasource.excel_reader import ExcelOrderRow


def _read_header(path: Path) -> list | None:
    """尽量读取源文件第一行表头；xlsx/xls/csv 均尝试，失败返回 None。"""
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        try:
            wb = load_workbook(path, read_only=True, data_only=True)
            try:
                sheet = wb.worksheets[0]
                row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), None)
                return list(row) if row is not None else None
            finally:
                wb.close()
        except Exception:
            return None
    if suffix == ".xls":
        try:
            import xlrd
            sheet = xlrd.open_workbook(str(path)).sheet_by_index(0)
            if sheet.nrows > 0:
                return sheet.row_values(0)
        except Exception:
            return None
        return None
    if suffix == ".csv":
        try:
            import csv as _csv
            for enc in ("utf-8-sig", "gbk", "utf-8"):
                try:
                    with open(path, "r", encoding=enc, errors="replace", newline="") as f:
                        row = next(_csv.reader(f), None)
                        return list(row) if row is not None else None
                except UnicodeDecodeError:
                    continue
        except Exception:
            return None
    return None


def write_mismatch_excel(
    source_path: str | Path,
    mismatched: list[tuple[ExcelOrderRow, str]],   # (原始行, 说明)
    output_path: str | Path | None = None,
) -> str:
    """
    把不匹配的行写入新 Excel：复制原始表头 + 原始行 + 末列"说明"。

    :param source_path:  原始 Excel 路径（用于复制表头和格式；读不到表头则用占位列名）
    :param mismatched:   不匹配列表 [(ExcelOrderRow, 说明文字), ...]
    :param output_path:  输出路径；None 则默认 source 同目录
    :returns: 实际保存路径
    """
    source_path = Path(source_path)
    if output_path is None or not str(output_path).strip():
        stem = source_path.stem if source_path.name else "订单"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = (source_path.parent if source_path.parent.exists()
                       else Path.cwd()) / f"{stem}_不匹配_{ts}.xlsx"
    else:
        output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 读原始表头（容错：csv/xls/锁定文件读不到时用占位表头）
    header = _read_header(source_path) if source_path.is_file() else None

    wb = Workbook()
    ws = wb.active
    ws.title = "不匹配订单"

    if header:
        ws.append(list(header) + ["说明"])
    else:
        # 以最长原始行推定列数，生成占位表头
        max_len = max((len(r.raw_row) for r, _ in mismatched), default=1)
        ws.append([f"列{i+1}" for i in range(max_len)] + ["说明"])

    # 数据行
    ncols = ws.max_column - 1  # 表头列数（不含"说明"列）
    for row_data, reason in mismatched:
        raw = list(row_data.raw_row)
        while len(raw) < ncols:
            raw.append(None)
        ws.append(raw + [reason])

    # 列宽自适应（简单版）
    for col in ws.columns:
        max_len = 0
        for cell in col:
            try:
                v = str(cell.value or "")
                if len(v) > max_len:
                    max_len = len(v)
            except Exception:
                pass
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 50)

    wb.save(output_path)
    return str(output_path)
