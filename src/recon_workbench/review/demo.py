"""``--stub`` 演示用的确定性结论。

只引用请求里已有的事实，不编造金额或发票；用于无密钥跑通流程与 CI。
真实审核能力取决于实际模型，与这里的演示无关。
"""

from __future__ import annotations

import json
from decimal import Decimal

from .schema import ReviewRequest

SMALL_DELTA = Decimal("300")


def demo_verdict(request: ReviewRequest) -> str:
    delta = Decimal(request.delta) if request.delta else None
    payload: dict[str, object] = {
        "verdict": "needs_review",
        "category": "other",
        "confidence": 0.5,
        "explanation": "仅凭当前对账事实无法判断差异原因。",
        "recommended_action": "请人工核对合同、送货单与发票。",
        "evidence": [f"状态 {request.status}"],
    }

    if request.status == "amount_mismatch" and delta is not None and abs(delta) <= SMALL_DELTA:
        payload.update(
            {
                "verdict": "approve",
                "category": "amount_diff",
                "confidence": 0.91,
                "explanation": (
                    f"台账 {request.ledger_amount} 与对账单 {request.statement_amount} "
                    f"相差 {request.delta}，差异金额很小，可能为运费或四舍五入尾差。"
                ),
                "recommended_action": "按对账单金额确认差异，并要求供应商补充差异说明。",
                "evidence": [
                    f"台账金额 {request.ledger_amount}",
                    f"对账单金额 {request.statement_amount}",
                    f"差额 {request.delta}",
                ],
            }
        )
    elif request.status == "only_in_ledger":
        payload.update(
            {
                "verdict": "needs_review",
                "category": "missing_in_statement",
                "confidence": 0.58,
                "explanation": (
                    f"台账记录了 {request.invoice_no}，但供应商对账单中没有这张票，可能对方漏记或本期未结算。"
                ),
                "recommended_action": "向供应商确认该发票是否已开具、是否计入本期对账单。",
                "evidence": [f"发票号 {request.invoice_no}", f"台账金额 {request.ledger_amount}"],
            }
        )
    elif request.status == "only_in_statement":
        payload.update(
            {
                "verdict": "needs_review",
                "category": "missing_in_ledger",
                "confidence": 0.55,
                "explanation": (f"对账单出现了 {request.invoice_no}，但内部台账没有对应记录，可能未入账或重复开票。"),
                "recommended_action": "核对采购入库单与发票原件，确认是否补入台账。",
                "evidence": [f"发票号 {request.invoice_no}", f"对账单金额 {request.statement_amount}"],
            }
        )
    elif request.status == "duplicate":
        payload.update(
            {
                "verdict": "needs_review",
                "category": "duplicate",
                "confidence": 0.52,
                "explanation": "同一发票号在来源文件中出现多次，需要确认是否为重复录入。",
                "recommended_action": "人工确认后删除重复行，并检查导入流程。",
                "evidence": [f"发票号 {request.invoice_no}"],
            }
        )

    return json.dumps(payload, ensure_ascii=False)
