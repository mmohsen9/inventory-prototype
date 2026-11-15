# -*- coding: utf-8 -*-
"""
تطبيق الداشبورد التفاعلي - نظام إدارة المخزون والمبيعات (شركة الكوب الأول)
Interactive Inventory & Sales Dashboard — Streamlit + SQLite (RTL Arabic)

يحوّل السكربت الثابت إلى تطبيق تفاعلي:
- إدخال المبيعات اليومية وتحديث المخزون تلقائيًا
- تسجيل دفعات التحميص مع فحص نسبة الإنتاج (Yield) لحظيًا
- موازنة قابلة للتحرير مع نسبة نمو لكل حساب
- مركز تنبيهات + استيراد ملفاتك الحالية + تصدير Excel بنفس التنسيق

التشغيل:  streamlit run "تطبيق_الداشبورد_التفاعلي.py"
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

# Mapping for column names from SQL (English) to Arabic UI
COLUMN_MAPPING = {
    'year': 'السنة',
    'month': 'الشهر',
    'mdate': 'التاريخ',
    'item_code': 'كود الصنف',
    'item_name': 'اسم الصنف',
    'coffee_type': 'نوع البن',
    'family': 'رقم العائلة',
    'code': 'اسم الصنف',
    'qty': 'حركة الكمية',
    'quantity': 'حركة الكمية',
    'value': 'القيمة',
    'total': 'الإجمالي',
    'movement_type': 'نوع الحركة',
    'source': 'المصدر',
    'reference': 'المرجع',
    'contact': 'العميل',
    'customer': 'العميل',
    'description': 'الوصف',
    'branch': 'الفرع',
    'amount': 'المبلغ',
    'status': 'الحالة',
    'channel': 'القناة',
    'note': 'الملاحظة',
    'gross': 'الإجمالي',
    'net': 'صافي المبيعات',
    'tax': 'الضرائب',
    'discount': 'الخصم',
    'orders': 'عدد الطلبات',
    'avg_order': 'متوسط الطلب',
    'visitors': 'عدد الزوار',
    'customers': 'عدد العملاء',
    'invoice_no': 'رقم الفاتورة',
    'invoice_num': 'رقم الفاتورة',
    'invoice_id': 'رقم الفاتورة',
    'invoice': 'رقم الفاتورة',
}

# Mapping for P&L categories to Arabic
CATEGORY_MAPPING = {
    'revenue': 'الإيرادات التشغيلية',
    'cogs': 'تكلفة البضاعة المباعة',
    'expense': 'المصروفات',
    'other_income': 'الإيرادات الأخرى',
    'إيراد': 'الإيرادات التشغيلية',
    'تكلفة مبيعات': 'تكلفة البضاعة المباعة',
    'مصروف': 'المصروفات',
}

def translate_category(cat):
    """Translate category code to Arabic display name."""
    return CATEGORY_MAPPING.get(cat, cat)

# ==========================================================================
# توافق مع إصدارات Streamlit القديمة (يعمل على 1.12 وأحدث)
# ==========================================================================
if not hasattr(st, "divider"):
    st.divider = lambda: st.markdown("---")

_st_dataframe = st.dataframe

# ==========================================================================
# دالة تجميع الأعمدة تلقائيًا (إضافة صف الإجمالي في نهاية كل عمود)
# ==========================================================================
_TOTAL_EXCLUDED_PATTERNS = [
    r"^(سنة|السنة|year|yr)$",
    r"^(شهر|الشهر|month)$",
    r"^(يوم|اليوم|day)$",
    r"(أيام|days)",
    r"^(تاريخ|التاريخ|date|mdate|created_at|updated_at)$",
    r"^(وقت|الوقت|time|ts|timestamp)$",
    r"(كود|code)",
    r"^(id|معرف|_id)$",
    r"(رقم|number|num)",
    r"(مرجع|reference|ref)",
    r"(فاتورة|invoice)",
    r"(عائلة|family)",
    r"(ترتيب|rank|index|#)",
    r"(هاتف|phone|جوال|mobile)",
    r"(نسبة|ratio|rate|pct|percent|%)",
    r"(متوسط|avg|average|mean)",
    r"(معدل)",
    r"(مؤشر|index)",
    r"(سعر|price|unit_price)",
    r"(حد|limit)",
]

_PREFERRED_LABEL_COLS = [
    "اسم الصنف", "المنتج", "اسم الحساب", "الحساب", "البيان", "الوصف",
    "الفرع", "القناة", "العميل", "المورد", "البند", "نوع البن", "نوع الحركة",
]


def _coerce_numeric_cols(target, ref_df, skip=None):
    """يعيد الأعمدة الرقمية في target إلى نوعها الرقمي وفق ref_df.

    ضروري لأن صف الإجمالي يحمل قيمًا نصية في بعض الأعمدة، فيحوّلها الدمج
    إلى object ويفشل بعدها تحويل Arrow داخل st.dataframe. عمود التسمية
    مستثنى حتى لا تُحوَّل كلمة «الإجمالي» إلى NaN.
    """
    for col in ref_df.columns:
        if col == skip or col not in target.columns:
            continue
        if not pd.api.types.is_numeric_dtype(ref_df[col]):
            continue
        try:
            target[col] = pd.to_numeric(target[col], errors="coerce")
        except Exception:
            pass


def add_column_totals(df: pd.DataFrame, label: str = "الإجمالي") -> pd.DataFrame:
    """إضافة صف تجميع (المجموع) في نهاية كل عمود للجداول الرقمية تلقائيًا."""
    if not isinstance(df, pd.DataFrame) or df.empty or len(df) < 1:
        return df

    # عدم تكرار صف الإجمالي إذا كان موجوداً مسبقاً في السطر الأخير
    for val in df.iloc[-1]:
        if isinstance(val, str) and val.strip().lower() in {
            "الإجمالي", "المجموع", "المجموع الكلي", "الاجمالي", "إجمالي", "مجموع", "total", "totals"
        }:
            return df
    if str(df.index[-1]).strip().lower() in {
        "الإجمالي", "المجموع", "المجموع الكلي", "الاجمالي", "إجمالي", "مجموع", "total", "totals"
    }:
        return df

    # استخراج الأعمدة الرقمية التي تقبل الجمع (مع استبعاد المعرفات والأكواد والنسب)
    sum_cols = []
    for col in df.columns:
        col_str = str(col).strip()
        if any(re.search(pat, col_str, re.IGNORECASE) for pat in _TOTAL_EXCLUDED_PATTERNS):
            continue
        series = df[col]
        if pd.api.types.is_numeric_dtype(series):
            valid = series.dropna()
            if len(valid) > 0 and valid.between(2000, 2100).all() and "سنة" in col_str.lower():
                continue
            sum_cols.append(col)
        else:
            try:
                cleaned = series.dropna().astype(str).str.replace(",", "").str.strip()
                if not cleaned.empty:
                    num_s = pd.to_numeric(cleaned, errors="coerce")
                    if num_s.notna().sum() == len(cleaned) and num_s.notna().sum() > 0:
                        sum_cols.append(col)
            except Exception:
                pass

    if not sum_cols:
        return df

    # استبعاد جداول سجل العمليات أو الجداول الوصفية
    first_col = str(df.columns[0]).strip().lower()
    if first_col in ["العملية", "action", "المستخدم", "user"]:
        return df

    # تحديد أفضل عمود لوضع تسمية "الإجمالي"
    label_col = None
    for pref in _PREFERRED_LABEL_COLS:
        if pref in df.columns and pref not in sum_cols:
            label_col = pref
            break

    if label_col is None:
        for c in df.columns:
            if c not in sum_cols:
                label_col = c
                break
        if label_col is None:
            label_col = df.columns[0]

    # حساب المجاميع
    totals = {}
    for col in sum_cols:
        try:
            s = df[col]
            if not pd.api.types.is_numeric_dtype(s):
                s = pd.to_numeric(s.astype(str).str.replace(",", "").str.strip(), errors="coerce")
            val = s.dropna().sum()
            if pd.api.types.is_integer_dtype(df[col]) and not df[col].isna().any():
                totals[col] = int(val)
            else:
                totals[col] = round(float(val), 2)
        except Exception:
            totals[col] = 0

    # بناء صف الإجمالي
    row_data = {}
    for col in df.columns:
        if col == label_col:
            row_data[col] = label
        elif col in totals:
            row_data[col] = totals[col]
        else:
            # الحفاظ على نوع العمود: الأعمدة الرقمية تبقى رقمية (None) ونصية ("-")
            # وإلا تحوّلت كل الأعمدة إلى object وفشل تحويلها بـ Arrow في st.dataframe
            row_data[col] = "-" if not pd.api.types.is_numeric_dtype(df[col]) else None

    tot_df = pd.DataFrame([row_data])
    # محاذاة الأعمدة الرقمية قبل الدمج: تمنع تحويلها إلى object وتتجنّب
    # تحذير pandas بشأن الأعمدة الفارغة/الكلها NA.
    _coerce_numeric_cols(tot_df, df, skip=label_col)
    out = pd.concat([df, tot_df], ignore_index=True)
    # إعادة الأعمدة الرقمية إلى نوعها الأصلي بعد الدمج، وإلا فشل تحويل Arrow
    # إلى Arrow داخل st.dataframe. عمود التسمية مستثنى حتى لا تُفقد «الإجمالي».
    _coerce_numeric_cols(out, df, skip=label_col)
    # عمود التسمية: إذا صار يحمل أرقامًا بجانب كلمة «الإجمالي» نُحوّله لنص
    # نظيف حتى يمرّ تحويل Arrow دون تحذير أو فقدان للتسمية.
    if label_col in out.columns and out[label_col].dtype == object:
        out[label_col] = out[label_col].map(
            lambda v: "" if v is None or (isinstance(v, float) and pd.isna(v))
            else (f"{v:g}" if isinstance(v, (int, float)) else str(v))
        )
    return out


def _df_to_html(df, hide_index):
    html = df.to_html(index=not hide_index, border=0, na_rep="", escape=True,
                      float_format=lambda x: f"{x:,.2f}", classes="rtl-tbl")
    return (
        "<style>.rtl-wrap{max-height:540px;overflow:auto;direction:rtl}"
        ".rtl-tbl{width:100%;border-collapse:collapse;font-size:0.9rem}"
        ".rtl-tbl th,.rtl-tbl td{border:1px solid rgba(128,128,128,.35);"
        "padding:6px 10px;text-align:right;white-space:nowrap}"
        ".rtl-tbl th{background:rgba(128,128,128,.18);position:sticky;top:0}"
        ".rtl-tbl tr:last-child td{font-weight:bold;background:rgba(128,128,128,.15);border-top:2px solid rgba(128,128,128,.5)}"
        "</style>"
        "<div class='rtl-wrap'>" + html + "</div>"
    )


def _compat_df(target, orig, data=None, *args, **kwargs):
    # خيار التجميع التلقائي في نهاية الأعمدة (مفعّل افتراضيًا)
    with_totals = kwargs.pop("with_totals", None)
    if with_totals is None:
        with_totals = kwargs.pop("show_totals", None)
    if with_totals is None:
        try:
            with_totals = st.session_state.get("show_table_totals", True)
        except Exception:
            with_totals = True

    if with_totals and isinstance(data, pd.DataFrame):
        try:
            data = add_column_totals(data)
        except Exception:
            pass

    if data is not None and getattr(data, "columns", None) is not None and "column_config" not in kwargs:
        try:
            cc = getattr(st, "column_config", None)
            if cc is not None and hasattr(cc, "Column"):
                kwargs["column_config"] = {
                    str(c): cc.Column(str(c), help=str(c)) for c in list(data.columns)
                }
        except (AttributeError, TypeError, ValueError):
            pass
    try:
        return orig(data, *args, **kwargs)
    except TypeError:
        if hasattr(data, "to_html"):
            try:
                return target.markdown(
                    _df_to_html(data, bool(kwargs.get("hide_index", False))),
                    unsafe_allow_html=True,
                )
            except Exception:
                pass
        for _k in ("use_container_width", "hide_index", "column_config", "width"):
            kwargs.pop(_k, None)
        return orig(data, *args, **kwargs)


st.dataframe = lambda data=None, *a, **k: _compat_df(st, _st_dataframe, data, *a, **k)

# ترقيع دالة الكلاس أيضًا حتى تشمل النداءات على الأعمدة/التبويبات مثل col.dataframe(...)
try:
    from streamlit.delta_generator import DeltaGenerator as _DG

    _dg_dataframe = _DG.dataframe

    def _dg_compat(self, data=None, *a, **k):
        return _compat_df(self, lambda d=None, *aa, **kk: _dg_dataframe(self, d, *aa, **kk),
                          data, *a, **k)

    _DG.dataframe = _dg_compat
except Exception:
    pass

# ==========================================================================
# المسارات والإعدادات الأساسية
# ==========================================================================
BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "01_ملفات_الإدخال_الشهرية"
STATIC_DIR = BASE_DIR / "02_مصادر_ثابتة"
DB_DIR = BASE_DIR / "03_قاعدة_البيانات"
OUTPUT_DIR = BASE_DIR / "04_المخرجات"

for _d in (INPUT_DIR, STATIC_DIR, DB_DIR, OUTPUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DB_PATH = DB_DIR / "app_interactive.db"
DASHBOARD_PATH = OUTPUT_DIR / "الداشبورد_المالي_والتشغيلي.xlsx"
ALERTS_PATH = OUTPUT_DIR / "تنبيهات_الشهر.xlsx"

DEFAULT_SETTINGS = {
    "growth_rate": "0.10",      # نسبة النمو الافتراضية للموازنة
    "roast_yield": "0.833",     # نسبة إنتاج التحميص المتوقعة
    "roast_tolerance": "0.012", # التفاوت المسموح به
    "base_year": "2025",        # سنة الأساس (الفعلي)
    "budget_year": "2026",      # سنة الموازنة
    "low_stock_qty": "10",      # حد التنبيه للمخزون المنخفض
}

MONTH_NAMES_AR = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
                  "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]

SALE = "بيع"
GREEN_OUT = "خروج أخضر للتحميص"
ROASTED_IN = "دخول محمص من التحميص"
BRANCH_OUT = "صرف محمص للفروع"
SAMPLE = "عينات وهدايا"
ADJUST = "تسوية أو إعادة تقييم"
OTHER = "أخرى"

# ==========================================================================
# أدوات مساعدة (منقولة من السكربت الأصلي)
# ==========================================================================
def normalize(value):
    text = str(value).replace("\n", " ").replace("\r", " ").strip().lower()
    return re.sub(r"\s+", " ", text)


def clean_code(value):
    if pd.isna(value):
        return ""
    text = str(value).strip()
    return text[:-2] if text.endswith(".0") else text


def number(value):
    if pd.isna(value):
        return 0.0
    text = str(value).strip().replace(",", "")
    text = text.replace("(", "-").replace(")", "")
    result = pd.to_numeric(text, errors="coerce")
    return float(result) if pd.notna(result) else 0.0


def find_column(columns, words):
    for column in columns:
        if any(word in normalize(column) for word in words):
            return column
    return None


def read_excel(path):
    try:
        return pd.read_excel(path, sheet_name=0)
    except Exception as exc:
        # xlsx: المرحلة 1 — لا نبتلع الخطأ بصمت
        import logging as _logging
        _logging.warning("read_excel فشل %s: %s", path, exc)
        return pd.DataFrame()


def read_csv_rows(path):
    last_exc = None
    for encoding in ["utf-8-sig", "utf-8", "cp1256", "latin1"]:
        try:
            with open(path, "r", encoding=encoding, errors="replace", newline="") as file:
                return list(csv.reader(file, delimiter=";"))
        except Exception as exc:
            last_exc = exc
            continue
    if last_exc is not None:
        import logging as _logging
        _logging.warning("read_csv_rows فشل %s: %s", path, last_exc)
    return []


def _is_valid_year(value):
    try:
        year = int(value)
    except Exception:
        return False
    return 2000 <= year <= 2100


def _years_from_text(text):
    years = []
    s = str(text or "").strip()
    if not s or s.lower() == "nan":
        return years
    compact = s.replace(" ", "").replace(",", "")
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", compact):
        return years
    for match in re.finditer(r"(?<![\d.])(20\d{2})(?![\d.])", s):
        year = int(match.group(1))
        if _is_valid_year(year) and year not in years:
            years.append(year)
    return years


def _excel_serial_to_date(value):
    try:
        serial = float(value)
    except Exception:
        return None
    if serial < 20000 or serial > 80000:
        return None
    try:
        return pd.to_datetime(serial, unit="D", origin="1899-12-30")
    except Exception:
        return None


def parse_cell_date(value):
    """يحول خلية تاريخ إكسل (نص أو رقم تسلسلي) إلى datetime إن أمكن."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day)
    serial = _excel_serial_to_date(value)
    if serial is not None:
        return serial.to_pydatetime()
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt)
        except Exception:
            continue
    _AR_MONTHS = {
        "يناير": 1, "فبراير": 2, "مارس": 3, "أبريل": 4, "مايو": 5, "يونيو": 6,
        "يوليو": 7, "أغسطس": 8, "سبتمبر": 9, "أكتوبر": 10, "نوفمبر": 11, "ديسمبر": 12,
        "كانون الثاني": 1, "شباط": 2, "آذار": 3, "نيسان": 4, "أيار": 5, "حزيران": 6,
        "تموز": 7, "آب": 8, "أيلول": 9, "تشرين الأول": 10, "تشرين الثاني": 11, "كانون الأول": 12,
    }
    _m = re.search(r"(\d{1,2})[\s\-/]+([^\s\d][^\s\d][^\s\d][^\s\d]+)[\s\-/]+(\d{4})", text)
    if _m:
        d, mon, y = int(_m.group(1)), _m.group(2).strip(), int(_m.group(3))
        mi = _AR_MONTHS.get(mon)
        if mi and 1 <= d <= 31 and 2000 <= y <= 2100:
            return datetime(y, mi, d)
    parsed = pd.to_datetime(text, errors="coerce", dayfirst=True)
    if pd.notna(parsed):
        return parsed.to_pydatetime()
    return None


def year_month_from_value(value, default_year=None, default_month=None):
    parsed = parse_cell_date(value)
    if parsed is None:
        return default_year, default_month
    return parsed.year, parsed.month


def extract_years_from_excel(path, raw=None, max_rows=12):
    """يقرأ عناوين السنوات من محتوى الملف (فترة التقرير / أعمدة الأشهر / اسم الملف)."""
    years = []
    name = Path(path).name if path else ""
    parent = Path(path).parent.name if path else ""
    for source in (name, parent):
        for year in _years_from_text(source):
            if year not in years:
                years.append(year)
    frame = raw
    if frame is None and path:
        try:
            frame = pd.read_excel(path, sheet_name=0, header=None, dtype=object, nrows=max_rows)
        except Exception:
            frame = None
    if frame is not None and not frame.empty:
        limit = min(max_rows, len(frame))
        for i in range(limit):
            for cell in frame.iloc[i].tolist():
                if cell is None or (isinstance(cell, float) and pd.isna(cell)):
                    continue
                if isinstance(cell, (int, float)) and not isinstance(cell, bool):
                    continue
                if isinstance(cell, datetime):
                    if _is_valid_year(cell.year) and cell.year not in years:
                        years.append(cell.year)
                    continue
                if isinstance(cell, date):
                    if _is_valid_year(cell.year) and cell.year not in years:
                        years.append(cell.year)
                    continue
                for year in _years_from_text(cell):
                    if year not in years:
                        years.append(year)
    return years


def detect_file_period(path, raw=None, default_year=None, default_month=12):
    """يضبط سنة/شهر الملف من محتواه ثم اسمه، دون الاعتماد على قيمة ثابتة."""
    years = extract_years_from_excel(path, raw=raw)
    year = max(years) if years else default_year
    month = default_month
    parent = Path(path).parent.name if path else ""
    pm = re.search(r"(20\d{2})[-_](0?[1-9]|1[0-2])", parent)
    if pm:
        year, month = int(pm.group(1)), int(pm.group(2))
    else:
        nm = Path(path).name if path else ""
        nm_m = re.search(r"(20\d{2})[-_](0?[1-9]|1[0-2])", nm)
        if nm_m:
            year, month = int(nm_m.group(1)), int(nm_m.group(2))
    if year is None:
        if default_year is not None:
            year = default_year
        else:
            try:
                year = s_int(get_settings(), "base_year")
            except Exception:
                year = int(DEFAULT_SETTINGS["base_year"])
    return int(year), int(month)


def sync_year_settings(years):
    """يضبط سنة الأساس وسنة الموازنة من السنوات المقروءة من الإكسل."""
    clean = sorted({int(y) for y in years if _is_valid_year(y)})
    if not clean:
        return
    latest = clean[-1]
    set_setting("base_year", latest)
    set_setting("budget_year", latest + 1)


def year_options(extra=None):
    """سنوات العناوين والقوائم: من البيانات المستوردة أولاً ثم الإعدادات."""
    years = set(list_years())
    years.update(movement_years())
    settings = get_settings()
    for key in ("base_year", "budget_year"):
        if _is_valid_year(settings.get(key)):
            years.add(int(float(settings[key])))
    if extra is not None:
        if isinstance(extra, (list, tuple, set)):
            years.update(int(y) for y in extra if _is_valid_year(y))
        elif _is_valid_year(extra):
            years.add(int(extra))
    return sorted(years) or [s_int(get_settings(), "base_year")]


def default_year_value(years=None):
    opts = years if years else year_options()
    if not opts:
        return s_int(get_settings(), "base_year")
    return opts[-1]


def _cacheable(fn):
    """يخزّن النتيجة بين إعادات التشغيل إذا توفّر st.cache_data (أو st.cache القديم)."""
    deco = getattr(st, "cache_data", None) or getattr(st, "cache", None)
    if deco is None:
        return fn
    try:
        return deco(show_spinner=False)(fn)
    except Exception:
        return fn


@_cacheable
def years_with_data():
    """السنوات التي تحتوي بيانات فعلية (حركات/فروع/فواتير) — لاختيار افتراضي ذكي."""
    years = set(movement_years()) | set(foodics_years())
    try:
        years.update(int(y) for y in query_df(
            "SELECT DISTINCT year FROM branch_sales")["year"].tolist())
    except Exception:
        pass
    return sorted(y for y in years if _is_valid_year(y))


def invalidate_year_caches():
    """يُفرغ مخزن سنوات البيانات بعد الاستيراد حتى لا يبقى الافتراضي على سنة قديمة."""
    clearer = getattr(years_with_data, "clear", None)
    if clearer:
        try:
            clearer()
        except Exception:
            pass


def year_default_index(options):
    """يرجّع index أحدث سنة تحتوي بيانات فعلية، أو آخر الخيارات إن لا توجد."""
    if not options:
        return 0
    with_data = years_with_data()
    if not with_data:
        return len(options) - 1
    for i in range(len(options) - 1, -1, -1):
        try:
            if int(options[i]) in with_data:
                return i
        except (TypeError, ValueError):
            continue
    return len(options) - 1


def item_meta(code):
    """يحدد نوع البن ورقم العائلة من كود الصنف."""
    code = (code or "").upper().strip()
    if code.startswith("GB"):
        coffee_type = "بن أخضر"
    elif code.startswith("RB"):
        coffee_type = "بن محمص"
    else:
        coffee_type = "غير مصنف"
    family = re.sub(r"^(GB|RB)", "", code, flags=re.IGNORECASE)
    return coffee_type, family


# ==========================================================================
# طبقة قاعدة البيانات (SQLite) — مع دعم المعاملات (Transaction) والفهارس
# ==========================================================================
# اتصال المعاملة النشطة (إن وجد) — يسمح بتجميع كل عمليات الاستيراد في
# معاملة واحدة ذرّية: لو فشل أي ملف يُستعاد كل شيء بدل ترك القاعدة فارغة.
_tx_conn = None


def connect():
    # WAL يحسّن التزامن مع Streamlit (قراءات متعددة أثناء الكتابة)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=10000;")
    except Exception:
        pass
    return conn


@contextmanager
def db_transaction():
    """معاملة ذرّية واحدة لكل عملية استيراد شاملة.

    الاستخدام:
        with db_transaction():
            execute("DELETE ...")
            insert_movements(df, from_report=1)  # تُنفّذ داخل نفس المعاملة
    """
    global _tx_conn
    conn = connect()
    _tx_conn = conn
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        _tx_conn = None
        conn.close()


def execute(sql, params=()):
    # إن كنا داخل db_transaction استخدم نفس الاتصال بلا commit منفصل
    if _tx_conn is not None:
        cur = _tx_conn.execute(sql, params)
        return cur.lastrowid
    with closing(connect()) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid


def executemany(sql, seq):
    if _tx_conn is not None:
        _tx_conn.executemany(sql, seq)
        return
    with closing(connect()) as conn:
        conn.executemany(sql, seq)
        conn.commit()


def query_df(sql, params=()):
    # القراءات داخل المعاملة ترى البيانات غير المُثبتة بعد — نستخدم نفس الاتصال
    if _tx_conn is not None:
        return pd.read_sql_query(sql, _tx_conn, params=params)
    with closing(connect()) as conn:
        return pd.read_sql_query(sql, conn, params=params)


def init_db():
    with closing(connect()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT
            );
            CREATE TABLE IF NOT EXISTS accounts (
                code TEXT PRIMARY KEY, name TEXT, type TEXT
            );
            CREATE TABLE IF NOT EXISTS items (
                code TEXT PRIMARY KEY, name TEXT, coffee_type TEXT, family TEXT
            );
            CREATE TABLE IF NOT EXISTS movements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER, month INTEGER, mdate TEXT,
                item_code TEXT, item_name TEXT, coffee_type TEXT, family TEXT,
                qty REAL, value REAL, total REAL,
                movement_type TEXT, source TEXT, reference TEXT,
                contact TEXT, description TEXT, branch TEXT,
                revenue_account TEXT, inventory_account TEXT,
                created_at TEXT, created_by TEXT, from_report INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS item_balances (
                item_code TEXT PRIMARY KEY, item_name TEXT, coffee_type TEXT, family TEXT,
                qty REAL, value REAL, year INTEGER, month INTEGER,
                source_file TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS branch_sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER, month INTEGER, mdate TEXT,
                invoice TEXT, contact TEXT, reference TEXT,
                amount REAL, status TEXT, source TEXT, branch TEXT,
                source_file TEXT, imported_at TEXT
            );
            CREATE TABLE IF NOT EXISTS channel_sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER, month INTEGER, channel TEXT,
                amount REAL, note TEXT, created_at TEXT, created_by TEXT,
                UNIQUE(year, month, channel)
            );
            CREATE TABLE IF NOT EXISTS pnl_monthly (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER, month INTEGER,
                account_code TEXT, account_name TEXT, category TEXT,
                amount REAL, source_file TEXT, imported_at TEXT
            );
            CREATE TABLE IF NOT EXISTS pnl_branch (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER, branch TEXT,
                account_code TEXT, account_name TEXT, category TEXT,
                amount REAL, source_file TEXT, imported_at TEXT
            );
            CREATE TABLE IF NOT EXISTS inventory_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER, item_code TEXT, item_name TEXT, coffee_type TEXT,
                opening_value REAL, purchases REAL, cogs REAL, adjustments REAL,
                closing_value REAL, sales REAL,
                opening_qty REAL, qty_sold REAL, qty_adjusted REAL, closing_qty REAL,
                source_file TEXT, imported_at TEXT
            );
            CREATE TABLE IF NOT EXISTS fin_figures (
                year INTEGER PRIMARY KEY,
                current_assets REAL, inventory REAL, current_liabilities REAL,
                total_assets REAL, total_equity REAL,
                source_file TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS financial_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                statement TEXT, year INTEGER, month INTEGER,
                account_code TEXT, account_name TEXT, account_type TEXT,
                amount REAL
            );
            CREATE TABLE IF NOT EXISTS raw_statements (
                statement TEXT, year INTEGER, month INTEGER,
                payload TEXT, imported_at TEXT
            );
            CREATE TABLE IF NOT EXISTS budget (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER, month INTEGER,
                account_code TEXT, account_name TEXT, account_type TEXT,
                actual_prev REAL, growth REAL, proposed REAL, approved REAL
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT, severity TEXT, category TEXT,
                message TEXT, resolved INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, usr TEXT, action TEXT, details TEXT
            );
            CREATE TABLE IF NOT EXISTS foodics_branch_sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER, branch TEXT, branch_code TEXT,
                gross REAL, net REAL, tax REAL, discount REAL,
                orders INTEGER, avg_order REAL, visitors INTEGER,
                customers INTEGER, qty REAL,
                source_file TEXT, imported_at TEXT,
                UNIQUE(year, branch_code)
            );
            CREATE TABLE IF NOT EXISTS foodics_product_sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER, branch TEXT, branch_code TEXT,
                product TEXT, product_code TEXT,
                gross REAL, net REAL, tax REAL, discount REAL,
                qty REAL, cost REAL, returns_amount REAL, returns_qty REAL,
                source_file TEXT, imported_at TEXT
            );
            """
        )
        # --- فهارس للأعمدة كثيرة الاستعلام (المرحلة 1: تحسين الأداء بدون تغيير المنطق) ---
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_mov_year_month ON movements(year, month);
            CREATE INDEX IF NOT EXISTS idx_mov_item      ON movements(item_code);
            CREATE INDEX IF NOT EXISTS idx_mov_type      ON movements(movement_type);
            CREATE INDEX IF NOT EXISTS idx_pnlm_year_month ON pnl_monthly(year, month);
            CREATE INDEX IF NOT EXISTS idx_pnlm_cat      ON pnl_monthly(category);
            CREATE INDEX IF NOT EXISTS idx_pnlb_year     ON pnl_branch(year);
            CREATE INDEX IF NOT EXISTS idx_branch_year_month ON branch_sales(year, month);
            CREATE INDEX IF NOT EXISTS idx_branch_invoice   ON branch_sales(invoice);
            CREATE INDEX IF NOT EXISTS idx_channel_year_month ON channel_sales(year, month);
            CREATE INDEX IF NOT EXISTS idx_invsum_year_item  ON inventory_summary(year, item_code);
            CREATE INDEX IF NOT EXISTS idx_foodics_b_year ON foodics_branch_sales(year);
            CREATE INDEX IF NOT EXISTS idx_foodics_p_year ON foodics_product_sales(year);
            """
        )
        # ترحيل: إضافة العمود لقواعد البيانات القديمة (نقطة جديدة، غير مخلّة بالبنية)
        try:
            conn.execute("ALTER TABLE movements ADD COLUMN from_report INTEGER DEFAULT 0")
        except Exception:
            pass
        # ترحيل لمرة واحدة لميزة الأرصدة المعتمدة: تنظيف الحركات المستوردة القديمة
        # (كانت غير موسومة فتُحسب خطأً كإدخال يدوي)؛ يُعاد ملؤها عند أول استيراد.
        row = conn.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
        version = row[0] if row else "1"
        if str(version) < "2":
            conn.execute("DELETE FROM movements")
            conn.execute("DELETE FROM item_balances")
            conn.execute("DELETE FROM branch_sales")
            conn.execute(
                "INSERT INTO settings(key, value) VALUES('schema_version', '2') "
                "ON CONFLICT(key) DO UPDATE SET value='2'"
            )
        for key, val in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, val))
        conn.commit()


# ---- الإعدادات ----
def get_settings():
    df = query_df("SELECT key, value FROM settings")
    data = dict(zip(df["key"], df["value"])) if not df.empty else {}
    merged = dict(DEFAULT_SETTINGS)
    merged.update(data)
    return merged


def set_setting(key, value):
    execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def s_float(settings, key):
    try:
        return float(settings.get(key, DEFAULT_SETTINGS[key]))
    except Exception:
        return float(DEFAULT_SETTINGS[key])


def s_int(settings, key):
    try:
        return int(float(settings.get(key, DEFAULT_SETTINGS[key])))
    except Exception:
        return int(float(DEFAULT_SETTINGS[key]))


# ---- سجل التدقيق ----
def audit(action, details=""):
    execute(
        "INSERT INTO audit_log(ts, usr, action, details) VALUES(?, ?, ?, ?)",
        (now_str(), current_user(), action, details),
    )


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def current_user():
    try:
        return st.session_state.get("user_name", "admin")
    except Exception:
        return "admin"


# ---- الأصناف والحسابات ----
def upsert_item(code, name, coffee_type=None, family=None):
    code = (code or "").upper().strip()
    if not code:
        return
    if coffee_type is None or family is None:
        coffee_type, family = item_meta(code)
    execute(
        "INSERT INTO items(code, name, coffee_type, family) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(code) DO UPDATE SET name=excluded.name, "
        "coffee_type=excluded.coffee_type, family=excluded.family",
        (code, str(name or "").strip(), coffee_type, family),
    )


def upsert_account(code, name, acc_type=""):
    code = clean_code(code)
    if not code:
        return
    execute(
        "INSERT INTO accounts(code, name, type) VALUES(?, ?, ?) "
        "ON CONFLICT(code) DO UPDATE SET name=excluded.name, type=excluded.type",
        (code, str(name or "").strip(), str(acc_type or "").strip()),
    )


def list_items():
    return query_df("SELECT code, name, coffee_type, family FROM items ORDER BY code")


def list_accounts():
    return query_df("SELECT code, name, type FROM accounts ORDER BY code")


# ---- الحركات ----
MOVE_COLS = [
    "year", "month", "mdate", "item_code", "item_name", "coffee_type", "family",
    "qty", "value", "total", "movement_type", "source", "reference",
    "contact", "description", "branch", "revenue_account", "inventory_account",
    "created_at", "created_by",
]


def add_movement(**kw):
    code = (kw.get("item_code") or "").upper().strip()
    coffee_type, family = item_meta(code)
    row = {c: kw.get(c) for c in MOVE_COLS}
    row["item_code"] = code
    row["coffee_type"] = kw.get("coffee_type") or coffee_type
    row["family"] = kw.get("family") or family
    row["created_at"] = now_str()
    row["created_by"] = current_user()
    row["qty"] = float(kw.get("qty") or 0)
    row["value"] = float(kw.get("value") or 0)
    row["total"] = float(kw.get("total") or 0)
    execute(
        "INSERT INTO movements(" + ",".join(MOVE_COLS) + ") VALUES(" + ",".join(["?"] * len(MOVE_COLS)) + ")",
        tuple(row.get(c) for c in MOVE_COLS),
    )
    if row["item_name"]:
        upsert_item(code, row["item_name"], row["coffee_type"], row["family"])


def current_stock():
    """الرصيد = الرصيد الختامي المعتمد من تقرير حركة المخزون + الحركات اليدوية اللاحقة فقط.
    (لا نجمع حركات التقرير لأنها لا تطابق الرصيد الختامي؛ التقرير هو المرجع).
    يضم كل الاصناف المسجلة حتى التي لم يعثر لها رصيد ختامي (رصيدها = 0)."""
    bal = query_df("SELECT item_code, item_name, coffee_type, qty, value FROM item_balances")
    man = query_df("SELECT item_code, MAX(item_name) AS item_name, MAX(coffee_type) AS coffee_type, COALESCE(SUM(qty),0) AS qty, COALESCE(SUM(value),0) AS value FROM movements WHERE COALESCE(from_report,0)=0 AND item_code<>'' GROUP BY item_code")
    items = query_df("SELECT code AS item_code, name AS item_name FROM items")
    cols = ["كود الصنف", "اسم الصنف", "نوع البن", "الرصيد الحالي", "قيمة الرصيد"]
    if bal.empty and man.empty and items.empty:
        return pd.DataFrame(columns=cols)
    parts = []
    for d in (items, bal, man):
        if not d.empty and "item_code" in d.columns:
            parts.append(d[["item_code"]])
    base = pd.concat(parts, ignore_index=True).drop_duplicates(subset=["item_code"]).reset_index(drop=True) if parts else pd.DataFrame(columns=["item_code"])
    merged = pd.merge(base, items, on="item_code", how="left")
    merged = pd.merge(merged, bal, on="item_code", how="left", suffixes=("", "_b"))
    merged = pd.merge(merged, man, on="item_code", how="left", suffixes=("", "_m"))
    for c in ["qty", "qty_m", "value", "value_m"]:
        if c not in merged.columns:
            merged[c] = 0
        else:
            merged[c] = pd.to_numeric(merged[c], errors="coerce").fillna(0)
    for c in ["item_name", "item_name_b", "item_name_m", "coffee_type", "coffee_type_b", "coffee_type_m"]:
        if c not in merged.columns:
            merged[c] = ""
        else:
            merged[c] = merged[c].fillna("").astype(str).replace("nan","")
    def _coalesce(cols_list):
        s = None
        for col in cols_list:
            cur = merged[col].replace("", pd.NA)
            s = cur if s is None else s.fillna(cur)
        return s.fillna("") if s is not None else pd.Series("", index=merged.index)
    out = pd.DataFrame({
        "كود الصنف": merged["item_code"],
        "اسم الصنف": _coalesce(["item_name_b", "item_name_m", "item_name"]),
        "نوع البن": _coalesce(["coffee_type_b", "coffee_type_m", "coffee_type"]),
        "الرصيد الحالي": (merged["qty"].astype(float) + merged["qty_m"].astype(float)).round(2),
        "قيمة الرصيد": (merged["value"].astype(float) + merged["value_m"].astype(float)).round(2),
    })
    return out[out["كود الصنف"].astype(str) != ""].sort_values("كود الصنف").reset_index(drop=True)


def validate_calculations():
    """يتحقق من صحة الأرقام المعروضة مقارنة بمدخلات الإكسيل."""
    issues = []
    settings = get_settings()
    base_year = s_int(settings, "base_year")
    budget_year = s_int(settings, "budget_year")

    pnl_monthly = query_df(
        "SELECT account_code, account_name, category, COALESCE(SUM(amount),0) AS amt "
        "FROM pnl_monthly WHERE year=? GROUP BY account_code, account_name, category",
        (base_year,),
    )
    budget = query_df(
        "SELECT account_code, account_name, COALESCE(SUM(actual_prev),0) AS actual_prev "
        "FROM budget WHERE year=? GROUP BY account_code, account_name",
        (budget_year,),
    )
    if not pnl_monthly.empty and not budget.empty:
        merged = pnl_monthly.merge(budget, on=["account_code", "account_name"], how="inner")
        for _, r in merged.iterrows():
            diff = abs(float(r["amt"]) - float(r["actual_prev"]))
            if diff > 1.0:
                issues.append(
                    f"انحراف موازنة {r['account_code']} {r['account_name']}: "
                    f"الفعلي {r['amt']:,.2f} مقابل الموازنة {r['actual_prev']:,.2f} (فرق {diff:,.2f})"
                )

    for _, r in query_df(
        "SELECT item_code, item_name, qty, value FROM item_balances"
    ).iterrows():
        man = query_df(
            "SELECT COALESCE(SUM(qty),0) AS q, COALESCE(SUM(value),0) AS v "
            "FROM movements WHERE item_code=? AND COALESCE(from_report,0)=0",
            (r["item_code"],),
        )
        mq = float(man["q"].iloc[0]) if not man.empty else 0.0
        mv = float(man["v"].iloc[0]) if not man.empty else 0.0
        if abs(mq - float(r["qty"])) > 0.01 or abs(mv - float(r["value"])) > 0.01:
            issues.append(
                f"رصيد غير مطابق للصنف {r['item_code']} {r['item_name']}: "
                f"الرصيد المعتمد ({r['qty']:,.2f}, {r['value']:,.2f}) مقابل اليدوي ({mq:,.2f}, {mv:,.2f})"
            )

    return issues


def stock_of(item_code):
    b = query_df("SELECT COALESCE(qty,0) AS s FROM item_balances WHERE item_code=?", (item_code,))
    base = float(b["s"].iloc[0]) if not b.empty else 0.0
    m = query_df(
        "SELECT COALESCE(SUM(qty),0) AS s FROM movements "
        "WHERE item_code=? AND COALESCE(from_report,0)=0", (item_code,)
    )
    manual = float(m["s"].iloc[0]) if not m.empty else 0.0
    return base + manual


def movements_df(year=None):
    """يرجع الحركات بأعمدة عربية تتوافق مع دوال الحساب."""
    where, params = "", ()
    if year is not None:
        where, params = " WHERE year=?", (int(year),)
    sql = f"""
        SELECT year AS 'year', month AS 'month', mdate AS 'mdate',
        item_code AS 'item_code', item_name AS 'item_name', coffee_type AS 'coffee_type',
        family AS 'family', qty AS 'qty', value AS 'value',
        total AS 'total', movement_type AS 'movement_type', source AS 'source',
        reference AS 'reference', contact AS 'contact', description AS 'description',
        branch AS 'branch' FROM movements{where} ORDER BY id
    """
    df = query_df(sql, params)
    df.rename(columns=COLUMN_MAPPING, inplace=True)
    return df


def movement_years():
    df = query_df("SELECT DISTINCT year FROM movements WHERE year IS NOT NULL ORDER BY year")
    return [int(y) for y in df["year"].tolist()] if not df.empty else []


def movement_months(year):
    df = query_df(
        "SELECT DISTINCT month FROM movements WHERE year=? AND month IS NOT NULL ORDER BY month",
        (int(year),),
    )
    return [int(m) for m in df["month"].tolist()] if not df.empty else []


def movement_period_summary(year=None, month=None):
    """ملخّص حركة المخزون لكل صنف خلال فترة (سنة/شهر) بناءً على مدخلات الإكسيل:
    الوارد (كميات موجبة) / المنصرف (كميات سالبة) / صافي الحركة / قيمة الحركة."""
    where = ["item_code<>''"]
    params = []
    if year is not None:
        where.append("year=?")
        params.append(int(year))
    if month is not None:
        where.append("month=?")
        params.append(int(month))
    sql = (
        "SELECT item_code, MAX(item_name) AS item_name, MAX(coffee_type) AS coffee_type, "
        "COALESCE(SUM(CASE WHEN qty>0 THEN qty ELSE 0 END),0) AS incoming, "
        "COALESCE(SUM(CASE WHEN qty<0 THEN -qty ELSE 0 END),0) AS outgoing, "
        "COALESCE(SUM(qty),0) AS net, COALESCE(SUM(value),0) AS val "
        "FROM movements WHERE " + " AND ".join(where) + " GROUP BY item_code ORDER BY item_code"
    )
    df = query_df(sql, tuple(params))
    if df.empty:
        return df
    df = df.rename(columns={
        "item_code": "كود الصنف", "item_name": "اسم الصنف", "coffee_type": "نوع البن",
        "incoming": "الوارد", "outgoing": "المنصرف", "net": "صافي الحركة", "val": "قيمة الحركة",
    })
    return df.round({"الوارد": 2, "المنصرف": 2, "صافي الحركة": 2, "قيمة الحركة": 2})


# فئات الحركة المعروضة في البيان المجمّع للصنف (ترتيب منطقي: وارد ← تحميص ← منصرف ← تسوية)
STATEMENT_CATEGORIES = [
    ("شراء / وارد", OTHER),
    ("تحميص (دخول محمص)", ROASTED_IN),
    ("تحميص (خروج أخضر)", GREEN_OUT),
    ("بيع", SALE),
    ("صرف للفروع", BRANCH_OUT),
    ("عينات وهدايا", SAMPLE),
    ("فروقات جردية", ADJUST),
]


def item_movement_statement(year, month=None):
    """بيان مجمّع لكل صنف من سجل الحركات: مجموع الكمية لكل فئة (تحميص/بيع/شراء/فروقات جردية/
    صرف للفروع/عينات) + صافي الحركة، مفلتراً على مستوى العام والشهر."""
    where = ["item_code<>''", "year=?"]
    params = [int(year)]
    if month is not None:
        where.append("month=?")
        params.append(int(month))
    sel = ["item_code", "MAX(item_name) AS item_name"]
    cat_params = []
    for i, (_label, mt) in enumerate(STATEMENT_CATEGORIES):
        sel.append(f"COALESCE(SUM(CASE WHEN movement_type=? THEN qty ELSE 0 END),0) AS c{i}")
        cat_params.append(mt)
    sel.append("COALESCE(SUM(qty),0) AS net")
    sql = ("SELECT " + ", ".join(sel) + " FROM movements WHERE " + " AND ".join(where) +
           " GROUP BY item_code ORDER BY item_code")
    # ترتيب الوسائط: وسائط CASE في SELECT أولاً ثم وسائط WHERE
    df = query_df(sql, tuple(cat_params) + tuple(params))
    if df.empty:
        return df
    rename = {"item_code": "كود الصنف", "item_name": "اسم الصنف", "net": "صافي الحركة"}
    for i, (label, _mt) in enumerate(STATEMENT_CATEGORIES):
        rename[f"c{i}"] = label
    df = df.rename(columns=rename)
    cat_labels = [c[0] for c in STATEMENT_CATEGORIES]
    df = df[["كود الصنف", "اسم الصنف"] + cat_labels + ["صافي الحركة"]]
    df = df[df[cat_labels].abs().sum(axis=1) > 0].copy()  # أصناف لها حركة فقط
    df["_a"] = df[cat_labels].abs().sum(axis=1)
    df = df.sort_values("_a", ascending=False).drop(columns="_a").reset_index(drop=True)
    return df.round({c: 1 for c in cat_labels + ["صافي الحركة"]})


# ---- التنبيهات ----
def add_alert(severity, category, message):
    execute(
        "INSERT INTO alerts(created_at, severity, category, message, resolved) VALUES(?, ?, ?, ?, 0)",
        (now_str(), severity, category, message),
    )


def open_alerts_count():
    df = query_df("SELECT COUNT(*) AS c FROM alerts WHERE resolved=0")
    return int(df["c"].iloc[0]) if not df.empty else 0


# ==========================================================================
# دوال الحساب (منقولة ومكيّفة لتقرأ من قاعدة البيانات)
# ==========================================================================
def pnl_df():
    return query_df(
        "SELECT year AS 'السنة', month AS 'الشهر', account_code AS 'كود الحساب', "
        "account_name AS 'اسم الحساب', account_type AS 'نوع الحساب', amount AS 'المبلغ' "
        "FROM financial_lines WHERE statement='pnl'"
    )


def build_budget(pnl, base_year, growth, budget_year):
    if pnl.empty:
        return pd.DataFrame()
    result = pnl[pnl["السنة"] == base_year].groupby(
        ["الشهر", "كود الحساب", "اسم الحساب", "نوع الحساب"], as_index=False
    )["المبلغ"].sum()
    if result.empty:
        return result
    result["السنة"] = budget_year
    result["actual_prev"] = result["المبلغ"]
    result["growth"] = growth
    result["proposed"] = result["actual_prev"] * (1 + growth)
    result["approved"] = result["proposed"]
    return result


def build_forecast(pnl):
    if pnl.empty:
        return pd.DataFrame()
    rows = []
    for (code, name, atype), group in pnl.groupby(["كود الحساب", "اسم الحساب", "نوع الحساب"]):
        values = group.sort_values(["السنة", "الشهر"])["المبلغ"]
        rows.append({
            "كود الحساب": code,
            "اسم الحساب": name,
            "نوع الحساب": atype,
            "متوسط آخر 6 أشهر": round(values.tail(6).mean(), 2),
            "متوسط آخر 12 شهرًا": round(values.tail(12).mean(), 2),
            "توقع الشهر التالي": round(values.tail(6).mean(), 2),
        })
    return pd.DataFrame(rows)


def build_budget_from_monthly(base_year, growth, budget_year):
    """موازنة شهرية مبنية على الفعلي الشهري (pnl_monthly) لسنة الأساس:
    لكل حساب ولكل شهر: مقترح = فعلي نفس الشهر × (1 + النمو)."""
    df = query_df(
        "SELECT month, account_code, account_name, category, COALESCE(SUM(amount),0) AS amt "
        "FROM pnl_monthly WHERE year=? GROUP BY month, account_code, account_name, category",
        (int(base_year),),
    )
    if df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"month": "الشهر", "account_code": "كود الحساب",
                            "account_name": "اسم الحساب", "category": "نوع الحساب", "amt": "المبلغ"})
    df["السنة"] = int(budget_year)
    df["actual_prev"] = df["المبلغ"].astype(float)
    df["growth"] = float(growth)
    df["proposed"] = df["actual_prev"] * (1 + float(growth))
    df["approved"] = df["proposed"]
    return df


def build_forecast_monthly():
    """توقع لكل حساب = متوسط آخر 6/12 شهرًا من الفعلي الشهري عبر كل الأعوام."""
    df = query_df(
        "SELECT year, month, account_code, account_name, COALESCE(SUM(amount),0) AS amt "
        "FROM pnl_monthly GROUP BY year, month, account_code, account_name "
        "ORDER BY account_code, year, month"
    )
    if df.empty:
        return pd.DataFrame()
    rows = []
    for (code, name), g in df.groupby(["account_code", "account_name"]):
        vals = g.sort_values(["year", "month"])["amt"]
        rows.append({
            "كود الحساب": code, "اسم الحساب": name,
            "متوسط آخر 6 أشهر": round(float(vals.tail(6).mean()), 2),
            "متوسط آخر 12 شهرًا": round(float(vals.tail(12).mean()), 2),
            "توقع الشهر التالي": round(float(vals.tail(6).mean()), 2),
        })
    return pd.DataFrame(rows)


def _pnl_history():
    return query_df(
        "SELECT year, month, account_code, account_name, category, COALESCE(SUM(amount),0) AS amt "
        "FROM pnl_monthly GROUP BY year, month, account_code, account_name, category "
        "ORDER BY year, month"
    )


def _inventory_month_index():
    """مؤشر موسمية من حركات المخزون الفعلية (كمية موجبة لكل شهر)."""
    df = query_df(
        "SELECT month, COALESCE(SUM(CASE WHEN qty>0 THEN qty ELSE 0 END),0) AS v "
        "FROM movements WHERE item_code<>'' AND month IS NOT NULL GROUP BY month"
    )
    if df.empty:
        return {m: 1.0 for m in range(1, 13)}
    total = float(df["v"].sum()) or 1.0
    avg = total / max(len(df), 1)
    idx = {m: 1.0 for m in range(1, 13)}
    for _, r in df.iterrows():
        idx[int(r["month"])] = (float(r["v"]) / avg) if avg else 1.0
    return idx


def _linear_fit(values):
    n = len(values)
    if n < 2:
        return 0.0, values[-1] if values else 0.0
    xs = list(range(n))
    x_mean = sum(xs) / n
    y_mean = sum(values) / n
    denom = sum((x - x_mean) ** 2 for x in xs) or 1.0
    slope = sum((x - x_mean) * (values[i] - y_mean) for i, x in enumerate(xs)) / denom
    intercept = y_mean - slope * x_mean
    return slope, intercept


def forecast_trend_horizon(months_ahead=12, category=None, branch=None):
    """توقع اتجاهي من قائمة الدخل الشهرية، مضروبًا في موسمية المخزون الفعلية.
    إن وُجد فرع يُوزَّع التوقع بنسبة مساهمة الفرع من Compare_Branches."""
    df = _pnl_history()
    if df.empty:
        return pd.DataFrame()
    if category:
        df = df[df["category"] == category]
    series = (df.groupby(["year", "month"], as_index=False)["amt"].sum()
                .sort_values(["year", "month"]).reset_index(drop=True))
    if series.empty:
        return pd.DataFrame()
    values = series["amt"].astype(float).tolist()
    n = len(values)
    slope, intercept = _linear_fit(values)
    seasonal = {m: [] for m in range(1, 13)}
    for _, r in series.iterrows():
        seasonal[int(r["month"])].append(float(r["amt"]))
    overall = (sum(values) / n) if n else 0.0
    pnl_index = {m: ((sum(v) / len(v)) / overall if overall and v else 1.0) for m, v in seasonal.items()}
    inv_index = _inventory_month_index()
    index = {m: (0.6 * pnl_index.get(m, 1.0) + 0.4 * inv_index.get(m, 1.0)) for m in range(1, 13)}
    share = 1.0
    if branch:
        br = query_df(
            "SELECT COALESCE(SUM(amount),0) AS v FROM pnl_branch WHERE branch=?", (str(branch),)
        )
        tot = query_df("SELECT COALESCE(SUM(amount),0) AS v FROM pnl_branch")
        bval = float(br["v"].iloc[0]) if not br.empty else 0.0
        tval = float(tot["v"].iloc[0]) if not tot.empty else 0.0
        share = (bval / tval) if tval else 1.0
    last_y, last_m = int(series.iloc[-1]["year"]), int(series.iloc[-1]["month"])
    rows = []
    for i in range(1, int(months_ahead) + 1):
        m = ((last_m - 1 + i) % 12) + 1
        y = last_y + ((last_m - 1 + i) // 12)
        trend = intercept + slope * (n - 1 + i)
        pred = max(0.0, trend * index.get(m, 1.0) * share)
        rows.append({
            "السنة": y, "الشهر": m, "الشهر العربي": MONTH_NAMES_AR[m - 1],
            "التوقع الاتجاهي": round(pred, 2),
            "مؤشر الموسمية": round(index.get(m, 1.0), 3),
            "حصة الفرع": round(share, 4),
        })
    return pd.DataFrame(rows)


def budget_whatif(base_year, budget_year, growth_low, growth_base, growth_high, branch=None):
    """سيناريوهات من الفعلي الشهري، مع إيراد/تكلفة الفروع من Compare_Branches ومخزون Summary."""
    frames = []
    labels = [("متشائم", growth_low), ("أساسي", growth_base), ("متفائل", growth_high)]
    br = query_df(
        "SELECT category, COALESCE(SUM(amount),0) AS amt FROM pnl_branch WHERE year=? "
        + ("AND branch=? " if branch else "")
        + "GROUP BY category",
        (int(base_year), str(branch)) if branch else (int(base_year),),
    )
    inv = query_df(
        "SELECT COALESCE(SUM(sales),0) AS sales, COALESCE(SUM(cogs),0) AS cogs, "
        "COALESCE(SUM(closing_value),0) AS stock FROM inventory_summary WHERE year=?",
        (int(base_year),),
    )
    br_rev = float(br.loc[br["category"] == "revenue", "amt"].sum()) if not br.empty else 0.0
    br_cogs = float(br.loc[br["category"] == "cogs", "amt"].sum()) if not br.empty else 0.0
    br_exp = float(br.loc[br["category"] == "expense", "amt"].sum()) if not br.empty else 0.0
    inv_sales = float(inv["sales"].iloc[0]) if not inv.empty else 0.0
    inv_cogs = float(inv["cogs"].iloc[0]) if not inv.empty else 0.0
    inv_stock = float(inv["stock"].iloc[0]) if not inv.empty else 0.0
    for label, g in labels:
        df = build_budget_from_monthly(base_year, g, budget_year)
        if df.empty and br.empty:
            continue
        cat = df["نوع الحساب"].astype(str) if (df is not None and not df.empty and "نوع الحساب" in df.columns) else pd.Series(dtype=str)
        rev = float(df.loc[cat.isin(["revenue", "إيراد"]), "proposed"].sum()) if not df.empty else br_rev * (1 + g)
        cogs = float(df.loc[cat.isin(["cogs", "تكلفة مبيعات"]), "proposed"].sum()) if not df.empty else br_cogs * (1 + g)
        exp = float(df.loc[cat.isin(["expense", "مصروف"]), "proposed"].sum()) if not df.empty else br_exp * (1 + g)
        if br_rev:
            rev = br_rev * (1 + g)
        if br_cogs:
            cogs = br_cogs * (1 + g)
        if br_exp:
            exp = br_exp * (1 + g)
        frames.append({
            "السيناريو": label,
            "نسبة النمو": g,
            "إيراد متوقع": rev,
            "تكلفة متوقعة": cogs,
            "مصروف متوقع": exp,
            "مبيعات المخزون": inv_sales * (1 + g),
            "تكلفة مخزون": inv_cogs * (1 + g),
            "قيمة مخزون ختامي": inv_stock * (1 + g * 0.5),
            "إجمالي الموازنة": (rev + cogs + exp) if (rev or cogs or exp) else (float(df["proposed"].sum()) if not df.empty else 0.0),
        })
    return pd.DataFrame(frames)


def budget_variance(year, branch=None):
    """مقارنة الفعلي بالموازنة: شهري من pnl_monthly، وسنوي من Compare_Branches عند اختيار فرع."""
    if branch:
        actual = query_df(
            "SELECT 12 AS month, account_code, account_name, COALESCE(SUM(amount),0) AS actual "
            "FROM pnl_branch WHERE year=? AND branch=? GROUP BY account_code, account_name",
            (int(year), str(branch)),
        )
        bud = query_df(
            "SELECT account_code, account_name, COALESCE(SUM(approved),0) AS budgeted "
            "FROM budget WHERE year=? GROUP BY account_code, account_name",
            (int(year),),
        )
        if actual.empty or bud.empty:
            return pd.DataFrame()
        m = actual.merge(bud, on=["account_code", "account_name"], how="inner")
    else:
        actual = query_df(
            "SELECT month, account_code, account_name, COALESCE(SUM(amount),0) AS actual "
            "FROM pnl_monthly WHERE year=? GROUP BY month, account_code, account_name",
            (int(year),),
        )
        bud = query_df(
            "SELECT month, account_code, account_name, COALESCE(SUM(approved),0) AS budgeted "
            "FROM budget WHERE year=? GROUP BY month, account_code, account_name",
            (int(year),),
        )
        if actual.empty or bud.empty:
            return pd.DataFrame()
        m = actual.merge(bud, on=["month", "account_code", "account_name"], how="inner")
    m["variance"] = m["actual"] - m["budgeted"]
    m["var_pct"] = m.apply(
        lambda r: (r["variance"] / r["budgeted"]) if r["budgeted"] else 0.0, axis=1
    )
    m = m.rename(columns={
        "month": "الشهر", "account_code": "كود الحساب", "account_name": "اسم الحساب",
        "actual": "الفعلي", "budgeted": "الموازنة", "variance": "الانحراف", "var_pct": "نسبة الانحراف",
    })
    return m.round({"الفعلي": 2, "الموازنة": 2, "الانحراف": 2, "نسبة الانحراف": 4})


def raise_budget_variance_alerts(year, threshold=0.10):
    """ينشئ تنبيهات للبنود التي يتجاوز انحرافها العتبة ولم تُبلَّغ بعد."""
    var = budget_variance(year)
    if var.empty:
        return 0
    flagged = var[var["نسبة الانحراف"].abs() >= float(threshold)]
    created = 0
    for _, r in flagged.iterrows():
        msg = (f"انحراف موازنة عام {int(year)} / {MONTH_NAMES_AR[int(r['الشهر']) - 1]} — "
               f"{r['كود الحساب']} {r['اسم الحساب']}: "
               f"{r['نسبة الانحراف'] * 100:.1f}% (فعلي {r['الفعلي']:,.0f} مقابل موازنة {r['الموازنة']:,.0f})")
        exists = query_df(
            "SELECT id FROM alerts WHERE resolved=0 AND category=? AND message=?",
            ("موازنة", msg),
        )
        if exists.empty:
            add_alert("تحذير", "موازنة", msg)
            created += 1
    return created


def build_seasonal_budget(base_year, budget_year, annual_growth=0.0):
    """موازنة شهرية: مؤشر P&L × موسمية المخزون الفعلية لكل حساب."""
    hist = _pnl_history()
    if hist.empty:
        return pd.DataFrame()
    base = hist[hist["year"] == int(base_year)]
    if base.empty:
        return pd.DataFrame()
    inv_index = _inventory_month_index()
    rows = []
    for (code, name, cat), g in hist.groupby(["account_code", "account_name", "category"]):
        g = g.sort_values(["year", "month"])
        month_avg = g.groupby("month")["amt"].mean()
        overall = float(g["amt"].mean()) or 1.0
        base_g = base[(base["account_code"] == code) & (base["account_name"] == name)]
        for mo in range(1, 13):
            actual = float(base_g.loc[base_g["month"] == mo, "amt"].sum()) if not base_g.empty else 0.0
            pnl_idx = float(month_avg.get(mo, overall)) / overall if overall else 1.0
            idx = 0.6 * pnl_idx + 0.4 * inv_index.get(mo, 1.0)
            proposed = actual * (1 + float(annual_growth)) * idx
            rows.append({
                "السنة": int(budget_year), "الشهر": mo, "كود الحساب": code,
                "اسم الحساب": name, "نوع الحساب": cat, "المبلغ": actual,
                "actual_prev": actual, "growth": float(annual_growth),
                "seasonal_index": round(idx, 3), "proposed": proposed, "approved": proposed,
            })
    return pd.DataFrame(rows)


def list_pnl_branches():
    df = query_df("SELECT DISTINCT branch FROM pnl_branch WHERE branch IS NOT NULL ORDER BY branch")
    return df["branch"].astype(str).tolist() if not df.empty else []


def build_roasting(inventory, roast_yield, tolerance):
    if inventory.empty:
        return pd.DataFrame()
    green = inventory[inventory["نوع الحركة"] == GREEN_OUT]
    roasted = inventory[inventory["نوع الحركة"] == ROASTED_IN]
    if green.empty and roasted.empty:
        return pd.DataFrame()

    for df in (green, roasted):
        if "رقم العائلة" not in df.columns:
            df["رقم العائلة"] = df["كود الصنف"].astype(str).str[:3]
        if "حركة الكمية" not in df.columns and "الكمية" in df.columns:
            df["حركة الكمية"] = df["الكمية"]

    green_summary = green.groupby(["السنة", "الشهر", "رقم العائلة"], as_index=False)["حركة الكمية"].sum()
    green_summary = green_summary.rename(columns={"حركة الكمية": "كمية الأخضر المحولة"})
    green_summary["كمية الأخضر المحولة"] = green_summary["كمية الأخضر المحولة"].abs()

    roasted_summary = roasted.groupby(["السنة", "الشهر", "رقم العائلة"], as_index=False)["حركة الكمية"].sum()
    roasted_summary = roasted_summary.rename(columns={"حركة الكمية": "كمية المحمص الناتجة"})

    result = green_summary.merge(roasted_summary, on=["السنة", "الشهر", "رقم العائلة"], how="outer").fillna(0)
    if result.empty:
        return result

    names = inventory[["رقم العائلة", "اسم الصنف"]].copy()
    names["اسم الصنف"] = names["اسم الصنف"].astype(str).str.strip()
    names = names[names["اسم الصنف"] != ""].drop_duplicates("رقم العائلة").rename(columns={"اسم الصنف": "الصنف"})
    result = result.merge(names, on="رقم العائلة", how="left")

    result["الناتج المتوقع"] = result["كمية الأخضر المحولة"] * roast_yield
    result["فرق الكمية"] = result["كمية المحمص الناتجة"] - result["الناتج المتوقع"]
    result["فرق النسبة"] = result.apply(
        lambda r: r["فرق الكمية"] / r["الناتج المتوقع"] if r["الناتج المتوقع"] else 0, axis=1
    )
    result["الحالة"] = result["فرق النسبة"].map(
        lambda v: "مقبول" if abs(v) <= tolerance else "يحتاج مراجعة"
    )
    return result[[
        "السنة", "الشهر", "رقم العائلة", "الصنف", "كمية الأخضر المحولة",
        "كمية المحمص الناتجة", "الناتج المتوقع", "فرق الكمية", "فرق النسبة", "الحالة",
    ]]


def build_inventory_summary(inventory):
    if inventory.empty:
        return pd.DataFrame()
    # Ensure required columns exist (handle different naming conventions)
    cols = {}
    if "حركة الكمية" not in inventory.columns and "الكمية" in inventory.columns:
        cols["حركة الكمية"] = inventory["الكمية"]
    if "قيمة الحركة" not in inventory.columns and "القيمة" in inventory.columns:
        cols["قيمة الحركة"] = inventory["القيمة"]
    if "الإجمالي" not in inventory.columns and "الإجمالي" in inventory.columns:
        cols["الإجمالي"] = inventory["الإجمالي"]
    for c, v in cols.items():
        inventory[c] = v
    return inventory.groupby(
        ["السنة", "الشهر", "كود الصنف", "اسم الصنف", "نوع البن", "رقم العائلة", "نوع الحركة"],
        as_index=False,
    ).agg({"حركة الكمية": "sum", "قيمة الحركة": "sum", "الإجمالي": "sum"})


def branch_sales_df(year=None):
    where, params = "", ()
    if year is not None:
        where, params = " WHERE year=?", (int(year),)
    df = query_df(
        "SELECT year AS 'year', month AS 'month', mdate AS 'mdate', invoice AS 'invoice', "
        "contact AS 'contact', reference AS 'reference', amount AS 'amount', status AS 'status', "
        "branch AS 'branch' FROM branch_sales" + where + " ORDER BY mdate, invoice",
        params,
    )
    df.rename(columns=COLUMN_MAPPING, inplace=True)
    return df

def branch_sales_summary(year=None):
    df = branch_sales_df(year)
    if df.empty:
        return df, pd.DataFrame()
    by_branch = df.groupby("الفرع", as_index=False).agg(
        **{"عدد الفواتير": ("رقم الفاتورة", "count"), "إجمالي المبيعات": ("المبلغ", "sum")}
    ).sort_values("إجمالي المبيعات", ascending=False).reset_index(drop=True)
    return df, by_branch


def branch_sales_grouped(year, month=None):
    """مبيعات مجمّعة من تقارير الإكسيل حسب العميل (Contact) والفرع (Branches)،
    على أساس سنوي (كل الأشهر) أو شهري (شهر محدّد)."""
    params = [int(year)]
    where = "year=?"
    if month:
        where += " AND month=?"
        params.append(int(month))
    df = query_df(
        "SELECT contact AS 'contact', branch AS 'branch', COUNT(*) AS 'عدد الفواتير', "
        f"COALESCE(SUM(amount),0) AS 'إجمالي المبيعات' FROM branch_sales WHERE {where} "
        "GROUP BY contact, branch ORDER BY 4 DESC",
        tuple(params),
    )
    df.rename(columns=COLUMN_MAPPING, inplace=True)
    if not df.empty:
        df["العميل"] = df["العميل"].apply(
            lambda x: str(x).strip() if str(x).strip() and str(x).strip().lower() != "nan" else "(بدون عميل)")
        df["الفرع"] = df["الفرع"].apply(
            lambda x: str(x).strip() if str(x).strip() and str(x).strip().lower() != "nan" else "غير محدد")
    return df


# ---- مبيعات القنوات اليدوية (تطبيقات التوصيل + الموقع) ----
DELIVERY_CHANNELS = ["جاهز", "نوفمبر", "هنجرستيشن", "الموقع الإلكتروني", "أخرى"]


def upsert_channel_sale(year, month, channel, amount, note=""):
    execute(
        "INSERT INTO channel_sales(year, month, channel, amount, note, created_at, created_by) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(year, month, channel) DO UPDATE SET "
        "amount=excluded.amount, note=excluded.note, created_at=excluded.created_at",
        (int(year), int(month), channel, float(amount), note, now_str(), current_user()),
    )


def channel_sales_df(year=None):
    where, params = "", ()
    if year is not None:
        where, params = " WHERE year=?", (int(year),)
    df = query_df(
        "SELECT year AS 'year', month AS 'month', channel AS 'القناة', "
        "amount AS 'المبلغ', note AS 'ملاحظة' FROM channel_sales" + where + " ORDER BY year, month, channel",
        params,
    )
    df.rename(columns=COLUMN_MAPPING, inplace=True)
    return df
def foodics_branch_df(year=None):
    where, params = "", ()
    if year is not None:
        where, params = " WHERE year=?", (int(year),)
    df = query_df(
        "SELECT year AS 'year', branch AS 'الفرع', branch_code AS 'كود الفرع', "
        "gross AS 'إجمالي المبيعات', net AS 'صافي المبيعات', tax AS 'الضرائب', "
        "discount AS 'الخصم', orders AS 'عدد الطلبات', avg_order AS 'متوسط الطلب', "
        "visitors AS 'عدد الزوار', customers AS 'عدد العملاء', qty AS 'صافي الكمية' "
        "FROM foodics_branch_sales" + where + " ORDER BY net DESC",
        params,
    )
    df.rename(columns=COLUMN_MAPPING, inplace=True)
    return df


def foodics_product_df(year=None, branch=None):
    where, params = [], []
    if year is not None:
        where.append("year=?")
        params.append(int(year))
    if branch:
        where.append("branch=?")
        params.append(branch)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    df = query_df(
        "SELECT year AS 'year', branch AS 'الفرع', branch_code AS 'كود الفرع', "
        "product AS 'المنتج', product_code AS 'كود المنتج', "
        "gross AS 'إجمالي المبيعات', net AS 'صافي المبيعات', tax AS 'الضرائب', "
        "discount AS 'الخصم', qty AS 'الكمية', cost AS 'التكلفة', "
        "returns_amount AS 'مبلغ الإرجاع', returns_qty AS 'كمية المرتجع' "
        "FROM foodics_product_sales" + clause + " ORDER BY net DESC",
        tuple(params),
    )
    df.rename(columns=COLUMN_MAPPING, inplace=True)
    return df


def foodics_years():
    years = set()
    for sql in (
        "SELECT DISTINCT year FROM foodics_branch_sales",
        "SELECT DISTINCT year FROM foodics_product_sales",
    ):
        df = query_df(sql)
        if not df.empty:
            years.update(int(y) for y in df["year"].dropna().tolist())
    return sorted(years)


def foodics_product_ranked(year, branch=None, top_n=20, worst=False):
    df = foodics_product_df(year, branch)
    if df.empty:
        return df
    g = df.groupby(["المنتج", "كود المنتج"], as_index=False).agg(
        **{
            "صافي المبيعات": ("صافي المبيعات", "sum"),
            "الكمية": ("الكمية", "sum"),
            "التكلفة": ("التكلفة", "sum"),
            "الخصم": ("الخصم", "sum"),
            "مبلغ الإرجاع": ("مبلغ الإرجاع", "sum"),
        }
    )
    g["هامش الربح"] = g["صافي المبيعات"] - g["التكلفة"]
    g["هامش %"] = g.apply(
        lambda r: (r["هامش الربح"] / r["صافي المبيعات"] * 100) if r["صافي المبيعات"] else 0.0,
        axis=1,
    )
    g = g.sort_values("صافي المبيعات", ascending=worst).reset_index(drop=True)
    if top_n:
        g = g.head(int(top_n))
    return g.round({
        "صافي المبيعات": 2, "الكمية": 1, "التكلفة": 2,
        "الخصم": 2, "مبلغ الإرجاع": 2, "هامش الربح": 2, "هامش %": 1,
    })


def low_stock_items(threshold=None):
    stock = current_stock()
    if stock.empty:
        return stock
    if threshold is None:
        threshold = s_float(get_settings(), "low_stock_qty")
    return stock[(stock["الرصيد الحالي"] > 0) & (stock["الرصيد الحالي"] <= float(threshold))]


def stagnant_items_df(days_threshold=60, stock=None):
    """تقرير الأصناف الراكدة: أصناف برصيد موجب لم تسجل حركة خروج/بيع منذ days_threshold يوماً."""
    if stock is None:
        stock = current_stock()
    if stock.empty:
        return pd.DataFrame()

    pos = stock[stock["الرصيد الحالي"] > 0].copy()
    if pos.empty:
        return pd.DataFrame()

    moves = query_df("""
        SELECT 
            item_code, 
            MAX(mdate) AS last_out_date,
            MAX(CASE WHEN movement_type = 'بيع' THEN mdate ELSE NULL END) AS last_sale_date
        FROM movements
        WHERE SUBSTR(mdate, 1, 2) = '20' AND length(mdate) = 10 AND item_code != ''
          AND (qty < 0 OR movement_type IN ('بيع', 'صرف محمص للفروع', 'خروج أخضر للتحميص', 'عينات وهدايا'))
        GROUP BY item_code
    """)

    m = pd.merge(pos, moves, left_on="كود الصنف", right_on="item_code", how="left")
    ref_date = date.today()

    def calc_days(d_str):
        if not d_str or pd.isna(d_str):
            return 999
        try:
            d = datetime.strptime(str(d_str)[:10], "%Y-%m-%d").date()
            return (ref_date - d).days
        except Exception:
            return 999

    m["أيام بدون حركة"] = m["last_out_date"].apply(calc_days)
    m["تاريخ آخر حركة خروج"] = m["last_out_date"].fillna("لا توجد حركة مسجلة")
    m["تاريخ آخر بيع"] = m["last_sale_date"].fillna("لا يوجد بيع مسجل")

    filtered = m[m["أيام بدون حركة"] >= days_threshold].copy()
    if filtered.empty:
        return pd.DataFrame()

    def get_status(d):
        if d >= 180:
            return "ركود حرج (أكثر من 6 أشهر)"
        elif d >= 90:
            return "راكد جدًا (3-6 أشهر)"
        elif d >= 60:
            return "راكد (2-3 أشهر)"
        else:
            return "بطيء الحركة (شهر - شهرين)"

    filtered["درجة الركود"] = filtered["أيام بدون حركة"].apply(get_status)
    filtered = filtered.sort_values("أيام بدون حركة", ascending=False).reset_index(drop=True)

    cols = [
        "كود الصنف", "اسم الصنف", "نوع البن", "الرصيد الحالي",
        "قيمة الرصيد", "تاريخ آخر حركة خروج", "تاريخ آخر بيع",
        "أيام بدون حركة", "درجة الركود"
    ]
    return filtered[cols]


def refresh_stock_alerts(threshold=None):
    """ينشئ تنبيهات للمخزون المنخفض والسالب إن لم تكن مفتوحة بالفعل."""
    if threshold is None:
        threshold = s_float(get_settings(), "low_stock_qty")
    stock = current_stock()
    if stock.empty:
        return 0
    open_msgs = set()
    existing = query_df("SELECT message FROM alerts WHERE resolved=0 AND category='مخزون'")
    if not existing.empty:
        open_msgs = set(existing["message"].astype(str).tolist())
    n = 0
    neg = stock[stock["الرصيد الحالي"] < 0]
    for _, r in neg.iterrows():
        msg = f"الصنف {r['كود الصنف']} رصيده سالب ({r['الرصيد الحالي']:g})."
        if msg not in open_msgs:
            add_alert("تحذير", "مخزون", msg)
            n += 1
    low = stock[(stock["الرصيد الحالي"] > 0) & (stock["الرصيد الحالي"] <= float(threshold))]
    for _, r in low.iterrows():
        msg = (
            f"مخزون منخفض: {r['كود الصنف']} — {r['اسم الصنف']} "
            f"رصيد {r['الرصيد الحالي']:g} (الحد {threshold:g})."
        )
        if msg not in open_msgs:
            add_alert("تنبيه", "مخزون", msg)
            n += 1
    return n


def list_years():
    years = set()
    for sql in (
        "SELECT DISTINCT year FROM pnl_monthly",
        "SELECT DISTINCT year FROM branch_sales",
        "SELECT DISTINCT year FROM channel_sales",
        "SELECT DISTINCT year FROM fin_figures",
        "SELECT DISTINCT year FROM foodics_branch_sales",
        "SELECT DISTINCT year FROM foodics_product_sales",
        "SELECT DISTINCT year FROM movements",
        "SELECT DISTINCT year FROM budget",
    ):
        df = query_df(sql)
        if not df.empty:
            years.update(int(y) for y in df["year"].dropna().tolist())
    return sorted(years)


def monthly_sales_series(year):
    """مبيعات كل شهر (1..12) = فواتير الفروع + مبيعات القنوات اليدوية."""
    s = pd.Series(0.0, index=range(1, 13))
    inv = query_df(
        "SELECT month, COALESCE(SUM(amount),0) AS v FROM branch_sales WHERE year=? GROUP BY month",
        (int(year),),
    )
    for _, r in inv.iterrows():
        m = int(r["month"]) if pd.notna(r["month"]) else 0
        if 1 <= m <= 12:
            s[m] += float(r["v"])
    ch = query_df(
        "SELECT month, COALESCE(SUM(amount),0) AS v FROM channel_sales WHERE year=? GROUP BY month",
        (int(year),),
    )
    for _, r in ch.iterrows():
        m = int(r["month"]) if pd.notna(r["month"]) else 0
        if 1 <= m <= 12:
            s[m] += float(r["v"])
    return s


def monthly_pnl_series(year, category):
    s = pd.Series(0.0, index=range(1, 13))
    df = query_df(
        "SELECT month, COALESCE(SUM(amount),0) AS v FROM pnl_monthly "
        "WHERE year=? AND category=? GROUP BY month", (int(year), category),
    )
    for _, r in df.iterrows():
        m = int(r["month"]) if pd.notna(r["month"]) else 0
        if 1 <= m <= 12:
            s[m] = float(r["v"])
    return s


def pnl_year_summary(year):
    df = query_df(
        "SELECT category, COALESCE(SUM(amount),0) AS v FROM pnl_monthly WHERE year=? GROUP BY category",
        (int(year),),
    )
    d = {row["category"]: float(row["v"]) for _, row in df.iterrows()}
    revenue = d.get("revenue", 0.0)
    cogs = d.get("cogs", 0.0)
    expense = d.get("expense", 0.0)
    other = d.get("other_income", 0.0)
    gross = revenue - cogs
    net = gross - expense + other
    return {
        "الإيرادات": revenue, "تكلفة المبيعات": cogs, "مجمل الربح": gross,
        "المصاريف التشغيلية": expense, "إيرادات أخرى": other, "صافي الربح": net,
        "هامش مجمل الربح %": (gross / revenue * 100) if revenue else 0.0,
        "هامش صافي الربح %": (net / revenue * 100) if revenue else 0.0,
    }


def stock_value_total():
    df = query_df("SELECT COALESCE(SUM(value),0) AS v FROM item_balances")
    return float(df["v"].iloc[0]) if not df.empty else 0.0


def financial_ratios(year):
    """نِسب مالية لسنة: السيولة، السريعة، دوران المخزون، الهوامش، العائد."""
    fig = query_df(
        "SELECT current_assets, inventory, current_liabilities, total_assets, total_equity "
        "FROM fin_figures WHERE year=?", (int(year),),
    )
    ca = cl = inv = ta = te = 0.0
    if not fig.empty:
        ca = float(fig["current_assets"].iloc[0] or 0)
        inv = float(fig["inventory"].iloc[0] or 0)
        cl = float(fig["current_liabilities"].iloc[0] or 0)
        ta = float(fig["total_assets"].iloc[0] or 0)
        te = float(fig["total_equity"].iloc[0] or 0)
    if inv <= 0:
        inv = stock_value_total()
    pnl = pnl_year_summary(year)
    cogs, net, rev = pnl["تكلفة المبيعات"], pnl["صافي الربح"], pnl["الإيرادات"]
    turnover = (cogs / inv) if inv else 0.0
    ratios = {
        "النسبة الجارية (السيولة)": (ca / cl) if cl else 0.0,
        "نسبة السيولة السريعة": ((ca - inv) / cl) if cl else 0.0,
        "معدل دوران المخزون (مرة)": turnover,
        "متوسط فترة بقاء المخزون (يوم)": (365 / turnover) if turnover else 0.0,
        "هامش مجمل الربح %": pnl["هامش مجمل الربح %"],
        "هامش صافي الربح %": pnl["هامش صافي الربح %"],
        "العائد على الأصول % (ROA)": (net / ta * 100) if ta else 0.0,
        "العائد على حقوق الملكية % (ROE)": (net / te * 100) if te else 0.0,
    }
    figures = {"الأصول المتداولة": ca, "المخزون": inv, "الخصوم المتداولة": cl,
               "إجمالي الأصول": ta, "حقوق الملكية": te}
    return ratios, figures


# ==========================================================================
# الاستيراد من ملفاتك الحالية (منقول من السكربت الأصلي)
# ==========================================================================
def _read_chart_table(path):
    """يكتشف صف العناوين ديناميكيًا ويطابق الأعمدة بالاسم الدقيق
    (يعالج خطأ دليل الحسابات: العناوين ليست في الصف الأول + تطابق Account مع Account Code)."""
    try:
        raw = pd.read_excel(path, sheet_name=0, header=None, dtype=object)
    except Exception:
        return None
    if raw.empty:
        return None
    header_idx = None
    for i in range(len(raw)):
        cells = [normalize(c) for c in raw.iloc[i].tolist()]
        if "account code" in cells or "كود الحساب" in cells or "رقم الحساب" in cells:
            header_idx = i
            break
    if header_idx is None:
        return None
    headers = [str(c).strip() for c in raw.iloc[header_idx].tolist()]
    body = raw.iloc[header_idx + 1:].copy()
    body.columns = headers

    def pick(exact):
        for col in headers:
            if normalize(col) in exact:
                return col
        return None

    code_col = pick(["account code", "رقم الحساب", "كود الحساب"])
    name_col = pick(["account", "account name", "اسم الحساب"])
    type_col = pick(["account type", "نوع الحساب"])
    if code_col is None or name_col is None:
        return None
    chart = pd.DataFrame({
        "كود الحساب": body[code_col].map(clean_code),
        "اسم الحساب": body[name_col].astype(str).str.strip(),
        "نوع الحساب": body[type_col].astype(str).str.strip() if type_col else "",
    })
    chart = chart[(chart["كود الحساب"] != "") & (chart["كود الحساب"].str.lower() != "nan")]
    return chart.drop_duplicates("كود الحساب")


def load_chart_of_accounts():
    files = list(STATIC_DIR.glob("*.xlsx")) + list(BASE_DIR.glob("*.xlsx"))
    for path in files:
        name = path.name.lower()
        if "chart" not in name and "account" not in name:
            continue
        chart = _read_chart_table(path)
        if chart is not None and not chart.empty:
            return chart, path.name
    return pd.DataFrame(columns=["كود الحساب", "اسم الحساب", "نوع الحساب"]), None


def attach_accounts(data, chart, code_column=None, name_column=None):
    output = data.copy()
    chart_by_code, chart_by_name = {}, {}
    for _, row in chart.iterrows():
        rec = {
            "كود الحساب": clean_code(row["كود الحساب"]),
            "اسم الحساب": str(row["اسم الحساب"]).strip(),
            "نوع الحساب": str(row["نوع الحساب"]).strip(),
        }
        chart_by_code[rec["كود الحساب"]] = rec
        chart_by_name[normalize(rec["اسم الحساب"])] = rec

    if code_column and code_column in output.columns:
        output["كود الحساب"] = output[code_column].map(clean_code)
    else:
        output["كود الحساب"] = ""
    if name_column and name_column in output.columns:
        source_names = output[name_column].astype(str).str.strip()
    else:
        source_names = pd.Series([""] * len(output), index=output.index)

    output["اسم الحساب"] = ""
    output["نوع الحساب"] = ""
    for index in output.index:
        code = clean_code(output.at[index, "كود الحساب"])
        source_name = str(source_names.loc[index]).strip()
        matched = chart_by_code.get(code)
        if matched is None and source_name:
            matched = chart_by_name.get(normalize(source_name))
        if matched is None and source_name:
            key = normalize(source_name)
            for cname, crec in chart_by_name.items():
                if key in cname or cname in key:
                    matched = crec
                    break
        if matched is not None:
            output.at[index, "كود الحساب"] = matched["كود الحساب"]
            output.at[index, "اسم الحساب"] = matched["اسم الحساب"]
            output.at[index, "نوع الحساب"] = matched["نوع الحساب"]
        else:
            output.at[index, "اسم الحساب"] = source_name
    return output


def extract_year_month(folder):
    match = re.search(r"(20\d{2})[-_](0?[1-9]|1[0-2])", str(folder))
    if match:
        return int(match.group(1)), int(match.group(2))
    return detect_file_period(folder, default_month=12)


def parse_profit_and_loss(data, year, chart):
    if data.empty:
        return pd.DataFrame()
    account_column = find_column(data.columns, ["account", "account name", "اسم الحساب"]) or data.columns[0]
    rows = []
    for _, row in data.iterrows():
        account_text = str(row.get(account_column, "")).strip()
        if not account_text:
            continue
        if normalize(account_text).startswith((
            "total", "gross profit", "net profit", "trading income", "cost of sales",
            "operating expenses", "other income", "إجمالي", "صافي",
        )):
            continue
        match = re.match(r"^\s*(\d+)\s*[-–]\s*(.*)$", account_text)
        source_code = match.group(1).strip() if match else ""
        source_name = match.group(2).strip() if match else account_text
        amount = sum(number(row.get(c, 0)) for c in data.columns if c != account_column)
        if amount:
            rows.append({
                "السنة": year, "الشهر": 12, "كود_مصدر": source_code,
                "اسم_مصدر": source_name, "المبلغ": amount,
            })
    result = pd.DataFrame(rows)
    if result.empty:
        return result
    result = attach_accounts(result, chart, "كود_مصدر", "اسم_مصدر")
    return result[["السنة", "الشهر", "كود الحساب", "اسم الحساب", "نوع الحساب", "المبلغ"]]


def parse_inventory(path, year, month):
    """يقرأ تقرير حركة الصنف (Xero) من CSV ويعيد (حركات, أرصدة ختامية).

    - صفوف Opening/Closing Balance تُستبعد من الحركات
    - صف Closing Balance يُستخرج كرصيد ختامي معتمد (qty=QoH, value=Value Movement)
      كما في صيغة Excel — لا يُحسب بالـ sum التقريبي
    - صفوف Total الفرعية تُستبعد
    """
    rows = read_csv_rows(path)
    if not rows:
        return pd.DataFrame(), pd.DataFrame()
    header_index = None
    for index, row in enumerate(rows):
        text = normalize(" ".join(row))
        if "date" in text and "qoh movement" in text:
            header_index = index
            break
    if header_index is None:
        return pd.DataFrame(), pd.DataFrame()

    headers = rows[header_index]
    records, balances = [], []
    current_code, current_name = "", ""
    for row in rows[header_index + 1:]:
        if len(row) < len(headers):
            row += [""] * (len(headers) - len(row))
        values = dict(zip(headers, row[:len(headers)]))
        first_cell = str(row[0]).strip() if row else ""
        item_match = re.match(r"^\s*((?:GB|RB)[A-Z0-9]*)\s*[-–]\s*(.*)", first_cell, re.IGNORECASE)
        if item_match:
            current_code = item_match.group(1).upper()
            current_name = item_match.group(2).strip()
            continue
        date_raw = str(values.get("Date", "")).strip()
        if not date_raw:
            continue
        low = normalize(date_raw)
        # استبعاد صفوف التجميع الفرعية (Total وحده أو Total <صنف>) قبل معالجته كحركة
        if low in _SUBTOTAL_MARKERS or low.startswith(tuple(m + " " for m in _SUBTOTAL_MARKERS)):
            continue
        # صفوف الأرصدة: Opening/Closing Balance — Closing هو المرجع المعتمد
        if low in ("opening balance", "closing balance") or low.startswith("closing bal"):
            if "closing" in low and current_code:
                balances.append({
                    "item_code": current_code, "item_name": current_name,
                    "qty": number(values.get("QoH Movement", 0)),
                    "value": number(values.get("Value Movement", 0)),
                    "year": year, "month": month, "source_file": Path(path).name,
                })
            continue
        text = normalize(
            f"{values.get('Source', '')} {values.get('Reference', '')} "
            f"{values.get('Contact', '')} {values.get('Description', '')}"
        )
        if "امر تحميص مستودع البن الاخضر" in text:
            mtype = GREEN_OUT
        elif "تحميص وارد لمستودع البن المحمص" in text:
            mtype = ROASTED_IN
        elif "سند صرف قهوه محموصه فرع" in text:
            mtype = BRANCH_OUT
        elif "عينات القهوة المحمصة" in text:
            mtype = SAMPLE
        elif "receivable invoice" in text:
            mtype = SALE
        elif any(w in text for w in ["adjustment", "جرد", "revaluation", "تعديل"]):
            mtype = ADJUST
        else:
            mtype = OTHER
        coffee_type, family = item_meta(current_code)
        rec_date = values.get("Date", "")
        parsed = parse_cell_date(rec_date)
        if parsed is not None:
            rec_year, rec_month, rec_mdate = parsed.year, parsed.month, parsed.strftime("%Y-%m-%d")
        else:
            rec_year, rec_month = year_month_from_value(rec_date, year, month)
            rec_mdate = str(rec_date)
        records.append({
            "year": rec_year or year, "month": rec_month or month, "mdate": rec_mdate,
            "item_code": current_code, "item_name": current_name,
            "coffee_type": coffee_type, "family": family,
            "qty": number(values.get("QoH Movement", 0)),
            "value": number(values.get("Value Movement", 0)),
            "total": number(values.get("Total", 0)),
            "movement_type": mtype, "source": values.get("Source", ""),
            "reference": values.get("Reference", ""), "contact": values.get("Contact", ""),
            "description": values.get("Description", ""), "branch": "",
            "revenue_account": values.get("Revenue Account", ""),
            "inventory_account": values.get("Inventory Account", ""),
        })
    return pd.DataFrame(records), pd.DataFrame(balances)


def insert_financial(df, statement):
    if df.empty:
        return 0
    seq = [
        (statement, int(r["السنة"]), int(r["الشهر"]), str(r["كود الحساب"]),
         str(r["اسم الحساب"]), str(r["نوع الحساب"]), float(r["المبلغ"]))
        for _, r in df.iterrows()
    ]
    executemany(
        "INSERT INTO financial_lines(statement, year, month, account_code, account_name, account_type, amount) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        seq,
    )
    return len(seq)


def insert_movements(df, from_report=0):
    if df.empty:
        return 0
    cols = MOVE_COLS + ["from_report"]
    seq = []
    for _, r in df.iterrows():
        d = {c: r.get(c) for c in MOVE_COLS}
        d["created_at"] = now_str()
        d["created_by"] = current_user()
        d["from_report"] = int(from_report)
        seq.append(tuple(d.get(c) for c in cols))
    executemany(
        "INSERT INTO movements(" + ",".join(cols) + ") VALUES(" + ",".join(["?"] * len(cols)) + ")",
        seq,
    )
    # تحديث كتالوج الأصناف
    for code, name in df[["item_code", "item_name"]].drop_duplicates().values:
        upsert_item(code, name)
    return len(seq)


def upsert_balance(rec):
    """تخزين الرصيد الختامي المعتمد من التقرير (نقطة جديدة)."""
    code = (rec.get("item_code") or "").upper().strip()
    if not code:
        return
    coffee_type, family = item_meta(code)
    execute(
        "INSERT INTO item_balances(item_code, item_name, coffee_type, family, qty, value, "
        "year, month, source_file, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(item_code) DO UPDATE SET item_name=excluded.item_name, "
        "coffee_type=excluded.coffee_type, family=excluded.family, qty=excluded.qty, "
        "value=excluded.value, year=excluded.year, month=excluded.month, "
        "source_file=excluded.source_file, updated_at=excluded.updated_at",
        (code, rec.get("item_name", ""), coffee_type, family,
         float(rec.get("qty") or 0), float(rec.get("value") or 0),
         rec.get("year"), rec.get("month"), rec.get("source_file", ""), now_str()),
    )
    upsert_item(code, rec.get("item_name", ""), coffee_type, family)


def insert_branch_sales(df, source_file=""):
    if df.empty:
        return 0
    seq = [
        (int(r["year"]), int(r["month"]), r["mdate"], r["invoice"], r["contact"],
         r["reference"], float(r["amount"]), r["status"], r["source"], r["branch"],
         source_file, now_str())
        for _, r in df.iterrows()
    ]
    executemany(
        "INSERT INTO branch_sales(year, month, mdate, invoice, contact, reference, "
        "amount, status, source, branch, source_file, imported_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", seq,
    )
    return len(seq)


def parse_inventory_xlsx(path, year, month):
    """يقرأ تقرير حركة الصنف (Xero) من ملف Excel ويعيد (حركات, أرصدة ختامية).
    الرصيد الختامي هو المرجع المعتمد (صف Closing Balance: الكمية من QoH والقيمة من Value Movement)."""
    try:
        raw = pd.read_excel(path, sheet_name=0, header=None, dtype=object)
    except Exception as exc:
        import logging as _logging
        _logging.warning("parse_inventory_xlsx فشل %s: %s", path, exc)
        return pd.DataFrame(), pd.DataFrame()
    if raw.empty:
        return pd.DataFrame(), pd.DataFrame()
    header_idx = None
    for i in range(min(15, len(raw))):
        cells = [normalize(c) for c in raw.iloc[i].tolist()]
        if "date" in cells and any("qoh" in c for c in cells):
            header_idx = i
            break
    if header_idx is None:
        return pd.DataFrame(), pd.DataFrame()

    hdr = [normalize(c) for c in raw.iloc[header_idx].tolist()]

    def col_of(*keys):
        for j, h in enumerate(hdr):
            if any(k in h for k in keys):
                return j
        return None

    c_date = col_of("date") or 0
    c_source = col_of("source")
    c_ref = col_of("reference")
    c_contact = col_of("contact")
    c_desc = col_of("description")
    c_total = col_of("total")
    c_value = col_of("value movement")
    c_qoh = col_of("qoh")

    # صفوف التجميع الفرعية: "Total" وحده أو "Total <اسم الصنف>" — العمود Date
    # يحمل التسمية بدل التاريخ، فلو لم نستبعدها تُستورد كحركة وهمية.
    subtotal_markers = _SUBTOTAL_MARKERS

    def is_subtotal_row(label):
        low = normalize(label)
        if not low:
            return False
        return low in subtotal_markers or low.startswith(
            tuple(m + " " for m in subtotal_markers)
        )

    code_re = re.compile(r"^\s*((?:GB|RB)[A-Z0-9]*)\s*[-–]\s*(.*)", re.IGNORECASE)
    moves, balances = [], []
    cur_code, cur_name = "", ""

    def cell(row, idx):
        return row[idx] if idx is not None and idx < len(row) else ""

    for i in range(header_idx + 1, len(raw)):
        row = raw.iloc[i].tolist()
        c0 = str(cell(row, 0)).strip()
        low = normalize(c0)
        m = code_re.match(c0)
        if m:
            cur_code = m.group(1).upper()
            cur_name = m.group(2).strip()
            continue
        if is_subtotal_row(c0):
            continue
        if low in ("opening balance", "closing balance") or low.startswith("closing bal"):
            if "closing" in low and cur_code:
                balances.append({
                    "item_code": cur_code, "item_name": cur_name,
                    "qty": number(cell(row, c_qoh)), "value": number(cell(row, c_value)),
                    "year": year, "month": month, "source_file": Path(path).name,
                })
            continue
        raw_date = cell(row, c_date)
        parsed = parse_cell_date(raw_date)
        if parsed is None:
            date_val = str(raw_date).strip()
            if not date_val or date_val.lower() == "nan":
                continue
            # صف يحمل تسمية قسم/مجموعة في عمود التاريخ (لا تاريخًا فعليًا):
            # يُستبعد إن لم يحمل أي كمية أو قيمة، وإلا لظهر كحركة بتاريخ مثل «Gift Cards».
            if not any(number(cell(row, j)) for j in (c_qoh, c_value, c_total)):
                continue
            row_year, row_month = year, month
        else:
            date_val = parsed.strftime("%Y-%m-%d")
            row_year, row_month = parsed.year, parsed.month
        if not cur_code:
            continue
        text = normalize(
            f"{cell(row, c_source)} {cell(row, c_ref)} {cell(row, c_contact)} {cell(row, c_desc)}"
        )
        if "امر تحميص مستودع البن الاخضر" in text:
            mtype = GREEN_OUT
        elif "تحميص وارد لمستودع البن المحمص" in text:
            mtype = ROASTED_IN
        elif "سند صرف قهوه محموصه فرع" in text:
            mtype = BRANCH_OUT
        elif "عينات القهوة المحمصة" in text:
            mtype = SAMPLE
        elif "receivable invoice" in text:
            mtype = SALE
        elif any(w in text for w in ["adjustment", "جرد", "revaluation", "تعديل"]):
            mtype = ADJUST
        else:
            mtype = OTHER
        coffee_type, family = item_meta(cur_code)
        moves.append({
            "year": row_year, "month": row_month, "mdate": date_val[:10],
            "item_code": cur_code, "item_name": cur_name,
            "coffee_type": coffee_type, "family": family,
            "qty": number(cell(row, c_qoh)), "value": number(cell(row, c_value)),
            "total": number(cell(row, c_total)),
            "movement_type": mtype, "source": str(cell(row, c_source)),
            "reference": str(cell(row, c_ref)), "contact": str(cell(row, c_contact)),
            "description": str(cell(row, c_desc)), "branch": "",
            "revenue_account": "", "inventory_account": "",
        })
    return pd.DataFrame(moves), pd.DataFrame(balances)


def parse_daily_sales(path, year, month):
    """يقرأ تقرير المبيعات اليومية لكل فرع (عمود Branches مقصود ويُوضَّح في التقرير)."""
    try:
        raw = pd.read_excel(path, sheet_name=0, header=None, dtype=object)
    except Exception as _exc:
        import logging as _logging
        _logging.warning("parse_daily_sales فشل %s: %s", path, _exc)
        return pd.DataFrame()
    if raw.empty:
        return pd.DataFrame()
    header_idx = None
    for i in range(min(20, len(raw))):
        cells = [normalize(c) for c in raw.iloc[i].tolist()]
        if "branches" in cells and any("invoice" in c for c in cells):
            header_idx = i
            break
    if header_idx is None:
        return pd.DataFrame()
    hdr = [normalize(c) for c in raw.iloc[header_idx].tolist()]

    def col_of(*keys):
        for j, h in enumerate(hdr):
            if any(k in h for k in keys):
                return j
        return None

    c_inv = col_of("invoice number") or 0
    c_contact = col_of("contact")
    c_date = col_of("invoice date")
    c_ref = col_of("reference")
    c_gross = col_of("gross", "amount", "total")
    c_status = col_of("status")
    c_source = col_of("source")
    c_branch = col_of("branches", "branch")
    rows = []
    for i in range(header_idx + 1, len(raw)):
        row = raw.iloc[i].tolist()
        inv = str(row[c_inv]).strip() if c_inv < len(row) else ""
        if not inv or inv.lower() == "nan" or normalize(inv).startswith("total"):
            continue
        raw_date = row[c_date] if c_date is not None and c_date < len(row) else ""
        parsed = parse_cell_date(raw_date)
        if parsed is None:
            d = str(raw_date).strip() if raw_date is not None else ""
            if not d or d.lower() == "nan":
                continue
            row_year, mo = year, month
            mm = re.search(r"(20\d{2})-(\d{2})-\d{2}", d)
            if mm:
                row_year, mo = int(mm.group(1)), int(mm.group(2))
            mdate = d[:10]
        else:
            row_year, mo = parsed.year, parsed.month
            mdate = parsed.strftime("%Y-%m-%d")
        branch = str(row[c_branch]).strip() if c_branch is not None and c_branch < len(row) else ""
        if not branch or branch.lower() == "nan":
            branch = "غير محدد"
        rows.append({
            "year": row_year, "month": mo, "mdate": mdate,
            "invoice": inv,
            "contact": str(row[c_contact]) if c_contact is not None and c_contact < len(row) else "",
            "reference": str(row[c_ref]) if c_ref is not None and c_ref < len(row) else "",
            "amount": number(row[c_gross]) if c_gross is not None and c_gross < len(row) else 0.0,
            "status": str(row[c_status]) if c_status is not None and c_status < len(row) else "",
            "source": str(row[c_source]) if c_source is not None and c_source < len(row) else "",
            "branch": branch,
        })
    return pd.DataFrame(rows)


def _foodics_header_row(raw, keys):
    """يجد صف العناوين في تقارير Foodics (العناوين ليست في الصف الأول)."""
    for i in range(min(25, len(raw))):
        cells = [normalize(c) for c in raw.iloc[i].tolist()]
        if all(any(k in c for c in cells) for k in keys):
            return i
    return None


def _foodics_col(hdr, *keys):
    for j, h in enumerate(hdr):
        if h in keys:
            return j
    for j, h in enumerate(hdr):
        if any(k and k in h for k in keys):
            return j
    return None


def _foodics_year_from_path(path, raw):
    year, _month = detect_file_period(path, raw=raw)
    return year


def _warn_missing_html_engine(path, exc):
    """يوضّح سبب فشل قراءة تقارير Foodics بدل إرجاع جدول فارغ بصمت."""
    msg = str(exc)
    if "lxml" in msg.lower() or "Import" in msg or isinstance(exc, ImportError):
        try:
            import sys
            st.error(
                "تعذّر قراءة تقارير Foodics: مكتبة **lxml** غير مثبّتة. "
                "ثبّتها بالأمر: `python -m pip install lxml`"
                f"\n\nالملف: {Path(path).name}"
            )
        except Exception:
            pass


def _parse_foodics_html_branch(path):
    """Parse Foodics branch report from HTML table format (.xls files)."""
    try:
        tables = pd.read_html(path)
    except Exception as exc:
        _warn_missing_html_engine(path, exc)
        return pd.DataFrame()
    if not tables:
        return pd.DataFrame()
    df = tables[0]
    # Flatten multi-level columns
    if isinstance(df.columns[0], tuple):
        df.columns = [c[1] for c in df.columns]
    # Skip header rows (Title, date range, empty, grouping)
    df = df.iloc[3:].reset_index(drop=True)
    # Map columns
    col_map = {
        "الفرع": "branch",
        "مرجع الفرع": "branch_code",
        "إجمالي المبيعات": "gross",
        "صافي المبيعات": "net",
        "الضرائب": "tax",
        "مبلغ الخصم": "discount",
        "عدد الطلبات": "orders",
        "متوسط الطلب": "avg_order",
        "عدد الزوار": "visitors",
        "عدد العملاء": "customers",
        "صافي الكمية": "qty",
    }
    df = df.rename(columns=col_map)
    # Keep only needed columns
    needed = ["branch", "branch_code", "gross", "net", "tax", "discount", "orders", "avg_order", "visitors", "customers", "qty"]
    for c in needed:
        if c not in df.columns:
            df[c] = 0
    df = df[needed]
    # Clean data
    for c in ["gross", "net", "tax", "discount", "avg_order", "qty"]:
        df[c] = df[c].apply(number)
    for c in ["orders", "visitors", "customers"]:
        df[c] = df[c].apply(lambda x: int(number(x)))
    df["branch"] = df["branch"].astype(str).str.strip()
    df["branch_code"] = df["branch_code"].astype(str).str.strip()
    # Remove empty/total rows
    df = df[df["branch"].str.len() > 0]
    df = df[~df["branch"].str.lower().isin(["nan", "none", "total"])]
    df = df[~df["branch"].str.contains("title|النطاق", case=False, na=False)]
    # Get year from path
    year = _foodics_year_from_path(path, None)
    df["year"] = year
    return df


def parse_foodics_branch(path):
    """يقرأ تقرير المبيعات حسب الفرع من Foodics."""
    # Try HTML format first (for .xls files)
    if str(path).lower().endswith(".xls"):
        df = _parse_foodics_html_branch(path)
        if not df.empty:
            return df
    # Fallback to Excel format
    try:
        raw = pd.read_excel(path, sheet_name=0, header=None, dtype=object)
    except Exception as _exc:
        import logging as _logging
        _logging.warning("parse_foodics_branch فشل %s: %s", path, _exc)
        return pd.DataFrame()
    if raw.empty:
        return pd.DataFrame()
    header_idx = _foodics_header_row(raw, ["الفرع", "إجمالي المبيعات"])
    if header_idx is None:
        header_idx = _foodics_header_row(raw, ["branch", "gross"])
    if header_idx is None:
        return pd.DataFrame()
    hdr = [normalize(c) for c in raw.iloc[header_idx].tolist()]
    c_br = _foodics_col(hdr, "الفرع", "branch")
    c_code = _foodics_col(hdr, "مرجع الفرع", "branch code", "كود الفرع")
    c_gross = _foodics_col(hdr, "إجمالي المبيعات", "gross")
    c_net = _foodics_col(hdr, "صافي المبيعات")
    if c_net is None:
        c_net = _foodics_col(hdr, "net sales", "net")
    c_tax = _foodics_col(hdr, "الضرائب", "tax")
    c_disc = _foodics_col(hdr, "مبلغ الخصم", "الخصم", "discount")
    c_orders = _foodics_col(hdr, "عدد الطلبات", "orders")
    c_avg = _foodics_col(hdr, "متوسط الطلب", "avg")
    c_vis = _foodics_col(hdr, "عدد الزوار", "visitors")
    c_cust = _foodics_col(hdr, "عدد العملاء", "customers")
    c_qty = _foodics_col(hdr, "صافي الكمية", "quantity")
    year = _foodics_year_from_path(path, raw)
    rows = []
    for i in range(header_idx + 1, len(raw)):
        row = raw.iloc[i].tolist()

        def cell(idx):
            return row[idx] if idx is not None and idx < len(row) else ""

        branch = str(cell(c_br)).strip()
        if not branch or branch.lower() in ("nan", "none") or normalize(branch).startswith("total"):
            continue
        if "title" in normalize(branch) or "النطاق" in branch:
            continue
        rows.append({
            "year": year,
            "branch": branch,
            "branch_code": str(cell(c_code)).strip() or branch,
            "gross": number(cell(c_gross)),
            "net": number(cell(c_net)),
            "tax": number(cell(c_tax)),
            "discount": number(cell(c_disc)),
            "orders": int(number(cell(c_orders))),
            "avg_order": number(cell(c_avg)),
            "visitors": int(number(cell(c_vis))),
            "customers": int(number(cell(c_cust))),
            "qty": number(cell(c_qty)),
        })
    return pd.DataFrame(rows)


def _parse_foodics_html_product(path):
    """Parse Foodics product report from HTML table format (.xls files)."""
    try:
        tables = pd.read_html(path)
    except Exception as exc:
        _warn_missing_html_engine(path, exc)
        return pd.DataFrame()
    if not tables:
        return pd.DataFrame()
    df = tables[0]
    # Flatten multi-level columns
    if isinstance(df.columns[0], tuple):
        df.columns = [c[1] for c in df.columns]
    # Skip header rows (Title, date range, grouping info, empty)
    df = df.iloc[4:].reset_index(drop=True)
    # Map columns
    col_map = {
        "الفرع": "branch",
        "مرجع الفرع": "branch_code",
        "المنتج": "product",
        "كود تعريف المنتج": "product_code",
        "إجمالي المبيعات": "gross",
        "صافي المبيعات": "net",
        "الضرائب": "tax",
        "مبلغ الخصم": "discount",
        "صافي الكمية": "qty",
        "التكلفة": "cost",
        "مبلغ الإرجاع": "returns_amount",
        "كمية المرتجع": "returns_qty",
    }
    df = df.rename(columns=col_map)
    # Keep only needed columns
    needed = ["branch", "branch_code", "product", "product_code", "gross", "net", "tax", "discount", "qty", "cost", "returns_amount", "returns_qty"]
    for c in needed:
        if c not in df.columns:
            df[c] = 0
    df = df[needed]
    # Clean data
    for c in ["gross", "net", "tax", "discount", "qty", "cost", "returns_amount", "returns_qty"]:
        df[c] = df[c].apply(number)
    df["branch"] = df["branch"].astype(str).str.strip()
    df["branch_code"] = df["branch_code"].astype(str).str.strip()
    df["product"] = df["product"].astype(str).str.strip()
    df["product_code"] = df["product_code"].astype(str).str.strip()
    # Remove empty rows
    df = df[df["product"].str.len() > 0]
    df = df[~df["product"].str.lower().isin(["nan", "none", "المنتج", "product", "title"])]
    # Get year from path
    year = _foodics_year_from_path(path, None)
    df["year"] = year
    return df


def parse_foodics_product(path):
    """يقرأ تقرير المبيعات حسب المنتج من Foodics (منتج × فرع)."""
    # Try HTML format first (for .xls files)
    if str(path).lower().endswith(".xls"):
        df = _parse_foodics_html_product(path)
        if not df.empty:
            return df
    # Fallback to Excel format
    try:
        raw = pd.read_excel(path, sheet_name=0, header=None, dtype=object)
    except Exception as _exc:
        import logging as _logging
        _logging.warning("parse_foodics_product فشل %s: %s", path, _exc)
        return pd.DataFrame()
    if raw.empty:
        return pd.DataFrame()
    header_idx = _foodics_header_row(raw, ["المنتج", "الفرع"])
    if header_idx is None:
        header_idx = _foodics_header_row(raw, ["product", "branch"])
    if header_idx is None:
        return pd.DataFrame()
    hdr = [normalize(c) for c in raw.iloc[header_idx].tolist()]
    c_br = _foodics_col(hdr, "الفرع", "branch")
    c_code = _foodics_col(hdr, "مرجع الفرع", "branch code", "كود الفرع")
    c_prod = _foodics_col(hdr, "المنتج", "product")
    c_pcode = _foodics_col(hdr, "كود تعريف المنتج", "product code", "كود المنتج")
    c_gross = _foodics_col(hdr, "إجمالي المبيعات", "gross")
    c_net = _foodics_col(hdr, "صافي المبيعات")
    if c_net is None:
        c_net = _foodics_col(hdr, "net sales", "net")
    c_tax = _foodics_col(hdr, "الضرائب", "tax")
    c_disc = _foodics_col(hdr, "مبلغ الخصم", "الخصم", "discount")
    c_qty = _foodics_col(hdr, "صافي الكمية", "quantity", "الكمية")
    c_cost = _foodics_col(hdr, "التكلفة", "cost")
    c_ret_amt = _foodics_col(hdr, "مبلغ الإرجاع", "return amount")
    c_ret_qty = _foodics_col(hdr, "كمية المرتجع", "return qty")
    year = _foodics_year_from_path(path, raw)
    rows = []
    for i in range(header_idx + 1, len(raw)):
        row = raw.iloc[i].tolist()

        def cell(idx):
            return row[idx] if idx is not None and idx < len(row) else ""

        product = str(cell(c_prod)).strip()
        branch = str(cell(c_br)).strip()
        if not product or product.lower() in ("nan", "none"):
            continue
        if normalize(product) in ("المنتج", "product", "title"):
            continue
        rows.append({
            "year": year,
            "branch": branch if branch.lower() not in ("nan", "none", "") else "غير محدد",
            "branch_code": str(cell(c_code)).strip(),
            "product": product,
            "product_code": str(cell(c_pcode)).strip(),
            "gross": number(cell(c_gross)),
            "net": number(cell(c_net)),
            "tax": number(cell(c_tax)),
            "discount": number(cell(c_disc)),
            "qty": number(cell(c_qty)),
            "cost": number(cell(c_cost)),
            "returns_amount": number(cell(c_ret_amt)),
            "returns_qty": number(cell(c_ret_qty)),
        })
    return pd.DataFrame(rows)


def insert_foodics_branch(df, source_file=""):
    if df.empty:
        return 0
    seq = [
        (int(r["year"]), str(r["branch"]), str(r["branch_code"]),
         float(r["gross"]), float(r["net"]), float(r["tax"]), float(r["discount"]),
         int(r["orders"]), float(r["avg_order"]), int(r["visitors"]),
         int(r["customers"]), float(r["qty"]), source_file, now_str())
        for _, r in df.iterrows()
    ]
    executemany(
        "INSERT INTO foodics_branch_sales(year, branch, branch_code, gross, net, tax, discount, "
        "orders, avg_order, visitors, customers, qty, source_file, imported_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(year, branch_code) DO UPDATE SET "
        "branch=excluded.branch, gross=excluded.gross, net=excluded.net, tax=excluded.tax, "
        "discount=excluded.discount, orders=excluded.orders, avg_order=excluded.avg_order, "
        "visitors=excluded.visitors, customers=excluded.customers, qty=excluded.qty, "
        "source_file=excluded.source_file, imported_at=excluded.imported_at",
        seq,
    )
    return len(seq)


def insert_foodics_product(df, source_file=""):
    if df.empty:
        return 0
    execute("DELETE FROM foodics_product_sales WHERE source_file=?", (source_file,))
    seq = [
        (int(r["year"]), str(r["branch"]), str(r["branch_code"]),
         str(r["product"]), str(r["product_code"]),
         float(r["gross"]), float(r["net"]), float(r["tax"]), float(r["discount"]),
         float(r["qty"]), float(r["cost"]), float(r["returns_amount"]), float(r["returns_qty"]),
         source_file, now_str())
        for _, r in df.iterrows()
    ]
    executemany(
        "INSERT INTO foodics_product_sales(year, branch, branch_code, product, product_code, "
        "gross, net, tax, discount, qty, cost, returns_amount, returns_qty, source_file, imported_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        seq,
    )
    return len(seq)


MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _pnl_category(section):
    s = normalize(section)
    if any(k in s for k in ["cost of", "direct cost", "تكلفة"]):
        return "cogs"
    if "other income" in s or "اخرى" in s and "ايراد" in s:
        return "other_income"
    if any(k in s for k in ["income", "revenue", "sales", "ايراد", "إيراد", "مبيعات", "دخل"]):
        return "revenue"
    if any(k in s for k in ["expense", "overhead", "depreciation", "admin", "operating",
                            "مصروف", "مصاريف", "اهلاك", "إهلاك", "تشغيل"]):
        return "expense"
    return None


def parse_pnl_monthly(path):
    """يقرأ قائمة الدخل الشهرية (أعمدة الأشهر Jan..Dec) إلى بنود شهرية نظيفة.
    يستبعد عمود Year to date ويصنّف البنود حسب القسم (إيراد/تكلفة مبيعات/مصروف)."""
    try:
        raw = pd.read_excel(path, sheet_name=0, header=None, dtype=object)
    except Exception as _exc:
        import logging as _logging
        _logging.warning("parse_pnl_monthly فشل %s: %s", path, _exc)
        return pd.DataFrame()
    if raw.empty:
        return pd.DataFrame()
    header_idx, month_cols = None, {}
    for i in range(min(20, len(raw))):
        cols = {}
        for j, c in enumerate(raw.iloc[i].tolist()):
            t = normalize(c)
            m = re.match(r"^([a-z]{3})[a-z]*\s*(20\d\d)", t)
            if m and m.group(1) in MONTH_MAP:
                cols[j] = (int(m.group(2)), MONTH_MAP[m.group(1)])
        if len(cols) >= 3:
            header_idx, month_cols = i, cols
            break
    if header_idx is None:
        return pd.DataFrame()

    code_re = re.compile(r"^\s*(\d+)\s*[-–]\s*(.*)$")
    section, records = "", []
    for i in range(header_idx + 1, len(raw)):
        row = raw.iloc[i].tolist()
        c0 = str(row[0]).strip() if row else ""
        if not c0 or c0.lower() == "nan":
            continue
        low = normalize(c0)
        if low.startswith(("total", "gross profit", "net profit", "إجمالي", "صافي", "مجمل")):
            continue
        m = code_re.match(c0)
        has_values = any(number(row[j]) for j in month_cols if j < len(row))
        if not m and not has_values:
            section = c0  # عنوان قسم (Trading Income / Cost of Sales / Operating Expenses ...)
            continue
        category = _pnl_category(section)
        if category is None:
            continue
        code = m.group(1) if m else ""
        name = m.group(2).strip() if m else c0
        for j, (yr, mo) in month_cols.items():
            if j >= len(row):
                continue
            amt = number(row[j])
            if amt:
                records.append({
                    "year": yr, "month": mo, "account_code": code,
                    "account_name": name, "category": category, "amount": amt,
                })
    return pd.DataFrame(records)


def parse_balance_sheet(path):
    """يجمع بنود الميزانية يدويًا (صفوف Total تُصدَّر أصفارًا) لكل سنة:
    الأصول المتداولة، المخزون، الخصوم المتداولة، إجمالي الأصول، حقوق الملكية."""
    try:
        raw = pd.read_excel(path, sheet_name=0, header=None, dtype=object)
    except Exception as _exc:
        import logging as _logging
        _logging.warning("parse_balance_sheet فشل %s: %s", path, _exc)
        return pd.DataFrame()
    if raw.empty:
        return pd.DataFrame()
    header_idx, year_cols = None, {}
    for i in range(min(12, len(raw))):
        cols = {}
        for j, c in enumerate(raw.iloc[i].tolist()):
            mm = re.search(r"(20\d\d)", str(c))
            if mm and normalize(c) not in ("account",):
                cols[j] = int(mm.group(1))
        if cols and any("account" in normalize(x) for x in raw.iloc[i].tolist()):
            header_idx, year_cols = i, cols
            break
    if header_idx is None:
        return pd.DataFrame()

    agg = {y: dict(current_assets=0.0, inventory=0.0, current_liabilities=0.0,
                   total_assets=0.0, total_equity=0.0) for y in year_cols.values()}
    top, sub = "", ""
    code_re = re.compile(r"^\s*\d+\s*[-–]")
    for i in range(header_idx + 1, len(raw)):
        row = raw.iloc[i].tolist()
        c0 = str(row[0]).strip() if len(row) > 0 else ""
        c1 = str(row[1]).strip() if len(row) > 1 else ""
        label = c1 if c1 and c1.lower() != "nan" else c0
        low = normalize(label)
        vals = {y: number(row[j]) for j, y in year_cols.items() if j < len(row)}
        has_val = any(v for v in vals.values())
        is_detail = bool(code_re.match(c1)) or (has_val and not low.startswith("total"))
        if c0 and c0.lower() != "nan" and not code_re.match(c0):
            top = normalize(c0)
        if not is_detail:
            if low.startswith("total") or not label or label.lower() == "nan":
                continue
            sub = low  # عنوان قسم فرعي (Bank / Current Assets / Current Liabilities ...)
            continue
        is_current_asset = ("current asset" in sub) or ("bank" in sub) or \
                           ("asset" in top and "current" in sub)
        is_current_liab = ("current liabilit" in sub) or ("current" in sub and "liab" in top)
        is_inventory = ("مخزون" in low) or ("inventory" in low)
        for y, v in vals.items():
            if "asset" in top:
                agg[y]["total_assets"] += v
            if "equity" in top:
                agg[y]["total_equity"] += v
            if is_current_asset:
                agg[y]["current_assets"] += v
            if is_inventory:
                agg[y]["inventory"] += v
            if is_current_liab:
                agg[y]["current_liabilities"] += v
    rows = [dict(year=y, source_file=Path(path).name, **d) for y, d in agg.items()]
    return pd.DataFrame(rows)


def insert_pnl_monthly(df, source_file=""):
    if df.empty:
        return 0
    # إبطال التكرار على مستوى (سنة/شهر): يسمح بإضافة ملف كل شهر على حِدة
    # دون أن يمحو ملفُ شهرٍ بياناتِ الأشهر الأخرى، ودون مضاعفة عند إعادة الاستيراد.
    pairs = sorted(set((int(r["year"]), int(r["month"])) for _, r in df.iterrows()))
    for y, mo in pairs:
        execute("DELETE FROM pnl_monthly WHERE year=? AND month=?", (y, mo))
    seq = [
        (int(r["year"]), int(r["month"]), r["account_code"], r["account_name"],
         r["category"], float(r["amount"]), source_file, now_str())
        for _, r in df.iterrows()
    ]
    executemany(
        "INSERT INTO pnl_monthly(year, month, account_code, account_name, category, "
        "amount, source_file, imported_at) VALUES(?,?,?,?,?,?,?,?)", seq,
    )
    return len(seq)


def parse_pnl_branches(path):
    """يقرأ 2025-Profit and Loss Compare_Branches.xlsx: حساب × فرع لسنة الملف."""
    try:
        raw = pd.read_excel(path, sheet_name=0, header=None, dtype=object)
    except Exception as _exc:
        import logging as _logging
        _logging.warning("parse_pnl_branches فشل %s: %s", path, _exc)
        return pd.DataFrame()
    if raw.empty:
        return pd.DataFrame()
    year, _month = detect_file_period(path, raw=raw)
    header_idx, branches = None, {}
    for i in range(min(12, len(raw))):
        cells = [str(c).strip() if pd.notna(c) else "" for c in raw.iloc[i].tolist()]
        if any(normalize(c) == "account" for c in cells):
            header_idx = i
            for j, c in enumerate(cells):
                low = normalize(c)
                if j == 0 or not c or low in ("account", "total", "الإجمالي"):
                    continue
                branches[j] = c
            break
    if header_idx is None or not branches:
        return pd.DataFrame()
    code_re = re.compile(r"^\s*(\d+)\s*[-–]\s*(.*)$")
    section, records = "", []
    for i in range(header_idx + 1, len(raw)):
        row = raw.iloc[i].tolist()
        c0 = str(row[0]).strip() if row else ""
        if not c0 or c0.lower() == "nan":
            continue
        low = normalize(c0)
        if low.startswith(("total", "gross profit", "net profit", "إجمالي", "صافي", "مجمل")):
            continue
        m = code_re.match(c0)
        has_values = any(number(row[j]) for j in branches if j < len(row))
        if not m and not has_values:
            section = c0
            continue
        category = _pnl_category(section)
        if category is None:
            continue
        code = m.group(1) if m else ""
        name = m.group(2).strip() if m else c0
        for j, branch in branches.items():
            if j >= len(row):
                continue
            amt = number(row[j])
            if amt:
                records.append({
                    "year": year, "branch": branch, "account_code": code,
                    "account_name": name, "category": category, "amount": amt,
                })
    return pd.DataFrame(records)


def insert_pnl_branch(df, source_file=""):
    if df.empty:
        return 0
    years = sorted({int(y) for y in df["year"].unique()})
    for y in years:
        execute("DELETE FROM pnl_branch WHERE year=?", (y,))
    seq = [
        (int(r["year"]), str(r["branch"]), str(r["account_code"]), str(r["account_name"]),
         str(r["category"]), float(r["amount"]), source_file, now_str())
        for _, r in df.iterrows()
    ]
    executemany(
        "INSERT INTO pnl_branch(year, branch, account_code, account_name, category, "
        "amount, source_file, imported_at) VALUES(?,?,?,?,?,?,?,?)", seq,
    )
    return len(seq)


def parse_inventory_summary(path):
    """يقرأ مجمع Inventory Item Summary: أرصدة/مبيعات/COGS لكل صنف."""
    try:
        raw = pd.read_excel(path, sheet_name=0, header=None, dtype=object)
    except Exception as _exc:
        import logging as _logging
        _logging.warning("parse_inventory_summary فشل %s: %s", path, _exc)
        return pd.DataFrame()
    if raw.empty:
        return pd.DataFrame()
    year, _month = detect_file_period(path, raw=raw)
    header_idx = None
    for i in range(min(12, len(raw))):
        cells = [normalize(c) for c in raw.iloc[i].tolist()]
        if "item code" in cells and ("closing quantity" in cells or "closing balance" in cells):
            header_idx = i
            break
    if header_idx is None:
        return pd.DataFrame()
    hdr = [normalize(c) for c in raw.iloc[header_idx].tolist()]

    def col_of(*keys):
        for j, h in enumerate(hdr):
            if any(k in h for k in keys):
                return j
        return None

    c_code = col_of("item code") or 0
    c_name = col_of("item name")
    c_open_v = col_of("opening balance")
    c_purch = col_of("purchases")
    c_cogs = col_of("cogs")
    c_adj = col_of("adjustments")
    c_close_v = col_of("closing balance")
    c_sales = col_of("sales")
    c_open_q = col_of("opening quantity")
    c_sold_q = col_of("quantity sold")
    c_adj_q = col_of("quantity adjusted")
    c_close_q = col_of("closing quantity")
    code_re = re.compile(r"^\s*((?:GB|RB)[A-Z0-9]*)", re.IGNORECASE)
    rows = []
    for i in range(header_idx + 1, len(raw)):
        row = raw.iloc[i].tolist()

        def cell(idx):
            return row[idx] if idx is not None and idx < len(row) else ""

        raw_code = str(cell(c_code)).strip()
        if not raw_code or raw_code.lower() in ("nan", "tracked", "untracked"):
            continue
        m = code_re.match(raw_code)
        code = (m.group(1) if m else raw_code.split("-")[0]).upper().strip()
        if not code:
            continue
        coffee_type, _family = item_meta(code)
        name = str(cell(c_name)).strip() if c_name is not None else raw_code
        if name.lower() == "nan":
            name = raw_code
        rows.append({
            "year": year, "item_code": code, "item_name": name, "coffee_type": coffee_type,
            "opening_value": number(cell(c_open_v)), "purchases": number(cell(c_purch)),
            "cogs": number(cell(c_cogs)), "adjustments": number(cell(c_adj)),
            "closing_value": number(cell(c_close_v)), "sales": number(cell(c_sales)),
            "opening_qty": number(cell(c_open_q)), "qty_sold": number(cell(c_sold_q)),
            "qty_adjusted": number(cell(c_adj_q)), "closing_qty": number(cell(c_close_q)),
        })
    return pd.DataFrame(rows)


def insert_inventory_summary(df, source_file=""):
    if df.empty:
        return 0
    years = sorted({int(y) for y in df["year"].unique()})
    for y in years:
        execute("DELETE FROM inventory_summary WHERE year=?", (y,))
    seq = [
        (int(r["year"]), str(r["item_code"]), str(r["item_name"]), str(r["coffee_type"]),
         float(r["opening_value"]), float(r["purchases"]), float(r["cogs"]), float(r["adjustments"]),
         float(r["closing_value"]), float(r["sales"]),
         float(r["opening_qty"]), float(r["qty_sold"]), float(r["qty_adjusted"]), float(r["closing_qty"]),
         source_file, now_str())
        for _, r in df.iterrows()
    ]
    executemany(
        "INSERT INTO inventory_summary(year, item_code, item_name, coffee_type, opening_value, "
        "purchases, cogs, adjustments, closing_value, sales, opening_qty, qty_sold, "
        "qty_adjusted, closing_qty, source_file, imported_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", seq,
    )
    return len(seq)


def upsert_fin_figures(df):
    if df.empty:
        return 0
    n = 0
    for _, r in df.iterrows():
        execute(
            "INSERT INTO fin_figures(year, current_assets, inventory, current_liabilities, "
            "total_assets, total_equity, source_file, updated_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(year) DO UPDATE SET current_assets=excluded.current_assets, "
            "inventory=excluded.inventory, current_liabilities=excluded.current_liabilities, "
            "total_assets=excluded.total_assets, total_equity=excluded.total_equity, "
            "source_file=excluded.source_file, updated_at=excluded.updated_at",
            (int(r["year"]), float(r["current_assets"]), float(r["inventory"]),
             float(r["current_liabilities"]), float(r["total_assets"]),
             float(r["total_equity"]), r.get("source_file", ""), now_str()),
        )
        n += 1
    return n


def save_raw_statement(statement, year, month, df):
    payload = df.to_json(orient="split", force_ascii=False)
    execute(
        "INSERT INTO raw_statements(statement, year, month, payload, imported_at) VALUES(?, ?, ?, ?, ?)",
        (statement, year, month, payload, now_str()),
    )


def import_inventory_report(log, paths=None):
    """يستورد تقارير حركة الصنف (Excel/CSV) من المجلد الرئيسي ومن مجلدات الأشهر (YYYY-MM):
    حركات كل الأعوام/الأشهر + الرصيد الختامي المعتمد من أحدث فترة فقط.

    المرحلة 1: CSV الآن يقرأ Closing Balance الحقيقي (لا sum تقديري).
    """
    by_period = {}
    if paths is None:
        paths = _scan_once()
    for path in paths:
        nm = normalize(path.name)
        if not (("inventory" in nm and "detail" in nm) or ("تفصيل" in nm and "inventory" in nm)):
            continue
        if "summary" in nm or "مجمع" in nm:
            continue
        year, month = detect_file_period(path)
        key = (year, month)
        prev = by_period.get(key)
        in_input = INPUT_DIR in path.parents
        if prev is None or (in_input and INPUT_DIR not in prev.parents):
            by_period[key] = path
    if not by_period:
        return
    latest = max(by_period)
    for (year, month) in sorted(by_period):
        path = by_period[(year, month)]
        ext = path.suffix.lower()
        if ext == '.csv':
            moves, balances = parse_inventory(path, year, month)
            n = insert_movements(moves, from_report=1)
            b = 0
            if (year, month) == latest and not balances.empty:
                for _, r in balances.iterrows():
                    upsert_balance(r.to_dict())
                    b += 1
            log.append(f"{path.name} ← حركات {year}-{month:02d}: {n} | أرصدة ختامية معتمدة: {b}")
        else:
            moves, balances = parse_inventory_xlsx(path, year, month)
            n = insert_movements(moves, from_report=1)
            b = 0
            if (year, month) == latest:
                for _, r in balances.iterrows():
                    upsert_balance(r.to_dict())
                    b += 1
            log.append(f"{path.name} ← حركات {year}-{month:02d}: {n} | أرصدة ختامية معتمدة: {b}")


def import_daily_sales(log, paths=None):
    """يستورد تقرير المبيعات اليومية لكل فرع (عمود Branches) من المجلد الرئيسي
    ومن مجلدات الأشهر (YYYY-MM) — لكل الأعوام، بدون تكرار الفترة الواحدة."""
    by_period = {}
    if paths is None:
        paths = _scan_once()
    for path in paths:
        nm = normalize(path.name)
        if not ("المبيعات اليومية" in nm or ("daily" in nm and "sale" in nm) or "لكل فرع" in nm):
            continue
        year, month = detect_file_period(path)
        by_period.setdefault((year, month), path)
    for (year, month) in sorted(by_period):
        path = by_period[(year, month)]
        df = parse_daily_sales(path, year, month)
        n = insert_branch_sales(df, source_file=path.name)
        log.append(f"{path.name} ← فواتير المبيعات حسب الفرع {year}: {n}")


_UPLOAD_ALLOWED_EXT = {".xlsx", ".xls", ".xlsm", ".csv"}

# تسميات صفوف التجميع الفرعية في تقارير المخزون: عمود Date يحمل التسمية
# بدل التاريخ، فلو لم تُستبعد تُستورد كحركة وهمية بقيم حقيقية.
_SUBTOTAL_MARKERS = ("total", "totals", "sum", "الإجمالي", "المجموع", "اجمالي", "مجموع")


def safe_upload_path(filename, dest_dir=DB_DIR):
    """مسار آمن داخل dest_dir لملف مرفوع: يقتلع أي مكوّن مجلدات من اسم
    الملف (يتجاوز المسار مثل ../../etc)، ويرفض الامتدادات غير المسموحة،
    ويتحقق أن المسار الناتج محدَّد داخل dest_dir فعلًا. يرجّع None عند الرفض."""
    raw = str(filename or "")
    # اقتلاع المكوّنات الشبيهة بالمسارات في كلا النظامين (\\ و /)
    name = raw.replace("\\", "/").split("/")[-1].strip()
    if not name or name.startswith("."):
        return None
    suffix = Path(name).suffix.lower()
    if suffix not in _UPLOAD_ALLOWED_EXT:
        return None
    dest = Path(dest_dir).resolve()
    candidate = (dest / ("upload_" + name)).resolve()
    try:
        candidate.relative_to(dest)
    except ValueError:
        return None
    return candidate


def _scan_report_xlsx():
    """كل ملفات Excel و CSV في المجلد الرئيسي + مجلدات الإدخال الشهرية (بدون الأرشيف)."""
    seen = set()
    paths = list(BASE_DIR.glob("*.xlsx")) + list(BASE_DIR.glob("*.xls")) + list(BASE_DIR.glob("*.csv"))
    if INPUT_DIR.exists():
        paths += list(INPUT_DIR.rglob("*.xlsx")) + list(INPUT_DIR.rglob("*.xls")) + list(INPUT_DIR.rglob("*.csv"))
    for p in paths:
        if p.name.startswith("~$"):
            continue
        rp = str(p.resolve())
        if rp in seen:
            continue
        seen.add(rp)
        yield p


def _scan_once():
    """مسح واحد يُعاد استخدامه لكل دوال الاستيراد — يحل مشكلة 7× قراءة متكررة."""
    return list(_scan_report_xlsx())


def import_pnl_monthly(log, paths=None):
    """قائمة الدخل الشهرية (أعمدة الأشهر) → جدول تحليلي نظيف لكل شهر."""
    if paths is None:
        paths = _scan_once()
    for path in paths:
        nm = normalize(path.name)
        if "profit and loss" not in nm and "الدخل" not in nm and "by_month" not in nm \
           and "by month" not in nm and "financial_year_by_month" not in nm \
           and "current_financial_year" not in nm and "current financial year" not in nm:
            continue
        df = parse_pnl_monthly(path)
        if df.empty:
            continue
        n = insert_pnl_monthly(df, source_file=path.name)
        yrs = sorted(set(int(y) for y in df["year"].unique()))
        log.append(f"{path.name} ← بنود قائمة الدخل الشهرية: {n} (سنوات: {', '.join(map(str, yrs))})")


def import_pnl_branches(log, paths=None):
    """يستورد Compare_Branches: أرباح وخسائر حسب الفرع من الملف الفعلي."""
    if paths is None:
        paths = _scan_once()
    for path in paths:
        nm = normalize(path.name)
        if "compare_branches" not in nm and "compare branches" not in nm:
            continue
        df = parse_pnl_branches(path)
        if df.empty:
            continue
        n = insert_pnl_branch(df, source_file=path.name)
        yrs = sorted({int(y) for y in df["year"].unique()})
        brs = sorted({str(b) for b in df["branch"].unique()})
        log.append(f"{path.name} ← أرباح وخسائر الفروع: {n} بند ({len(brs)} فرع، سنوات: {', '.join(map(str, yrs))})")


def import_inventory_summary(log, paths=None):
    """يستورد مجمع Inventory Item Summary للربط مع التوقع والانحراف."""
    if paths is None:
        paths = _scan_once()
    for path in paths:
        nm = normalize(path.name)
        if "inventory" not in nm or ("summary" not in nm and "مجمع" not in nm):
            continue
        df = parse_inventory_summary(path)
        if df.empty:
            continue
        n = insert_inventory_summary(df, source_file=path.name)
        log.append(f"{path.name} ← ملخص المخزون السنوي: {n} صنف")


def import_foodics_reports(log, paths=None):
    """يستورد تقارير Foodics (مبيعات حسب الفرع / حسب المنتج) من المجلد الرئيسي ومجلدات الإدخال."""
    n_br = n_pr = 0
    if paths is None:
        paths = _scan_once()
    for path in paths:
        nm = normalize(path.name)
        if "foodics" not in nm:
            continue
        if "منتج" in nm or "product" in nm:
            df = parse_foodics_product(path)
            n = insert_foodics_product(df, source_file=path.name)
            n_pr += n
            log.append(f"{path.name} ← مبيعات منتجات Foodics: {n}")
        elif "فرع" in nm or "branch" in nm:
            df = parse_foodics_branch(path)
            n = insert_foodics_branch(df, source_file=path.name)
            n_br += n
            log.append(f"{path.name} ← مبيعات فروع Foodics: {n}")
    if n_br == 0 and n_pr == 0:
        log.append("ملاحظة: لم يُعثر على ملفات Foodics (المبيعات حسب الفرع / حسب المنتج).")
    else:
        invalidate_year_caches()


def import_balance_sheet_figures(log, paths=None):
    """الميزانية العمومية → أصول/خصوم متداولة ومخزون لكل سنة (لحساب النسب)."""
    if paths is None:
        paths = _scan_once()
    for path in paths:
        nm = normalize(path.name)
        if "balance sheet" not in nm and "الميزانية" not in nm and "balance_sheet" not in nm:
            continue
        df = parse_balance_sheet(path)
        if df.empty:
            continue
        n = upsert_fin_figures(df)
        log.append(f"{path.name} ← أرقام الميزانية للنسب المالية: {n} سنة")


def import_from_folders():
    """يعيد استخدام منطق السكربت الأصلي لاستيراد كل المجلدات الشهرية إلى قاعدة البيانات.

    المرحلة 1: كل الحذف+الإدخال داخل معاملة واحدة ذرّية (db_transaction) +
    مسح واحد للملفات يُعاد استخدامه (_scan_once) + يبتلع الأخطاء مع log واضح.
    """
    log: list[str] = []
    all_paths = _scan_once()
    try:
        with db_transaction():
            # إعادة استيراد غير مكرّرة: نحذف بيانات التقرير السابقة (وأي حركات قديمة بدون وسم)
            # مع الحفاظ على الإدخالات اليدوية (from_report=0 صراحةً)
            execute("DELETE FROM movements WHERE from_report=1 OR from_report IS NULL")
            execute("DELETE FROM item_balances")
            execute("DELETE FROM branch_sales")
            execute("DELETE FROM pnl_monthly")
            execute("DELETE FROM pnl_branch")
            execute("DELETE FROM inventory_summary")
            execute("DELETE FROM foodics_branch_sales")
            execute("DELETE FROM foodics_product_sales")

            chart, chart_name = load_chart_of_accounts()
            if not chart.empty:
                for _, r in chart.iterrows():
                    upsert_account(r["كود الحساب"], r["اسم الحساب"], r["نوع الحساب"])
                log.append(f"شجرة الحسابات: {len(chart)} حساب ({chart_name})")
            else:
                log.append("تحذير: تعذّر قراءة شجرة الحسابات.")

            # كل دوال الاستيراد تشارك نفس قائمة المسارات + نفس المعاملة
            _call_import_safely(import_inventory_report, log, all_paths, label="حركة المخزون")
            _call_import_safely(import_inventory_summary, log, all_paths, label="ملخص المخزون")
            _call_import_safely(import_daily_sales, log, all_paths, label="المبيعات اليومية")
            _call_import_safely(import_pnl_monthly, log, all_paths, label="قائمة الدخل الشهرية")
            _call_import_safely(import_pnl_branches, log, all_paths, label="أرباح الفروع")
            _call_import_safely(import_balance_sheet_figures, log, all_paths, label="الميزانية")
            _call_import_safely(import_foodics_reports, log, all_paths, label="Foodics")
    except Exception as exc:
        log.append(f"فشل الاستيراد (تم التراجع عن كل التغييرات): {exc}")
        audit("import_folders_failed", f"{exc} | {' | '.join(log[-5:])}")
        try:
            add_alert("تحذير", "استيراد", f"فشل الاستيراد الشامل: {exc}")
        except Exception:
            pass
        return log
    # بعد نجاح المعاملة: مهام ما بعد الالتزام (لا تُعاد عند rollback)
    invalidate_year_caches()
    try:
        n_alerts = refresh_stock_alerts()
    except Exception as exc:
        log.append(f"تحذير: تعذّر تحديث تنبيهات المخزون: {exc}")
        n_alerts = 0
    if n_alerts:
        log.append(f"تنبيهات مخزون جديدة: {n_alerts}")
    found_years = list_years()
    if found_years:
        sync_year_settings(found_years)
        log.append("سنوات الملفات: " + "، ".join(f"عام {y}" for y in found_years))

    try:
        issues = validate_calculations()
    except Exception as exc:
        log.append(f"تحذير: تعذّر التحقق من الحسابات: {exc}")
        issues = []
    if issues:
        for iss in issues[:20]:
            log.append(f"تنبيه تحقق: {iss}")
        try:
            add_alert("تحذير", "تحقق", f"{len(issues)} انحراف/مطابقة بعد الاستيراد. راجع صفحة التحقق.")
        except Exception:
            pass

    folders = sorted([f for f in INPUT_DIR.iterdir() if f.is_dir()]) if INPUT_DIR.exists() else []
    if not folders:
        log.append("ملاحظة: لا توجد مجلدات أشهر إضافية داخل مجلد الإدخال (تم الاعتماد على تقارير المجلد الرئيسي).")
        audit("import_folders", " | ".join(log))
        return log

    for folder in folders:
        year, month = extract_year_month(folder)
        for path in folder.iterdir():
            ext = path.suffix.lower()
            file_year, file_month = detect_file_period(path, default_year=year, default_month=month)
            if ext == ".csv":
                try:
                    inv, balances = parse_inventory(path, file_year, file_month)
                except Exception as exc:
                    log.append(f"{path.name} ← تخطي (خطأ قراءة CSV): {exc}")
                    continue
                try:
                    n = insert_movements(inv, from_report=1)
                    log.append(f"{path.name} ← حركة مخزون: {n}")
                    if not balances.empty:
                        for _, r in balances.iterrows():
                            upsert_balance(r.to_dict())
                except Exception as exc:
                    log.append(f"{path.name} ← تخطي (خطأ حفظ): {exc}")
            elif ext in (".xlsx", ".xls"):
                data = read_excel(path)
                if data.empty:
                    continue
                fname = path.name.lower()
                sig = " ".join(normalize(c) for c in data.columns)
                if "profit and loss" in fname or "profit and loss" in sig:
                    try:
                        parsed = parse_profit_and_loss(data, file_year, chart)
                        n = insert_financial(parsed, "pnl")
                        log.append(f"{path.name} ← أرباح وخسائر {file_year}: {n}")
                    except Exception as exc:
                        log.append(f"{path.name} ← تخطي (خطأ P&L): {exc}")
                elif "trial balance" in fname or "trial balance" in sig:
                    try:
                        save_raw_statement("trial", file_year, file_month, data)
                        log.append(f"{path.name} ← ميزان مراجعة {file_year} (حُفظ خام): {len(data)}")
                    except Exception as exc:
                        log.append(f"{path.name} ← تخطي (خطأ Trial): {exc}")
                elif "balance sheet" in fname or "balance sheet" in sig:
                    try:
                        save_raw_statement("balance", file_year, file_month, data)
                        log.append(f"{path.name} ← ميزانية عمومية {file_year} (حُفظت خام): {len(data)}")
                    except Exception as exc:
                        log.append(f"{path.name} ← تخطي (خطأ Balance): {exc}")
    found_years = list_years()
    if found_years:
        sync_year_settings(found_years)
        log.append("سنوات الملفات: " + "، ".join(f"عام {y}" for y in found_years))
    audit("import_folders", " | ".join(log))
    return log


def _call_import_safely(fn, log, paths, label=""):
    """يستدعي دالة استيراد واحدة مع التقاط الخطأ بدل إسقاط كل العملية."""
    try:
        fn(log, paths)
    except Exception as exc:
        log.append(f"تخطي {label} ({fn.__name__}): {exc}")


# ==========================================================================
# التصدير إلى Excel (منقول من السكربت الأصلي)
# ==========================================================================
def write_sheet(writer, name, data):
    if data is None or data.empty:
        data = pd.DataFrame({"ملاحظة": ["لا توجد بيانات متاحة لهذه الصفحة"]})
    data.to_excel(writer, sheet_name=name[:31], index=False)


def style_workbook(path):
    from openpyxl import load_workbook
    from openpyxl.styles import Font, PatternFill

    workbook = load_workbook(path)
    fill = PatternFill("solid", fgColor="1F4E78")
    for ws in workbook.worksheets:
        ws.sheet_view.rightToLeft = True
        ws.freeze_panes = "A2"
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = fill
    workbook.save(path)


def build_exports():
    settings = get_settings()
    accounts = list_accounts().rename(columns={"code": "كود الحساب", "name": "اسم الحساب", "type": "نوع الحساب"})
    pnl = pnl_df()
    inv = movements_df()
    budget = query_df(
        "SELECT year AS 'year', month AS 'الشهر', account_code AS 'كود الحساب', "
        "account_name AS 'اسم الحساب', account_type AS 'نوع الحساب', actual_prev AS 'فعلي الأساس', "
        "growth AS 'نمو', proposed AS 'موازنة مقترحة', approved AS 'موازنة معتمدة' FROM budget"
    )
    forecast = build_forecast(pnl)
    inv_summary = build_inventory_summary(inv)
    roasting = build_roasting(inv, s_float(settings, "roast_yield"), s_float(settings, "roast_tolerance"))
    stock = current_stock()
    branch_detail, branch_by = branch_sales_summary()

    with pd.ExcelWriter(DASHBOARD_PATH, engine="openpyxl") as writer:
        write_sheet(writer, "شجرة الحسابات", accounts)
        write_sheet(writer, "قائمة الأرباح والخسائر", pnl)
        write_sheet(writer, "الموازنة", budget)
        write_sheet(writer, "التوقعات", forecast)
        write_sheet(writer, "الرصيد الحالي المعتمد", stock)
        write_sheet(writer, "حركة المخزون", inv)
        write_sheet(writer, "تحليل المخزون", inv_summary)
        write_sheet(writer, "تحليل التحميص", roasting)
        write_sheet(writer, "المبيعات حسب الفرع", branch_by)
        write_sheet(writer, "تفاصيل فواتير الفروع", branch_detail)
        write_sheet(writer, "فروع Foodics", foodics_branch_df())
        write_sheet(writer, "منتجات Foodics", foodics_product_df())
    style_workbook(DASHBOARD_PATH)

    alerts_review = roasting[roasting["الحالة"] == "يحتاج مراجعة"] if not roasting.empty else pd.DataFrame()
    with pd.ExcelWriter(ALERTS_PATH, engine="openpyxl") as writer:
        write_sheet(writer, "فروقات التحميص", alerts_review)
        write_sheet(writer, "ملخص المخزون", inv_summary)
    style_workbook(ALERTS_PATH)
    audit("export_excel", DASHBOARD_PATH.name)
    return DASHBOARD_PATH, ALERTS_PATH


# ==========================================================================
# طبقة التوافق مع إصدارات Streamlit المختلفة
# ==========================================================================
def rerun():
    fn = getattr(st, "rerun", None) or getattr(st, "experimental_rerun", None)
    if fn:
        fn()


def _call_with_fallback(call, variants):
    """ينفّذ نداءً بأعلى الميزات المتاحة لهذا الإصدار من Streamlit."""
    for kwargs in variants:
        try:
            return call(**kwargs)
        except TypeError:
            continue
    return call()


def space(size="small"):
    """مسافة رأسية بين الأقسام — ترجع لبديل آمن على الإصدارات القديمة."""
    fn = getattr(st, "space", None)
    if fn:
        try:
            return fn(size)
        except TypeError:
            try:
                return fn()
            except Exception:
                pass
    sizes = {"small": 8, "medium": 16, "large": 28}
    px = sizes.get(str(size), 8)
    st.markdown(f"<div style='height:{px}px'></div>", unsafe_allow_html=True)


def kpi(col, label, value, delta=None, delta_color="normal", help=None):
    """بطاقة مؤشر (KPI) بحدود عرضية — تعمل على الإصدارات القديمة والحديثة.

    معامل help اختياري: يُمرّر إلى st.metric إن دعمه الإصدار، وإلا يُتجاهل.
    """
    # جرّب help إن وُجد، ثم بدونه
    variants = []
    if help is not None:
        variants.append({"label": label, "value": value, "delta": delta, "delta_color": delta_color, "help": help, "border": True})
        variants.append({"label": label, "value": value, "delta": delta, "help": help, "border": True})
        variants.append({"label": label, "value": value, "delta": delta, "help": help})
    variants.append({"label": label, "value": value, "delta": delta, "delta_color": delta_color, "border": True})
    variants.append({"label": label, "value": value, "delta": delta, "delta_color": delta_color})
    variants.append({"label": label, "value": value, "delta": delta})
    return _call_with_fallback(col.metric, variants)


@contextmanager
def card(title=None):
    """حاوية بحد عرضية (بطاقة) لتجميع العناصر بصريًا مع توافق إصدارات Streamlit."""
    container = _call_with_fallback(st.container, [{"border": True}, {}])
    if title:
        container.subheader(title)
    yield container


def search_box(label, key, placeholder="اكتب جزء من الكود أو الاسم...", icon=":material/search:"):
    """حقل بحث بأيقونة Material — يرجع للنسخة بدون أيقونة على الإصدارات القديمة."""
    for kwargs in ({"placeholder": placeholder, "icon": icon},
                   {"placeholder": placeholder},
                   {}):
        try:
            return st.text_input(label, "", key=key, **kwargs)
        except TypeError:
            continue
    return None


def search_mask(df, cols, term):
    """قناع بحث موحّد: حساس لحالة الأحرف، يتجاهل المسافات الزائدة،
    ويعامل النص كنص عادي (regex=False) حتى لا تفشل أحرف مثل ( ? [ ."""
    if not term or not str(term).strip():
        return pd.Series(True, index=df.index)
    t = str(term).strip().lower()
    mask = pd.Series(False, index=df.index)
    for c in cols:
        if c in df.columns:
            mask = mask | df[c].astype(str).str.lower().str.contains(
                t, regex=False, na=False
            )
    return mask


def _icon_button(label, icon, fallback_icon):
    """زر بأيقونة Material مع رجوع آمن للإيموجي في الإصدارات القديمة."""
    try:
        return st.button(label, icon=icon)
    except TypeError:
        return st.button(f"{fallback_icon} {label}")


def money_columns(spec):
    """يبني column_config لتنسيق الأرقام (مبالغ/كميات) بأمان على أي إصدار."""
    cc = getattr(st, "column_config", None)
    if cc is None or not hasattr(cc, "NumberColumn"):
        return None
    out = {}
    try:
        for col_name, fmt in spec.items():
            if col_name is None:
                continue
            out[col_name] = cc.NumberColumn(format=fmt)
        return out or None
    except Exception:
        return None


# تنسيقات عددية شائعة — بفواصل الآلاف (مثال: 4,726,305)
MONEY_FMT = "%,.0f"
QTY_FMT = "%,.1f"


def editable_table(df, key):
    if hasattr(st, "data_editor"):
        try:
            return st.data_editor(df, key=key, num_rows="fixed")
        except TypeError:
            try:
                return st.data_editor(df, key=key)
            except TypeError:
                pass
    if hasattr(st, "experimental_data_editor"):
        return st.experimental_data_editor(df, key=key)
    st.warning("إصدار Streamlit لديك قديم لا يدعم الجداول القابلة للتحرير. "
               "يرجى التحديث بالأمر: pip install -U streamlit")
    st.dataframe(df)
    return None


# ==========================================================================
# واجهة المستخدم
# ==========================================================================
def setup_page():
    st.set_page_config(page_title="داشبورد الكوب الأول", page_icon="C", layout="wide")
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+Arabic:wght@400;500;600;700&display=swap');
        html, body, .stApp, [class*="css"] {
            font-family: "Noto Sans Arabic", "Segoe UI", Tahoma, sans-serif;
        }
        .stApp { background: #000000; }
        .stApp, .main, section[data-testid="stSidebar"] { direction: rtl; }
        h1, h2, h3, h4, h5, p, label, .stMarkdown { text-align: right; color: #FFFFFF; }
        .stDataFrame, .stTable { direction: rtl; }
        div[data-testid="stMetricValue"] { direction: ltr; font-variant-numeric: tabular-nums; color: #FFFFFF; }
        /* بطاقات المؤشرات: احتواء الأرقام المفصولة (مثل 1,676,305) بدون قص */
        div[data-testid="stMetric"] { min-width: 0; }
        div[data-testid="stMetricValue"] {
            font-size: clamp(1.02rem, 1.55vw, 1.50rem) !important;
            line-height: 1.2 !important;
            white-space: nowrap !important;
            overflow: hidden !important;
            text-overflow: ellipsis !important;
            letter-spacing: -0.02em;
            word-spacing: -0.04em;
            max-width: 100%;
            display: block;
        }
        div[data-testid="stMetricLabel"] {
            font-size: 0.78rem !important;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        @media (max-width: 1200px) {
            div[data-testid="stMetric"] { padding: 10px 9px !important; }
            div[data-testid="stMetricValue"] { font-size: clamp(0.92rem, 2.0vw, 1.30rem) !important; }
        }
        @media (max-width: 900px) {
            div[data-testid="stMetricValue"] { font-size: 1.10rem !important; }
        }
        section[data-testid="stSidebar"] {
            background: #000000;
            border-left: 1px solid #333333;
        }
        section[data-testid="stSidebar"] * { color: #FFFFFF !important; }
        section[data-testid="stSidebar"] .stRadio label {
            padding: 6px 10px; border-radius: 8px;
        }
        div[data-testid="stMetric"] {
            background: #1A1A1A;
            border: 1px solid #333333;
            border-radius: 12px;
            padding: 12px 14px;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.3);
        }
        div[data-testid="stMetric"] * { color: #FFFFFF !important; }
        .stButton > button {
            border-radius: 8px;
            border: 1px solid #333333;
            background: #1A1A1A;
            color: #FFFFFF;
            transition: background 200ms ease, border-color 200ms ease;
        }
        .stButton > button:hover { border-color: #555555; background: #2A2A2A; }
        .kpi-hint { color: #CCCCCC; font-size: 0.85rem; margin-top: -6px; }
        /* Ensure chart text and labels are visible on black background */
        .stChart, .stVegaLiteChart, .stAltairChart {
            color: #FFFFFF !important;
        }
        .stChart svg text, .stVegaLiteChart svg text, .stAltairChart svg text {
            fill: #FFFFFF !important;
        }
        .stChart svg .tick text, .stVegaLiteChart svg .tick text, .stAltairChart svg .tick text {
            fill: #CCCCCC !important;
            font-size: 12px !important;
        }
        .stChart svg .axis text, .stVegaLiteChart svg .axis text, .stAltairChart svg .axis text {
            fill: #CCCCCC !important;
        }
        .stChart svg .legend text, .stVegaLiteChart svg .legend text, .stAltairChart svg .legend text {
            fill: #CCCCCC !important;
        }
        /* Ensure data table text is readable on black background */
        .stDataFrame {
            color: #FFFFFF !important;
        }
        .stDataFrame th {
            color: #FFFFFF !important;
            background-color: #1A1A1A !important;
            font-weight: 600 !important;
        }
        .stDataFrame td {
            color: #FFFFFF !important;
            background-color: #1A1A1A !important;
        }
        .stDataFrame tr:hover td {
            background-color: #2A2A2A !important;
        }
        @media (prefers-reduced-motion: reduce) {
            .stButton > button { transition: none; }
        }

        </style>
        <script>
        (function(){
            function setTitles(){
                document.querySelectorAll('div[data-testid="stMetricValue"]').forEach(function(el){
                    var txt = el.textContent.trim();
                    if(txt && !el.getAttribute('title')) el.setAttribute('title', txt);
                });
            }
            setTitles();
            var obs = new MutationObserver(setTitles);
            obs.observe(document.body, {childList:true, subtree:true});
        })();
        </script>
        """,
        unsafe_allow_html=True,
    )


def check_dependencies():
    """ينبّه مرة واحدة عند بدء التشغيل لو ناقص مكتبة مطلوبة لقراءة المصادر."""
    missing = []
    for mod, why in (("lxml", "قراءة تقارير Foodics (صيغة HTML داخل .xls)"),
                     ("openpyxl", "قراءة ملفات Excel")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(why)
    if missing:
        st.sidebar.warning(
            "مكتبات ناقصة: " + "، ".join(missing) +
            "\n\nثبّتها بالأمر: `python -m pip install lxml openpyxl`"
        )
        return False
    return True


def sidebar():
    st.sidebar.title("الكوب الأول")
    st.sidebar.caption("نظام إدارة المخزون والمبيعات التفاعلي")
    st.session_state["user_name"] = st.sidebar.text_input("اسم المستخدم", value=current_user())
    check_dependencies()
    pages = [
        "لوحة المعلومات",
        "ملخص مبيعات العملاء والفروع",
        "مبيعات Foodics",
        "مبيعات التطبيقات والموقع",
        "المخزون والحركات",
        "الموازنة والتوقعات",
        "التحليل المالي",
        "استيراد البيانات",
        "الأصناف والحسابات",
        "التنبيهات",
        "الإعدادات",
        "تصدير Excel",
    ]
    count = open_alerts_count()
    if count:
        st.sidebar.error(f"لديك {count} تنبيه غير محلول")
    space("small")
    st.sidebar.checkbox(
        "إظهار صف التجميع أسفل الجداول",
        value=True,
        key="show_table_totals",
        help="تجميع تلقائي للأعمدة الرقمية (المبالغ والكميات) في نهاية كل جدول",
    )
    return st.sidebar.radio("انتقل إلى", pages)


def item_selector(label, prefix=None, key=None):
    """قائمة اختيار الأصناف مع البحث. prefix=GB للأخضر، RB للمحمص."""
    items = list_items()
    if prefix:
        items = items[items["code"].str.upper().str.startswith(prefix)]
    if items.empty:
        st.info("لا توجد أصناف بعد. أضِف أصنافًا أو استورد بياناتك أولًا.")
        return None
    
    # Create search box for filtering
    search_key = f"{key}_search" if key else None
    if search_key:
        search_text = search_box(f"بحث في {label}", search_key, "اكتب جزء من الكود أو الاسم...")
    else:
        search_text = ""
    
    # Filter options based on search
    options = items["code"].tolist()
    labels = {r["code"]: f"{r['code']} — {r['name']}" for _, r in items.iterrows()}
    
    if search_text and len(search_text) >= 2:
        search_lower = search_text.lower()
        filtered_options = [
            c for c in options 
            if search_lower in c.lower() or search_lower in labels[c].lower()
        ]
        if not filtered_options:
            st.warning("لا توجد نتائج مطابقة")
            filtered_options = options[:1]  # Keep at least one option
        options = filtered_options
    
    return st.selectbox(label, options, format_func=lambda c: labels.get(c, c), key=key)


# ---- صفحة لوحة المعلومات ----
def page_dashboard():
    st.header("لوحة المعلومات")

    yrs = year_options()
    if not yrs:
        st.info("لا توجد سنوات بعد. ارفع ملفات الأشهر/الأعوام عبر صفحة الاستيراد.")
        return
    year = st.selectbox("السنة", yrs, index=year_default_index(yrs),
                        format_func=lambda y: f"عام {int(y)}", key="dash_year")
    
    inv = movements_df(year)
    stock = current_stock()
    fb = foodics_branch_df(year)
    if inv.empty and fb.empty:
        st.info("لا توجد بيانات بعد. ابدأ من صفحة «استيراد البيانات» أو أدخل مبيعات ودفعات تحميص.")
        return

    sales = inv[inv["نوع الحركة"] == SALE] if not inv.empty else pd.DataFrame()
    total_sales = sales["الإجمالي"].sum() if not sales.empty else 0
    stock_value = stock["قيمة الرصيد"].sum() if not stock.empty else 0
    settings = get_settings()
    roasting = build_roasting(inv, s_float(settings, "roast_yield"), s_float(settings, "roast_tolerance")) if not inv.empty else pd.DataFrame()
    avg_yield = 0
    if not roasting.empty and roasting["كمية الأخضر المحولة"].sum():
        avg_yield = roasting["كمية المحمص الناتجة"].sum() / roasting["كمية الأخضر المحولة"].sum() * 100
    pos_count = int((stock["الرصيد الحالي"] > 0).sum()) if not stock.empty else 0
    foodics_net = float(fb["صافي المبيعات"].sum()) if not fb.empty else 0

    c1, c2, c3, c4, c5 = st.columns(5)
    kpi(c1, "مبيعات الحركات", f"{total_sales:,.0f}")
    kpi(c2, "مبيعات Foodics", f"{foodics_net:,.0f}")
    kpi(c3, "أصناف برصيد", f"{pos_count}")
    kpi(c4, "قيمة المخزون", f"{stock_value:,.0f}")
    kpi(c5, "كفاءة التحميص", f"{avg_yield:,.1f}%")

    low = low_stock_items()
    neg = stock[stock["الرصيد الحالي"] < 0] if not stock.empty else pd.DataFrame()
    if not neg.empty or not low.empty:
        space("medium")
        a1, a2 = st.columns(2)
        if not neg.empty:
            a1.error(f"{len(neg)} صنف برصيد سالب — راجع صفحة المخزون.")
        if not low.empty:
            a2.warning(f"{len(low)} صنف بمخزون منخفض (حد {s_float(settings, 'low_stock_qty'):g}).")
            with st.expander("عرض الأصناف ذات المخزون المنخفض"):
                st.dataframe(low, hide_index=True)

    space("medium")
    col_a, col_b = st.columns(2)
    with col_a, card("المبيعات حسب الشهر"):
        if not sales.empty:
            by_month = sales.groupby("الشهر")["الإجمالي"].sum()
            st.bar_chart(by_month)
        else:
            st.caption("لا توجد مبيعات مسجلة بعد.")
    with col_b, card("توزيع الحركات حسب النوع"):
        if not inv.empty:
            by_type = inv.groupby("نوع الحركة")["حركة الكمية"].count()
            st.bar_chart(by_type)
        elif not fb.empty:
            st.caption("مبيعات فروع Foodics")
            st.bar_chart(fb.set_index("الفرع")["صافي المبيعات"])
        else:
            st.caption("لا توجد حركات بعد.")

    if not stock.empty:
        with card("الرصيد الحالي للأصناف"):
            st.caption("تُعرض الأصناف التي رصيدها أكبر من صفر فقط.")
            positive = stock[stock["الرصيد الحالي"] > 0]

            # Add item search
            search_dash = search_box("بحث بالصنف (كود أو اسم)", "dash_stock_search",
                                     "اكتب جزء من كود أو اسم الصنف...")
            if search_dash:
                positive = positive[search_mask(positive, ["كود الصنف", "اسم الصنف"], search_dash)]

            st.dataframe(positive, hide_index=True)

    if not fb.empty:
        with card("ملخص فروع Foodics"):
            st.caption(f"صافي المبيعات: {foodics_net:,.0f} — عدد الفروع: {len(fb)}")
            cc1, cc2 = st.columns(2)
            with cc1:
                show = fb[["الفرع", "صافي المبيعات", "عدد الطلبات", "متوسط الطلب"]].copy()
                show["صافي المبيعات"] = show["صافي المبيعات"].round(0)
                show["متوسط الطلب"] = show["متوسط الطلب"].round(2)

                # Add branch search
                search_fb = search_box("بحث بالفرع", "dash_fb_search", "اكتب جزء من اسم الفرع...")
                if search_fb:
                    show = show[search_mask(show, ["الفرع"], search_fb)]

                st.dataframe(show, hide_index=True)
            with cc2:
                st.bar_chart(show.set_index("الفرع")["صافي المبيعات"])

    detail, by_branch = branch_sales_summary(year)
    if not by_branch.empty:
        with card("المبيعات حسب الفرع (فواتير)"):
            total_branch = detail["المبلغ"].sum()
            st.caption(f"عدد الفواتير: {len(detail):,} — إجمالي المبيعات: {total_branch:,.0f}")
            cc1, cc2 = st.columns(2)
            with cc1:
                st.dataframe(by_branch, hide_index=True)
            with cc2:
                st.bar_chart(by_branch.set_index("الفرع")["إجمالي المبيعات"])
            with st.expander("تفاصيل الفواتير لكل فرع"):
                branches = ["(الكل)"] + by_branch["الفرع"].tolist()
                pick = st.selectbox("اختر الفرع", branches, key="dash_branch_pick")
                view = detail if pick == "(الكل)" else detail[detail["الفرع"] == pick]

                # Add search
                search_inv = search_box("بحث في الفاتورة / العميل / المرجع", "dash_inv_search",
                                        "اكتب رقم الفاتورة أو اسم العميل أو المرجع...")
                if search_inv:
                    view = view[search_mask(view, ["رقم الفاتورة", "العميل", "المرجع"], search_inv)]

                st.dataframe(view, hide_index=True)


# ---- صفحة ملخص مبيعات العملاء والفروع ----
def page_sales():
    st.header("ملخص مبيعات العملاء والفروع")
    st.caption("سجّل عملية بيع؛ يُخصم المخزون تلقائيًا ويُحدَّث لوح المعلومات فورًا.")
    with st.form("sale_form", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            d = st.date_input("التاريخ", value=date.today())
            item = item_selector("الصنف", key="sale_item")
            qty = st.number_input("الكمية", min_value=0.0, step=1.0)
        with col2:
            price = st.number_input("سعر الوحدة", min_value=0.0, step=1.0)
            branch = st.text_input("الفرع")
            customer = st.text_input("العميل")
        reference = st.text_input("رقم المرجع / الفاتورة")
        submitted = st.form_submit_button("حفظ عملية البيع")

    if submitted:
        if not item or qty <= 0:
            st.error("اختر صنفًا وأدخل كمية أكبر من صفر.")
            return
        available = stock_of(item)
        add_movement(
            year=d.year, month=d.month, mdate=d.isoformat(), item_code=item,
            item_name=_name_of(item), qty=-abs(qty), total=qty * price, value=0,
            movement_type=SALE, branch=branch, contact=customer, reference=reference,
            description="بيع يدوي",
        )
        audit("sale", f"{item} x{qty} @ {price}")
        st.success(f"تم تسجيل بيع {qty:g} من {item} بقيمة {qty * price:,.0f}.")
        if available - qty < 0:
            add_alert("تحذير", "مخزون", f"الصنف {item} أصبح رصيده سالبًا بعد البيع ({available - qty:g}).")
            st.warning(f"تنبيه: الرصيد أصبح سالبًا ({available - qty:g}). سُجّل تنبيه.")

    _sales_summary_section()


def _sales_summary_section():
    """ملخص مختصر ومجمّع لمبيعات العملاء والفروع من تقرير الإكسيل — أساس سنوي/شهري."""
    with card("ملخص مبيعات العملاء والفروع (من تقرير الإكسيل)"):
        syears = query_df("SELECT DISTINCT year FROM branch_sales ORDER BY year")
        if syears.empty:
            st.info("لا توجد بيانات مبيعات مستوردة بعد. استورد «تقرير المبيعات اليومية لكل فرع» من صفحة الاستيراد.")
            return
        yrs = year_options([int(y) for y in syears["year"].tolist()])
        c1, c2 = st.columns([1, 2])
        basis = c1.radio("الأساس", ["سنوي", "شهري"], horizontal=True, key="bs_basis")
        year = c2.selectbox("السنة", yrs, index=year_default_index(yrs), format_func=lambda y: f"عام {int(y)}", key="bs_year")
        month = None
        if basis == "شهري":
            mons = query_df("SELECT DISTINCT month FROM branch_sales WHERE year=? ORDER BY month", (int(year),))
            mlist = [int(x) for x in mons["month"].tolist()] or list(range(1, 13))
            month = st.selectbox("الشهر", mlist,
                                 format_func=lambda x: f"{x} - {MONTH_NAMES_AR[x - 1]}", key="bs_month")

        g = branch_sales_grouped(year, month)
        if g.empty:
            st.info("لا توجد فواتير في هذه الفترة.")
            return

        # Add customer search
        search_cust = search_box("بحث بالعميل", "bs_search_cust", "اكتب جزء من اسم العميل...")
        if search_cust:
            g = g[search_mask(g, ["العميل"], search_cust)]

        label = f"عام {int(year)}" if not month else f"{MONTH_NAMES_AR[month - 1]} {int(year)}"
        k1, k2, k3, k4 = st.columns(4)
        kpi(k1, "إجمالي المبيعات", f"{g['إجمالي المبيعات'].sum():,.0f}")
        kpi(k2, "عدد الفواتير", f"{int(g['عدد الفواتير'].sum()):,}")
        kpi(k3, "عدد العملاء", f"{g['العميل'].nunique():,}")
        kpi(k4, "عدد الفروع", f"{g['الفرع'].nunique():,}")
        st.caption(f"مبيعات {label} — مجمّعة حسب العميل (Contact) مع توضيح الفرع (Branches).")
        per_cust = g.groupby("العميل", as_index=False).agg(
            **{"عدد الفواتير": ("عدد الفواتير", "sum"), "مجموع المبيعات": ("إجمالي المبيعات", "sum")}
        )
        brs = (g.groupby("العميل")["الفرع"]
               .apply(lambda s: "، ".join(sorted(set(str(x) for x in s))))
               .reset_index().rename(columns={"الفرع": "الفروع"}))
        per_cust = (per_cust.merge(brs, on="العميل", how="left")
                    .sort_values("مجموع المبيعات", ascending=False).reset_index(drop=True))
        per_cust["مجموع المبيعات"] = per_cust["مجموع المبيعات"].round(2)
        cc = money_columns({"مجموع المبيعات": MONEY_FMT})
        st.dataframe(per_cust, hide_index=True, **({"column_config": cc} if cc else {}))

    with st.expander("تفصيل حسب العميل والفرع"):
        detail = g.copy()
        detail["إجمالي المبيعات"] = detail["إجمالي المبيعات"].round(2)
        st.dataframe(detail, hide_index=True)

    by_branch = g.groupby("الفرع", as_index=False).agg(
        **{"عدد الفواتير": ("عدد الفواتير", "sum"), "إجمالي المبيعات": ("إجمالي المبيعات", "sum")}
    ).sort_values("إجمالي المبيعات", ascending=False).reset_index(drop=True)
    by_branch["إجمالي المبيعات"] = by_branch["إجمالي المبيعات"].round(2)
    with st.expander("عرض مختصر حسب الفرع"):
        st.dataframe(by_branch, hide_index=True)


def _name_of(code):
    items = list_items()
    row = items[items["code"] == code]
    return row["name"].iloc[0] if not row.empty else code


# ---- صفحة دفعات التحميص ----
def page_roasting():
    st.header("دفعات التحميص")
    st.caption("أدخل كمية البن الأخضر الخارجة والمحمص الناتج؛ تُحسب نسبة الإنتاج (Yield) لحظيًا مع تنبيه عند تجاوز الحد.")
    settings = get_settings()
    target = s_float(settings, "roast_yield")
    tol = s_float(settings, "roast_tolerance")

    green = st.number_input("كمية البن الأخضر الخارجة (كجم)", min_value=0.0, step=1.0, key="g_kg")
    roasted = st.number_input("كمية البن المحمص الناتجة (كجم)", min_value=0.0, step=1.0, key="r_kg")

    if green > 0:
        yield_ratio = roasted / green
        diff = yield_ratio - target
        ok = abs(diff) <= tol
        c1, c2, c3 = st.columns(3)
        c1.metric("النسبة الفعلية", f"{yield_ratio * 100:,.1f}%")
        c2.metric("النسبة المستهدفة", f"{target * 100:,.1f}%")
        c3.metric("الحالة", "مقبول" if ok else "يحتاج مراجعة")
        if ok:
            st.success("النسبة ضمن الحد المسموح.")
        else:
            st.warning(f"النسبة خارج الحد المسموح (±{tol * 100:.1f}%).")

    with st.form("roast_form", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            d = st.date_input("تاريخ الدفعة", value=date.today())
            green_item = item_selector("صنف البن الأخضر", prefix="GB", key="roast_green")
        with col2:
            reference = st.text_input("رقم الدفعة / المرجع")
            roasted_item = item_selector("صنف البن المحمص", prefix="RB", key="roast_roasted")
        submitted = st.form_submit_button("حفظ دفعة التحميص")

    if submitted:
        if not green_item or not roasted_item or green <= 0 or roasted <= 0:
            st.error("اختر صنفي الأخضر والمحمص وأدخل كميتين أكبر من صفر.")
            return
        add_movement(
            year=d.year, month=d.month, mdate=d.isoformat(), item_code=green_item,
            item_name=_name_of(green_item), qty=-abs(green), movement_type=GREEN_OUT,
            reference=reference, description="دفعة تحميص",
        )
        add_movement(
            year=d.year, month=d.month, mdate=d.isoformat(), item_code=roasted_item,
            item_name=_name_of(roasted_item), qty=abs(roasted), movement_type=ROASTED_IN,
            reference=reference, description="دفعة تحميص",
        )
        ratio = roasted / green if green else 0
        audit("roast_batch", f"{green_item}->{roasted_item} yield={ratio:.3f}")
        st.success(f"تم حفظ الدفعة. النسبة الفعلية {ratio * 100:.1f}%.")
        if abs(ratio - target) > tol:
            add_alert("مراجعة", "تحميص",
                      f"دفعة {reference or '-'}: نسبة الإنتاج {ratio * 100:.1f}% خارج الحد "
                      f"(المستهدف {target * 100:.1f}% ±{tol * 100:.1f}%).")
            st.warning("سُجّل تنبيه لأن النسبة خارج الحد المسموح.")


# ---- صفحة المخزون والحركات ----
def _render_coffee_type_totals(df, qty_col, val_col, qty_label, val_label, caption):
    """تجميع الكمية والمبلغ حسب نوع البن (أخضر / محمص) للفترة المعروضة."""
    if df is None or df.empty or "نوع البن" not in df.columns:
        return
    g = df.copy()
    g["نوع البن"] = g["نوع البن"].apply(
        lambda x: "بن أخضر" if "أخضر" in str(x) else ("بن محمص" if "محمص" in str(x) else "أخرى"))
    agg = (g.groupby("نوع البن")
             .agg(**{qty_label: (qty_col, "sum"), val_label: (val_col, "sum")})
             .reset_index())
    order = {"بن أخضر": 0, "بن محمص": 1, "أخرى": 2}
    agg["_o"] = agg["نوع البن"].map(order).fillna(9)
    agg = agg.sort_values("_o").drop(columns="_o")
    total = pd.DataFrame({"نوع البن": ["الإجمالي"],
                          qty_label: [agg[qty_label].sum()], val_label: [agg[val_label].sum()]})
    out = pd.concat([agg, total], ignore_index=True)
    out[qty_label] = out[qty_label].round(1)
    out[val_label] = out[val_label].round(1)
    st.markdown(f"**{caption} (كميات ومبلغ):**")
    st.dataframe(out, hide_index=True)


def _render_movement_summary(summ, caption, key_all):
    if summ is None or summ.empty:
        st.info("لا توجد حركات لهذه الفترة.")
        return
    st.caption(caption)

    # Add item search
    search_key = f"{key_all}_search"
    search_item = search_box("بحث بالصنف (كود أو اسم)", search_key, "اكتب جزء من كود أو اسم الصنف...")

    m1, m2, m3 = st.columns(3)
    kpi(m1, "إجمالي الوارد", f"{summ['الوارد'].sum():,.0f}")
    kpi(m2, "إجمالي المنصرف", f"{summ['المنصرف'].sum():,.0f}")
    kpi(m3, "صافي الحركة", f"{summ['صافي الحركة'].sum():,.0f}")
    _render_coffee_type_totals(summ, "صافي الحركة", "قيمة الحركة",
                               "صافي الحركة", "قيمة الحركة", "تجميع الحركة حسب نوع البن")
    show_all = st.checkbox("إظهار الأصناف بلا حركة أيضًا", value=False, key=key_all)
    view = summ if show_all else summ[(summ["الوارد"] != 0) | (summ["المنصرف"] != 0)]

    if search_item:
        view = view[search_mask(view, ["كود الصنف", "اسم الصنف"], search_item)]

    cc = money_columns({"صافي الحركة": QTY_FMT, "قيمة الحركة": MONEY_FMT})
    st.dataframe(view, hide_index=True, **({"column_config": cc} if cc else {}))


def page_inventory():
    st.header("المخزون والحركات")

    tab1, tab_m, tab_y, tab2, tab3 = st.tabs(
        ["الرصيد الحالي", "رصيد شهري", "رصيد سنوي", "بيان الصنف المجمّع", "سجل الحركات"]
    )

    with tab1:
        stock = current_stock()
        if stock.empty:
            st.info("لا توجد حركات مخزون بعد.")
        else:
            with card("الرصيد الحالي"):
                q1, q2 = st.columns([2, 1])
                search = search_box("بحث بالكود أو الاسم", "inv_search", "اكتب جزء من الكود أو الاسم...")
                show_all = q2.checkbox("إظهار كل الأصناف (بما فيها الصفر والسالب)", value=False)
                view = stock if show_all else stock[stock["الرصيد الحالي"] > 0]
                if search:
                    view = view[search_mask(view, ["كود الصنف", "اسم الصنف"], search)]
                st.caption("افتراضيًا تُعرض الأصناف التي رصيدها أكبر من صفر فقط.")
                _render_coffee_type_totals(view, "الرصيد الحالي", "قيمة الرصيد",
                                           "الكمية", "القيمة", "تجميع رصيد المخزون حسب نوع البن")
                cc = money_columns({"قيمة الرصيد": MONEY_FMT, "الرصيد الحالي": QTY_FMT})
                st.dataframe(view, hide_index=True, **({"column_config": cc} if cc else {}))
                neg = stock[stock["الرصيد الحالي"] < 0]
                if not neg.empty:
                    st.warning(f"يوجد {len(neg)} صنف برصيد سالب (فعّل الخيار أعلاه لعرضها).")
                low = low_stock_items()
                if not low.empty:
                    st.warning(f"يوجد {len(low)} صنف بمخزون منخفض.")

            # ---- تقرير الأصناف الراكدة ----
            with st.expander("تقرير الأصناف الراكدة (بطيئة الحركة)", expanded=False):
                st.caption("أصناف يتوفر منها رصيد حالي بالمستودع ولكن لم تسجل أي حركة خروج أو بيع منذ فترة محددة.")
                c_sl, c_fl = st.columns([2, 1])
                stagnant_days = c_sl.slider(
                    "معيار الركود (عدد الأيام بدون أي حركة خروج أو بيع)",
                    min_value=15,
                    max_value=365,
                    value=60,
                    step=15,
                    key="stagnant_days_slider",
                    help="يحدد الأصناف التي تجاوزت هذه المدة دون أي عملية صرف أو بيع أو تحميص"
                )
                stag_df = stagnant_items_df(stagnant_days, stock)
                if stag_df.empty:
                    st.success(f"ممتاز! لا توجد أصناف راكدة تجاوزت {stagnant_days} يوماً بدون حركة خروج.")
                else:
                    coffee_types = ["الكل"] + sorted(stag_df["نوع البن"].dropna().unique().tolist())
                    c_pick = c_fl.selectbox("تصفية حسب نوع البن", coffee_types, key="stag_coffee_pick")
                    if c_pick != "الكل":
                        stag_df = stag_df[stag_df["نوع البن"] == c_pick]

                    m1, m2, m3, m4 = st.columns(4)
                    kpi(m1, "عدد الأصناف الراكدة", f"{len(stag_df):,}")
                    kpi(m2, "إجمالي الكمية الراكدة", f"{stag_df['الرصيد الحالي'].sum():,.1f} كجم")
                    kpi(m3, "قيمة رأس المال المجمد", f"{stag_df['قيمة الرصيد'].sum():,.2f} ر.س")
                    max_days = stag_df["أيام بدون حركة"].max()
                    kpi(m4, "أطول فترة ركود", f"{max_days:,} يوم" if max_days < 999 else "غير محدد")

                    cc = money_columns({"قيمة الرصيد": MONEY_FMT, "الرصيد الحالي": QTY_FMT})
                    st.dataframe(stag_df, hide_index=True, **({"column_config": cc} if cc else {}))

    with tab_m:
        yrs = movement_years()
        if not yrs:
            st.info("لا توجد حركات مخزون بعد.")
        else:
            c1, c2 = st.columns(2)
            y = c1.selectbox("السنة", yrs, index=len(yrs) - 1, format_func=lambda v: f"عام {int(v)}", key="inv_m_year")
            mons = movement_months(y)
            if not mons:
                st.info("لا توجد حركات لهذه السنة.")
            else:
                mo = c2.selectbox("الشهر", mons,
                                  format_func=lambda x: f"{x} - {MONTH_NAMES_AR[x - 1]}", key="inv_m_month")
                _render_movement_summary(
                    movement_period_summary(y, mo),
                    f"حركة المخزون لكل صنف خلال {MONTH_NAMES_AR[mo - 1]} {y} (من مدخلات الإكسيل).",
                    "inv_m_all",
                )

    with tab_y:
        yrs = movement_years()
        if not yrs:
            st.info("لا توجد حركات مخزون بعد.")
        else:
            y = st.selectbox("السنة", yrs, index=len(yrs) - 1, format_func=lambda v: f"عام {int(v)}", key="inv_y_year")
            _render_movement_summary(
                movement_period_summary(y, None),
                f"إجمالي حركة المخزون لكل صنف خلال عام {y} (من مدخلات الإكسيل).",
                "inv_y_all",
            )

    with tab2:
        with card("بيان مجمّع للصنف — من سجل الحركات"):
            st.caption("لكل صنف: ماذا تم عليه من تحميص، فروقات جردية، بيع، شراء/وارد، صرف للفروع، عينات — "
                       "مفلتراً على مستوى العام والشهر.")
            yrs = movement_years()
            if not yrs:
                st.info("لا توجد حركات مخزون بعد.")
            else:
                c1, c2 = st.columns(2)
                basis = c1.radio("الأساس", ["سنوي", "شهري"], horizontal=True, key="stmt_basis")
                y = c2.selectbox("السنة", yrs, index=len(yrs) - 1, format_func=lambda v: f"عام {int(v)}", key="stmt_year")
                mo = None
                if basis == "شهري":
                    mons = movement_months(y)
                    if mons:
                        mo = st.selectbox("الشهر", mons,
                                          format_func=lambda x: f"{x} - {MONTH_NAMES_AR[x - 1]}", key="stmt_month")
                stmt = item_movement_statement(y, mo)
                if stmt.empty:
                    st.info("لا توجد حركات في هذه الفترة.")
                else:
                    label = f"عام {y}" if not mo else f"{MONTH_NAMES_AR[mo - 1]} {y}"

                    # Add item search
                    search_item = search_box("بحث بالصنف (كود أو اسم)", "stmt_search", "اكتب جزء من كود أو اسم الصنف...")
                    if search_item:
                        stmt = stmt[search_mask(stmt, ["كود الصنف", "اسم الصنف"], search_item)]

                    totals = {c: stmt[c].sum() for c in stmt.columns if c not in ("كود الصنف", "اسم الصنف")}
                    k = st.columns(4)
                    kpi(k[0], "عدد الأصناف المتحرّكة", f"{len(stmt):,}")
                    kpi(k[1], "تحميص (دخول محمص)", f"{totals.get('تحميص (دخول محمص)', 0):,.0f}")
                    kpi(k[2], "بيع", f"{totals.get('بيع', 0):,.0f}")
                    kpi(k[3], "فروقات جردية", f"{totals.get('فروقات جردية', 0):,.0f}")
                    st.caption(f"بيان الحركات المجمّع لكل صنف — {label} (الكميات؛ الموجب وارد والسالب منصرف).")
                    st.dataframe(stmt, hide_index=True)

        with st.expander("إدخال يدوي: صرف للفروع / تسوية جرد"):
            with st.form("dispatch_form", clear_on_submit=True):
                d = st.date_input("التاريخ", value=date.today(), key="disp_date")
                item = item_selector("الصنف", prefix="RB", key="disp_item")
                qty = st.number_input("الكمية المصروفة", min_value=0.0, step=1.0, key="disp_qty")
                branch = st.text_input("الفرع", key="disp_branch")
                if st.form_submit_button("حفظ الصرف") and item and qty > 0:
                    add_movement(year=d.year, month=d.month, mdate=d.isoformat(), item_code=item,
                                 item_name=_name_of(item), qty=-abs(qty), movement_type=BRANCH_OUT,
                                 branch=branch, description="صرف للفرع")
                    audit("branch_out", f"{item} x{qty} -> {branch}")
                    st.success("تم تسجيل الصرف.")

            with st.form("adjust_form", clear_on_submit=True):
                d2 = st.date_input("التاريخ", value=date.today(), key="adj_date")
                item2 = item_selector("الصنف", key="adj_item")
                delta = st.number_input("قيمة التسوية (+ زيادة / - نقص)", step=1.0, key="adj_delta")
                reason = st.text_input("السبب", key="adj_reason")
                if st.form_submit_button("حفظ التسوية") and item2 and delta != 0:
                    add_movement(year=d2.year, month=d2.month, mdate=d2.isoformat(), item_code=item2,
                                 item_name=_name_of(item2), qty=delta, movement_type=ADJUST,
                                 description=reason or "تسوية جرد")
                    audit("adjust", f"{item2} {delta:+g} ({reason})")
                    st.success("تم تسجيل التسوية.")

    with tab3:
        inv = movements_df()
        if inv.empty:
            st.info("لا توجد حركات.")
        else:
            # Year/Month filter
            yrs = sorted(inv["السنة"].dropna().unique().tolist())
            f_year, f_month, f_search = st.columns([1, 1, 2])
            year_pick = f_year.selectbox("السنة", ["الكل"] + yrs, key="mov_year")
            if year_pick != "الكل":
                inv = inv[inv["السنة"] == int(year_pick)]
                mons = sorted(inv["الشهر"].dropna().unique().tolist())
            else:
                mons = list(range(1, 13))
            month_pick = f_month.selectbox("الشهر", ["الكل"] + mons, key="mov_month",
                                           format_func=lambda x: f"{x} - {MONTH_NAMES_AR[x - 1]}" if x != "الكل" else "الكل")
            if month_pick != "الكل":
                inv = inv[inv["الشهر"] == int(month_pick)]
            
            types = ["الكل"] + sorted(inv["نوع الحركة"].dropna().unique().tolist())
            f1, f2 = st.columns([1, 2])
            pick = f1.selectbox("تصفية حسب نوع الحركة", types)
            q = f2.text_input("بحث في الصنف / المرجع / العميل", key="mov_search")
            view = inv if pick == "الكل" else inv[inv["نوع الحركة"] == pick]
            if q:
                view = view[search_mask(
                    view,
                    ("كود الصنف", "اسم الصنف", "المرجع", "العميل", "العميل أو المورد", "الوصف"),
                    q,
                )]
            
            st.caption(f"عرض آخر {min(500, len(view))} حركة من أصل {len(view):,}.")
            st.dataframe(view.tail(500), hide_index=True)


# ---- صفحة الموازنة والتوقعات — إعادة تصميم للوضوح (المرحلة 1b) ----
def _budget_help_box(icon, title, body):
    """صندوق شرح صغير بلون هادئ — يوحّد شكل كل التبويبات."""
    st.markdown(
        f"<div style='background:#1E293B;border:1px solid #334155;border-radius:10px;"
        f"padding:12px 16px;margin:6px 0 14px 0'>"
        f"<span style='font-size:1.15rem'>{icon}</span> "
        f"<b style='color:#E2E8F0'>{title}</b><br>"
        f"<span style='color:#94A3B8;font-size:0.92rem'>{body}</span></div>",
        unsafe_allow_html=True,
    )


def page_budget():
    st.header("الموازنة والتوقعات")
    st.caption("خطّط للسنة القادمة، وتوقّع الأشهر القادمة، وراقب هل أنت على المسار.")

    # ── بطاقة تمهيدية: ما الذي تراه في هذه الصفحة؟ ──
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns(4)
        c1.markdown("**① الموازنة**<br><span style='color:#94A3B8;font-size:0.85rem'>كم نتوقع أن نربح/ننفق السنة القادمة؟</span>", unsafe_allow_html=True)
        c2.markdown("**② التوقعات**<br><span style='color:#94A3B8;font-size:0.85rem'>ماذا سيحدث في الأشهر الـ 12 القادمة؟</span>", unsafe_allow_html=True)
        c3.markdown("**③ ماذا لو؟**<br><span style='color:#94A3B8;font-size:0.85rem'>لو نمت 5% أو 15% — ماذا يتغيّر؟</span>", unsafe_allow_html=True)
        c4.markdown("**④ هل نحن على المسار؟**<br><span style='color:#94A3B8;font-size:0.85rem'>الفعلي مقابل ما خططنا له</span>", unsafe_allow_html=True)

    settings = get_settings()
    default_growth = s_float(settings, "growth_rate")

    yrs = query_df("SELECT DISTINCT year FROM pnl_monthly ORDER BY year")
    if yrs.empty:
        st.info("لا توجد قائمة دخل شهرية بعد. ارفع ملفات الأشهر/الأعوام (Current financial year by month) ثم استورد من صفحة «استيراد البيانات».")
        return
    years = year_options([int(y) for y in yrs["year"].tolist()])

    # ================================================================
    # القسم 1 — إنشاء الموازنة (معالج خطوة بخطوة)
    # ================================================================
    st.subheader("① إنشاء الموازنة")
    _budget_help_box(
        "💡", "كيف تُحسب الموازنة؟",
        "نأخذ <b>فعلي سنة سابقة</b> (مثلاً كل مصاريف وإيرادات 2024 شهرًا بشهر) ونضربها في "
        "<b>(1 + نسبة النمو)</b>. مثال: إيراد يناير 2024 = 100,000 ونسبة النمو 10% → موازنة يناير 2025 = 110,000. "
        "يمكنك تعديل أي بند لاحقًا يدويًا.",
    )

    # مثال حسابي حي
    with st.container(border=True):
        st.markdown("**مثال سريع — كيف تُطبّق نسبة النمو؟**")
        ex_base = st.number_input("جرّب: فعلي شهر سابق", value=100000.0, step=10000.0, key="budget_example_base")
        ex_g = st.slider("نسبة النمو للتجربة", -20, 50, int(default_growth * 100), key="budget_example_g") / 100.0
        ex_result = ex_base * (1 + ex_g)
        e1, e2, e3 = st.columns(3)
        e1.metric("الفعلي السابق", f"{ex_base:,.0f}")
        e2.metric("نسبة النمو", f"{ex_g * 100:+.0f}%")
        e3.metric("الموازنة المقترحة", f"{ex_result:,.0f}", delta=f"{ex_result - ex_base:+,.0f}")

    c1, c2, c3 = st.columns(3)
    base_year = c1.selectbox(
        "سنة الأساس (الفعلي)",
        years, index=len(years) - 1,
        format_func=lambda y: f"عام {int(y)}",
        help="السنة التي نأخذ أرقامها الفعلية كأساس. عادةً آخر سنة مكتملة.",
    )
    budget_opts = sorted(set(years + [int(base_year) + 1]))
    default_idx = budget_opts.index(int(base_year) + 1) if int(base_year) + 1 in budget_opts else len(budget_opts) - 1
    budget_year = c2.selectbox(
        "سنة الموازنة (المستهدفة)",
        budget_opts, index=default_idx,
        format_func=lambda y: f"عام {int(y)}",
        help="السنة التي نُنشئ لها الموازنة. عادةً السنة القادمة.",
    )
    growth_pct = c3.number_input(
        "نسبة النمو %", value=float(default_growth * 100), step=1.0,
        help="نسبة واحدة تُطبّق على كل البنود. مثال: 10 تعني كل بند ينمو 10%. يمكنك تعديل بنود محددة لاحقًا.",
    )
    growth = growth_pct / 100.0

    # ملخص ما سيحدث قبل الضغط
    preview_count = query_df(
        "SELECT COUNT(DISTINCT account_code) AS c FROM pnl_monthly WHERE year=?", (int(base_year),)
    )
    n_accounts = int(preview_count["c"].iloc[0]) if not preview_count.empty else 0
    st.caption(f"سيتم إنشاء موازنة لـ **{n_accounts} حساب × 12 شهر** = {n_accounts * 12} بندًا لعام {int(budget_year)} على أساس عام {int(base_year)} بنسبة {growth_pct:+.0f}%.")

    if st.button("توليد/تحديث الموازنة الشهرية", type="primary", use_container_width=True):
        generated = build_budget_from_monthly(base_year, growth, int(budget_year))
        execute("DELETE FROM budget WHERE year=?", (int(budget_year),))
        if not generated.empty:
            seq = [
                (int(r["السنة"]), int(r["الشهر"]), str(r["كود الحساب"]), str(r["اسم الحساب"]),
                 str(r["نوع الحساب"]), float(r["actual_prev"]), float(r["growth"]),
                 float(r["proposed"]), float(r["approved"]))
                for _, r in generated.iterrows()
            ]
            executemany(
                "INSERT INTO budget(year, month, account_code, account_name, account_type, "
                "actual_prev, growth, proposed, approved) VALUES(?,?,?,?,?,?,?,?,?)", seq,
            )
        audit("budget_generate", f"{len(generated)} rows y={budget_year} base={base_year}")
        st.success(f"تم توليد {len(generated)} بند موازنة لعام {int(budget_year)} على أساس {int(base_year)} — راجع الجدول أدناه.")
        rerun()

    # ── عرض الموازنة الحالية ──
    budget = query_df(
        "SELECT id, year, month AS 'الشهر', account_code AS 'كود الحساب', account_name AS 'اسم الحساب', "
        "actual_prev AS 'فعلي الأساس', growth AS 'النمو', proposed AS 'موازنة مقترحة', "
        "approved AS 'موازنة معتمدة' FROM budget ORDER BY year, account_code, month"
    )
    if budget.empty:
        st.info("لا توجد موازنة بعد — اضغط «توليد/تحديث الموازنة الشهرية» أعلاه.")
    else:
        st.divider()
        st.subheader("② الموازنة الحالية")
        byears = sorted(int(y) for y in budget["year"].unique())
        vy = st.selectbox("سنة الموازنة المعروضة", byears, index=len(byears) - 1,
                          format_func=lambda y: f"عام {int(y)}", key="budget_view_year")
        b = budget[budget["year"] == vy].drop(columns=["year"]).copy()

        # تبويب العرض: سنوي vs شهري
        tab_annual, tab_monthly = st.tabs(["ملخص سنوي", "تفصيل شهري + تحرير"])
        with tab_annual:
            _budget_help_box("📊", "الملخص السنوي",
                             "مجموع 12 شهرًا لكل حساب. «فعلي الأساس» = مجموع ما صُرف/رُبح في سنة الأساس. "
                             "«مقترحة» = فعلي × (1+النمو). «معتمدة» = ما اعتمدته الإدارة (قابل للتعديل).")
            agg = b.groupby(["كود الحساب", "اسم الحساب"], as_index=False)[
                ["فعلي الأساس", "موازنة مقترحة", "موازنة معتمدة"]].sum()
            agg["الزيادة المقترحة"] = agg["موازنة مقترحة"] - agg["فعلي الأساس"]
            m1, m2, m3, m4 = st.columns(4)
            kpi(m1, "إجمالي الفعلي (الأساس)", f"{agg['فعلي الأساس'].sum():,.0f}")
            kpi(m2, "إجمالي المقترحة", f"{agg['موازنة مقترحة'].sum():,.0f}")
            kpi(m3, "الزيادة", f"{agg['الزيادة المقترحة'].sum():,.0f}")
            kpi(m4, "إجمالي المعتمدة", f"{agg['موازنة معتمدة'].sum():,.0f}")
            # رسم مبسط
            try:
                chart_df = agg.set_index("اسم الحساب")[["فعلي الأساس", "موازنة معتمدة"]].head(12)
                st.bar_chart(chart_df)
            except Exception:
                pass
            cc = money_columns({"فعلي الأساس": MONEY_FMT, "موازنة مقترحة": MONEY_FMT, "موازنة معتمدة": MONEY_FMT, "الزيادة المقترحة": MONEY_FMT})
            st.dataframe(agg, hide_index=True, **({"column_config": cc} if cc else {}))

        with tab_monthly:
            _budget_help_box("✏️", "التفصيل الشهري",
                             "اختر شهرًا لترى كل حساب فيه. يمكنك تعديل «النمو» لحساب واحد أو «الموازنة المعتمدة» مباشرة ثم الحفظ.")
            months = sorted(int(x) for x in b["الشهر"].unique())
            mo = st.selectbox("الشهر", months,
                              format_func=lambda x: f"{x} - {MONTH_NAMES_AR[x - 1]}", key="budget_month_pick")
            bm_all = b[b["الشهر"] == mo].drop(columns=["الشهر"]).copy()
            # بحث سريع
            q = search_box("بحث عن حساب", "budget_search", "اكتب كود أو اسم الحساب...")
            bm = bm_all[search_mask(bm_all, ["كود الحساب", "اسم الحساب"], q)] if q else bm_all
            with card(f"موازنة {MONTH_NAMES_AR[mo - 1]} {int(vy)} — عدّل «النمو» أو «الموازنة المعتمدة» ثم احفظ"):
                edited = editable_table(bm, key=f"budget_editor_{vy}_{mo}")
                if edited is not None:
                    if st.button("حفظ تعديلات الشهر", key=f"save_month_{vy}_{mo}"):
                        for _, r in edited.iterrows():
                            new_growth = float(r["النمو"])
                            proposed = float(r["فعلي الأساس"]) * (1 + new_growth)
                            execute(
                                "UPDATE budget SET growth=?, proposed=?, approved=? WHERE id=?",
                                (new_growth, proposed, float(r["موازنة معتمدة"]), int(r["id"])),
                            )
                        audit("budget_edit", f"{len(edited)} rows m={mo}")
                        st.success("تم حفظ التعديلات.")
                        rerun()
                else:
                    st.caption("وضع التحرير المبسّط (اختر بندًا وعدّله). لجدول تحرير كامل حدّث Streamlit.")
                    ids = bm["id"].tolist()
                    if ids:
                        labels = {int(r["id"]): f"{r['كود الحساب']} - {r['اسم الحساب']}" for _, r in bm.iterrows()}
                        pick = st.selectbox("اختر بندًا", ids, format_func=lambda i: labels.get(int(i), str(i)), key=f"budget_pick_{vy}_{mo}")
                        row = bm[bm["id"] == pick].iloc[0]
                        cc1, cc2 = st.columns(2)
                        new_growth = cc1.number_input("النمو", value=float(row["النمو"]), step=0.01, format="%.2f", key=f"bg_{pick}")
                        new_approved = cc2.number_input("الموازنة المعتمدة", value=float(row["موازنة معتمدة"]), step=1000.0, key=f"ba_{pick}")
                        st.caption(f"المقترحة ستُعاد حسابها = {float(row['فعلي الأساس']):,.0f} × (1 + {new_growth:.2%}) = {float(row['فعلي الأساس']) * (1 + new_growth):,.0f}")
                        if st.button("حفظ البند", key=f"save_one_{pick}"):
                            proposed = float(row["فعلي الأساس"]) * (1 + new_growth)
                            execute(
                                "UPDATE budget SET growth=?, proposed=?, approved=? WHERE id=?",
                                (new_growth, proposed, new_approved, int(pick)),
                            )
                            audit("budget_edit", f"id={pick}")
                            st.success("تم حفظ البند.")
                            rerun()

    # ================================================================
    # القسم 3 — التحليل المتقدم (4 تبويبات موضّحة)
    # ================================================================
    st.divider()
    st.subheader("③ تحليل متقدّم")
    st.caption("أدوات إضافية — استخدمها بعد إنشاء الموازنة.")

    t_fc, t_if, t_var, t_sz = st.tabs(
        ["توقعات الأشهر القادمة", "ماذا لو؟ (سيناريوهات)", "هل نحن على المسار؟", "الموازنة الموسمية"]
    )

    with t_fc:
        _budget_help_box(
            "🔮", "توقعات الأشهر القادمة",
            "نرسم <b>خط اتجاه</b> من كل الأشهر السابقة (انحدار خطي) ثم نضربه في <b>موسمية</b> كل شهر "
            "(هل هذا الشهر عادةً أعلى من المتوسط؟) + موسمية المخزون. يمكنك تصفية حسب التصنيف أو الفرع.",
        )
        with card("الإعدادات"):
            h1, h2, h3 = st.columns(3)
            ahead = h1.slider("عدد الأشهر المتوقعة", 3, 24, 12, key="fc_ahead",
                              help="كم شهرًا للأمام تريد توقعه.")
            cats = query_df("SELECT DISTINCT category FROM pnl_monthly WHERE category IS NOT NULL")
            cat_opts = ["الكل"] + (sorted([translate_category(c) for c in cats["category"].astype(str).tolist()]) if not cats.empty else [])
            cat_pick = h2.selectbox("التصنيف", cat_opts, key="fc_cat",
                                    help="إيرادات / تكلفة / مصاريف — أو الكل.")
            br_opts = ["كل الفروع"] + list_pnl_branches()
            br_pick = h3.selectbox("الفرع", br_opts, key="fc_br",
                                   help="لو اخترت فرعًا، يُوزّع التوقع بنسبة مساهمته.")

            cat_filter = None if cat_pick == "الكل" else next((k for k, v in CATEGORY_MAPPING.items() if v == cat_pick), cat_pick)
            trend = forecast_trend_horizon(
                ahead,
                cat_filter,
                None if br_pick == "كل الفروع" else br_pick,
            )
            if trend.empty:
                st.info("لا توجد بيانات كافية للتوقع الاتجاهي — تحتاج على الأقل شهرين من البيانات.")
            else:
                # عدّ الأشهر الفريدة بدون استعلام إضافي (تجنب DB hit داخل كل rerun للعرض فقط)
                n_hist = len(trend)  # بديل سريع؛ العدد الحقيقي في trend نفسه
                try:
                    n_hist = int(query_df("SELECT COUNT(DISTINCT year||'-'||month) AS c FROM pnl_monthly")["c"].iloc[0])
                except Exception:
                    pass
                st.markdown(f"**بناءً على {n_hist} شهرًا سابقًا** — التوقع للأشهر القادمة:")
                m1, m2, m3 = st.columns(3)
                kpi(m1, "إجمالي المتوقع", f"{trend['التوقع الاتجاهي'].sum():,.0f}")
                kpi(m2, "متوسط الشهر", f"{trend['التوقع الاتجاهي'].mean():,.0f}")
                kpi(m3, "أعلى شهر متوقع", f"{trend['التوقع الاتجاهي'].max():,.0f}")
                # رسم التوقع
                try:
                    st.line_chart(trend.set_index("الشهر العربي")["التوقع الاتجاهي"])
                except Exception:
                    pass
                st.caption("«مؤشر الموسمية» > 1 يعني الشهر أعلى من المتوسط، < 1 أقل. «حصة الفرع» = نسبة مساهمته.")
                cc = money_columns({"التوقع الاتجاهي": MONEY_FMT})
                st.dataframe(trend, hide_index=True, **({"column_config": cc} if cc else {}))
        fc = build_forecast_monthly()
        if not fc.empty:
            with st.expander("جدول مرجعي: متوسط كل حساب (آخر 6 و 12 شهرًا)"):
                st.caption("المتوسط البسيط لكل حساب — للمقارنة السريعة مع التوقع الاتجاهي أعلاه.")
                st.dataframe(fc, hide_index=True)

    with t_if:
        _budget_help_box(
            "🎲", "ماذا لو؟ — سيناريوهات النمو",
            "نفس سنة الأساس، لكن بثلاث نسب نمو مختلفة: <b>متشائم</b> (نمو سالب)، <b>أساسي</b> (توقّعك)، "
            "<b>متفائل</b> (نمو عالٍ). ترى كيف يتغيّر الإيراد والتكلفة والمخزون في كل سيناريو.",
        )
        with card("اختر نسب النمو للسيناريوهات الثلاثة"):
            w1, w2, w3 = st.columns(3)
            g_low = w1.number_input("متشائم %", value=-5.0, step=1.0, key="wi_low",
                                    help="سيناريو سيئ — مثال: -5% = انكماش 5%") / 100.0
            g_base = w2.number_input("أساسي %", value=float(growth_pct), step=1.0, key="wi_base",
                                     help="السيناريو الأرجح — نفس نسبة الموازنة") / 100.0
            g_high = w3.number_input("متفائل %", value=15.0, step=1.0, key="wi_high",
                                     help="سيناريو جيد — مثال: 15% نمو") / 100.0
            br_opts = ["كل الفروع"] + list_pnl_branches()
            br_pick = st.selectbox("الفرع (Compare_Branches)", br_opts, key="wi_br",
                                   help="لو اخترت فرعًا تُحسب السيناريوهات من أرقامه فقط.")
            scenarios = budget_whatif(
                base_year, int(budget_year), g_low, g_base, g_high,
                None if br_pick == "كل الفروع" else br_pick,
            )
            if scenarios.empty:
                st.info("لا توجد قائمة دخل لسنة الأساس.")
            else:
                # بطاقات مقارنة بصرية
                cols = st.columns(3)
                for i, (_, row) in enumerate(scenarios.iterrows()):
                    with cols[i]:
                        st.markdown(
                            f"<div style='background:#0F172A;border:1px solid #1E293B;border-radius:10px;padding:12px;text-align:center'>"
                            f"<div style='color:#94A3B8;font-size:0.85rem'>{row['السيناريو']} ({row['نسبة النمو']*100:+.0f}%)</div>"
                            f"<div style='color:#F1F5F9;font-size:1.35rem;font-weight:700'>{row['إجمالي الموازنة']:,.0f}</div>"
                            f"<div style='color:#64748B;font-size:0.8rem'>إجمالي الموازنة</div>"
                            f"<div style='margin-top:8px;color:#94A3B8;font-size:0.82rem'>إيراد {row['إيراد متوقع']:,.0f} · تكلفة {row['تكلفة متوقعة']:,.0f}</div>"
                            f"</div>", unsafe_allow_html=True,
                        )
                st.divider()
                cc = money_columns({"إجمالي الموازنة": MONEY_FMT, "إيراد متوقع": MONEY_FMT, "تكلفة متوقعة": MONEY_FMT,
                                    "مصروف متوقع": MONEY_FMT, "مبيعات المخزون": MONEY_FMT, "تكلفة مخزون": MONEY_FMT})
                st.dataframe(scenarios, hide_index=True, with_totals=False, **({"column_config": cc} if cc else {}))
                best = scenarios.loc[scenarios["إجمالي الموازنة"].idxmax()]
                st.caption(f"أعلى إجمالي: **{best['السيناريو']}** ({best['إجمالي الموازنة']:,.0f}) — لكن الأعلى ليس دائمًا الأفضل؛ راجع هامش الربح.")

    with t_var:
        _budget_help_box(
            "🎯", "هل نحن على المسار؟ — الفعلي مقابل الموازنة",
            "نقارن ما <b>حدث فعلاً</b> بما <b>خططنا له</b> في الموازنة. الانحراف = الفعلي − الموازنة. "
            "لو تجاوز الانحراف العتبة التي تحددها، نعتبر البند «متجاوزًا» ويمكن إنشاء تنبيه له.",
        )
        vyrs = query_df("SELECT DISTINCT year FROM budget ORDER BY year")
        if vyrs.empty:
            st.info("ولّد موازنة أولًا (القسم ①) ثم ارجع هنا للمقارنة.")
        else:
            v_year = st.selectbox(
                "سنة المقارنة", [int(y) for y in vyrs["year"].tolist()],
                format_func=lambda y: f"عام {int(y)}", key="var_year",
                help="السنة التي لها موازنة وفعلي معًا.",
            )
            c_thr, c_br = st.columns(2)
            threshold = c_thr.slider("عتبة التنبيه %", 5, 50, 10, key="var_thr",
                                     help="أي بند انحرافه أكبر من هذه النسبة يُعتبر متجاوزًا.") / 100.0
            br_opts = ["كل الفروع"] + list_pnl_branches()
            br_pick = c_br.selectbox("الفرع", br_opts, key="var_br")
            var = budget_variance(v_year, None if br_pick == "كل الفروع" else br_pick)
            if var.empty:
                st.info("لا يوجد تقاطع بين الفعلي والموازنة لهذه السنة — تأكد أن السنة لها فعلي وموازنة معًا.")
            else:
                hot = var[var["نسبة الانحراف"].abs() >= threshold]
                c1, c2, c3 = st.columns(3)
                kpi(c1, "بنود متجاوزة العتبة", f"{len(hot):,}", help=f"من أصل {len(var)} بندًا")
                kpi(c2, "صافي الانحراف", f"{var['الانحراف'].sum():,.0f}",
                    help="مجموع الفعلي − الموازنة لكل البنود")
                kpi(c3, "أكبر انحراف %", f"{var['نسبة الانحراف'].abs().max() * 100:.1f}%")
                if not hot.empty:
                    st.warning(f"يوجد {len(hot)} بندًا متجاوزًا للعتبة ({threshold*100:.0f}%) — راجع الجدول أدناه (مرتب حسب الأكبر).")
                show = var.copy()
                show["نسبة الانحراف"] = (show["نسبة الانحراف"] * 100).round(1)
                show = show.assign(_a=show["نسبة الانحراف"].abs()).sort_values("_a", ascending=False).drop(columns="_a")
                # تلوين الانحراف: أخضر لو قريب، أحمر لو بعيد (عبر تنسيق بسيط)
                cc = money_columns({"الفعلي": MONEY_FMT, "الموازنة": MONEY_FMT, "الانحراف": MONEY_FMT, "نسبة الانحراف": "%.1f%%"})
                st.dataframe(show, hide_index=True, **({"column_config": cc} if cc else {}))
                if st.button("إنشاء تنبيهات للبنود المتجاوزة", key="var_alert_btn"):
                    n = raise_budget_variance_alerts(v_year, threshold)
                    if n:
                        st.success(f"تم إنشاء {n} تنبيهًا جديدًا — راجع صفحة التنبيهات.")
                        rerun()
                    else:
                        st.info("لا توجد بنود جديدة لإنشاء تنبيه لها (قد تكون مُنشأة مسبقًا).")

    with t_sz:
        _budget_help_box(
            "🗓️", "الموازنة الموسمية",
            "بدل توزيع النمو بالتساوي على 12 شهرًا، نوزّعه حسب <b>موسمية كل شهر</b>: "
            "إذا كان يناير عادةً أعلى من المتوسط، يأخذ حصة أكبر من النمو. الموسمية تُحسب من سجل P&L والمخزون معًا.",
        )
        with card("إعدادات الموازنة الموسمية"):
            sz_growth = st.number_input("نمو سنوي أساسي %", value=float(growth_pct), step=1.0, key="sz_g",
                                        help="النمو قبل توزيعه موسميًا.") / 100.0
            c_gen, c_prev = st.columns([1, 2])
            do_gen = c_gen.button("توليد موازنة موسمية", type="primary", key="sz_gen")
            c_prev.caption("سيُعاد توزيع موازنة كل حساب على 12 شهرًا حسب مؤشر الموسمية، ثم تُستبدل الموازنة الحالية.")
            if do_gen:
                seasonal = build_seasonal_budget(base_year, int(budget_year), sz_growth)
                execute("DELETE FROM budget WHERE year=?", (int(budget_year),))
                if not seasonal.empty:
                    seq = [
                        (int(r["السنة"]), int(r["الشهر"]), str(r["كود الحساب"]), str(r["اسم الحساب"]),
                         str(r["نوع الحساب"]), float(r["actual_prev"]), float(r["growth"]),
                         float(r["proposed"]), float(r["approved"]))
                        for _, r in seasonal.iterrows()
                    ]
                    executemany(
                        "INSERT INTO budget(year, month, account_code, account_name, account_type, "
                        "actual_prev, growth, proposed, approved) VALUES(?,?,?,?,?,?,?,?,?)", seq,
                    )
                audit("budget_seasonal", f"{len(seasonal)} rows y={budget_year}")
                st.success(f"تم توليد {len(seasonal)} بند موازنة موسمية لعام {int(budget_year)} — راجع الجدول أدناه وعدّل ما يلزم.")
                rerun()
            preview = build_seasonal_budget(base_year, int(budget_year), sz_growth)
            if preview.empty:
                st.info("لا توجد بيانات لسنة الأساس.")
            else:
                st.markdown("**معاينة قبل التوليد** — كيف سيُوزّع كل حساب على الأشهر:")
                # ملخص حسابي + رسم موسمية
                view = preview.groupby(["كود الحساب", "اسم الحساب"], as_index=False).agg(
                    فعلي=("actual_prev", "sum"), موسمي=("proposed", "sum"),
                    مؤشر=("seasonal_index", "mean"),
                )
                view["الفرق"] = view["موسمي"] - view["فعلي"]
                m1, m2 = st.columns(2)
                kpi(m1, "إجمالي الفعلي", f"{view['فعلي'].sum():,.0f}")
                kpi(m2, "إجمالي الموسمي", f"{view['موسمي'].sum():,.0f}")
                # رسم مؤشر الموسمية الشهري
                try:
                    monthly_idx = preview.groupby("الشهر")["seasonal_index"].mean()
                    monthly_idx.index = [MONTH_NAMES_AR[i - 1] for i in monthly_idx.index]
                    st.bar_chart(monthly_idx)
                    st.caption("مؤشر الموسمية الشهري (>1 = شهر أعلى من المتوسط).")
                except Exception:
                    pass
                cc = money_columns({"فعلي": MONEY_FMT, "موسمي": MONEY_FMT, "الفرق": MONEY_FMT})
                st.dataframe(view.round(2), hide_index=True, **({"column_config": cc} if cc else {}))


# ---- صفحة الاستيراد ----
def page_import():
    st.header("استيراد البيانات")
    st.caption("استورد ملفاتك الحالية إلى قاعدة البيانات. يُعاد استخدام نفس منطق قراءة ملفاتك.")

    with card("1) استيراد شامل"):
        st.write(
            "يقرأ تقرير حركة الصنف (الرصيد الختامي = المرجع المعتمد للأرصدة)، "
            "وتقرير المبيعات اليومية لكل فرع (عمود Branches)، وتقارير Foodics "
            "(المبيعات حسب الفرع / حسب المنتج)، وشجرة الحسابات — "
            f"من المجلد الرئيسي، بالإضافة لأي مجلدات أشهر داخل `{INPUT_DIR.name}`.\n\n"
            "ملاحظة: يُعاد الاستيراد دون تكرار، ويحافظ على أي إدخالات يدوية سجّلتها."
        )
        if _icon_button("استيراد كل شيء الآن", ":material/download:", "⬇️"):
            with st.spinner("جارٍ الاستيراد..."):
                log = import_from_folders()
            st.success("اكتمل الاستيراد.")
            for line in log:
                st.write("•", line)

    with card("2) رفع ملف مفرد"):
        up = st.file_uploader("ارفع ملف Excel (أرباح وخسائر) أو CSV (حركة مخزون)", type=["xlsx", "xls", "csv"], accept_multiple_files=True)
        if up is not None:
            coly, colm = st.columns(2)
            yrs = year_options()
            year = coly.selectbox("السنة", yrs, index=len(yrs) - 1, format_func=lambda y: f"عام {int(y)}", key="up_year")
            month = colm.number_input("الشهر", min_value=1, max_value=12, value=12, key="up_month")
            if st.button("استيراد الملف المرفوع"):
                for uploaded_file in up:
                    tmp = safe_upload_path(uploaded_file.name)
                    if tmp is None:
                        st.error(f"تم تجاهل ملف بامتداد أو اسم غير مسموح: {uploaded_file.name}")
                        continue
                    with open(tmp, "wb") as f:
                        f.write(uploaded_file.getbuffer())
                    try:
                        detected_year, detected_month = detect_file_period(tmp, default_year=int(year), default_month=int(month))
                        year, month = detected_year, detected_month
                        if tmp.suffix.lower() == ".csv":
                            moves, balances = parse_inventory(tmp, int(year), int(month))
                            n = insert_movements(moves)
                            b = 0
                            if not balances.empty:
                                for _, _r in balances.iterrows():
                                    upsert_balance(_r.to_dict())
                                    b += 1
                            st.success(f"تم استيراد {n} حركة مخزون | أرصدة ختامية معتمدة: {b}.")
                        else:
                            data = read_excel(tmp)
                            chart, _ = load_chart_of_accounts()
                            parsed = parse_profit_and_loss(data, int(year), chart)
                            n = insert_financial(parsed, "pnl")
                            st.success(f"تم استيراد {n} بند أرباح وخسائر.")
                    finally:
                        try:
                            tmp.unlink()
                        except Exception:
                            pass

    with card("3) استيراد شجرة الحسابات"):
        if st.button("تحميل شجرة الحسابات من مجلد المصادر"):
            chart, name = load_chart_of_accounts()
            if chart.empty:
                st.warning("لم يُعثر على ملف شجرة حسابات في مجلد المصادر.")
            else:
                for _, r in chart.iterrows():
                    upsert_account(r["كود الحساب"], r["اسم الحساب"], r["نوع الحساب"])
            st.success(f"تم تحميل {len(chart)} حساب من {name}.")


# ---- صفحة الأصناف والحسابات ----
def page_masters():
    st.header("الأصناف والحسابات")
    tab1, tab2 = st.tabs(["الأصناف", "شجرة الحسابات"])

    with tab1:
        with card("الأصناف"):
            with st.form("item_form", clear_on_submit=True):
                c1, c2 = st.columns(2)
                code = c1.text_input("كود الصنف (يبدأ بـ GB للأخضر أو RB للمحمص)")
                name = c2.text_input("اسم الصنف")
                if st.form_submit_button("إضافة / تحديث صنف") and code:
                    upsert_item(code, name)
                    audit("item_add", code)
                    st.success(f"تم حفظ الصنف {code.upper()}.")
            st.dataframe(list_items(), hide_index=True)

    with tab2:
        with card("شجرة الحسابات"):
            with st.form("acc_form", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                code = c1.text_input("كود الحساب")
                name = c2.text_input("اسم الحساب")
                atype = c3.text_input("نوع الحساب")
                if st.form_submit_button("إضافة / تحديث حساب") and code:
                    upsert_account(code, name, atype)
                    audit("account_add", code)
                    st.success(f"تم حفظ الحساب {code}.")
            st.dataframe(list_accounts(), hide_index=True)


# ---- صفحة التنبيهات ----
def page_alerts():
    st.header("مركز التنبيهات")
    if _icon_button("فحص المخزون الآن", ":material/inventory:", "📦"):
        n = refresh_stock_alerts()
        if n:
            st.success(f"أُضيف {n} تنبيه جديد.")
            rerun()
        else:
            st.info("لا توجد تنبيهات مخزون جديدة.")
    df = query_df(
        "SELECT id, created_at AS 'التاريخ', severity AS 'الأهمية', category AS 'الفئة', "
        "message AS 'الرسالة', resolved AS 'محلول' FROM alerts ORDER BY resolved, id DESC"
    )
    if df.empty:
        st.success("لا توجد تنبيهات.")
        return
    open_df = df[df["محلول"] == 0]
    with card(f"تنبيهات مفتوحة ({len(open_df)})"):
        cats = ["الكل"] + sorted(open_df["الفئة"].dropna().unique().tolist()) if not open_df.empty else ["الكل"]
        cat = st.selectbox("تصفية حسب الفئة", cats, key="alert_cat")
        view_open = open_df if cat == "الكل" else open_df[open_df["الفئة"] == cat]
        for _, r in view_open.iterrows():
            c1, c2 = st.columns([5, 1])
            c1.warning(f"[{r['الفئة']}] {r['الرسالة']}  — {r['التاريخ']}")
            if c2.button("تم الحل", key=f"resolve_{r['id']}"):
                execute("UPDATE alerts SET resolved=1 WHERE id=?", (int(r["id"]),))
                audit("alert_resolved", str(r["id"]))
                rerun()
        with st.expander("عرض كل التنبيهات (المحلولة أيضًا)"):
            st.dataframe(df, hide_index=True)


# ---- صفحة الإعدادات ----
def page_settings():
    st.header("الإعدادات")
    settings = get_settings()
    with st.form("settings_form"):
        c1, c2 = st.columns(2)
        growth = c1.number_input("نسبة النمو الافتراضية للموازنة", value=s_float(settings, "growth_rate"),
                                 step=0.01, format="%.2f")
        roast_yield = c2.number_input("نسبة إنتاج التحميص المستهدفة", value=s_float(settings, "roast_yield"),
                                      step=0.001, format="%.3f")
        tol = c1.number_input("التفاوت المسموح في التحميص", value=s_float(settings, "roast_tolerance"),
                              step=0.001, format="%.3f")
        base_year = c2.number_input("سنة الأساس", value=s_int(settings, "base_year"), step=1)
        budget_year = c1.number_input("سنة الموازنة", value=s_int(settings, "budget_year"), step=1)
        low_qty = c2.number_input("حد المخزون المنخفض (كمية)", value=s_float(settings, "low_stock_qty"),
                                  min_value=0.0, step=1.0)
        if st.form_submit_button("حفظ الإعدادات"):
            set_setting("growth_rate", growth)
            set_setting("roast_yield", roast_yield)
            set_setting("roast_tolerance", tol)
            set_setting("base_year", int(base_year))
            set_setting("budget_year", int(budget_year))
            set_setting("low_stock_qty", low_qty)
            audit("settings_update", "")
            st.success("تم حفظ الإعدادات.")

    with card("سجل التدقيق (آخر 100 عملية)"):
        st.dataframe(
            query_df("SELECT ts AS 'الوقت', usr AS 'المستخدم', action AS 'العملية', details AS 'التفاصيل' "
                     "FROM audit_log ORDER BY id DESC LIMIT 100"),
            hide_index=True,
        )


# ---- صفحة التصدير ----
def page_export():
    st.header("تصدير Excel")
    st.caption("يُنشئ ملفات Excel بنفس تنسيق نظامك السابق (RTL، رؤوس ملوّنة، تجميد الصف الأول).")
    with card("تصدير الملفات"):
        if _icon_button("إنشاء ملفات الداشبورد والتنبيهات", ":material/table:", "📊"):
            with st.spinner("جارٍ إنشاء الملفات..."):
                dash, alerts = build_exports()
            st.success("تم إنشاء الملفات في مجلد المخرجات.")
            with open(dash, "rb") as f:
                st.download_button("تنزيل الداشبورد", f.read(), file_name=dash.name)
            with open(alerts, "rb") as f:
                st.download_button("تنزيل التنبيهات", f.read(), file_name=alerts.name)


# ---- صفحة مبيعات Foodics (نقاط البيع) ----
def page_foodics():
    st.header("مبيعات Foodics")
    st.caption("تحليل مبيعات نقاط البيع حسب الفرع والمنتج من تقارير Foodics.")
    years = foodics_years()
    
    col_years, col_import = st.columns([3, 1])
    with col_years:
        if not years:
            st.info("لا توجد بيانات Foodics بعد.")
        else:
            year = st.selectbox("السنة", years, index=len(years) - 1, format_func=lambda y: f"عام {int(y)}", key="fd_year")
            year = int(year)
    with col_import:
        st.write("")
        if _icon_button("تحديث/استيراد Foodics", ":material/refresh:", "🔄"):
            with st.spinner("جارٍ استيراد تقارير Foodics..."):
                log = []
                import_foodics_reports(log)
            st.success("اكتمل استيراد Foodics.")
            for line in log:
                st.write("•", line)
            rerun()

    if not years:
        return

    br = foodics_branch_df(year)
    pr = foodics_product_df(year)

    t1, t2, t3 = st.tabs(["حسب الفرع", "أفضل المنتجات", "أضعف المنتجات"])

    with t1:
        if br.empty:
            st.info("لا توجد مبيعات فروع لهذه السنة.")
        else:
            with card("مبيعات الفروع"):
                # Branch search
                search_br = search_box("بحث بالفرع", "fd_br_search", "اكتب جزء من اسم الفرع...")

                k1, k2, k3, k4 = st.columns(4)
                kpi(k1, "صافي المبيعات", f"{br['صافي المبيعات'].sum():,.0f}")
                kpi(k2, "عدد الطلبات", f"{int(br['عدد الطلبات'].sum()):,}")
                kpi(k3, "متوسط الطلب", f"{(br['صافي المبيعات'].sum() / br['عدد الطلبات'].sum()) if br['عدد الطلبات'].sum() else 0:,.1f}")
                kpi(k4, "عدد الفروع", f"{len(br)}")
                show = br.copy()
                if search_br:
                    show = show[search_mask(show, ["الفرع"], search_br)]
                for c in ("إجمالي المبيعات", "صافي المبيعات", "الضرائب", "الخصم", "متوسط الطلب"):
                    if c in show.columns:
                        show[c] = show[c].round(2)
                cc = money_columns({"إجمالي المبيعات": MONEY_FMT, "صافي المبيعات": MONEY_FMT,
                                    "الضرائب": MONEY_FMT, "الخصم": MONEY_FMT, "متوسط الطلب": "%.2f"})
                st.dataframe(show, hide_index=True, **({"column_config": cc} if cc else {}))
                st.bar_chart(show.set_index("الفرع")["صافي المبيعات"])
                with st.expander("الخصومات والزوار"):
                    extra = br[["الفرع", "الخصم", "عدد الزوار", "عدد العملاء", "صافي الكمية"]].copy()
                    extra["الخصم"] = extra["الخصم"].round(2)
                    extra["صافي الكمية"] = extra["صافي الكمية"].round(1)
                    st.dataframe(extra, hide_index=True)

    with t2:
        with card("أفضل المنتجات"):
            branches = ["(كل الفروع)"] + (sorted(pr["الفرع"].dropna().unique().tolist()) if not pr.empty else [])
            pick = st.selectbox("الفرع", branches, key="fd_top_br")
            branch = None if pick == "(كل الفروع)" else pick
            top_n = st.slider("عدد المنتجات", 5, 50, 20, key="fd_top_n")

            # Product search
            search_prod = search_box("بحث بالمنتج", "fd_top_search", "اكتب جزء من اسم المنتج...")

            ranked = foodics_product_ranked(year, branch, top_n=top_n, worst=False)
            if ranked.empty:
                st.info("لا توجد مبيعات منتجات لهذه الفترة.")
            else:
                if search_prod:
                    ranked = ranked[search_mask(ranked, ["المنتج"], search_prod)]
                m1, m2, m3 = st.columns(3)
                kpi(m1, "صافي المبيعات (المعروضة)", f"{ranked['صافي المبيعات'].sum():,.0f}")
                kpi(m2, "التكلفة", f"{ranked['التكلفة'].sum():,.0f}")
                kpi(m3, "هامش الربح", f"{ranked['هامش الربح'].sum():,.0f}")
                cc = money_columns({"صافي المبيعات": MONEY_FMT, "التكلفة": MONEY_FMT, "هامش الربح": MONEY_FMT})
                st.dataframe(ranked, hide_index=True, **({"column_config": cc} if cc else {}))
                chart_df = ranked.set_index("المنتج")["صافي المبيعات"].head(15)
                st.bar_chart(chart_df)

    with t3:
        with card("أضعف المنتجات"):
            branches = ["(كل الفروع)"] + (sorted(pr["الفرع"].dropna().unique().tolist()) if not pr.empty else [])
            pick = st.selectbox("الفرع", branches, key="fd_low_br")
            branch = None if pick == "(كل الفروع)" else pick
            n = st.slider("عدد المنتجات", 5, 50, 15, key="fd_low_n")

            # Product search
            search_prod = search_box("بحث بالمنتج", "fd_low_search", "اكتب جزء من اسم المنتج...")

            ranked = foodics_product_ranked(year, branch, top_n=n, worst=True)
            if ranked.empty:
                st.info("لا توجد مبيعات منتجات لهذه الفترة.")
            else:
                if search_prod:
                    ranked = ranked[search_mask(ranked, ["المنتج"], search_prod)]
                st.caption("الأقل مبيعًا حسب صافي المبيعات — مفيد لمراجعة القائمة أو التسعير.")
                cc = money_columns({"صافي المبيعات": MONEY_FMT, "التكلفة": MONEY_FMT, "هامش الربح": MONEY_FMT})
                st.dataframe(ranked, hide_index=True, **({"column_config": cc} if cc else {}))

    with st.expander("كل سجلات المنتجات"):
        q = st.text_input("بحث عن منتج", key="fd_prod_q")
        view = pr
        if q:
            view = pr[search_mask(pr, ("المنتج", "كود المنتج"), q)]
        st.dataframe(view.head(500), hide_index=True)


# ---- صفحة مبيعات التطبيقات والموقع (إدخال يدوي شهري) ----
def page_channels():
    st.header("مبيعات التطبيقات والموقع الإلكتروني")
    st.caption("أدخل مبيعات كل قناة (جاهز، نوفمبر، هنجرستيشن، الموقع الإلكتروني) شهريًا. "
               "إعادة الإدخال لنفس (السنة/الشهر/القناة) تُحدِّث القيمة.")

    with st.form("channel_form", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        yrs = year_options()
        year = c1.selectbox("السنة", yrs, index=len(yrs) - 1, format_func=lambda y: f"عام {int(y)}", key="ch_in_year")
        month = c2.selectbox("الشهر", list(range(1, 13)),
                             format_func=lambda m: f"{m} - {MONTH_NAMES_AR[m - 1]}")
        channel = c3.selectbox("القناة", DELIVERY_CHANNELS)
        custom = st.text_input("اسم قناة مخصص (اختياري، يُستخدم بدل الاختيار أعلاه)")
        amount = st.number_input("قيمة المبيعات", min_value=0.0, step=100.0)
        note = st.text_input("ملاحظة (اختياري)")
        if st.form_submit_button("حفظ / تحديث"):
            ch = custom.strip() or channel
            if amount > 0:
                upsert_channel_sale(int(year), int(month), ch, amount, note)
                audit("channel_sale", f"{year}-{month} {ch}={amount}")
                st.success(f"تم حفظ مبيعات {ch} لشهر {MONTH_NAMES_AR[int(month) - 1]} {int(year)}.")
            else:
                st.error("أدخل قيمة أكبر من صفر.")

    df = channel_sales_df()
    if df.empty:
        st.info("لا توجد مبيعات قنوات مُدخلة بعد.")
        return

    space("medium")
    years = sorted(int(y) for y in df["السنة"].unique())
    ysel = st.selectbox("اعرض سنة", years, index=len(years) - 1,
                        format_func=lambda y: f"عام {int(y)}", key="ch_year")
    year_df = df[df["السنة"] == ysel]
    with card(f"مبيعات القنوات لعام {int(ysel)}"):
        total = year_df["المبلغ"].sum()
        metric_col = st.columns(1)[0]
        kpi(metric_col, "إجمالي مبيعات القنوات", f"{total:,.0f}")
        pivot = year_df.pivot_table(index="الشهر", columns="القناة", values="المبلغ",
                                    aggfunc="sum", fill_value=0)
        st.dataframe(pivot)
        by_channel = year_df.groupby("القناة")["المبلغ"].sum().sort_values(ascending=False)
        st.bar_chart(by_channel)
        with st.expander("كل السجلات"):
            st.dataframe(df, hide_index=True)


# ---- صفحة التحليل المالي والنسب المحاسبية ----
def _fmt_compare_table(metric_dict_by_year, years):
    rows = []
    metrics = list(next(iter(metric_dict_by_year.values())).keys())
    for metric in metrics:
        row = {"البند": metric}
        for y in years:
            row[str(int(y))] = round(metric_dict_by_year[y][metric], 2)
        if len(years) == 2:
            a, b = metric_dict_by_year[years[0]][metric], metric_dict_by_year[years[1]][metric]
            row["التغير %"] = round((b - a) / a * 100, 1) if a else 0.0
        rows.append(row)
    return pd.DataFrame(rows)


def page_finance():
    st.header("التحليل المالي والنسب المحاسبية")
    years = year_options()
    if not years:
        st.info("لا توجد بيانات مالية بعد. استورد قائمة الدخل والميزانية من صفحة «استيراد البيانات».")
        return

    tab1, tab2, tab3 = st.tabs(["مقارنة 12 شهر", "الربحية (سنوي)", "النسب المالية"])

    # ---------- Tab 1: 12-month comparison ----------
    with tab1:
        st.caption("مقارنة المبيعات والمصاريف شهريًا بين السنوات المقروءة من ملفات الإكسل (المبيعات = قائمة الدخل + مبيعات القنوات اليدوية).")
        c1, c2 = st.columns(2)
        ya = c1.selectbox("السنة الأولى", years, index=0, format_func=lambda y: f"عام {int(y)}", key="fa_ya")
        yb = c2.selectbox("السنة الثانية", years, index=len(years) - 1, format_func=lambda y: f"عام {int(y)}", key="fa_yb")

        # المبيعات: قائمة الدخل + القنوات اليدوية
        def revenue_of(y):
            base = monthly_pnl_series(y, "revenue")
            ch = pd.Series(0.0, index=range(1, 13))
            cq = query_df("SELECT month, COALESCE(SUM(amount),0) v FROM channel_sales WHERE year=? GROUP BY month", (int(y),))
            for _, r in cq.iterrows():
                m = int(r["month"])
                if 1 <= m <= 12:
                    ch[m] += float(r["v"])
            return base.add(ch, fill_value=0)

        def expenses_of(y):
            return monthly_pnl_series(y, "cogs").add(monthly_pnl_series(y, "expense"), fill_value=0)

        rev_a, rev_b = revenue_of(ya), revenue_of(yb)
        exp_a, exp_b = expenses_of(ya), expenses_of(yb)
        table = pd.DataFrame({
            "الشهر": MONTH_NAMES_AR,
            f"مبيعات {int(ya)}": rev_a.values, f"مبيعات {int(yb)}": rev_b.values,
            f"مصاريف {int(ya)}": exp_a.values, f"مصاريف {int(yb)}": exp_b.values,
        })
        table[f"صافي {int(ya)}"] = table[f"مبيعات {int(ya)}"] - table[f"مصاريف {int(ya)}"]
        table[f"صافي {int(yb)}"] = table[f"مبيعات {int(yb)}"] - table[f"مصاريف {int(yb)}"]
        totals = {"الشهر": "الإجمالي"}
        for c in table.columns[1:]:
            totals[c] = table[c].sum()
        show = pd.concat([table, pd.DataFrame([totals])], ignore_index=True)
        for c in show.columns[1:]:
            show[c] = show[c].round(0)
        cc = money_columns({c: MONEY_FMT for c in show.columns[1:]})
        st.dataframe(show, hide_index=True, **({"column_config": cc} if cc else {}))

        ch1, ch2 = st.columns(2)
        with ch1, card("المبيعات شهريًا"):
            st.line_chart(pd.DataFrame({int(ya): rev_a.values, int(yb): rev_b.values},
                                       index=MONTH_NAMES_AR))
        with ch2, card("المصاريف شهريًا"):
            st.line_chart(pd.DataFrame({int(ya): exp_a.values, int(yb): exp_b.values},
                                       index=MONTH_NAMES_AR))
        d_rev = rev_b.sum() - rev_a.sum()
        d_exp = exp_b.sum() - exp_a.sum()
        k1, k2, k3 = st.columns(3)
        kpi(k1, f"إجمالي مبيعات {int(yb)}", f"{rev_b.sum():,.0f}",
            f"{(d_rev / rev_a.sum() * 100) if rev_a.sum() else 0:,.1f}%")
        kpi(k2, f"إجمالي مصاريف {int(yb)}", f"{exp_b.sum():,.0f}",
            f"{(d_exp / exp_a.sum() * 100) if exp_a.sum() else 0:,.1f}%", delta_color="inverse")
        kpi(k3, f"صافي {int(yb)}", f"{(rev_b.sum() - exp_b.sum()):,.0f}")

    # ---------- Tab 2: annual profitability ----------
    with tab2:
        with card("الربحية السنوية"):
            st.caption("ملخص الربحية السنوي من قائمة الدخل.")
            summ = {y: pnl_year_summary(y) for y in years}
            if all(all(v == 0 for v in s.values()) for s in summ.values()):
                st.warning("لا توجد بيانات قائمة دخل شهرية. تأكد من استيراد ملف «Current financial year by month».")
            else:
                comp = _fmt_compare_table(summ, years)
                st.dataframe(comp, hide_index=True, with_totals=False)
                latest = years[-1]
                s = summ[latest]
                c1, c2, c3, c4 = st.columns(4)
                kpi(c1, f"إيرادات {int(latest)}", f"{s['الإيرادات']:,.0f}")
                kpi(c2, "مجمل الربح", f"{s['مجمل الربح']:,.0f}")
                kpi(c3, "صافي الربح", f"{s['صافي الربح']:,.0f}")
                kpi(c4, "هامش صافي الربح", f"{s['هامش صافي الربح %']:,.1f}%")

    # ---------- Tab 3: financial ratios ----------
    with tab3:
        # صندوق تمهيدي دائم — حتى لو لا توجد أرقام
        st.markdown(
            "<div style='background:#1E293B;border:1px solid #334155;border-radius:10px;"
            "padding:12px 16px;margin:6px 0 14px 0'>"
            "🔍 <b style='color:#E2E8F0'>ما هي النسب المالية؟</b><br>"
            "<span style='color:#94A3B8;font-size:0.92rem'>"
            "النسب تحوّل أرقام الميزانية وقائمة الدخل إلى <b>مؤشرات مفهومة</b>: "
            "هل نستطيع سداد التزاماتنا؟ هل المخزون يتحرك بسرعة؟ هل نربح جيدًا من كل ريال مبيعات؟ "
            "اختر سنة وشاهد كل نسبة مع <b>شرح مبسّط + حكم سريع (جيد/انتبه)</b>."
            "</span></div>",
            unsafe_allow_html=True,
        )
        with card("النسب المالية"):
            ysel = st.selectbox("السنة", years, index=len(years) - 1, format_func=lambda y: f"عام {int(y)}", key="ratio_year")
            ratios, figures = financial_ratios(ysel)

            # ── دليل تفسير النسب (مرجع سريع) ──
            # معادلة + ماذا تعني + متى تكون جيدة
            _RATIO_GUIDE = {
                "النسبة الجارية (السيولة)": {
                    "formula": "الأصول المتداولة ÷ الخصوم المتداولة",
                    "meaning": "هل تملك الشركة ما يكفي من الأصول قصيرة الأجل لسداد ديونها قصيرة الأجل؟",
                    "good": "> 1.5 جيد — أقل من 1 يعني خطر سيولة.",
                },
                "نسبة السيولة السريعة": {
                    "formula": "(الأصول المتداولة − المخزون) ÷ الخصوم المتداولة",
                    "meaning": "نفس السيولة لكن بدون الاعتماد على بيع المخزون (أكثر تحفظًا).",
                    "good": "> 1 جيد — يقيس السيولة الحقيقية دون انتظار بيع البضاعة.",
                },
                "معدل دوران المخزون (مرة)": {
                    "formula": "تكلفة المبيعات ÷ المخزون",
                    "meaning": "كم مرة يُباع ويُعوّض المخزون خلال السنة. كلما زاد، تحرّك البضاعة أسرع.",
                    "good": "مقارنة سنوية: الارتفاع = تحسّن. انخفاض كبير = ركود مخزون.",
                },
                "متوسط فترة بقاء المخزون (يوم)": {
                    "formula": "365 ÷ معدل الدوران",
                    "meaning": "كم يومًا يبقى المنتج في المستودع قبل بيعه في المتوسط.",
                    "good": "أقل = أفضل. لمقهى/مطعم: أسابيع قليلة. للبن الأخضر: قد يطول قليلاً.",
                },
                "هامش مجمل الربح %": {
                    "formula": "(الإيرادات − تكلفة المبيعات) ÷ الإيرادات × 100",
                    "meaning": "من كل 100 ريال مبيعات، كم يبقى بعد خصم تكلفة البضاعة مباشرة (قبل المصاريف)؟",
                    "good": "أعلى = تسعير أو تكلفة أفضل. قارن بسنتك الماضية.",
                },
                "هامش صافي الربح %": {
                    "formula": "صافي الربح ÷ الإيرادات × 100",
                    "meaning": "من كل 100 ريال مبيعات، كم يبقى ربح صافٍ بعد كل المصاريف؟",
                    "good": "أعلى = إدارة مصاريف أفضل. حتى 5–10% جيد لقطاع الضيافة.",
                },
                "العائد على الأصول % (ROA)": {
                    "formula": "صافي الربح ÷ إجمالي الأصول × 100",
                    "meaning": "كم يحقق كل ريال من أصول الشركة من ربح؟ يقيس كفاءة استخدام الأصول.",
                    "good": "أعلى = أصولك تعمل بجد. قارن سنويًا.",
                },
                "العائد على حقوق الملكية % (ROE)": {
                    "formula": "صافي الربح ÷ حقوق الملكية × 100",
                    "meaning": "كم يحقق كل ريال استثمره الملاك من ربح؟ أهم نسبة للملاك.",
                    "good": "أعلى = استثمار الملاك مُجدٍ. > 10% جيد عمومًا.",
                },
            }

            def _verdict(name, val):
                """حكم سريع بلون — لا يغني عن التحليل، لكنه يوجّه النظر."""
                if name == "النسبة الجارية (السيولة)":
                    if val >= 1.5: return "✅ جيد"
                    if val >= 1.0: return "⚠️ مقبول"
                    return "🔴 انتبه"
                if name == "نسبة السيولة السريعة":
                    if val >= 1.0: return "✅ جيد"
                    if val >= 0.7: return "⚠️ مقبول"
                    return "🔴 انتبه"
                if name == "متوسط فترة بقاء المخزون (يوم)":
                    if val == 0: return "—"
                    if val <= 45: return "✅ سريع"
                    if val <= 90: return "⚠️ متوسط"
                    return "🔴 بطيء"
                if "هامش" in name or "العائد" in name:
                    if val >= 10: return "✅ ممتاز"
                    if val >= 5: return "✅ جيد"
                    if val >= 0: return "⚠️ ضعيف"
                    return "🔴 خسارة"
                if "دوران" in name:
                    if val >= 6: return "✅ سريع"
                    if val >= 3: return "⚠️ متوسط"
                    return "🔴 بطيء"
                return "—"

            # ── جدول النسب مع الشرح ──
            # ملاحظة: عمود المعادلة مخفي عن العرض (يبقى في الدليل أدناه عند الحاجة)
            show_formula = st.toggle("إظهار عمود المعادلة", value=False, key="ratio_show_formula",
                                     help="للمحاسبين فقط — يوضح كيف حُسبت كل نسبة")
            rows = []
            for name, val in ratios.items():
                g = _RATIO_GUIDE.get(name, {})
                rows.append({
                    "النسبة": name,
                    "القيمة": round(val, 2),
                    "المعادلة": g.get("formula", "—"),
                    "ماذا تعني؟": g.get("meaning", "—"),
                    "متى تكون جيدة؟": g.get("good", "—"),
                    "الحكم": _verdict(name, val),
                })
            all_cols = ["النسبة", "القيمة", "المعادلة", "ماذا تعني؟", "متى تكون جيدة؟", "الحكم"]
            show_cols = all_cols if show_formula else [c for c in all_cols if c != "المعادلة"]
            rt = pd.DataFrame(rows, columns=all_cols)[show_cols]

            # أحجام أعمدة مناسبة للعرض: النسبة أوسع، القيمة والحكم مضيّقان، الشرح مرن
            cc = getattr(st, "column_config", None)
            col_cfg = None
            if cc is not None:
                try:
                    col_cfg = {
                        "النسبة": cc.TextColumn("النسبة", width="medium"),
                        "القيمة": cc.NumberColumn("القيمة", width="small", format="%.2f"),
                        "ماذا تعني؟": cc.TextColumn("ماذا تعني؟", width="large"),
                        "متى تكون جيدة؟": cc.TextColumn("متى تكون جيدة؟", width="medium"),
                        "الحكم": cc.TextColumn("الحكم", width="small"),
                    }
                    if show_formula:
                        col_cfg["المعادلة"] = cc.TextColumn("المعادلة", width="medium")
                    # احتفظ فقط بالأعمدة المعروضة
                    col_cfg = {k: v for k, v in col_cfg.items() if k in show_cols}
                except Exception:
                    col_cfg = None
            st.dataframe(rt, hide_index=True, use_container_width=True,
                         column_config=col_cfg, height=min(520, 40 + len(rt) * 42))
            st.caption("💡 «الحكم» دليل سريع فقط — القرار الحقيقي يكون بمقارنة نفس النسبة عبر السنوات (التبويبان السابقان).")

            cc1, cc2 = st.columns(2)
            with cc1, card("أرقام الميزانية المستخدمة"):
                st.caption("هذه الأرقام من ملف الميزانية (Balance Sheet). لو كانت صفرًا، استورد الملف أو عدّل يدويًا أدناه.")
                fg = pd.DataFrame({"البند": list(figures.keys()),
                                   "القيمة": [round(v, 0) for v in figures.values()]})
                st.dataframe(fg, hide_index=True)
            with cc2, card("كيف تُقرأ الأرقام؟"):
                st.markdown(
                    "<span style='color:#94A3B8;font-size:0.9rem'>"
                    "• <b>الأصول المتداولة</b> = نقد + مخزون + ذمم مدينة (ما يمكن تسييله خلال سنة).<br>"
                    "• <b>الخصوم المتداولة</b> = ذمم دائنة + التزامات قصيرة (ما يجب سداده خلال سنة).<br>"
                    "• <b>حقوق الملكية</b> = ما يملكه الشركاء بعد سداد كل الديون.<br>"
                    "• <b>هامش الربح</b> = نسبة ما يبقى من المبيعات كربح.<br>"
                    "</span>", unsafe_allow_html=True,
                )

            with st.expander("تعديل أرقام الميزانية يدويًا (اختياري)"):
                st.caption("لو لم تستورد ملف الميزانية، أدخل الأرقام هنا — ستُستخدم مباشرة في النسب أعلاه.")
                with st.form("fig_form"):
                    f1, f2, f3 = st.columns(3)
                    ca = f1.number_input("الأصول المتداولة", value=float(figures["الأصول المتداولة"]), step=1000.0,
                                         help="نقد + مخزون + ذمم مدينة")
                    inv = f2.number_input("المخزون", value=float(figures["المخزون"]), step=1000.0,
                                          help="قيمة المخزون من المستودع")
                    cl = f3.number_input("الخصوم المتداولة", value=float(figures["الخصوم المتداولة"]), step=1000.0,
                                         help="ديون قصيرة الأجل")
                    ta = f1.number_input("إجمالي الأصول", value=float(figures["إجمالي الأصول"]), step=1000.0,
                                         help="كل ما تملكه الشركة")
                    te = f2.number_input("حقوق الملكية", value=float(figures["حقوق الملكية"]), step=1000.0,
                                         help="رأس المال + الأرباح المبقاة")
                    if st.form_submit_button("حفظ الأرقام"):
                        upsert_fin_figures(pd.DataFrame([{
                            "year": int(ysel), "current_assets": ca, "inventory": inv,
                            "current_liabilities": cl, "total_assets": ta, "total_equity": te,
                            "source_file": "إدخال يدوي",
                        }]))
                        audit("fin_figures_manual", str(ysel))
                        st.success("تم الحفظ. أعد اختيار السنة لتحديث النسب.")
                        rerun()


# ==========================================================================
# التشغيل
# ==========================================================================
def main():
    setup_page()
    init_db()
    page = sidebar()
    if page == "لوحة المعلومات":
        page_dashboard()
    elif page in ("ملخص مبيعات العملاء والفروع", "المبيعات اليومية"):
        page_sales()
    elif page == "مبيعات Foodics":
        page_foodics()
    elif page == "مبيعات التطبيقات والموقع":
        page_channels()
    elif page == "المخزون والحركات":
        page_inventory()
    elif page == "الموازنة والتوقعات":
        page_budget()
    elif page == "التحليل المالي":
        page_finance()
    elif page == "استيراد البيانات":
        page_import()
    elif page == "الأصناف والحسابات":
        page_masters()
    elif page == "التنبيهات":
        page_alerts()
    elif page == "الإعدادات":
        page_settings()
    elif page == "تصدير Excel":
        page_export()


if __name__ == "__main__":
    main()