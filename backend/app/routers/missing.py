"""缺件补传专用接口：缺件清单核收、补传工单分派/认领/回执。

这些接口承载通用动作接口无法表达的结构化参数（确认号、分段列表），
业务判断仍全部放在领域服务内，路由层只做参数转发。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.schemas import ActionResult, ClaimPayload, ReceiptPayload
from app.services.data_transfer import DataTransferService
from app.services.missing_item import MissingItemService

router = APIRouter(prefix="/api", tags=["缺件补传"])


def _result(entry, message: str) -> ActionResult:
    if entry is None:
        raise HTTPException(status_code=400, detail=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/missing_item/{item_id}/receipts", response_model=ActionResult)
def register_missing_receipt(item_id: int, payload: ReceiptPayload) -> ActionResult:
    """凭确认回执核收缺件分段；同一回执重复提交只记一次。"""
    entry, message = MissingItemService().register_receipt(
        item_id, payload.ack_no, payload.segments, payload.source_transfer,
    )
    if entry is None:
        raise HTTPException(status_code=400, detail=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/missing_item/{item_id}/dispatch", response_model=ActionResult)
def dispatch_resend(item_id: int) -> ActionResult:
    """分派补传：生成工单、停止原尝试，工单注明可接续进度与需传分段。"""
    entry, message = MissingItemService().dispatch_resend(item_id)
    if entry is None:
        raise HTTPException(status_code=400, detail=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/resend_order/{order_id}/claim", response_model=ActionResult)
def claim_resend_order(order_id: int, payload: ClaimPayload) -> ActionResult:
    """补传员认领工单。"""
    entry, message = DataTransferService().claim_order(order_id, payload.claimer)
    return _result(entry, message)


@router.post("/resend_order/{order_id}/receipts", response_model=ActionResult)
def attach_order_receipt(order_id: int, payload: ReceiptPayload) -> ActionResult:
    """补传工单取得确认回执后核收缺件分段。"""
    entry, message = DataTransferService().attach_order_receipt(
        order_id, payload.ack_no, payload.segments,
    )
    if entry is None:
        raise HTTPException(status_code=400, detail=message)
    return ActionResult(ok=True, message=message, entry=entry)
