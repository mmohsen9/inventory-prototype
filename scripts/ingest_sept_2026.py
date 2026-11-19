import os
import sys
from pathlib import Path

# Ensure root directory is in sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app import app, db
from services.findash_service import (
    import_pnl_monthly_csv,
    import_branch_compare_csv,
    import_balance_sheet_csv,
    import_inventory_summary_csv,
    import_foodics_branch_sales_csv,
    import_foodics_product_sales_csv,
    get_dashboard_summary,
    get_pnl_statement_table,
    get_financial_analysis_data,
    clear_findash_cache,
)

sept_dir = BASE_DIR / "تطبيق_الداشبورد_التفاعلي" / "01_ملفات_الإدخال_الشهرية" / "2026-09"

with app.app_context():
    print("=== STARTING SEPTEMBER 2026 DATA INGESTION ===")
    
    # 1. P&L Monthly (Jan - Sep 2026)
    pnl_path = sept_dir / "Current_financial_year_by_month_2026_09.csv"
    if pnl_path.exists():
        count_pnl = import_pnl_monthly_csv(pnl_path, default_year=2026)
        print(f"✅ Imported {count_pnl} monthly P&L records for 2026.")
    
    # 2. P&L Compare Branches (9 months ended 30 Sep 2026)
    branch_pnl_path = sept_dir / "Profit_and_Loss_Compare_Branches_2026_09.csv"
    if branch_pnl_path.exists():
        count_branch = import_branch_compare_csv(branch_pnl_path, year=2026)
        print(f"✅ Imported {count_branch} branch P&L records for 2026.")
        
    # 3. Balance Sheet (30 Sep 2026)
    bs_path = sept_dir / "Balance_Sheet_2026_09.csv"
    if bs_path.exists():
        ok_bs = import_balance_sheet_csv(bs_path, year=2026, month=9)
        print(f"✅ Balance sheet import status: {ok_bs}")

    # 4. Inventory Item Summary (Tracked items up to 30 Sep 2026)
    inv_path = sept_dir / "Inventory_Item_Summary_2026_09.csv"
    if inv_path.exists():
        count_inv = import_inventory_summary_csv(inv_path, year=2026, month=9)
        print(f"✅ Imported {count_inv} inventory item balances for September 2026.")

    # 5. Foodics Branch Sales
    f_branch_path = sept_dir / "المبيعات حسب الفرع Foodics2026.xls"
    if f_branch_path.exists():
        count_f_br = import_foodics_branch_sales_csv(f_branch_path, year=2026, month=0)
        print(f"✅ Imported {count_f_br} Foodics branch sales records.")

    # 6. Foodics Product Sales
    f_prod_path = sept_dir / "المبيعات حسب المنتج Foodics 2026.xls"
    if f_prod_path.exists():
        count_f_prod = import_foodics_product_sales_csv(f_prod_path, default_year=2026, month=0)
        print(f"✅ Imported {count_f_prod} Foodics product sales records.")

    clear_findash_cache()
    db.session.commit()
    print("=== INGESTION FINISHED SUCCESSFULLY ===")
