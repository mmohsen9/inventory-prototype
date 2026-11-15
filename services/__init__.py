"""
طبقة الخدمات المركزية (Service Layer)
لفصل منطق الأعمال وقواعد المخزون والعمليات التشغيلية عن متحكمات الـ HTTP
مع الحفاظ الكامل على توافق المسارات وواجهات الـ Routes.
"""
from services.inventory_service import (
    adjust_stock_quantity,
    import_stock_from_csv,
    get_stock_balance,
)
from services.transfer_service import (
    confirm_transfer_receipt,
    approve_transfer_with_variance,
)

__all__ = [
    "adjust_stock_quantity",
    "import_stock_from_csv",
    "get_stock_balance",
    "confirm_transfer_receipt",
    "approve_transfer_with_variance",
]

