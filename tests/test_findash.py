"""
tests/test_findash.py — فحوصات الداشبورد التفاعلي المالي والمزامنة الشهرية لـ Xero و Foodics
يغطي:
  1) إنشاء الجداول والتحقق من المخطط (ensure_findash_schema)
  2) استيراد وتحليل ملفات P&L والمركز المالي ومبيعات الفروع
  3) احتساب المؤشرات المالية (KPIs) ونسب السيولة ورأس المال العامل
  4) مسارات الداشبورد وقائمة الدخل ومقارنة الفروع والمزامنة
  5) محرك كشف قيود التسوية والتحديث الذكي لـ Xero (Reconciliation & Diff Detection)
  6) مزامنة مبيعات فوديكس الشهرية وتجميع الفروع
"""
import json
import pytest
from unittest.mock import patch, MagicMock

from models import db, User
from models_findash import (
    ensure_findash_schema,
    FinAccount,
    FinMonthlyPnl,
    FinBalanceFigure,
    FinBranchSales,
    FinSyncSnapshot,
)
from services.findash_service import (
    get_dashboard_summary,
    get_pnl_statement_table,
    get_sync_matrix,
    sync_month_from_xero,
    sync_month_from_foodics,
    import_all_historical_data,
    _clean_num,
)


def test_clean_num_helper():
    """اختبار تنظيف وتحويل الأرقام المحاسبية والأقواس السالبة."""
    assert _clean_num("1,234.56") == 1234.56
    assert _clean_num("(500.00)") == -500.00
    assert _clean_num("-") == 0.0
    assert _clean_num(None) == 0.0
    assert _clean_num(100) == 100.0


def test_findash_schema_and_models(db_session):
    """التحقق من إنشاء جداول الداشبورد المالي بنجاح."""
    ensure_findash_schema()

    pnl = FinMonthlyPnl(
        year=2026,
        month=1,
        account_code="200",
        account_name="المبيعات",
        category="Trading Income",
        amount=150000.0,
        branch="Total"
    )
    db_session.add(pnl)

    bal = FinBalanceFigure(
        year=2026,
        month=1,
        current_assets=200000.0,
        inventory=50000.0,
        current_liabilities=100000.0,
        total_assets=500000.0,
        total_liabilities=100000.0,
        total_equity=400000.0
    )
    db_session.add(bal)
    db_session.commit()

    assert bal.current_ratio == 2.0
    assert bal.quick_ratio == 1.5
    assert bal.working_capital == 100000.0


def test_dashboard_kpis_and_summary(db_session):
    """التحقق من صحة احتساب إجمالي الإيرادات وتكلفة المبيعات وصافي الربح."""
    # إضافة إيرادات وتكاليف ومصروفات
    db_session.add_all([
        FinMonthlyPnl(year=2026, month=1, account_code="200", account_name="مبيعات", category="Trading Income", amount=100000.0, branch="Total"),
        FinMonthlyPnl(year=2026, month=1, account_code="310", account_name="تكلفة مبيعات", category="Cost of Sales", amount=40000.0, branch="Total"),
        FinMonthlyPnl(year=2026, month=1, account_code="400", account_name="رواتب", category="Operating Expenses", amount=25000.0, branch="Total"),
        FinMonthlyPnl(year=2026, month=1, account_code="410", account_name="إيجار", category="Operating Expenses", amount=15000.0, branch="Total"),
    ])
    db_session.commit()

    data = get_dashboard_summary(year=2026, month=1)
    kpis = data["kpis"]

    assert kpis["total_revenue"] == 100000.0
    assert kpis["total_cogs"] == 40000.0
    assert kpis["gross_profit"] == 60000.0
    assert kpis["gross_margin"] == 60.0
    assert kpis["total_expenses"] == 40000.0
    assert kpis["net_profit"] == 20000.0
    assert kpis["net_margin"] == 20.0


def test_pnl_statement_table_generation(db_session):
    """التحقق من تبويب جدول قائمة الدخل الرسمي."""
    db_session.add_all([
        FinMonthlyPnl(year=2026, month=2, account_code="200", account_name="مبيعات فوديكس", category="Trading Income", amount=50000.0, branch="Total"),
        FinMonthlyPnl(year=2026, month=2, account_code="310", account_name="تكلفة حليب وبن", category="Cost of Sales", amount=15000.0, branch="Total"),
    ])
    db_session.commit()

    pnl_table = get_pnl_statement_table(year=2026, month=2, branch="Total")
    assert pnl_table["summary"]["total_revenue"] == 50000.0
    assert pnl_table["summary"]["gross_profit"] == 35000.0
    assert "Trading Income" in pnl_table["sections"]


def test_findash_routes_access(client, test_data):
    """التحقق من حماية المسارات برمز الدخول وسلاسة فتح صفحات الداشبورد."""
    # 1. بدون تسجيل دخول -> إعادة توجيه إلى /login
    resp = client.get("/finance/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]

    # 2. تسجيل دخول كمدير
    with client.session_transaction() as sess:
        sess["user_id"] = 1  # admin user

    resp = client.get("/finance/")
    assert resp.status_code == 200
    assert "لوحة التحكم المالية" in resp.data.decode("utf-8")

    resp_pnl = client.get("/finance/pnl")
    assert resp_pnl.status_code == 200

    resp_branches = client.get("/finance/branches")
    assert resp_branches.status_code == 200

    resp_bal = client.get("/finance/balance-sheet")
    assert resp_bal.status_code == 200

    resp_sync = client.get("/finance/sync")
    assert resp_sync.status_code == 200


@patch("services.findash_service._get_xero_auth_headers")
@patch("requests.get")
def test_xero_reconciliation_and_diff_detection(mock_requests_get, mock_xero_auth, db_session):
    """
    التحقق من كشف قيود التسوية والتحديث الذكي:
    - المزامنة الأولى تسجل الأرقام كـ synced.
    - المزامنة الثانية تكتشف فرق في أحد الحسابات وتسجله كـ reconciled_with_diff مع تفاصيل الفرق.
    """
    mock_xero_auth.return_value = (True, {"Authorization": "Bearer fake_token", "Xero-Tenant-Id": "fake_id"}, None)

    # 1. المزامنة الأولى: مبيعات 100,000 ريال
    mock_report_1 = {
        "Reports": [{
            "ReportTitles": ["Profit and Loss", "Alkoob Alawal", "Jan 2026"],
            "Rows": [
                {
                    "RowType": "Section",
                    "Title": "Income",
                    "Rows": [{
                        "RowType": "Row",
                        "Cells": [{"Value": "200 - Sales"}, {"Value": "100000.00"}]
                    }]
                }
            ]
        }]
    }

    mock_resp_1 = MagicMock()
    mock_resp_1.status_code = 200
    mock_resp_1.json.return_value = mock_report_1
    mock_requests_get.return_value = mock_resp_1

    res_1 = sync_month_from_xero(year=2026, month=1)
    assert res_1["success"] is True
    assert res_1["diff_detected"] is False
    assert res_1["status"] == "synced"

    # 2. المزامنة الثانية (تم تسجيل قيد تسوية لاحق في زيرو وأصبحت المبيعات 115,000 ريال)
    mock_report_2 = {
        "Reports": [{
            "ReportTitles": ["Profit and Loss", "Alkoob Alawal", "Jan 2026"],
            "Rows": [
                {
                    "RowType": "Section",
                    "Title": "Income",
                    "Rows": [{
                        "RowType": "Row",
                        "Cells": [{"Value": "200 - Sales"}, {"Value": "115000.00"}]
                    }]
                }
            ]
        }]
    }
    mock_resp_2 = MagicMock()
    mock_resp_2.status_code = 200
    mock_resp_2.json.return_value = mock_report_2
    mock_requests_get.return_value = mock_resp_2

    res_2 = sync_month_from_xero(year=2026, month=1)
    assert res_2["success"] is True
    assert res_2["diff_detected"] is True
    assert res_2["status"] == "reconciled_with_diff"
    assert len(res_2["diff_summary"]) == 1
    assert res_2["diff_summary"][0]["delta"] == 15000.0

    # التحقق من تحديث السجل في قاعدة البيانات
    rec = FinMonthlyPnl.query.filter_by(year=2026, month=1, account_code="200").first()
    assert rec.amount == 115000.0


@patch("services.findash_service._get_foodics_token")
@patch("requests.get")
def test_foodics_monthly_sales_sync(mock_requests_get, mock_token, db_session):
    """التحقق من مزامنة مبيعات الفروع لشهر محدد من Foodics API."""
    mock_token.return_value = "fake_foodics_token"

    mock_days_payload = {
        "data": [
            {
                "id": "day_1",
                "business_date": "2026-03-01",
                "branch": {"id": "br_01", "name": "Alsahafa Branch", "reference": "B05"},
                "gross_sales": 20000.0,
                "net_sales": 18000.0,
                "taxes": 2700.0,
                "discount": 2000.0,
                "order_count": 600
            },
            {
                "id": "day_2",
                "business_date": "2026-03-02",
                "branch": {"id": "br_01", "name": "Alsahafa Branch", "reference": "B05"},
                "gross_sales": 15000.0,
                "net_sales": 14000.0,
                "taxes": 2100.0,
                "discount": 1000.0,
                "order_count": 400
            }
        ]
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_days_payload
    mock_requests_get.return_value = mock_resp

    res = sync_month_from_foodics(year=2026, month=3)
    assert res["success"] is True
    assert res["branches_synced"] == 1
    assert res["total_net_sales"] == 32000.0

    b_rec = FinBranchSales.query.filter_by(year=2026, month=3, branch_name="Alsahafa Branch").first()
    assert b_rec is not None
    assert b_rec.net_sales == 32000.0
    assert b_rec.orders_count == 1000
    assert b_rec.avg_order == 32.0


def test_inventory_stock_and_stagnant_report(db_session):
    """التحقق من حساب أرصدة المخزون وتقارير الأصناف الراكدة وبطيئة الحركة."""
    from models_findash import FinItemBalance, FinItemMovement
    from services.findash_service import get_inventory_current_stock, get_stagnant_items

    # إضافة أرصدة
    db_session.add_all([
        FinItemBalance(item_code="GB001", item_name="بن كولمبي أخضر", coffee_type="بن أخضر", qty=100.0, value=2500.0, year=2026, month=8),
        FinItemBalance(item_code="RB001", item_name="بن كولمبي محمص", coffee_type="بن محمص", qty=20.0, value=1200.0, year=2026, month=8),
    ])
    db_session.commit()

    stock = get_inventory_current_stock(show_all=False)
    assert stock["count"] == 2
    assert stock["total_qty"] == 120.0
    assert stock["total_value"] == 3700.0
    assert stock["totals"]["green"]["qty"] == 100.0
    assert stock["totals"]["roasted"]["qty"] == 20.0

    # فحص الراكد
    stag = get_stagnant_items(days_threshold=60)
    assert stag["threshold"] == 60


def test_inventory_periodic_and_aggregated_statement(db_session):
    """التحقق من حساب الرصيد الشهري/السنوي وبيان الصنف المجمّع."""
    from models_findash import FinItemMovement
    from services.findash_service import get_inventory_periodic_balance, get_aggregated_item_statement

    db_session.add_all([
        FinItemMovement(year=2026, month=1, item_code="GB001", item_name="بن أخضر", coffee_type="بن أخضر", qty=50.0, value=1000.0, movement_type="شراء/وارد"),
        FinItemMovement(year=2026, month=1, item_code="GB001", item_name="بن أخضر", coffee_type="بن أخضر", qty=-20.0, value=-400.0, movement_type="تحميص (خروج أخضر)"),
        FinItemMovement(year=2026, month=1, item_code="RB001", item_name="بن محمص", coffee_type="بن محمص", qty=17.0, value=600.0, movement_type="تحميص (تحويل محمص)"),
        FinItemMovement(year=2026, month=1, item_code="RB001", item_name="بن محمص", coffee_type="بن محمص", qty=-10.0, value=-350.0, movement_type="بيع"),
    ])
    db_session.commit()

    # رصيد شهري
    monthly = get_inventory_periodic_balance(year=2026, month=1)
    assert monthly["total_inward"] == 67.0
    assert monthly["total_outward"] == 30.0
    assert monthly["net_movement"] == 37.0

    # بيان مجمع
    statement = get_aggregated_item_statement(year=2026, month=1)
    assert statement["active_items_count"] == 2
    assert statement["total_roast_in"] == 17.0
    assert statement["total_sales"] == -10.0


def test_budget_forecast_and_analysis_routes(client, test_data, db_session):
    """التحقق من مسارات المخزون والموازنة والتحليل المالي."""
    with client.session_transaction() as sess:
        sess["user_id"] = 1

    # مسار المخزون - التبويبات الأربعة
    resp_inv = client.get("/finance/inventory")
    assert resp_inv.status_code == 200
    assert "الرصيد الحالي" in resp_inv.data.decode("utf-8")

    resp_inv_mon = client.get("/finance/inventory?subtab=monthly&year=2026&month=1")
    assert resp_inv_mon.status_code == 200
    assert "رصيد شهري" in resp_inv_mon.data.decode("utf-8")

    resp_inv_yr = client.get("/finance/inventory?subtab=yearly&year=2026")
    assert resp_inv_yr.status_code == 200
    assert "رصيد سنوي" in resp_inv_yr.data.decode("utf-8")

    resp_inv_stmt = client.get("/finance/inventory?subtab=statement&year=2026")
    assert resp_inv_stmt.status_code == 200
    assert "بيان مجمع للصنف" in resp_inv_stmt.data.decode("utf-8") or "بيان الصنف المجمّع" in resp_inv_stmt.data.decode("utf-8")

    # مسار الموازنة
    resp_bgt = client.get("/finance/budget")
    assert resp_bgt.status_code == 200
    assert "الموازنة المستهدفة" in resp_bgt.data.decode("utf-8")

    # مسار التحليل المالي
    resp_anl = client.get("/finance/analysis")
    assert resp_anl.status_code == 200
    assert "النسب المالية" in resp_anl.data.decode("utf-8") or "التحليل" in resp_anl.data.decode("utf-8")


def test_branch_forecast_projection_calculation(db_session):
    """التحقق من حساب توقعات الفروع بدقة ونسبة المساهمة ودون حدوث خطأ KeyError: 0."""
    from services.findash_service import get_advanced_forecast_projection

    # إضافة بيانات تاريخية شهرية لـ Total
    for m in range(1, 13):
        db_session.add(FinMonthlyPnl(year=2025, month=m, account_code="200", account_name="مبيعات", category="Trading Income", amount=100000.0, branch="Total"))
        db_session.add(FinMonthlyPnl(year=2026, month=m, account_code="200", account_name="مبيعات", category="Trading Income", amount=120000.0, branch="Total"))

    # إضافة بيانات الفروع
    db_session.add(FinMonthlyPnl(year=2026, month=0, account_code="200", account_name="مبيعات", category="Trading Income", amount=300000.0, branch="Branch Alpha"))
    db_session.add(FinMonthlyPnl(year=2026, month=0, account_code="200", account_name="مبيعات", category="Trading Income", amount=700000.0, branch="Branch Beta"))
    db_session.commit()

    # فحص التوقع الكلي
    fc_all = get_advanced_forecast_projection(months_ahead=6, category="Trading Income", branch="all")
    assert fc_all["months_count"] == 24
    assert fc_all["total_forecast"] > 0
    assert fc_all["branch_share"] == 1.0

    # فحص توقع الفرع Alpha
    fc_alpha = get_advanced_forecast_projection(months_ahead=6, category="Trading Income", branch="Branch Alpha")
    assert fc_alpha["months_count"] == 24
    assert fc_alpha["branch_share"] == 0.3  # 300,000 / 1,000,000 = 30%
    assert abs(fc_alpha["total_forecast"] - (fc_all["total_forecast"] * 0.3)) < 1.0

    # فحص توقع الفرع Beta
    fc_beta = get_advanced_forecast_projection(months_ahead=6, category="Trading Income", branch="Branch Beta")
    assert fc_beta["branch_share"] == 0.7  # 700,000 / 1,000,000 = 70%
    assert abs(fc_beta["total_forecast"] - (fc_all["total_forecast"] * 0.7)) < 1.0


def test_customer_branch_sales_and_route(client, test_data, db_session):
    """التحقق من تقرير ملخص وتفصيل مبيعات العملاء والفروع (سنوي، شهري، إجمالي عام)."""
    from models_findash import FinInvoiceSale
    from services.findash_service import get_customer_branch_sales

    # إضافة فواتير تجريبية
    db_session.add_all([
        FinInvoiceSale(year=2026, month=1, invoice="INV-001", contact="Customer A", branch="Hittin branch", amount=50000.0),
        FinInvoiceSale(year=2026, month=1, invoice="INV-002", contact="Customer A", branch="Alsahafa branch", amount=30000.0),
        FinInvoiceSale(year=2026, month=2, invoice="INV-003", contact="Customer B", branch="Hittin branch", amount=20000.0),
        FinInvoiceSale(year=2025, month=12, invoice="INV-004", contact="Customer C", branch="RAFA", amount=40000.0),
    ])
    db_session.commit()

    # 1. فحص سنوي 2026
    data_yr = get_customer_branch_sales(basis="yearly", year=2026)
    assert data_yr["kpis"]["total_sales"] == 100000.0
    assert data_yr["kpis"]["invoices_count"] == 3
    assert data_yr["kpis"]["customers_count"] == 2
    assert data_yr["kpis"]["branches_count"] == 2
    assert len(data_yr["by_customer"]) == 2
    assert data_yr["by_customer"][0]["contact"] == "Customer A"
    assert data_yr["by_customer"][0]["amount"] == 80000.0
    assert len(data_yr["by_customer_and_branch"]) == 3

    # 2. فحص شهري (2026 شهر 1)
    data_mon = get_customer_branch_sales(basis="monthly", year=2026, month=1)
    assert data_mon["kpis"]["total_sales"] == 80000.0
    assert data_mon["kpis"]["invoices_count"] == 2
    assert data_mon["kpis"]["customers_count"] == 1

    # 3. فحص الإجمالي العام (All-Time)
    data_all = get_customer_branch_sales(basis="all")
    assert data_all["kpis"]["total_sales"] >= 140000.0
    assert data_all["kpis"]["invoices_count"] >= 4

    # 4. فحص مسار العرض في الواجهة
    with client.session_transaction() as sess:
        sess["user_id"] = 1

    resp = client.get("/finance/sales?basis=yearly&year=2026")
    assert resp.status_code == 200
    assert "ملخص مبيعات العملاء والفروع" in resp.data.decode("utf-8")
    assert "تفصيل حسب العميل والفرع" in resp.data.decode("utf-8")


def test_inventory_tabs_and_calculations(client, test_data, db_session):
    """فحص حسابات المخزون والحركات وتبويباته الخمسة."""
    from models_findash import FinItemBalance, FinItemMovement
    from services.findash_service import (
        get_inventory_current_stock,
        get_stagnant_items,
        get_inventory_periodic_balance,
        get_aggregated_item_statement,
        get_inventory_movements_log,
    )

    # إضافة رصيد تجريبي
    db_session.add_all([
        FinItemBalance(year=2026, month=8, item_code="GB01", item_name="Green A", coffee_type="بن أخضر", qty=100.0, value=5000.0),
        FinItemBalance(year=2026, month=8, item_code="RB01", item_name="Roasted B", coffee_type="بن محمص", qty=50.0, value=3000.0),
        FinItemMovement(year=2026, month=8, mdate="2026-08-15", item_code="GB01", item_name="Green A", coffee_type="بن أخضر", movement_type="شراء", qty=100.0, value=5000.0),
        FinItemMovement(year=2026, month=8, mdate="2026-08-20", item_code="RB01", item_name="Roasted B", coffee_type="بن محمص", movement_type="بيع", qty=-10.0, value=-600.0),
    ])
    db_session.commit()

    # 1. الرصيد الحالي
    stock = get_inventory_current_stock()
    assert stock["total_qty"] >= 150.0
    assert stock["total_value"] >= 8000.0
    assert stock["totals"]["green"]["qty"] >= 100.0
    assert stock["totals"]["roasted"]["qty"] >= 50.0

    # 2. تقرير الأصناف الراكدة
    stag = get_stagnant_items(days_threshold=0)
    assert stag["count"] >= 2

    # 3. سجل الحركات
    log = get_inventory_movements_log(year=2026, month=8)
    assert log["total_matching"] >= 2
    assert len(log["available_types"]) >= 7

    # 4. فحص المسار والعرض
    with client.session_transaction() as sess:
        sess["user_id"] = 1

    r_curr = client.get("/finance/inventory?subtab=current")
    assert r_curr.status_code == 200
    assert "تجميع رصيد المخزون حسب نوع البن" in r_curr.data.decode("utf-8")

    r_mov = client.get("/finance/inventory?subtab=movements")
    assert r_mov.status_code == 200
    assert "سجل حركات المخزون التفصيلية" in r_mov.data.decode("utf-8")




def test_financial_ratios_exact_values_and_verdicts(db_session):
    """التحقق من احتساب النسب المالية الثمانية ومطابقتها التامة لأرقام الشاشة."""
    from models_findash import FinBalanceFigure, FinMonthlyPnl
    from services.findash_service import get_financial_analysis_data

    # إضافة ميزانية 2026 مطابقة للواقع
    fig = FinBalanceFigure(
        year=2026,
        month=8,
        current_assets=3064470.76,
        inventory=2443568.02,
        current_liabilities=3245682.22,
        total_assets=17973539.85,
        total_equity=14727857.63,
    )
    db_session.add(fig)

    # إضافة إيرادات وتكاليف ومصاريف لعام 2026
    db_session.add_all([
        FinMonthlyPnl(year=2026, month=1, account_code="200", account_name="مبيعات", category="Trading Income", amount=13295025.88, branch="Total"),
        FinMonthlyPnl(year=2026, month=1, account_code="310", account_name="تكلفة مبيعات", category="Cost of Sales", amount=7188065.47, branch="Total"),
        FinMonthlyPnl(year=2026, month=1, account_code="400", account_name="مصروفات", category="Operating Expenses", amount=2146573.87, branch="Total"),
    ])
    db_session.commit()

    data = get_financial_analysis_data(selected_year=2026)
    ratios = {r["name"]: r["value"] for r in data["ratios_rows"]}
    verdicts = {r["name"]: r["verdict"] for r in data["ratios_rows"]}

    assert ratios["النسبة الجارية (السيولة)"] == 0.94
    assert "انتبه" in verdicts["النسبة الجارية (السيولة)"]

    assert ratios["نسبة السيولة السريعة"] == 0.19
    assert "انتبه" in verdicts["نسبة السيولة السريعة"]

    assert ratios["معدل دوران المخزون (مرة)"] == 2.94
    assert "بطيء" in verdicts["معدل دوران المخزون (مرة)"]

    assert ratios["متوسط فترة بقاء المخزون (يوم)"] == 124.08
    assert "بطيء" in verdicts["متوسط فترة بقاء المخزون (يوم)"]

    assert 45.0 <= ratios["هامش مجمل الربح %"] <= 47.0
    assert "ممتاز" in verdicts["هامش مجمل الربح %"]

    assert 29.0 <= ratios["هامش صافي الربح %"] <= 31.0
    assert "ممتاز" in verdicts["هامش صافي الربح %"]

    assert ratios["العائد على الأصول % (ROA)"] == 22.03
    assert "ممتاز" in verdicts["العائد على الأصول % (ROA)"]

    assert ratios["العائد على حقوق الملكية % (ROE)"] == 26.89
    assert "ممتاز" in verdicts["العائد على حقوق الملكية % (ROE)"]

    assert round(data["total_ratios_sum"], 1) == 252.8


def test_channel_sales_and_foodics_deduction(db_session):
    """التحقق من تسجيل مبيعات التطبيقات وخصمها التلقائي من مبيعات فوديكس."""
    from models_findash import FinBranchSales, FinChannelSale
    from services.findash_service import get_channel_sales_data, get_dashboard_summary, upsert_channel_sale

    # 1. إضافة مبيعات فوديكس
    db_session.add(FinBranchSales(
        year=2026, month=5, branch_name="Hittin branch", net_sales=100000.0, net_profit=30000.0
    ))
    db_session.commit()

    # قبل إضافة التطبيقات
    dash_before = get_dashboard_summary(year=2026, month=5)
    assert dash_before["foodics_sales_total"] == 100000.0

    # 2. تسجيل مبيعات تطبيق جاهز
    upsert_channel_sale(year=2026, month=5, channel="جاهز", amount=25000.0, branch="Hittin branch", orders_count=120)

    # بعد إضافة التطبيقات: خصمها تلقائياً من فوديكس
    dash_after = get_dashboard_summary(year=2026, month=5)
    assert dash_after["foodics_sales_gross"] == 100000.0
    assert dash_after["channel_sales_deducted"] == 25000.0
    assert dash_after["foodics_sales_total"] == 75000.0

    # فحص شاشة التطبيقات
    ch_data = get_channel_sales_data(year=2026, month=5)
    assert ch_data["kpis"]["total_channels_sales"] == 25000.0
    assert ch_data["kpis"]["foodics_gross"] == 100000.0
    assert ch_data["kpis"]["foodics_net"] == 75000.0
    assert ch_data["kpis"]["total_orders"] == 120


def test_channel_sales_csv_import(db_session):
    """اختبار استيراد ملف CSV لمبيعات التطبيقات مع التعرف التلقائي على الأعمدة."""
    import io
    from services.findash_service import import_channel_sales_excel
    from models_findash import FinChannelSale

    csv_content = """التاريخ,الفرع,التطبيق,المبيعات,الطلبات,ملاحظة
2026-06-01,Alsahafa branch,هنقرستيشن,18500.50,95,تسوية أسبوعية
2026-06-01,Hittin branch,تويو,12300.00,60,مبيعات الويكند
"""
    f = io.BytesIO(csv_content.encode("utf-8-sig"))
    res = import_channel_sales_excel(f, "test_channels.csv")
    assert res["success"] is True
    assert res["imported"] == 2
    assert res["total_amount"] == 30800.50

    recs = FinChannelSale.query.filter_by(year=2026, month=6).all()
    assert len(recs) == 2


def test_channel_sales_idempotency_and_zero_channels_hiding(db_session):
    """
    التحقق من:
    1. عدم مضاعفة البيانات عند رفع نفس المستند مرتين (Idempotency).
    2. تطبيع مترادفات القنوات ('شيفز' -> 'ذا شيفز').
    3. إخفاء القنوات غير الموجودة أو التي إجمالي مبيعاتها 0 من العرض.
    """
    import io
    from services.findash_service import import_channel_sales_excel, get_channel_sales_data, normalize_channel_name
    from models_findash import FinChannelSale

    # 1. اختبار تطبيع الأسماء
    assert normalize_channel_name("شيفز") == "ذا شيفز"
    assert normalize_channel_name("The Chefz") == "ذا شيفز"
    assert normalize_channel_name("جاهز") == "جاهز"
    assert normalize_channel_name("نوفمبر order") == "تطبيق نوفمبر order"
    assert normalize_channel_name("الموقع الإلكتروني") == "الموقع الإلكتروني / سلة"

    # 2. ملف يحتوي على شيفز
    csv_content = """التاريخ,الفرع,التطبيق,المبيعات,الطلبات
2026-01-01,RAFA,شيفز,658.00,10
2026-02-01,RAFA,شيفز,455.00,8
"""
    f1 = io.BytesIO(csv_content.encode("utf-8-sig"))
    res1 = import_channel_sales_excel(f1, "duplicate_test.csv")
    assert res1["success"] is True
    assert res1["total_amount"] == 1113.00

    # رفع نفس المستند مرة ثانية تماماً
    f2 = io.BytesIO(csv_content.encode("utf-8-sig"))
    res2 = import_channel_sales_excel(f2, "duplicate_test.csv")
    assert res2["success"] is True
    assert res2["total_amount"] == 1113.00

    # التأكد من عدم تضاعف السجلات في قاعدة البيانات
    recs = FinChannelSale.query.filter_by(source_file="duplicate_test.csv").all()
    assert len(recs) == 2  # بقيت 2 وليست 4
    total_in_db = sum(r.amount for r in recs)
    assert round(total_in_db, 2) == 1113.00  # لم تتضاعف إلى 2226

    # 3. التأكد من إخفاء القنوات التي ليس لها مبيعات (مثل هنقرستيشن، تويو، سلة)
    ch_data = get_channel_sales_data(year=2026, branch="RAFA")
    assert "ذا شيفز" in ch_data["channels"]
    assert "شيفز" not in ch_data["channels"]  # تم تطبيعها إلى 'ذا شيفز'
    assert "هنقرستيشن" not in ch_data["channels"]  # مخفية لأن مبيعاتها 0
    assert "تويو" not in ch_data["channels"]  # مخفية لأن مبيعاتها 0
    assert "الموقع الإلكتروني / سلة" not in ch_data["channels"]  # مخفية لأن مبيعاتها 0


def test_analysis_and_channels_routes(client, test_data, db_session):
    """التحقق من عمل مسارات التحليل المالي ومبيعات التطبيقات والتعديل اليدوي."""
    with client.session_transaction() as sess:
        sess["user_id"] = 1

    # 1. التحليل المالي التبويب 3 (النسب المالية) لجميع الأعوام
    for yr in [2026, 2025, 2024]:
        r_ratios = client.get(f"/finance/analysis?subtab=ratios&year={yr}")
        assert r_ratios.status_code == 200
        html_r = r_ratios.data.decode("utf-8")
        assert "ما هي النسب المالية؟" in html_r
        assert "النسبة الجارية (السيولة)" in html_r
        assert "معدل دوران المخزون (مرة)" in html_r

    # التحقق من الرابط المحتوي على معاملات مدمجة
    r_malformed = client.get("/finance/analysis?subtab=ratios?year=2025&show_formula=0")
    assert r_malformed.status_code == 200
    assert "ما هي النسب المالية؟" in r_malformed.data.decode("utf-8")

    # 2. التحليل المالي التبويب 1 (مقارنة 12 شهر)
    r_mon = client.get("/finance/analysis?subtab=monthly&year_a=2024&year_b=2026")
    assert r_mon.status_code == 200
    assert "مقارنة 12 شهر" in r_mon.data.decode("utf-8")

    # 3. شاشة مبيعات التطبيقات
    r_ch = client.get("/finance/channels?year=2026")
    assert r_ch.status_code == 200
    assert "مبيعات التطبيقات والموقع" in r_ch.data.decode("utf-8")
    assert "صافي Foodics (بعد خصم التطبيقات)" in r_ch.data.decode("utf-8")

    # 4. تحديث أرقام الميزانية عبر API
    r_up = client.post("/finance/api/balance-sheet/update", json={
        "year": 2026,
        "current_assets": 3000000,
        "inventory": 2000000,
        "current_liabilities": 1500000,
        "total_assets": 10000000,
        "total_equity": 8000000,
    })
    assert r_up.status_code == 200
    assert r_up.get_json()["success"] is True



def test_interactive_features_drilldown_and_live_search(client, test_data, db_session):
    """التحقق من عمل خاصية النقر للتفصيل بالرسم البياني والبحث الحي اللحظي للجداول."""
    with client.session_transaction() as sess:
        sess["user_id"] = 1

    # 1. فحص خدمة API التفصيل اللحظي بالرسم البياني Click-to-Drilldown
    r_dd = client.get("/finance/api/drilldown/month?year=2026&month=1")
    assert r_dd.status_code == 200
    dd_json = r_dd.get_json()
    assert dd_json["success"] is True
    assert dd_json["year"] == 2026
    assert dd_json["month"] == 1
    assert "branches" in dd_json
    assert "channels" in dd_json
    assert "top_customers" in dd_json
    assert "gross_sales" in dd_json

    # 2. فحص وجود نافذة التفصيل اللحظي Drilldown Modal في لوحة التحكم
    r_dash = client.get("/finance/?year=2026")
    assert r_dash.status_code == 200
    dash_html = r_dash.data.decode("utf-8")
    assert "drilldown-modal" in dash_html
    assert "openMonthDrilldown" in dash_html
    assert "انقر على أي شهر للتفصيل" in dash_html

    # 3. فحص وجود محرك البحث الحي اللحظي في base.html وجداول المبيعات والمخزون
    assert "initLiveTableSearch" in dash_html
    r_sales = client.get("/finance/sales?basis=yearly&year=2026")
    assert r_sales.status_code == 200
    assert 'data-live-search=".fin-table"' in r_sales.data.decode("utf-8")


def test_sync_reconciliation_details_and_kpis(client, test_data, db_session):
    """التحقق من حساب فروقات التسوية والمطابقة وقيد اليومية المقترح ونقطة API التفصيلية."""
    with client.session_transaction() as sess:
        sess["user_id"] = 1

    # 1. اختبار مسار المزامنة والتسويات الرئيسي
    r_sync = client.get("/finance/sync?year=2026")
    assert r_sync.status_code == 200
    html = r_sync.data.decode("utf-8")
    assert "مصفوفة المزامنة ورصد الفروقات والتسويات الشهرية" in html
    assert "reconciliation-modal" in html
    assert "openReconciliationModal" in html

    # 2. اختبار API تفاصيل التسوية وقيد اليومية لشهر 1
    r_det = client.get("/finance/api/sync/reconciliation-detail?year=2026&month=1")
    assert r_det.status_code == 200
    det = r_det.get_json()
    assert det["success"] is True
    assert det["year"] == 2026
    assert det["month"] == 1
    assert "foodics_gross" in det
    assert "xero_total" in det
    assert "est_vat" in det
    assert "apps_total" in det
    assert "gross_diff" in det
    assert "journal_lines" in det
    assert len(det["journal_lines"]) >= 3


def test_sync_live_xero_2026_and_api(client, test_data, db_session):
    """
    التحقق من:
    1. عمل محرك المزامنة الحية لعام 2026 (sync_live_xero_2026) ودعمه للـ Offline Fallback.
    2. استجابة مسار API الفوري (/finance/api/sync-live-2026) بنجاح.
    3. ظهور زر وشريط المزامنة الحية لعام 2026 في لوحات التحكم وقسم المخزون والمزامنة.
    """
    from services.findash_service import sync_live_xero_2026
    from models_findash import FinItemBalance, FinSyncSnapshot

    # 1. فحص تشغيل دالة المزامنة الحية مباشرة
    res = sync_live_xero_2026()
    assert res["success"] is True
    assert "items_updated" in res
    assert "total_stock_qty" in res
    assert "total_stock_value" in res

    # 2. تسجيل جلسة مستخدم وفحص نقطة الـ API
    with client.session_transaction() as sess:
        sess["user_id"] = 1

    r_api = client.post("/finance/api/sync-live-2026")
    assert r_api.status_code == 200
    api_data = r_api.get_json()
    assert api_data["success"] is True

    # 3. فحص وجود زر المزامنة اللحظية في الترويسة العامة base.html
    r_dash = client.get("/finance/?year=2026")
    assert r_dash.status_code == 200
    html_dash = r_dash.data.decode("utf-8")
    assert "btn-live-sync-2026" in html_dash
    assert "triggerLiveSync2026" in html_dash

    # 4. فحص وجود شريط الحالة المتزامنة في شاشة المخزون لعام 2026
    r_inv = client.get("/finance/inventory?subtab=current")
    assert r_inv.status_code == 200
    html_inv = r_inv.data.decode("utf-8")
    assert "أرصدة المخزون لعام 2026 متزامنة ومطابقة حياً مع Xero" in html_inv

    # 5. فحص وجود زر المزامنة الحية في مركز المزامنة والتسويات sync_hub
    r_sync = client.get("/finance/sync?year=2026")
    assert r_sync.status_code == 200
    html_sync = r_sync.data.decode("utf-8")
    assert "btn-sync-live-hub" in html_sync


def test_foodics_branch_and_product_sales(client, test_data, db_session):
    """التحقق من صحة تقارير Foodics: حسب الفرع، أفضل المنتجات، وأضعف المنتجات، والربط بالواجهة."""
    from models_findash import FinBranchSales, FinFoodicsProductSale
    from services.findash_service import get_foodics_branch_sales, get_foodics_product_ranked

    b1 = FinBranchSales(
        year=2026, month=8, branch_name="Hitten-branch", branch_code="B04",
        gross_sales=6990284.93, net_sales=6061967.50, tax=909722.60,
        discount=18594.84, orders_count=190881, avg_order=31.76,
        visitors=190881, customers=544, qty=475771.0, net_profit=4500000.0,
    )
    b2 = FinBranchSales(
        year=2026, month=8, branch_name="Alsahafa - HMG", branch_code="B05",
        gross_sales=4259697.15, net_sales=3579126.45, tax=537542.14,
        discount=143028.57, orders_count=170646, avg_order=20.97,
        visitors=170646, customers=3690, qty=310802.0, net_profit=2500000.0,
    )
    db_session.add_all([b1, b2])

    p1 = FinFoodicsProductSale(
        year=2026, month=8, branch="Hitten-branch", branch_code="B04",
        product="قهوة اليوم بارده", product_code="sk-0667",
        gross=3000000.0, net=2842724.46, tax=400000.0, discount=150000.0,
        qty=243512.0, cost=314617.08, returns_amount=0.0, returns_qty=0.0,
    )
    p2 = FinFoodicsProductSale(
        year=2026, month=8, branch="Hitten-branch", branch_code="B04",
        product="Lime water", product_code="sk-0738",
        gross=10.0, net=8.0, tax=1.0, discount=1.0,
        qty=1.0, cost=3.5, returns_amount=0.0, returns_qty=0.0,
    )
    db_session.add_all([p1, p2])
    db_session.commit()

    # 1. فحص دالة مبيعات الفروع
    res_b = get_foodics_branch_sales(year=2026)
    assert res_b["totals"]["branch_count"] >= 2
    assert res_b["totals"]["net_sum"] > 0
    assert res_b["totals"]["orders_sum"] > 0

    # 2. فحص أفضل المنتجات
    res_top = get_foodics_product_ranked(year=2026, worst=False)
    assert len(res_top["items"]) >= 1
    assert res_top["items"][0]["product"] == "قهوة اليوم بارده"
    assert res_top["items"][0]["profit"] > 0
    assert res_top["items"][0]["margin_pct"] > 80.0

    # 3. فحص أضعف المنتجات
    res_worst = get_foodics_product_ranked(year=2026, worst=True)
    assert len(res_worst["items"]) >= 1
    assert res_worst["items"][0]["product"] == "Lime water"

    # 4. فحص استجابة المسار وظهور التبويبات والمسمى الجديد
    with client.session_transaction() as sess:
        sess["user_id"] = 1

    r = client.get("/finance/branches?year=2026")
    assert r.status_code == 200
    html = r.data.decode("utf-8")
    assert "مبيعات Foodics" in html
    assert "حسب الفرع" in html
    assert "أفضل المنتجات" in html
    assert "أضعف المنتجات" in html
    assert "صافي المبيعات" in html
    assert "عدد الطلبات" in html


def test_aggregated_item_statement_totals_and_empty_month(client, test_data):
    """فحص حساب مجاميع بيان الصنف المجمع في بايثون بدلاً من جينجا والتعامل مع الشهور الفارغة."""
    from services.findash_service import get_aggregated_item_statement

    # 1. فحص شهر فارغ (شهر 9 مثلاً)
    res_empty = get_aggregated_item_statement(year=2026, month=9)
    assert res_empty["active_items_count"] == 0
    assert "totals" in res_empty
    assert res_empty["totals"]["purchase_in"] == 0.0
    assert res_empty["totals"]["net_qty"] == 0.0

    # 2. فحص استجابة المسار وظهور تنبيه الشهر الفارغ بدون أخطاء Bad Gateway / 500
    with client.session_transaction() as sess:
        sess["user_id"] = 1

    r = client.get("/finance/inventory?subtab=statement&year=2026&month=9&basis=monthly")
    assert r.status_code == 200
    html = r.data.decode("utf-8")
    assert "لا توجد حركات مخزون مسجلة لشهر" in html
    assert "عرض الأساس السنوي" in html

    # 3. فحص مسار المركز المالي مع month=0 ونسب السيولة
    r_bs = client.get("/finance/balance-sheet?year=2026&month=0")
    assert r_bs.status_code == 200
    html_bs = r_bs.data.decode("utf-8")
    assert "المركز المالي" in html_bs

