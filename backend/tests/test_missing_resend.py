# -*- coding: utf-8 -*-
"""缺件清单与补传核收链路测试。

覆盖业务规则：
1. 同名测量文件按航次+资料类别核对，他航次先到件不抵消本航次缺项；
2. 补传请求关联缺件清单项目，工单注明可接续进度、需传分段与原尝试停止方式；
3. 同一回执重复提交只记一次核收，同一缺件最多结清一次；
4. 部分确认必须列出已收齐分段，全段实际核收前报告仍有缺件；
5. 报告提交时检查仍未核收项目；
6. 另起传输后原尝试被停止，不能再消耗连接或改变核收；
7. 题设端到端场景：A 温盐包 P1 已收/P2 无确认、B 未提交，向 A 认领补传并重发 P1 原回执。
"""
from __future__ import annotations

import pytest

from app.store import store


@pytest.fixture(autouse=True)
def reset_store():
    store.reset()


def _missing_svc():
    from app.services.missing_item import MissingItemService
    return MissingItemService()


def _transfer_svc():
    from app.services.data_transfer import DataTransferService
    return DataTransferService()


def _item(item_id: int):
    return store.find("missing_item", item_id)


def _order(order_id: int):
    return store.find("resend_order", order_id)


# ============================================================
# 1. 自然键核对：他航次同名件不能抵消本航次缺项
# ============================================================
def test_same_named_file_in_other_cruise_does_not_offset():
    svc = _missing_svc()
    # 本航次 A 队温盐包仍缺 P2
    item_a = svc.find_item("CRUI-2026-S1", "水文测量", "温盐包", "A队")
    assert item_a is not None
    assert svc.outstanding_segments(item_a) == ["P2"]

    # 上一航次同名温盐包已结清
    item_old = svc.find_item("CRUI-2025-Y2", "水文测量", "温盐包", "A队")
    assert item_old["status"] == "已结清"

    # 本航次缺件不被他航次结清件抵消
    assert item_a["status"] != "已结清"
    outstanding = [
        row for row in svc.checklist_for_report(1, cruise="CRUI-2026-S1")
        if row["未核收分段"]
    ]
    names = {(row["成果名称"], row["提交方"]) for row in outstanding}
    assert ("温盐包", "A队") in names
    assert ("温盐包", "B队") in names


def test_different_submitter_same_artifact_is_separate_item():
    svc = _missing_svc()
    item_b = svc.find_item("CRUI-2026-S1", "水文测量", "温盐包", "B队")
    assert item_b["已核收分段"] == []
    assert set(svc.outstanding_segments(item_b)) == {"P1", "P2"}


# ============================================================
# 2. 题设端到端：向 A 分派补传、认领、重发 P1 原回执、P2 补传
# ============================================================
def test_a_resend_order_indicates_only_p2_and_stops_original_attempt():
    svc = _missing_svc()
    transfer = _transfer_svc()

    # 原尝试 101 仍在传输中、占着连接
    original = store.find("data_transfer", 101)
    assert original["status"] == "传输中"

    # 向 A 认领补传：分派工单（迟到的部分确认此时已在种子里：P1 已核收）
    item, msg = svc.dispatch_resend(1)
    assert item is not None, msg
    order_id = item["关联工单"][0]
    order = _order(order_id)

    # 值班员实际要传哪些段：只剩 P2
    assert order["需传分段"] == ["P2"]
    assert order["应传分段"] == ["P1", "P2"]
    # 可接续原进度（P1 已核收）
    assert order["接续原进度"] is True
    assert order["已核收分段"] == ["P1"]
    # 原尝试已停止，连接释放
    assert store.find("data_transfer", 101)["status"] == "已停止"
    assert "停止" in order["停止原尝试说明"]

    # 补传员认领工单后应看到同样的接续信息
    claimed, claim_msg = transfer.claim_order(order_id, "补传员小赵")
    assert claimed is not None, claim_msg
    assert claimed["认领人"] == "补传员小赵"
    assert "P2" in claim_msg

    # 原尝试停止后不能再确认完成，不能改变核收
    again, stop_msg = transfer.run_action(101, "确认完成")
    assert again is None and "停止" in stop_msg
    again, stop_msg = transfer.run_action(101, "停止尝试")
    assert again is None and "停止" in stop_msg


def test_resubmitting_original_p1_receipt_is_idempotent():
    svc = _missing_svc()
    transfer = _transfer_svc()
    item, _ = svc.dispatch_resend(1)
    order_id = item["关联工单"][0]

    # A 重发 P1 时带着原回执 ACK-P1-001（工单先被认领）
    transfer.claim_order(order_id, "补传员小赵")
    order, msg = transfer.attach_order_receipt(order_id, "ACK-P1-001", ["P1"])
    assert order is not None
    # 重复回执不产生新核收：分段状态不变、确认号不重复登记
    assert order["已核收分段"] == ["P1"]
    assert order["回执确认号"] == []  # 重复回执未写入工单确认号
    assert _item(1)["核收次数"] == 1
    assert _item(1)["回执明细"] and _item(1)["回执明细"][0]["确认号"] == "ACK-P1-001"
    assert "重复" in msg

    # 直接走缺件清单核收同样幂等
    again, again_msg = svc.register_receipt(1, "ACK-P1-001", ["P1"])
    assert again is not None and "重复" in again_msg
    assert _item(1)["核收次数"] == 1

    # 缺件仍未结清，工单仍在进行中（P2 未到）
    assert _item(1)["status"] == "补传中"
    assert _order(order_id)["status"] == "已认领"


def test_p2_receipt_clears_item_once_and_closes_order():
    svc = _missing_svc()
    transfer = _transfer_svc()
    item, _ = svc.dispatch_resend(1)
    order_id = item["关联工单"][0]

    # P2 新回执到达
    order, msg = transfer.attach_order_receipt(order_id, "ACK-P2-002", ["P2"])
    assert order["status"] == "已核收"
    item = _item(1)
    assert item["status"] == "已结清"
    assert item["已核收分段"] == ["P1", "P2"]
    assert item["核收次数"] == 2
    assert item["结清时间"] is not None

    # 结清后再提交任何回执都不再改变核收
    later, later_msg = svc.register_receipt(1, "ACK-P2-002", ["P2"])
    assert later is not None
    assert "重复" in later_msg or "已结清" in later_msg
    assert _item(1)["核收次数"] == 2

    # 已结清的缺件不能再分派补传
    none_entry, dispatch_msg = svc.dispatch_resend(1)
    assert none_entry is None and ("结清" in dispatch_msg or "全部核收" in dispatch_msg)


# ============================================================
# 3. 部分确认必须列出已收齐分段，全段核收前报告仍有缺件
# ============================================================
def test_partial_receipt_lists_received_segments_and_keeps_report_blocked():
    compile_svc = _results_compile_svc()
    svc = _missing_svc()

    # 初始：报告 1 有未核收缺件，提交被拒绝
    entry, msg = compile_svc.run_action(1, "提交汇编")
    assert entry is None
    assert "缺件" in msg

    # B 队先核收 P1（部分确认）
    item, rmsg = svc.register_receipt(2, "ACK-B-P1-101", ["P1"])
    assert item["status"] == "部分核收"
    assert item["已核收分段"] == ["P1"]
    assert svc.outstanding_segments(item) == ["P2"]

    # 报告仍有缺件，仍不能提交
    entry, msg = compile_svc.run_action(1, "提交汇编")
    assert entry is None
    outstanding = compile_svc.outstanding_missing_items(1)
    b_row = next(row for row in outstanding if row["提交方"] == "B队")
    assert b_row["未核收分段"] == ["P2"]
    a_row = next(row for row in outstanding if row["提交方"] == "A队")
    assert a_row["未核收分段"] == ["P2"]


def test_report_can_submit_only_after_all_segments_accepted():
    compile_svc = _results_compile_svc()
    svc = _missing_svc()
    transfer = _transfer_svc()

    # 结清 A 的 P2
    item, _ = svc.dispatch_resend(1)
    transfer.attach_order_receipt(item["关联工单"][0], "ACK-P2-002", ["P2"])
    # 结清 B 的 P1、P2
    svc.register_receipt(2, "ACK-B-P1-101", ["P1"])
    svc.register_receipt(2, "ACK-B-P2-102", ["P2"])

    assert svc.outstanding_segments(_item(1)) == []
    assert svc.outstanding_segments(_item(2)) == []

    report = store.find("results_compile", 1)
    report["status"] = "汇编中"
    entry, msg = compile_svc.run_action(1, "提交汇编")
    assert entry is not None, msg
    assert entry["status"] == "已提交"


def test_receipt_rejects_unknown_segment():
    svc = _missing_svc()
    entry, msg = svc.register_receipt(1, "ACK-X-999", ["P9"])
    assert entry is None and "应到分段" in msg


# ============================================================
# 4. 迟到的部分确认与补传分派同到
# ============================================================
def test_late_partial_confirmation_arrives_with_dispatch():
    svc = _missing_svc()
    transfer = _transfer_svc()

    # 构造新缺件：两段都未核收
    entry, _ = svc.create_entry({
        "航次编号": "CRUI-2026-S1", "资料类别": "水文测量",
        "成果名称": "溶解氧包", "提交方": "C队",
        "应到分段": ["D1", "D2"], "汇编报告": 1,
    })
    new_id = entry["id"]

    # 迟到的部分确认（D1）与补传分派同到：先落回执，再分派
    item, rmsg = svc.register_receipt(new_id, "ACK-D1-LATE", ["D1"])
    assert item["status"] == "部分核收"
    item, dmsg = svc.dispatch_resend(new_id)
    assert item is not None, dmsg
    order = _order(item["关联工单"][0])

    # 工单按最新进度生成：可接续、只需传 D2
    assert order["接续原进度"] is True
    assert order["已核收分段"] == ["D1"]
    assert order["需传分段"] == ["D2"]


# ============================================================
# 5. 同一缺件只允许一个进行中工单；停止的工单不再收回执
# ============================================================
def test_single_active_order_per_item_and_stopped_order_rejects_receipt():
    svc = _missing_svc()
    transfer = _transfer_svc()

    item, _ = svc.dispatch_resend(2)
    first_order = item["关联工单"][0]

    # 重复分派被拒绝
    again, msg = svc.dispatch_resend(2)
    assert again is None and "进行中" in msg

    # 停止工单后不再接受回执
    stopped, stop_msg = transfer.stop_order(first_order)
    assert stopped["status"] == "已停止"
    none_entry, receipt_msg = transfer.attach_order_receipt(first_order, "ACK-B-P1-101", ["P1"])
    assert none_entry is None and "停止" in receipt_msg

    # 旧工单停止后允许重新分派，新工单不受旧尝试影响
    item, dmsg = svc.dispatch_resend(2)
    assert item is not None, dmsg
    new_order = _order(item["关联工单"][-1])
    assert new_order["id"] != first_order
    assert new_order["status"] == "待认领"


def test_order_without_received_segments_requires_full_resend():
    svc = _missing_svc()
    # B 队没有任何已核收分段：工单应要求整体重发、不可接续
    item, _ = svc.dispatch_resend(2)
    order = _order(item["关联工单"][0])
    assert order["接续原进度"] is False
    assert order["需传分段"] == ["P1", "P2"]
    assert order["已核收分段"] == []


# ============================================================
# 6. 题设核对量：A 提交时的确认号与 B 的缺件数
# ============================================================
def test_scenario_assertions_ack_numbers_and_b_missing_count():
    svc = _missing_svc()
    transfer = _transfer_svc()

    # A 的确认号：P1 原回执 + P2 新回执
    item, _ = svc.dispatch_resend(1)
    order_id = item["关联工单"][0]
    # 重发 P1 原回执（幂等，不计入工单确认号）
    transfer.claim_order(order_id, "补传员小赵")
    dup_order, dup_msg = transfer.attach_order_receipt(order_id, "ACK-P1-001", ["P1"])
    assert "重复" in dup_msg
    assert dup_order["回执确认号"] == []
    # P2 补传确认
    order, _ = transfer.attach_order_receipt(order_id, "ACK-P2-002", ["P2"])

    a_item = _item(1)
    ack_numbers = [detail["确认号"] for detail in a_item["回执明细"]]
    assert ack_numbers == ["ACK-P1-001", "ACK-P2-002"]
    assert order["回执确认号"] == ["ACK-P2-002"]  # 仅新回执计入工单

    # B 尚未提交同名文件：应到 2 段、已核收 0 段，缺件数按分段计为 2
    b_item = _item(2)
    b_missing = len(svc.outstanding_segments(b_item))
    assert b_missing == 2
    assert b_item["核收次数"] == 0


def _results_compile_svc():
    from app.services.results_compile import ResultsCompileService
    return ResultsCompileService()
