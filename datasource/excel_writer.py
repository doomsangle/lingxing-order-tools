"""
Excel 输出：把不匹配的订单行写入新 Excel（复制原始数据 + 末列加"说明"）。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Sequence

from openpyxl import Workbook, load_workbook

from datasource.excel_reader import ExcelOrderRow


def write_mismatch_excel(
    source_path: str | Path,
    mismatched: list[tuple[ExcelOrderRow, str]],   # (原始行, 说明)
    output_path: str | Path | None = None,
) -> str:
    """
    把不匹配的行写入新 Excel：复制原始表头 + 原始行 + 末列"说明"。

    :param source_path:  原始 Excel 路径（用于复制表头和格式）
    :param mismatched:   不匹配列表 [(ExcelOrderRow, 说明文字), ...]
    :param output_path:  输出路径；None 则默认 source 同目录
    :returns: 实际保存路径
    """
    source_path = Path(source_path)
    if output_path is None or not str(output_path).strip():
        stem = source_path.stem
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = source_path.parent / f"{stem}_不匹配_{ts}.xlsx"
    else:
        output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 读原始表头
    wb_src = load_workbook(source_path, read_only=True, data_only=True)
    sheet_src = wb_src.worksheets[0]
    header = [cell for cell in next(sheet_src.iter_rows(min_row=1, max_row=1, values_only=True))]
    wb_src.close()

    # 写新 Excel
    wb = Workbook()
    ws = wb.active
    ws.title = "不匹配订单"

    # 表头 + "说明" 列
    ws.append(list(header) + ["说明"])

    # 数据行
    for row_data, reason in mismatched:
        raw = list(row_data.raw_row)
        # 补齐到表头长度
        while len(raw) < len(header):
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
