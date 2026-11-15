"""
خدمة إدارة التحويلات وحركات الاستلام (Transfer Service)
فصل دورة حياة الشحنات والفروقات المخزنية والتأكيد والاعتماد.
"""
from datetime import datetime
from models import db, TransferOrder, record_stock_movement
import sync_queue as sq


def confirm_transfer_receipt(transfer, received_data, confirmed_by_id):
    """
    تأكيد استلام شحنة تحويل في الفرع، وتحديث كميات الاستلام والفروقات.
    received_data: قاموس بمطابقة {line_id: {'qty_received': float, 'variance_reason': str}}
    يعيد: (has_variance: bool, message: str)
    """
    if transfer.status in ["received", "approved"]:
        return False, "تم استلام هذا السند مسبقاً ولا يمكن تكرار العملية"

    has_variance = False

    for line in transfer.lines:
        line_info = received_data.get(line.id)
        if line_info and line_info.get("qty_received") is not None:
            line.qty_received = float(line_info["qty_received"])
            if line.qty_received != line.qty_sent:
                has_variance = True
                line.variance_reason = line_info.get("variance_reason") or "فرق في الاستلام"

    transfer.confirmed_by = confirmed_by_id
    transfer.confirmed_at = datetime.utcnow()

    if has_variance:
        transfer.status = "needs_review"
        db.session.commit()
        return True, "تم تسجيل الاستلام، ويوجد فرق في الكميات بانتظار اعتماد المحاسب"

    # في حال المطابقة التامة: تسجيل حركات المخزون للوجهة
    for line in transfer.lines:
        record_stock_movement(
            item_id=line.item_id,
            location_id=transfer.to_location_id,
            movement_type="transfer_in",
            quantity_change=line.qty_received or line.qty_sent,
            reference_type="transfer",
            reference_id=transfer.id,
            notes=f"تحويل وارد من {transfer.from_location.name_ar if transfer.from_location else ''}",
            user_id=confirmed_by_id,
        )

    transfer.status = "received"
    db.session.commit()
    sq.enqueue("transfer", transfer.id)
    return False, "تم تأكيد الاستلام بنجاح ومطابقة الكميات بالكامل"


def approve_transfer_with_variance(transfer, approver_id):
    """
    اعتماد الفروقات المخزنية للتحويل بواسطة المحاسب وترحيل الكميات الفعلية لطابور فوديكس.
    يعيد: (success: bool, message: str)
    """
    if transfer.status != "needs_review":
        return False, "هذا السند غير متاح للاعتماد أو تم اعتماده مسبقاً"

    for line in transfer.lines:
        qty_in = line.qty_received if line.qty_received is not None else line.qty_sent
        record_stock_movement(
            item_id=line.item_id,
            location_id=transfer.to_location_id,
            movement_type="transfer_in",
            quantity_change=qty_in,
            reference_type="transfer",
            reference_id=transfer.id,
            notes=f"تحويل وارد (معتمد مع فروقات: {line.variance_qty or 0}) من {transfer.from_location.name_ar if transfer.from_location else ''}",
            user_id=approver_id,
        )

    transfer.status = "approved"
    db.session.commit()
    sq.enqueue("transfer", transfer.id)
    return True, "تم اعتماد التحويل مع الفروقات وإدراجه في طابور الترحيل"

