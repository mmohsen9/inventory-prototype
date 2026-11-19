"""
app_findash.py — متحكمات ومسارات الداشبورد التفاعلي المالي والمزامنة الشهرية (Flask Blueprint)
مسار البادئة: /finance
"""
import json
import logging
from datetime import datetime
from functools import wraps

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, jsonify, flash
)
from models import db, User
from models_findash import (
    ensure_findash_schema,
    FinMonthlyPnl,
    FinBalanceFigure,
    FinBranchSales,
    FinSyncSnapshot,
    FinItemBalance,
    FinItemMovement,
    FinBudget,
    FinChannelSale,
    FinFoodicsProductSale,
)
from services.findash_service import (
    get_dashboard_summary,
    get_pnl_statement_table,
    get_sync_matrix,
    get_sync_kpis,
    get_month_reconciliation_details,
    sync_month_from_xero,
    sync_month_from_foodics,
    import_all_historical_data,
    get_inventory_current_stock,
    get_stagnant_items,
    get_inventory_periodic_balance,
    get_aggregated_item_statement,
    get_inventory_movements_log,
    get_budget_forecast_data,
    get_advanced_forecast_projection,
    get_financial_analysis_data,
    get_customer_branch_sales,
    get_channel_sales_data,
    import_channel_sales_excel,
    upsert_channel_sale,
    MONTH_NAMES_AR,
    get_month_drilldown_data,
    sync_live_xero_2026,
    get_foodics_years,
    get_foodics_branches_list,
    get_foodics_branch_sales,
    get_foodics_product_ranked,
    sync_foodics_reports_and_live,
)

logger = logging.getLogger(__name__)

findash_bp = Blueprint(
    "findash",
    __name__,
    url_prefix="/finance",
    template_folder="templates/findash",
)


@findash_bp.record_once
def _on_findash_init(state):
    with state.app.app_context():
        ensure_findash_schema()


def findash_permission_required(perm_code="finance.dashboard"):
    """فحص تسجيل الدخول والصلاحيات المحددة لشاشات الداشبورد المالي بنمط فوديكس."""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            user_id = session.get("user_id")
            if not user_id:
                return redirect(url_for("login"))
            user = User.query.get(user_id)
            if not user or not user.active:
                return redirect(url_for("login"))
            if user.role == "admin":
                return f(*args, **kwargs)
            if perm_code and not user.has_permission(perm_code):
                flash("لا تملك صلاحية الوصول لهذه الشاشة المالية", "error")
                return redirect(url_for("dashboard"))
            elif not perm_code and not user.has_permission("finance.dashboard"):
                flash("لا تملك صلاحية الوصول للداشبورد المالي", "error")
                return redirect(url_for("dashboard"))
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def findash_login_required(f):
    return findash_permission_required("finance.dashboard")(f)


def _get_current_user():
    user_id = session.get("user_id")
    if user_id:
        return User.query.get(user_id)
    return None


# قائمة الأعوام المالية الثابتة المعتمدة
AVAILABLE_FIN_YEARS = [2026, 2025, 2024]


@findash_bp.context_processor
def inject_common_findash_context():
    last_live_snap = FinSyncSnapshot.query.filter_by(
        source="xero", report_type="live_sync_2026"
    ).order_by(FinSyncSnapshot.last_synced_at.desc()).first()
    last_xero_live_sync = (
        last_live_snap.last_synced_at.strftime("%Y-%m-%d %H:%M")
        if (last_live_snap and last_live_snap.last_synced_at)
        else None
    )
    return {
        "user": _get_current_user(),
        "lang": session.get("lang", "ar"),
        "month_names_ar": MONTH_NAMES_AR,
        "available_years": AVAILABLE_FIN_YEARS,
        "last_xero_live_sync": last_xero_live_sync,
    }


# ─────────────────────────────────────────────────────────────────────────────
# شاشات العرض الرئيسية
# ─────────────────────────────────────────────────────────────────────────────

@findash_bp.route("/")
@findash_login_required
def index():
    """لوحة التحكم التنفيذية والمؤشرات المالية والمخططات البيانية."""
    year = request.args.get("year", default=2026, type=int)
    month = request.args.get("month", default=0, type=int)

    data = get_dashboard_summary(year=year, month=month)
    return render_template(
        "findash/dashboard.html",
        data=data,
        active_tab="dashboard",
    )


@findash_bp.route("/sales")
@findash_permission_required("finance.sales")
def sales():
    """ملخص مبيعات العملاء والفروع (سنوي، شهري، وإجمالي عام) مع تفصيل حسب العميل والفرع."""
    basis = request.args.get("basis", default="yearly", type=str)
    year = request.args.get("year", default=2026, type=int) if basis != "all" else None
    month = request.args.get("month", default=1, type=int) if basis == "monthly" else None
    search = request.args.get("search", default="", type=str)

    data = get_customer_branch_sales(basis=basis, year=year, month=month, search=search)

    return render_template(
        "findash/sales.html",
        data=data,
        active_tab="sales",
    )


@findash_bp.route("/pnl")
@findash_permission_required("finance.pnl")
def pnl():
    """قائمة الدخل الشهرية والتفصيلية المبوب بالحسابات."""
    year = request.args.get("year", default=2026, type=int)
    month = request.args.get("month", default=0, type=int)
    branch = request.args.get("branch", default="Total", type=str)

    pnl_data = get_pnl_statement_table(year=year, month=month, branch=branch)
    available_years = AVAILABLE_FIN_YEARS

    branches_avail = db.session.query(FinMonthlyPnl.branch).filter(FinMonthlyPnl.year == year).distinct().all()
    available_branches = [b[0] for b in branches_avail if b[0]] or ["Total"]

    return render_template(
        "findash/pnl.html",
        pnl_data=pnl_data,
        available_years=available_years,
        available_branches=available_branches,
        active_tab="pnl",
    )


@findash_bp.route("/branches", methods=["GET", "POST"])
@findash_bp.route("/foodics", methods=["GET", "POST"])
@findash_permission_required("finance.foodics")
def branches():
    """مبيعات Foodics: تحليل مبيعات نقاط البيع حسب الفرع والمنتج من تقارير Foodics."""
    year = request.args.get("year", default=2026, type=int)
    active_tab = request.args.get("tab", default="by_branch", type=str)
    branch = request.args.get("branch", default="(كل الفروع)", type=str)
    top_n = request.args.get("top_n", default=20, type=int)
    search_br = request.args.get("search_br", default="", type=str)
    search_prod = request.args.get("search_prod", default="", type=str)

    available_years = get_foodics_years()
    if year not in available_years and available_years:
        year = available_years[-1]

    branch_sales_data = get_foodics_branch_sales(year=year, search=search_br)
    branches_list = ["(كل الفروع)"] + get_foodics_branches_list(year=year)

    top_products_data = get_foodics_product_ranked(
        year=year,
        branch=branch,
        top_n=top_n,
        worst=False,
        search=search_prod,
    )

    worst_products_data = get_foodics_product_ranked(
        year=year,
        branch=branch,
        top_n=top_n if top_n <= 15 else 15,
        worst=True,
        search=search_prod,
    )

    return render_template(
        "findash/branches.html",
        selected_year=year,
        available_years=available_years,
        active_tab="branches",
        foodics_tab=active_tab,
        branch_sales=branch_sales_data,
        branches_list=branches_list,
        selected_branch=branch,
        top_n=top_n,
        search_br=search_br,
        search_prod=search_prod,
        top_products=top_products_data,
        worst_products=worst_products_data,
    )


@findash_bp.route("/foodics/sync", methods=["POST"])
@findash_permission_required("finance.foodics")
def sync_foodics():
    """تحديث واستيراد بيانات وتقارير Foodics."""
    year = request.form.get("year", default=2026, type=int)
    res = sync_foodics_reports_and_live(year=year)
    flash(f"✅ {res.get('message', 'اكتمل تحديث تقارير ومبيعات Foodics بنجاح.')}", "success")
    return redirect(url_for("findash.branches", year=year))


@findash_bp.route("/balance-sheet")
@findash_permission_required("finance.balance_sheet")
def balance_sheet():
    """المركز المالي ونسب السيولة ورأس المال العامل."""
    year = request.args.get("year", default=2026, type=int)
    month = request.args.get("month", default=0, type=int)

    q = FinBalanceFigure.query.filter_by(year=year)
    if month and month > 0:
        q = q.filter_by(month=month)
    figures = q.order_by(FinBalanceFigure.month.desc()).all()
    if figures:
        # تفضيل السجل الأكثر اكتمالاً (الذي يحتوي على خصوم متداولة معتمدة)
        valid_figs = [f for f in figures if (f.current_liabilities or 0.0) != 0]
        latest = valid_figs[0] if valid_figs else figures[0]
    else:
        latest = None
    available_years = AVAILABLE_FIN_YEARS

    # حساب نسب السيولة ديناميكياً لجميع الأعوام المتاحة لتغذية الرسم البياني
    years_labels = [str(y) for y in sorted(available_years)]
    chart_current_ratios = []
    chart_quick_ratios = []
    for y_val in sorted(available_years):
        fig_y = FinBalanceFigure.query.filter_by(year=y_val).order_by(FinBalanceFigure.month.desc()).first()
        if fig_y and (fig_y.current_liabilities or 0.0) > 0:
            c_r = round((fig_y.current_assets or 0.0) / fig_y.current_liabilities, 2)
            q_r = round(((fig_y.current_assets or 0.0) - (fig_y.inventory or 0.0)) / fig_y.current_liabilities, 2)
        elif fig_y and (fig_y.current_liabilities or 0.0) < 0:
            c_r = round((fig_y.current_assets or 0.0) / abs(fig_y.current_liabilities), 2)
            q_r = round(((fig_y.current_assets or 0.0) - (fig_y.inventory or 0.0)) / abs(fig_y.current_liabilities), 2)
        else:
            c_r = 0.0
            q_r = 0.0
        chart_current_ratios.append(c_r)
        chart_quick_ratios.append(q_r)

    # حساب رصيد الإقفال المعتمد (Snapshot وليس جمعاً عشوائياً للشهور)
    summary_figures = {
        "current_assets": latest.current_assets if latest else 0.0,
        "inventory": latest.inventory if latest else 0.0,
        "current_liabilities": latest.current_liabilities if latest else 0.0,
        "working_capital": latest.working_capital if latest else 0.0,
        "total_assets": latest.total_assets if latest else 0.0,
        "total_equity": latest.total_equity if latest else 0.0,
    }

    return render_template(
        "findash/balance_sheet.html",
        figures=figures,
        latest=latest,
        summary=summary_figures,
        chart_years=years_labels,
        chart_current_ratios=chart_current_ratios,
        chart_quick_ratios=chart_quick_ratios,
        selected_year=year,
        selected_month=month,
        available_years=available_years,
        active_tab="balance_sheet",
    )


@findash_bp.route("/sync")
@findash_permission_required("finance.sync")
def sync_hub():
    """مركز المزامنة السحابية الشهرية لـ Xero و Foodics ومطابقة الفروقات."""
    year = request.args.get("year", default=2026, type=int)
    matrix = get_sync_matrix(year=year)
    kpis = get_sync_kpis(matrix)
    available_years = AVAILABLE_FIN_YEARS

    recent_diffs = FinSyncSnapshot.query.filter(
        FinSyncSnapshot.year == year,
        FinSyncSnapshot.status == "reconciled_with_diff"
    ).order_by(FinSyncSnapshot.last_synced_at.desc()).all()

    return render_template(
        "findash/sync_hub.html",
        matrix=matrix,
        kpis=kpis,
        selected_year=year,
        available_years=available_years,
        recent_diffs=recent_diffs,
        active_tab="sync",
    )


@findash_bp.route("/inventory")
@findash_permission_required("finance.inventory")
def inventory():
    """المخزون والحركات: الرصيد الحالي، الشهري، السنوي، وبيان الصنف المجمّع."""
    subtab = request.args.get("subtab", default="current", type=str)
    year = request.args.get("year", default=2026, type=int)
    month = request.args.get("month", default=1, type=int)
    search = request.args.get("search", default="", type=str)
    show_all = request.args.get("show_all", default="0") == "1"
    days_threshold = request.args.get("days", default=60, type=int)
    stag_coffee_type = request.args.get("stag_type", default="all", type=str)
    basis = request.args.get("basis", default="yearly", type=str)
    mov_type = request.args.get("mov_type", default="الكل", type=str)

    current_stock = {}
    stagnant_report = {}
    monthly_balance = {}
    yearly_balance = {}
    aggregated_statement = {}
    movements_log = {}

    if subtab == "current":
        current_stock = get_inventory_current_stock(year=year, show_all=show_all, search=search)
        stagnant_report = get_stagnant_items(days_threshold=days_threshold, coffee_type=stag_coffee_type)
    elif subtab == "monthly":
        monthly_balance = get_inventory_periodic_balance(year=year, month=month, search=search, show_all=show_all)
    elif subtab == "yearly":
        yearly_balance = get_inventory_periodic_balance(year=year, month=None, search=search, show_all=show_all)
    elif subtab == "statement":
        aggregated_statement = get_aggregated_item_statement(year=year, month=month if basis == "monthly" else None, search=search)
    elif subtab == "movements":
        movements_log = get_inventory_movements_log(
            year=year if year > 0 else None,
            month=month if month > 0 else None,
            movement_type=mov_type,
            search=search,
            limit=500
        )

    available_years = AVAILABLE_FIN_YEARS

    return render_template(
        "findash/inventory.html",
        subtab=subtab,
        year=year,
        month=month,
        basis=basis,
        search=search,
        show_all=show_all,
        days_threshold=days_threshold,
        stag_coffee_type=stag_coffee_type,
        mov_type=mov_type,
        current_stock=current_stock,
        stagnant_report=stagnant_report,
        monthly_balance=monthly_balance,
        yearly_balance=yearly_balance,
        aggregated_statement=aggregated_statement,
        movements_log=movements_log,
        available_years=available_years,
        month_names_ar=MONTH_NAMES_AR,
        active_tab="inventory",
    )


@findash_bp.route("/budget")
@findash_permission_required("finance.budget")
def budget():
    """الموازنة والتوقعات: توليد الموازنة، تحليل الانحدار والموسمية ورصد الانحرافات."""
    base_year = request.args.get("base_year", default=2025, type=int)
    default_budget_year = 2027 if base_year >= 2026 else 2026
    budget_year = request.args.get("budget_year", default=default_budget_year, type=int)
    if "budget_year" not in request.args and budget_year <= base_year:
        budget_year = base_year + 1

    growth_pct = request.args.get("growth", default=10.0, type=float)
    growth_rate = growth_pct / 100.0

    forecast_ahead = request.args.get("ahead", default=12, type=int)
    cat_filter = request.args.get("cat", default="all", type=str)
    branch_filter = request.args.get("branch", default="all", type=str)

    budget_data = get_budget_forecast_data(base_year=base_year, budget_year=budget_year, growth_rate=growth_rate)
    forecast_trend = get_advanced_forecast_projection(months_ahead=forecast_ahead, category=cat_filter, branch=branch_filter)

    categories_avail = ["all", "Trading Income", "Cost of Sales", "Operating Expenses", "Other Income"]
    branches_avail = db.session.query(FinMonthlyPnl.branch).distinct().all()
    available_branches = ["all"] + [b[0] for b in branches_avail if b[0] and b[0] != "Total"]

    available_base_years = [2024, 2025, 2026]
    available_budget_years = [2025, 2026, 2027, 2028]

    return render_template(
        "findash/budget.html",
        budget_data=budget_data,
        forecast_trend=forecast_trend,
        base_year=base_year,
        budget_year=budget_year,
        growth_pct=growth_pct,
        forecast_ahead=forecast_ahead,
        cat_filter=cat_filter,
        branch_filter=branch_filter,
        categories_avail=categories_avail,
        available_branches=available_branches,
        available_base_years=available_base_years,
        available_budget_years=available_budget_years,
        active_tab="budget",
    )


@findash_bp.route("/analysis")
@findash_permission_required("finance.pnl")
def analysis():
    """التحليل المالي والنسب المحاسبية: 3 تبويبات مطابقة لتطبيق الداشبورد التفاعلي."""
    subtab = request.args.get("subtab", default="ratios", type=str)
    year = request.args.get("year", default=2026, type=int)

    # معالجة دفاعية لأي استعلامات مدمجة
    if "?" in subtab:
        parts = subtab.split("?", 1)
        subtab = parts[0]
        for p in parts[1].split("&"):
            if "=" in p:
                k, v = p.split("=", 1)
                if k == "year":
                    try:
                        year = int(v)
                    except ValueError:
                        pass
    year_a = request.args.get("year_a", default=2024, type=int)
    year_b = request.args.get("year_b", default=2026, type=int)
    show_formula = request.args.get("show_formula", default="0") in ["1", "true", "True"]

    analysis_data = get_financial_analysis_data(
        selected_year=year,
        year_a=year_a,
        year_b=year_b,
        show_formula=show_formula,
    )
    return render_template(
        "findash/analysis.html",
        analysis_data=analysis_data,
        subtab=subtab,
        selected_year=year,
        year_a=year_a,
        year_b=year_b,
        show_formula=show_formula,
        active_tab="analysis",
    )


@findash_bp.route("/channels", methods=["GET", "POST"])
@findash_permission_required("finance.sales")
def channels():
    """مبيعات التطبيقات والمواقع الإلكترونية: عرض، إدخال يدوي، واستيراد من إكسيل."""
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "import_excel" and "file" in request.files:
            file = request.files["file"]
            if file and file.filename:
                res = import_channel_sales_excel(file, file.filename)
                if res.get("success"):
                    flash(f"✅ {res.get('message')}", "success")
                else:
                    flash(f"⚠️ {res.get('message')}", "danger")
                return redirect(url_for("findash.channels"))
        elif action == "manual_add":
            try:
                y = int(request.form.get("year", 2026))
                m = int(request.form.get("month", 1))
                ch = request.form.get("channel_custom", "").strip() or request.form.get("channel", "أخرى")
                amt = float(request.form.get("amount", 0.0))
                br = request.form.get("branch", "كل الفروع").strip() or "كل الفروع"
                ord_cnt = int(request.form.get("orders_count", 0))
                note = request.form.get("note", "").strip()
                if amt > 0:
                    upsert_channel_sale(year=y, month=m, channel=ch, amount=amt, branch=br, orders_count=ord_cnt, note=note)
                    flash(f"✅ تم حفظ مبيعات {ch} لشهر {m}/{y} بنجاح.", "success")
                else:
                    flash("⚠️ يرجى إدخال مبلغ صحيح أكبر من الصفر.", "warning")
            except Exception as e:
                flash(f"❌ حدث خطأ أثناء الحفظ: {e}", "danger")
            return redirect(url_for("findash.channels", year=request.form.get("year", 2026)))

    year = request.args.get("year", default=2026, type=int)
    month = request.args.get("month", default=0, type=int)
    branch = request.args.get("branch", default="all", type=str)
    search = request.args.get("search", default=None, type=str)

    data = get_channel_sales_data(year=year, month=month, branch=branch, search=search)
    return render_template(
        "findash/channels.html",
        data=data,
        active_tab="channels",
    )


@findash_bp.route("/api/channels/delete/<int:sale_id>", methods=["POST"])
@findash_login_required
def api_channel_delete(sale_id: int):
    """حذف سجل مبيعات قناة وتطبيق."""
    rec = FinChannelSale.query.get(sale_id)
    if rec:
        db.session.delete(rec)
        db.session.commit()
        return jsonify({"success": True, "message": "تم حذف السجل بنجاح."})
    return jsonify({"success": False, "message": "السجل غير موجود."}), 404


@findash_bp.route("/api/balance-sheet/update", methods=["POST"])
@findash_login_required
def api_balance_sheet_update():
    """تعديل أرقام الميزانية العمومية يدوياً."""
    payload = request.get_json(silent=True) or request.form
    try:
        y = int(payload.get("year", 2026))
        ca = float(payload.get("current_assets", 0.0))
        inv = float(payload.get("inventory", 0.0))
        cl = float(payload.get("current_liabilities", 0.0))
        ta = float(payload.get("total_assets", 0.0))
        te = float(payload.get("total_equity", 0.0))

        rec = FinBalanceFigure.query.filter_by(year=y).order_by(FinBalanceFigure.month.desc()).first()
        if not rec:
            rec = FinBalanceFigure(year=y, month=12)
            db.session.add(rec)
        rec.current_assets = ca
        rec.inventory = inv
        rec.current_liabilities = cl
        rec.total_assets = ta
        rec.total_equity = te
        rec.source = "إدخال يدوي"
        db.session.commit()
        return jsonify({"success": True, "message": f"تم تحديث أرقام الميزانية لعام {y} بنجاح."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 400


# ─────────────────────────────────────────────────────────────────────────────
# واجهات الـ API للتشغيل اللحظي والمزامنة عبر AJAX
# ─────────────────────────────────────────────────────────────────────────────

@findash_bp.route("/api/drilldown/month")
@findash_login_required
def api_drilldown_month():
    """نقطة تشغيل تفاعلية عند النقر على عمود شهر بالرسم البياني لتفصيل مبيعاته وفروعه."""
    year = request.args.get("year", default=2026, type=int)
    month = request.args.get("month", default=1, type=int)
    data = get_month_drilldown_data(year=year, month=month)
    return jsonify(data)


@findash_bp.route("/api/forecast")
@findash_permission_required("finance.budget")
def api_forecast():
    """نقطة تشغيل تفاعلية لحساب التوقعات المستقبلية بناء على الانحدار والموسمية."""
    ahead = request.args.get("ahead", default=12, type=int)
    cat = request.args.get("cat", default="all", type=str)
    branch = request.args.get("branch", default="all", type=str)

    res = get_advanced_forecast_projection(months_ahead=ahead, category=cat, branch=branch)
    return jsonify(res)


@findash_bp.route("/api/sync/reconciliation-detail", methods=["GET"])
@findash_permission_required("finance.sync")
def api_sync_reconciliation_detail():
    """كشف تفاصيل الفروقات والتسويات المحاسبية لشهر محدد وقيد اليومية المقترح."""
    year = request.args.get("year", default=2026, type=int)
    month = request.args.get("month", default=1, type=int)
    details = get_month_reconciliation_details(year=year, month=month)
    return jsonify(details)


@findash_bp.route("/api/sync-month", methods=["POST"])
@findash_permission_required("finance.sync")
def api_sync_month():
    """نقطة تشغيل مزامنة شهر محدد من Xero وفوديكس مع كشف الفروقات."""
    payload = request.get_json(silent=True) or request.form
    year = int(payload.get("year", 2026))
    month = int(payload.get("month", 1))
    source = payload.get("source", "all")

    response_data = {
        "success": True,
        "year": year,
        "month": month,
        "xero_result": None,
        "foodics_result": None,
        "message": ""
    }

    messages = []
    if source in ["xero", "all"]:
        x_res = sync_month_from_xero(year, month)
        response_data["xero_result"] = x_res
        if x_res.get("success"):
            if x_res.get("source_used") == "local_database":
                messages.append(x_res.get("message", "تمت المطابقة مع السجلات المعتمدة بنجاح."))
            elif x_res.get("diff_detected"):
                messages.append(f"Xero: تم التحديث مع كشف {len(x_res.get('diff_summary', []))} قيود تسوية/فروقات.")
            else:
                messages.append("Xero: تم التحديث والمطابقة التامة 100%.")
        else:
            messages.append(f"Xero: {x_res.get('error')}")

    if source in ["foodics", "all"]:
        f_res = sync_month_from_foodics(year, month)
        response_data["foodics_result"] = f_res
        if f_res.get("success"):
            if f_res.get("source_used") == "local_database":
                messages.append(f_res.get("message", "تمت مطابقة مبيعات الفروع بنجاح."))
            else:
                messages.append(f"Foodics: تم تحديث مبيعات {f_res.get('branches_synced')} فروع.")
        else:
            messages.append(f"Foodics: {f_res.get('error')}")

    response_data["message"] = " | ".join(messages)
    return jsonify(response_data)


@findash_bp.route("/api/import-historical", methods=["POST"])
@findash_permission_required("finance.sync")
def api_import_historical():
    """نقطة تشغيل استيراد كافة ملفات الإكسيل/CSV المحفوظة للأعوام السابقة بنقرة واحدة."""
    res = import_all_historical_data()
    msg = (
        f"اكتمل الاستيراد بنجاح: "
        f"{res['pnl_rows']} سطر أرباح وخسائر، "
        f"{res['branch_pnl_rows']} سطر فروع، "
        f"{res['balance_sheets']} ميزانيات عمومية، "
        f"{res['foodics_sales']} مبيعات فروع فوديكس."
    )
    return jsonify({"success": True, "details": res, "message": msg})

@findash_bp.route("/api/archive/upload", methods=["POST"])
@findash_permission_required("finance.sync")
def api_upload_archive_file():
    """
    رفع واستيراد ملف إكسيل/CSV معتمد لأي عام مالي منتهٍ لأرشفته وتثبيته في النظام.
    """
    if "file" not in request.files:
        return jsonify({"success": False, "error": "لم يتم إرفاق أي ملف"}), 400

    file = request.files["file"]
    if not file or not file.filename:
        return jsonify({"success": False, "error": "الملف غير صالح"}), 400

    year = request.form.get("year", type=int) or 2026
    report_type = request.form.get("report_type", "auto")

    from werkzeug.utils import secure_filename
    from pathlib import Path

    ext = Path(file.filename).suffix.lower()
    if ext not in [".xlsx", ".xls", ".csv", ".txt"]:
        return jsonify({"success": False, "error": "صيغة الملف غير مدعومة (المسموح: xlsx, xls, csv)"}), 400

    archive_dir = Path("data/financial_archive") / str(year)
    archive_dir.mkdir(parents=True, exist_ok=True)

    safe_name = secure_filename(file.filename) or f"archive_{year}{ext}"
    dest_path = archive_dir / safe_name
    file.save(str(dest_path))

    f_low = safe_name.lower()
    imported_count = 0
    msg = ""

    if report_type == "pnl" or any(k in f_low for k in ["pnl", "profit", "دخل", "current_financial"]):
        imported_count = import_pnl_monthly_csv(dest_path, default_year=year)
        msg = f"تم بنجاح أرشفة وتثبيت قائمة الدخل لعام {year} بعدد {imported_count} سجلاً."
    elif report_type == "balance_sheet" or "balance" in f_low or "مركز" in f_low:
        ok = import_balance_sheet_csv(dest_path, year=year, month=12)
        imported_count = 1 if ok else 0
        msg = f"تم بنجاح أرشفة المركز المالي لعام {year}."
    elif report_type == "inventory" or any(k in f_low for k in ["inventory", "مخزون", "مجمع"]):
        imported_count = import_inventory_summary_csv(dest_path, year=year, month=12)
        msg = f"تم بنجاح أرشفة وتثبيت رصيد المخزون لعام {year} بعدد {imported_count} صنفاً."
    elif report_type == "branches" or "branch" in f_low or "فروع" in f_low:
        imported_count = import_branch_compare_csv(dest_path, year=year)
        msg = f"تم بنجاح أرشفة بيانات الفروع لعام {year} بعدد {imported_count} سجلاً."
    else:
        res = import_all_historical_data()
        msg = f"تم حفظ الملف واستيراد وتحديث الأرشيف المالي لعام {year} بنجاح."

    clear_findash_cache()

    return jsonify({
        "success": True,
        "year": year,
        "filename": safe_name,
        "imported_count": imported_count,
        "message": msg
    })


@findash_bp.route("/api/sync-live-2026", methods=["POST"])
@findash_permission_required("finance.sync")
def api_sync_live_2026():
    """تشغيل المزامنة الفورية اللحظية لبيانات عام 2026 مع Xero (المخزون + القوائم المالية)."""
    try:
        res = sync_live_xero_2026()
        return jsonify(res)
    except Exception as exc:
        logger.error("فشل المزامنة الحية لعام 2026: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500
