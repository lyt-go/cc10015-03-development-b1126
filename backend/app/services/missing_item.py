"""缺件清单领域服务。

岸端汇编报告核对时，按「航次编号 + 资料类别 + 成果名称 + 提交方」登记缺件项：
- 同名测量文件只在同一航次、同一资料类别内核对，其他航次先到的同名件不能抵消本航次缺项；
- 支持分段（尺号）核收，部分核收必须列明已收齐分段，全部应到分段实际核收后才结清；
- 同一确认回执重复提交只记一次核收，同一缺件最多结清一次；
- 分派补传会在数据传输服务生成补传工单，工单与缺件项双向关联。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class MissingItemService(BaseModuleService):
    MODULE = "missing_item"
    ENTITY = "缺件项"
    OPERATION_TERM = "登记缺件"
    LIST_FIELDS = [
        "航次编号", "资料类别", "成果名称", "提交方", "应到分段",
        "已核收分段", "汇编报告", "备注说明",
    ]
    REQUIRED_FIELDS = ["航次编号", "资料类别", "成果名称", "提交方"]
    STATUS_ORDER = ["待分派", "补传中", "部分核收", "已结清"]
    ACTION_RULES = {"分派补传": "补传中"}
    NEGATIVE_ACTIONS: list[str] = []

    # ------------------------------------------------------------------ 登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing

        rows = store.rows(self.MODULE)

        def _segments(value: Any) -> list[str]:
            if value is None:
                return []
            if isinstance(value, str):
                # 通用登记弹窗提交的是逗号/顿号分隔文本
                return [part.strip() for part in re.split(r"[,，、]", value) if part.strip()]
            return [str(part).strip() for part in value if str(part).strip()]

        entry: dict[str, Any] = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "航次编号": values.get("航次编号"),
            "资料类别": values.get("资料类别"),
            "成果名称": values.get("成果名称"),
            "提交方": values.get("提交方"),
            "应到分段": _segments(values.get("应到分段")),
            "已核收分段": _segments(values.get("已核收分段")),
            "汇编报告": values.get("汇编报告"),
            "原传输记录": values.get("原传输记录"),
            "备注说明": values.get("备注说明", ""),
            "回执明细": [],
            "关联工单": [],
            "核收次数": 0,
            "结清时间": None,
            "status": "待分派",
            "pending": True,
            "abnormal": False,
        }
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "分派补传":
            return self.dispatch_resend(entry_id)
        return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

    # ------------------------------------------------------------------ 查询
    def find_item(
        self, cruise: str, category: str, artifact: str, submitter: str,
    ) -> dict[str, Any] | None:
        """按自然键查找缺件项：航次 + 资料类别 + 成果名称 + 提交方。"""
        for row in store.rows(self.MODULE):
            if (
                row.get("航次编号") == cruise
                and row.get("资料类别") == category
                and row.get("成果名称") == artifact
                and row.get("提交方") == submitter
            ):
                return row
        return None

    def outstanding_segments(self, item: dict[str, Any]) -> list[str]:
        """仍未核收的分段，按应到分段顺序返回。"""
        received = set(item.get("已核收分段") or [])
        return [seg for seg in (item.get("应到分段") or []) if seg not in received]

    def checklist_for_report(self, report_id: int, cruise: str | None = None) -> list[dict[str, Any]]:
        """报告视角的缺件清单，含每项仍缺的分段。"""
        result: list[dict[str, Any]] = []
        for item in store.rows(self.MODULE):
            linked = item.get("汇编报告") == report_id or (
                cruise is not None and item.get("航次编号") == cruise
            )
            if not linked:
                continue
            result.append({
                "缺件项": item["id"],
                "航次编号": item.get("航次编号"),
                "资料类别": item.get("资料类别"),
                "成果名称": item.get("成果名称"),
                "提交方": item.get("提交方"),
                "应到分段": list(item.get("应到分段") or []),
                "已核收分段": list(item.get("已核收分段") or []),
                "未核收分段": self.outstanding_segments(item),
                "status": item.get("status"),
            })
        return result

    # ------------------------------------------------------------------ 核收
    def register_receipt(
        self,
        item_id: int,
        ack_no: str,
        segments: list[str],
        source_transfer: int | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """凭确认回执核收分段。

        幂等规则：
        - 同一确认号重复提交不产生任何核收效果，回执只记一次；
        - 缺件项结清后不再接受新的核收效果，同一缺件最多结清一次。
        """
        item = store.find(self.MODULE, item_id)
        if item is None:
            return None, f"{self.ENTITY} {item_id} 不存在或已归档"
        ack_no = str(ack_no or "").strip()
        if not ack_no:
            return None, "确认回执号不能为空"
        segments = [str(seg).strip() for seg in segments if str(seg).strip()]
        if not segments:
            return None, "回执至少要包含一个核收分段"

        # 同一缺件最多核收一次：已结清后任何回执都不再改变核收状态
        if item.get("status") == "已结清":
            return item, f"缺件项 {item_id} 已结清，回执{ack_no}不再重复核收"

        # 同一回执重复提交只记一次核收
        known_acks = {detail.get("确认号") for detail in item.get("回执明细", [])}
        idempotent_key = f"{self.MODULE}:{item_id}:receipt:{ack_no}"
        if ack_no in known_acks or store.is_idempotent(idempotent_key):
            return item, f"回执{ack_no}重复提交，仅记一次核收，缺件不重复结清"

        unknown = [seg for seg in segments if seg not in (item.get("应到分段") or [])]
        if unknown:
            return None, f"分段{'、'.join(unknown)}不在应到分段范围内，不能核收"

        received: list[str] = list(item.get("已核收分段") or [])
        for seg in item.get("应到分段") or []:
            if seg in segments and seg not in received:
                received.append(seg)
        item["已核收分段"] = received

        item.setdefault("回执明细", []).append({
            "确认号": ack_no,
            "分段": list(segments),
            "来源传输": source_transfer,
            "核收时间": datetime.now().isoformat(timespec="seconds"),
        })
        item["核收次数"] = int(item.get("核收次数", 0)) + 1
        store.mark_idempotent(idempotent_key)

        if not self.outstanding_segments(item):
            item["status"] = "已结清"
            item["pending"] = False
            item["结清时间"] = datetime.now().isoformat(timespec="seconds")
        else:
            # 部分确认：必须能列出已经收齐的分段
            item["status"] = "部分核收"
            item["pending"] = True
        return item, f"回执{ack_no}已核收分段{'、'.join(segments)}"

    # ------------------------------------------------------------------ 分派
    def dispatch_resend(self, item_id: int) -> tuple[dict[str, Any] | None, str]:
        """分派补传：由数据传输服务生成补传工单并关联本缺件项。"""
        item = store.find(self.MODULE, item_id)
        if item is None:
            return None, f"{self.ENTITY} {item_id} 不存在或已归档"
        if item.get("status") == "已结清":
            return None, f"缺件项 {item_id} 已全部核收，无需分派补传"

        # 迟到的部分确认可能与分派同到：工单按当前已核收进度计算需传分段，
        # 因此先落库回执再分派（回执由 register_receipt 单独调用后，这里读到的就是最新进度）。
        from app.services.data_transfer import DataTransferService

        # 分派前重新核对：迟到回执可能已把缺件结清，结清项不再产生工单
        if not self.outstanding_segments(item):
            item["status"] = "已结清"
            item["pending"] = False
            if not item.get("结清时间"):
                item["结清时间"] = datetime.now().isoformat(timespec="seconds")
            return None, f"缺件项 {item_id} 已全部核收，无需分派补传"

        order, order_msg = DataTransferService().create_resend_order(item)
        if order is None:
            return None, order_msg

        item.setdefault("关联工单", []).append(order["id"])
        if item.get("status") != "已结清":
            item["status"] = "补传中"
            item["pending"] = True
        return item, f"已分派补传工单{order['id']}，实际需传分段：{'、'.join(order.get('需传分段') or []) or '无'}"
