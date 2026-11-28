"""数据传输领域服务：完成传输后自动联动成果汇编更新数据完整度。
支持事务快照回滚、重试机制、原尝试停止以及面向缺件清单的补传工单。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DataTransferService(BaseModuleService):
    MODULE = "data_transfer"
    ENTITY = "传输记录"
    OPERATION_TERM = "发起传输"
    LIST_FIELDS = ["数据内容", "数据量", "传输方式", "目标系统", "传输状态", "操作人", "传输时间", "备注说明"]
    REQUIRED_FIELDS = ["数据内容", "数据量", "传输方式"]
    STATUS_ORDER = ["待传输", "传输中", "已完成", "已失败", "已停止"]
    ACTION_RULES = {
        "发起传输": "传输中",
        "确认完成": "已完成",
        "重试传输": "待传输",
        "停止尝试": "已停止",
    }
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        # 已停止的原尝试不能再消耗连接、不能再确认完成或改变核收
        if entry.get("status") == "已停止":
            return None, f"{self.ENTITY} {entry_id} 的原尝试已停止，不能再执行{action}"

        # 幂等检查
        idempotent_key = f"{self.MODULE}:{entry_id}:{action}"
        if store.is_idempotent(idempotent_key):
            return None, f"动作「{action}」已执行，请勿重复操作"

        # 重试传输：只能从失败状态重试
        if action == "重试传输" and entry.get("status") != "已失败":
            return None, "只能重试已失败的传输"

        # 停止尝试：只允许在连接仍占用（待传输/传输中）时停止
        if action == "停止尝试" and entry.get("status") not in ("待传输", "传输中"):
            return None, "只能停止仍在进行中的传输尝试"

        # 保存快照
        tx_id = f"{self.MODULE}:{entry_id}:{action}:tx"
        store.save_snapshot(tx_id, self.MODULE, entry_id)

        entry["status"] = target
        if action == "停止尝试":
            entry["pending"] = False
            entry["attempt_stopped"] = True
            entry["stopped_time"] = _now()
        else:
            entry["pending"] = target in ("待传输", "传输中")
        entry["abnormal"] = False

        if action == "重试传输":
            entry["retry_count"] = entry.get("retry_count", 0) + 1
            entry["last_retry_time"] = str(Any)

        store.mark_idempotent(idempotent_key)

        # 传输完成后联动成果汇编更新数据完整度
        if action == "确认完成":
            self._update_results_completeness(entry)

        return entry, f"{self.ENTITY}已{action}"

    def stop_attempt(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """停止原传输尝试：另起补传传输前必须释放原连接。"""
        return self.run_action(entry_id, "停止尝试")

    def _update_results_completeness(self, entry: dict[str, Any]) -> None:
        """数据传输完成后更新成果汇编中对应的数据完整度。"""
        data_content = entry.get("数据内容", "")
        results_rows = store.rows("results_compile")
        updated_count = 0
        for row in results_rows:
            if row.get("status") in ("汇编中", "已提交", "需补充"):
                current = row.get("数据完整度", "")
                if data_content not in str(current):
                    row["数据完整度"] = f"{current} | 已接收：{data_content}"
                    row["pending"] = True
                    row["transfer_complete"] = True
                    row["last_transfer_time"] = entry.get("传输时间", "")
                    updated_count += 1
        # 记录传输对成果汇编的影响范围
        entry["compile_updates"] = updated_count

    # ================================================================ 补传工单
    def create_resend_order(
        self, missing_item: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """依据缺件清单项目创建补传工单，并停止该资料的原传输尝试。

        工单必须让补传员知道：
        - 能否接续原进度（已有分段核收时可接续，未核收任何分段时须整体重发）；
        - 实际需要重发哪些分段（缺件项当前仍未核收的分段）；
        - 原尝试如何停止（原传输记录被置为已停止，释放连接）。
        """
        item_id = int(missing_item["id"])
        orders = store.rows("resend_order")

        # 同一缺件同时只允许一个进行中的工单，避免多套尝试并行消耗连接
        active = [
            order for order in orders
            if order.get("缺件项") == item_id and order.get("status") in ("待认领", "已认领", "部分核收")
        ]
        if active:
            return None, f"缺件项 {item_id} 已有进行中的补传工单{active[0]['id']}，请先停止或核收"

        from app.services.missing_item import MissingItemService

        missing_svc = MissingItemService()
        received = list(missing_item.get("已核收分段") or [])
        needed = missing_svc.outstanding_segments(missing_item)
        if not needed:
            return None, f"缺件项 {item_id} 全部分段已核收，无需补传"

        original_id = missing_item.get("原传输记录")
        original_status = None
        if original_id is not None:
            original = store.find(self.MODULE, int(original_id))
            if original is not None:
                original_status = original.get("status")
                # 另起传输时停止原尝试，原尝试不能继续消耗连接或改变核收
                if original_status in ("待传输", "传输中"):
                    stopped, stop_msg = self.stop_attempt(int(original_id))
                    if stopped is None:
                        return None, f"原传输尝试无法停止：{stop_msg}"
                    original_status = stopped.get("status")

        order = {
            "id": max((int(row.get("id", 0)) for row in orders), default=0) + 1,
            "缺件项": item_id,
            "航次编号": missing_item.get("航次编号"),
            "资料类别": missing_item.get("资料类别"),
            "成果名称": missing_item.get("成果名称"),
            "提交方": missing_item.get("提交方"),
            "应传分段": list(missing_item.get("应到分段") or []),
            "已核收分段": received,
            "需传分段": needed,
            "接续原进度": bool(received),
            "原传输记录": original_id,
            "原尝试状态": original_status,
            "停止原尝试说明": (
                f"原传输记录{original_id}已停止，连接已释放" if original_id is not None
                else "无关联原传输记录"
            ),
            "回执确认号": [],
            "认领人": None,
            "认领时间": None,
            "分派时间": _now(),
            "核收时间": None,
            "备注说明": "",
            "status": "待认领",
            "pending": True,
            "abnormal": False,
        }
        orders.append(order)
        return order, f"补传工单{order['id']}已创建"

    def claim_order(self, order_id: int, claimer: str = "补传员") -> tuple[dict[str, Any] | None, str]:
        """补传员认领工单，确认可接续的进度与需重发的分段。"""
        order = store.find("resend_order", order_id)
        if order is None:
            return None, f"补传工单 {order_id} 不存在"
        if order.get("status") not in ("待认领",):
            return None, f"补传工单 {order_id} 已被认领或已结束，不能重复认领"
        order["status"] = "已认领"
        order["认领人"] = claimer
        order["认领时间"] = _now()
        return order, (
            f"工单{order_id}已认领：{'可接续原进度，已核收' if order.get('接续原进度') else '无可接续进度，整体重发'}"
            f"{'、'.join(order.get('已核收分段') or []) or '无'}；需传分段：{'、'.join(order.get('需传分段') or [])}"
        )

    def stop_order(self, order_id: int) -> tuple[dict[str, Any] | None, str]:
        """停止补传工单：停止后不再接受回执核收。"""
        order = store.find("resend_order", order_id)
        if order is None:
            return None, f"补传工单 {order_id} 不存在"
        if order.get("status") in ("已核收", "已停止"):
            return None, f"补传工单 {order_id} 已{order.get('status')}，不能停止"
        order["status"] = "已停止"
        order["pending"] = False
        order["stopped_time"] = _now()
        return order, f"补传工单{order_id}已停止"

    def attach_order_receipt(
        self, order_id: int, ack_no: str, segments: list[str],
    ) -> tuple[dict[str, Any] | None, str]:
        """补传工单取得确认回执后，由成果汇编侧凭回执核收缺件分段。

        同一确认号在同一工单内只记一次，缺件项的结清幂等由缺件清单服务保证。
        """
        order = store.find("resend_order", order_id)
        if order is None:
            return None, f"补传工单 {order_id} 不存在"
        if order.get("status") == "已停止":
            return None, f"补传工单 {order_id} 已停止，不再接受回执"
        ack_no = str(ack_no or "").strip()
        if not ack_no:
            return None, "确认回执号不能为空"
        if ack_no in (order.get("回执确认号") or []):
            return order, f"回执{ack_no}重复提交，工单与缺件均只记一次"

        from app.services.missing_item import MissingItemService

        missing_svc = MissingItemService()
        missing_row = store.find("missing_item", int(order["缺件项"]))
        known_acks = {detail.get("确认号") for detail in (missing_row or {}).get("回执明细", [])}
        if ack_no in known_acks:
            # 回执已在缺件项核收过（例如重发时携带原回执）：只提示，不重复登记
            return order, f"回执{ack_no}重复提交，仅记一次核收，缺件不重复结清"

        updated_item, item_msg = missing_svc.register_receipt(
            int(order["缺件项"]), ack_no, segments,
            source_transfer=order.get("原传输记录"),
        )
        if updated_item is None:
            return None, item_msg

        order.setdefault("回执确认号", []).append(ack_no)
        order["已核收分段"] = list(updated_item.get("已核收分段") or [])
        order["需传分段"] = missing_svc.outstanding_segments(updated_item)
        if not order["需传分段"]:
            order["status"] = "已核收"
            order["pending"] = False
            order["核收时间"] = _now()
        else:
            order["status"] = "部分核收"
            order["pending"] = True
        return order, item_msg


class ResendOrderService(BaseModuleService):
    """补传工单模块：让通用资源路由可以查询/导出工单，动作由数据传输服务承载。"""

    MODULE = "resend_order"
    ENTITY = "补传工单"
    LIST_FIELDS = [
        "航次编号", "资料类别", "成果名称", "提交方", "需传分段",
        "已核收分段", "接续原进度", "原尝试状态", "认领人",
    ]
    REQUIRED_FIELDS = ["航次编号", "资料类别", "成果名称", "提交方"]
    STATUS_ORDER = ["待认领", "已认领", "部分核收", "已核收", "已停止"]
    ACTION_RULES = {"认领工单": "已认领", "停止工单": "已停止"}

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "认领工单":
            return DataTransferService().claim_order(entry_id)
        if action == "停止工单":
            return DataTransferService().stop_order(entry_id)
        return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
