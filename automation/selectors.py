"""
领星 ERP 订单列表页 DOM 选择器。

使用策略：
- 每个元素提供 **候选 selector 列表**，按命中概率从高到低排序
- 运行时依次尝试，**第一个成功的即锁定**，并在日志中打印
- 锁定后的 selector 可以手动回写到本文件，加速后续命中

页面 URL: https://erp.lingxing.com/erp/orderList
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SelectorSet:
    """一组候选选择器，供 BrowserController 尝试。"""
    candidates: list[str] = field(default_factory=list)
    locked: str = ""  # 运行时命中后填入，优先级最高

    def all(self) -> list[str]:
        if self.locked:
            return [self.locked] + self.candidates
        return list(self.candidates)


# ----------------------------------------------------------------------
# 领星 ERP（ant-design 风格）订单列表页候选选择器
# ----------------------------------------------------------------------
LINGXING_SELECTORS = {
    # 搜索区域的"订单号"输入框
    "order_id_input": SelectorSet(candidates=[
        # ---- 通过 label / placeholder / name 匹配（最稳）----
        'input[placeholder*="订单号"]',
        'input[placeholder*="order"]',
        'input#orderId',
        'input[name="orderId"]',
        'input[name="order_id"]',
        'input[name="orderNumber"]',
        'input[placeholder*="ASIN"]',   # 有时同一搜索栏会混排，但我们要的是订单号
        # ---- 通过 ant-design 结构 ----
        '.ant-form-item:has(.ant-form-item-label:has-text("订单号")) input.ant-input',
        '.ant-form-item:has(.ant-form-item-label:has-text("订单号")) input',
        '.ant-input-affix-wrapper input',
        # ---- 回退：顶部 form 下第一个文本 input ----
        '.ant-form input.ant-input[type="text"]',
        '.ant-card-head + .ant-form input.ant-input',
        '.ant-layout-content .ant-form input.ant-input',
    ]),

    # 搜索按钮
    "search_button": SelectorSet(candidates=[
        'button:has-text("搜索")',
        'button.ant-btn:has-text("搜索")',
        'button.ant-btn-primary:has-text("搜索")',
        'button:has-text("查询")',
        'button.ant-btn-primary',
        '.ant-form button.ant-btn-primary',
    ]),

    # 结果表格 body（用于判断是否有数据）
    "table_body": SelectorSet(candidates=[
        '.ant-table-body',
        '.ant-table-container .ant-table-body',
        '.ant-table-tbody',
        'table.ant-table',
        '#orderTable',
        '#orderListTable',
    ]),

    # "暂无数据"空状态标记
    "empty_state": SelectorSet(candidates=[
        '.ant-empty',
        '.ant-empty-description:has-text("暂无数据")',
        '.ant-empty-description',
        'img[src*="empty"]',
    ]),

    # 表格中的行（在已知订单号时用更精确的行定位）
    "table_row": SelectorSet(candidates=[
        '.ant-table-tbody > tr.ant-table-row',
        'tbody > tr',
    ]),

    # 表格里"订单状态"列的表头（用于动态确定列索引，比硬编码 nth-child 稳）
    "status_header": SelectorSet(candidates=[
        'th:has-text("订单状态")',
        'th:has-text("状态")',
        '.ant-table-thead th:has-text("状态")',
        '.ant-table-thead th:has-text("订单状态")',
    ]),
}
