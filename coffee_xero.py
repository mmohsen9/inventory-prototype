"""
coffee_xero.py — تكامل Xero API لأوامر التحميص
يُرسل مستندين لكل أمر تحميص مكتمل:
  1) Payable Credit Note  — خصم مخزون البن الأخضر (حساب 635)
  2) Payable Invoice/Bill — إثبات وارد البن المحمص  (حساب 636)
"""
import os
import re
import json
import shutil
import base64
import logging
import tempfile
import subprocess
import requests
import urllib.parse
from datetime import datetime, date
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# دوال مساعدة
# ─────────────────────────────────────────────────────────────────────────────

def _get_setting(key: str, default: str = "") -> str:
    """يجلب قيمة إعداد من جدول CoffeeSetting أو متغيرات البيئة — يُستدعى متأخراً لتجنب circular import."""
    from models_coffee import CoffeeSetting
    val = CoffeeSetting.get(key, None)
    if val is not None and str(val).strip():
        return str(val).strip()
    env_val = os.environ.get(key.upper()) or os.environ.get(key)
    return str(env_val).strip() if env_val else default


def refresh_xero_token() -> Tuple[bool, Optional[str]]:
    """
    يقوم بتجديد رمز الوصول لـ Xero باستخدام Refresh Token تلقائياً في الخلفية.
    يعيد (success, error_message).
    """
    import time
    from requests.auth import HTTPBasicAuth
    from models_coffee import CoffeeSetting

    client_id = _get_setting("xero_client_id").strip()
    client_secret = _get_setting("xero_client_secret").strip()
    refresh_token = _get_setting("xero_refresh_token").strip()

    if not client_id or not client_secret or not refresh_token:
        return False, "بيانات Client ID أو Client Secret أو Refresh Token غير متوفرة"

    try:
        resp = requests.post(
            "https://identity.xero.com/connect/token",
            auth=HTTPBasicAuth(client_id, client_secret),
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=20,
        )
        if resp.status_code != 200:
            err = _parse_xero_error(resp)
            logger.error("Xero token refresh failed (HTTP %s): %s", resp.status_code, err)
            return False, f"فشل تجديد رمز Xero: {err}"

        data = resp.json()
        new_access = data.get("access_token")
        new_refresh = data.get("refresh_token")
        expires_in = data.get("expires_in", 1800)

        if not new_access:
            return False, "لم يُعد Xero رمز وصول صالحاً"

        CoffeeSetting.set("xero_bearer_token", new_access)
        if new_refresh:
            CoffeeSetting.set("xero_refresh_token", new_refresh)

        expires_at = time.time() + float(expires_in)
        CoffeeSetting.set("xero_token_expires_at", str(expires_at))

        logger.info("Xero token refreshed successfully, valid for %s seconds", expires_in)
        return True, None
    except Exception as e:
        logger.exception("Exception during Xero token refresh: %s", e)
        return False, str(e)


def _xero_headers(auto_refresh: bool = True) -> dict:
    import time

    token = _get_setting("xero_bearer_token").strip()
    refresh_token = _get_setting("xero_refresh_token").strip()
    client_id = _get_setting("xero_client_id").strip()
    expires_at_str = _get_setting("xero_token_expires_at").strip()

    # إذا كان التوكن منتهي الصلاحية أو متبقي أقل من دقيقتين ونتوفر على refresh_token
    if auto_refresh and refresh_token and client_id:
        needs_refresh = False
        if not token:
            needs_refresh = True
        elif expires_at_str:
            try:
                exp_ts = float(expires_at_str)
                if time.time() >= (exp_ts - 120):  # قبل الانتهاء بدقيقتين
                    needs_refresh = True
            except ValueError:
                pass

        if needs_refresh:
            ok, _ = refresh_xero_token()
            if ok:
                token = _get_setting("xero_bearer_token").strip()

    if not token:
        raise ValueError("xero_bearer_token غير مضبوط في إعدادات القهوة")
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "xero-tenant-id": _get_setting("xero_tenant_id"),
    }


def _xero_request(method: str, url: str, **kwargs) -> requests.Response:
    """
    ينفذ طلب HTTP إلى Xero مع ترويسات التوثيق والتجديد التلقائي للتوكن عند انتهاء الصلاحية أو خطأ 401.
    """
    headers = kwargs.pop("headers", None)
    if headers is None:
        headers = _xero_headers()

    resp = requests.request(method, url, headers=headers, **kwargs)
    if resp.status_code == 401:
        # انتهى التوكن أو غير صالح، محاولة التجديد الفوري ثم إعادة المحاولة مرة واحدة
        ok, _ = refresh_xero_token()
        if ok:
            headers = _xero_headers(auto_refresh=False)
            resp = requests.request(method, url, headers=headers, **kwargs)
    return resp


XERO_BASE = "https://api.xero.com/api.xro/2.0"


def _fmt_date(d) -> str:
    """يُحوّل date أو string إلى YYYY-MM-DD."""
    if isinstance(d, (date, datetime)):
        return d.strftime("%Y-%m-%d")
    return str(d)


def _parse_xero_error(err_obj) -> str:
    """
    يستخرج رسائل التحقق والأخطاء الواضحة من رد Xero أو استثناء HTTP بدلاً من الـ JSON الخام أو كائن Response.
    يتعامل بمرونة مع requests.Response أو requests.HTTPError أو أي Exception.
    """
    if hasattr(err_obj, "status_code"):
        resp = err_obj
    else:
        resp = getattr(err_obj, "response", err_obj)

    full_msg = ""
    data = None
    if hasattr(resp, "json"):
        try:
            data = resp.json()
        except Exception:
            data = None

    if isinstance(data, dict):
        val_errs = []
        for el in data.get("Elements", []):
            if isinstance(el, dict):
                for ve in el.get("ValidationErrors", []):
                    if isinstance(ve, dict) and ve.get("Message"):
                        msg = ve["Message"].strip()
                        if msg and msg not in val_errs:
                            val_errs.append(msg)

        for ve in data.get("ValidationErrors", []):
            if isinstance(ve, dict) and ve.get("Message"):
                msg = ve["Message"].strip()
                if msg and msg not in val_errs:
                    val_errs.append(msg)

        if val_errs:
            full_msg = " | ".join(val_errs)
        else:
            full_msg = data.get("Message") or data.get("Detail") or ""

    raw_text = ""
    if hasattr(resp, "text") and resp.text:
        raw_text = str(resp.text).strip()
    elif isinstance(err_obj, str):
        raw_text = err_obj.strip()
    else:
        raw_text = str(err_obj)

    final_msg = str(full_msg or raw_text)

    translations = [
        ("AuthenticationUnsuccessful", "فشل التوثيق مع Xero — يرجى التحقق من مفتاح الربط (Bearer Token)"),
        ("The access token has expired", "انتهت صلاحية Token في Xero — يرجى تجديده من صفحة الإعدادات"),
        ("token expired", "انتهت صلاحية Token في Xero — يرجى تجديده من صفحة الإعدادات"),
        ("TokenExpired", "انتهت صلاحية Token في Xero — يرجى تجديده من صفحة الإعدادات"),
        ("Credit Note not of valid status for modification", "الإشعار الدائن موجود مسبقاً في Xero ولا يمكن تعديله (معتمد أو مسدد)"),
        ("This document cannot be edited as it has a payment", "رقم السند مستخدم مسبقاً في Xero لمستند قديم مسدد — يرجى استخدام رقم سند جديد أو تعديل التسلسل"),
        ("The Credit Note Number", "رقم الإشعار الدائن مستخدم مسبقاً في Xero لمستند آخر"),
        ("already in use", "رقم السند مستخدم مسبقاً في Xero — يرجى استخدام رقم تسلسلي جديد"),
        ("Invoice not of valid status for modification", "الفاتورة موجودة مسبقاً في Xero ولا يمكن تعديلها"),
        ("ItemCode", "كود الصنف غير معرف في Xero — تحقق من كود الصنف"),
        ("Account code", "كود الحساب المحاسبي غير صالح في Xero"),
    ]
    translated = []
    for eng, ar in translations:
        if eng.lower() in final_msg.lower():
            if ar not in translated:
                translated.append(ar)

    if translated:
        return str(f"{' | '.join(translated)} ({final_msg[:300]})")
    return str(final_msg)[:400]


def _find_existing_xero_document(endpoint: str, doc_number: str, expected_total: Optional[float] = None) -> Optional[dict]:
    """
    يتحقق مما إذا كان المستند (Credit Note أو Invoice) موجوداً مسبقاً في Xero باستخدام رقمه.
    مفيد لتجنب الفشل عند إعادة الترحيل في حال كان المستند قد أنشئ بالفعل في Xero.
    إذا تم تمرير expected_total، يتم التحقق من تطابق المبلغ الإجمالي لتفادي الربط الخاطئ بمستند سابق يحمل نفس الرقم نتيجة تعارض التسلسل.
    """
    if not doc_number:
        return None
    try:
        clean_num = doc_number.strip()
        encoded_num = urllib.parse.quote(clean_num)
        if endpoint == "CreditNotes":
            # في Xero مسار CreditNotes/{id} يقبل فقط معرف GUID، وللبحث برقم الإشعار يُستخدم query parameter where بدقة
            resp = requests.get(
                f'{XERO_BASE}/CreditNotes?where=CreditNoteNumber%3D%3D%22{encoded_num}%22',
                headers=_xero_headers(),
                timeout=15
            )
        else:
            resp = requests.get(
                f"{XERO_BASE}/Invoices?InvoiceNumbers={encoded_num}",
                headers=_xero_headers(),
                timeout=15
            )
            if resp.status_code != 200:
                resp = requests.get(
                    f'{XERO_BASE}/Invoices?where=InvoiceNumber%3D%3D%22{encoded_num}%22',
                    headers=_xero_headers(),
                    timeout=15
                )
        if resp.status_code == 200:
            data = resp.json()
            items = data.get(endpoint, [])
            if items:
                doc = items[0]
                if expected_total is not None:
                    doc_total = float(doc.get("Total") or 0.0)
                    if abs(doc_total - expected_total) > 0.05:
                        logger.warning(
                            f"Xero document {doc_number} exists but total {doc_total} does not match expected {expected_total}. Collision detected."
                        )
                        return None
                return doc
    except Exception as e:
        logger.warning(f"Error querying existing Xero document: {e}")
    return None


def _round_line_amount(total_cost: float, qty: float, unit: float) -> float:
    """
    يعيد حساب LineAmount ليتطابق تماماً مع total_cost
    ويتجنب أخطاء التقريب عند ضرب Qty × UnitAmount في Xero.
    """
    return round(total_cost, 2)


# ─────────────────────────────────────────────────────────────────────────────
# بناء هيكل LineItems للمستند
# ─────────────────────────────────────────────────────────────────────────────

def _build_line_items(lines, side: str) -> list:
    """
    يبني قائمة LineItems لـ Xero.
    side = 'green'   → يستخدم ItemCode الأخضر (GB...)
    side = 'roasted' → يستخدم ItemCode المحمص (RB...)
    """
    items = []
    tracking_cat = _get_setting("xero_tracking_category", "Branches")
    green_tv     = _get_setting("xero_green_tracking_val", "Green Coffee")
    roasted_tv   = _get_setting("xero_roasted_tracking_val", "Roasted Coffee")

    for line in lines:
        if side == "green":
            item_code   = line.green_item.xero_item_code if line.green_item else ""
            qty         = round(line.green_qty_kg or 0, 4)
            unit_amount = round(line.green_unit_cost or 0, 4)
            line_amount = round(line.green_total_cost or 0, 2)
            tracking_val = green_tv
        else:  # roasted
            item_code    = line.roasted_item.xero_item_code if line.roasted_item else ""
            qty          = round(line.roasted_qty_kg_actual or 0, 4)
            unit_amount  = round(line.roasted_unit_cost or 0, 6)
            # القيمة الإجمالية للسطر = نفس تكلفة السطر الأخضر (لضمان التطابق بالهللة)
            line_amount  = round(line.green_total_cost or 0, 2)
            # إعادة حساب unit_amount من line_amount للتطابق الدقيق
            if qty and qty > 0:
                unit_amount = round(line_amount / qty, 6)
            tracking_val = roasted_tv

        items.append({
            "ItemCode":     item_code.strip(),
            "Description":  item_code.strip(),
            "Quantity":     qty,
            "UnitAmount":   unit_amount,
            "LineAmount":   line_amount,
            "AccountCode":  _get_setting(
                "xero_green_account" if side == "green" else "xero_roasted_account",
                "635" if side == "green" else "636"
            ),
            "TaxType":      "NONE",
            "Tracking": [{
                "Name":   tracking_cat,
                "Option": tracking_val,
            }] if tracking_cat else [],
        })
    return items


# ─────────────────────────────────────────────────────────────────────────────
# المستند الأول: Payable Credit Note — خصم مخزون الأخضر
# ─────────────────────────────────────────────────────────────────────────────

def build_green_credit_note_payload(order) -> dict:
    ref      = order.order_number
    contact  = _get_setting("xero_green_contact", "امر تحميص مستودع البن الاخضر")
    lines    = [l for l in order.lines if l.line_status in ("green_issued", "completed")]
    return {
        "Type":               "ACCPAYCREDIT",
        "Contact":            {"Name": contact},
        "Date":               _fmt_date(order.order_date),
        "CreditNoteNumber":   ref,
        "Reference":          ref,
        "Status":             "AUTHORISED",
        "LineAmountTypes":    "Exclusive",
        "CurrencyCode":       "SAR",
        "LineItems":          _build_line_items(lines, "green"),
    }


def build_roasted_bill_payload(order) -> dict:
    ref     = order.order_number
    contact = _get_setting("xero_roasted_contact", "تحميص وارد لمستودع البن المحمص")
    lines   = [l for l in order.lines if l.line_status == "completed"]
    return {
        "Type":            "ACCPAY",
        "Contact":         {"Name": contact},
        "Date":            _fmt_date(order.order_date),
        "DueDate":         _fmt_date(order.order_date),
        "InvoiceNumber":   ref,
        "Reference":       ref,
        "Status":          "AUTHORISED",
        "LineAmountTypes": "Exclusive",
        "CurrencyCode":    "SAR",
        "LineItems":       _build_line_items(lines, "roasted"),
    }


def _post_credit_note_resilient(payload: dict, timeout: int = 30) -> requests.Response:
    """
    يرسل مستند Credit Note إلى Xero مع إعادة محاولة ذكية فورية:
    إذا رفض Xero الطلب بأحد الأخطاء التالية:
    1) خطأ تعثر المخزون التتبعي:
       'The total cost pool of tracked inventory items cannot be reduced to less than zero'
       أو كود غير صالح 'The Item code ... is not valid':
       -> يتم تجريد ItemCode فوراً ليتم القيد المالي المباشر على الحساب (636/635) بدون لمس رصيد المخزون في زيرو.
    2) خطأ فروق التقريب بالهللات:
       'The line total X does not match the expected line total Y':
       -> يتم تجريد LineAmount ليحسبه محرك زيرو الداخلي بدقة تامة ويقبل السند.
    3) أي خطأ Validation آخر بالبنود:
       -> يتم تجريد ItemCode و LineAmount مع الاحتفاظ بالحساب المالي والكمية وسعر الوحدة والوصف ومراكز التكلفة (Tracking).
    """
    # تجريد LineAmount دائماً قبل الإرسال لزيرو ليحسبه محرك Xero الداخلي مباشرة ويمنع خطأ فروق التقريب بالهللات
    clean_payload = dict(payload)
    clean_lines = []
    for li in payload.get("LineItems", []):
        clean_li = dict(li)
        clean_li.pop("LineAmount", None)
        clean_lines.append(clean_li)
    clean_payload["LineItems"] = clean_lines

    resp = requests.post(
        f"{XERO_BASE}/CreditNotes",
        headers=_xero_headers(),
        json={"CreditNotes": [clean_payload]},
        timeout=timeout,
    )
    if resp.status_code in (200, 201):
        return resp

    err_text = (resp.text or "").lower()
    if not err_text and hasattr(resp, "json"):
        try:
            err_text = json.dumps(resp.json()).lower()
        except Exception:
            pass

    # إذا كان المستند موجوداً مسبقاً في زيرو أو لا يمكن تعديله، فلا فائدة من محاولة fallback بالبنود
    if any(k in err_text for k in ("not of valid status", "cannot be edited", "already in use", "already exists")):
        return resp

    is_recoverable = any(k in err_text for k in (
        "item code", "itemcode", "cost pool", "tracked", "inventory",
        "line total", "expected line total", "validationerror"
    ))

    if is_recoverable:
        fallback_payload = dict(payload)
        fallback_lines = []
        needs_strip_item = any(k in err_text for k in ("item code", "itemcode", "cost pool", "tracked"))
        for li in payload.get("LineItems", []):
            clean_li = dict(li)
            # تجريد LineAmount دائماً في الـ fallback ليحسبه محرك Xero الداخلي مباشرة ويمنع خطأ فروق التقريب بالهللات
            clean_li.pop("LineAmount", None)
            if needs_strip_item:
                clean_li.pop("ItemCode", None)
            fallback_lines.append(clean_li)
        fallback_payload["LineItems"] = fallback_lines

        try:
            resp2 = requests.post(
                f"{XERO_BASE}/CreditNotes",
                headers=_xero_headers(),
                json={"CreditNotes": [fallback_payload]},
                timeout=timeout,
            )
            if resp2.status_code in (200, 201):
                logger.info("Xero credit note posted successfully via resilient fallback (without LineAmount).")
                return resp2

            logger.warning("Xero credit note fallback returned HTTP %s: %s", resp2.status_code, (resp2.text or "")[:300])
            err2_text = (resp2.text or "").lower()

            # محاولة إضافية 2: إذا كان الخطأ بسبب كود الصنف أو المخزون التتبعي
            if not needs_strip_item and any(k in err2_text for k in ("item code", "itemcode", "cost pool", "tracked", "validationerror")):
                no_item_lines = []
                for li in fallback_lines:
                    clean_ni = dict(li)
                    clean_ni.pop("ItemCode", None)
                    no_item_lines.append(clean_ni)
                no_item_payload = dict(fallback_payload)
                no_item_payload["LineItems"] = no_item_lines
                resp2 = requests.post(
                    f"{XERO_BASE}/CreditNotes",
                    headers=_xero_headers(),
                    json={"CreditNotes": [no_item_payload]},
                    timeout=timeout,
                )
                if resp2.status_code in (200, 201):
                    logger.info("Xero credit note posted successfully after stripping ItemCode.")
                    return resp2
                err2_text = (resp2.text or "").lower()

            # فحص إضافي: إذا كان سبب الرفض هو Tracking Category أو Option غير معرف في زيرو
            if any(k in err2_text for k in ("tracking", "option")):
                no_track_payload = dict(fallback_payload)
                no_track_lines = []
                for li in fallback_payload.get("LineItems", []):
                    clean_nt = dict(li)
                    clean_nt.pop("ItemCode", None)
                    clean_nt.pop("Tracking", None)
                    no_track_lines.append(clean_nt)
                no_track_payload["LineItems"] = no_track_lines

                resp3 = requests.post(
                    f"{XERO_BASE}/CreditNotes",
                    headers=_xero_headers(),
                    json={"CreditNotes": [no_track_payload]},
                    timeout=timeout,
                )
                if resp3.status_code in (200, 201):
                    logger.info("Xero credit note posted successfully without Tracking.")
                    return resp3
                return resp3

            return resp2
        except Exception as e:
            logger.warning("Exception during Xero credit note resilient fallback: %s", e)

    return resp


def post_credit_note(order) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    يُنشئ Payable Credit Note (ACCPAYCREDIT) في Xero.
    يعيد (success, credit_note_id, error_message)
    """
    lines = [l for l in order.lines if l.line_status in ("green_issued", "completed")]
    if not lines:
        return False, None, "لا توجد أسطر معتمدة في الأمر"

    total = round(sum(l.green_total_cost or 0 for l in lines), 2)
    payload = build_green_credit_note_payload(order)

    try:
        resp = _post_credit_note_resilient(payload, timeout=30)
        if resp.status_code in (200, 201):
            data = resp.json()
            cn   = data["CreditNotes"][0]
            cn_id = cn["CreditNoteID"]
            actual_total = float(cn.get("Total") or total)

            # ─ إغلاق Credit Note بـ Cash Refund على الحساب الوسيط ────────────
            clearing = _get_setting("xero_clearing_account", "851")
            if clearing and cn_id and actual_total > 0:
                _apply_cash_refund(cn_id, actual_total, order.order_date, clearing)

            url = f"https://go.xero.com/AccountsPayable/View.aspx?creditnoteID={cn_id}"
            return True, cn_id, None
        else:
            # التعافي الذكي في حال كان الإشعار موجوداً مسبقاً في زيرو ومسدداً
            existing = _find_existing_xero_document("CreditNotes", order.order_number, expected_total=total)
            if existing and existing.get("CreditNoteID"):
                cn_id = existing.get("CreditNoteID")
                rem_credit = float(existing.get("RemainingCredit") or 0.0)
                clearing = _get_setting("xero_clearing_account", "851")
                if clearing and rem_credit > 0:
                    _apply_cash_refund(cn_id, rem_credit, order.order_date, clearing)
                logger.info("Found existing Xero Credit Note %s for order %s, linked successfully.", cn_id, order.order_number)
                return True, cn_id, None

            err = _parse_xero_error(resp)
            return False, None, err

    except Exception as e:
        return False, None, str(e)


def _apply_cash_refund(cn_id: str, amount: float, txn_date, account_code: str):
    """يُغلق Credit Note بـ Cash Refund (Remaining Credit = 0)."""
    payload = {
        "Payments": [{
            "CreditNote": {"CreditNoteID": cn_id},
            "Account":    {"Code": account_code},
            "Date":       _fmt_date(txn_date),
            "Amount":     round(amount, 2),
        }]
    }
    try:
        requests.post(
            f"{XERO_BASE}/Payments",
            headers=_xero_headers(),
            json=payload,
            timeout=20,
        )
    except Exception as e:
        print(f"[Xero] تحذير — Cash Refund فشل: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# المستند الثاني: Payable Invoice/Bill — إثبات وارد المحمص
# ─────────────────────────────────────────────────────────────────────────────

def post_bill(order) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    يُنشئ Payable Invoice / Bill (ACCPAY) في Xero.
    يعيد (success, bill_id, error_message)
    """
    ref     = order.order_number.replace("RB-", "")
    contact = _get_setting("xero_roasted_contact", "تحميص وارد لمستودع البن المحمص")
    lines   = [l for l in order.lines if l.line_status == "completed"]

    if not lines:
        return False, None, "لا توجد أسطر مكتملة في الأمر"

    total = round(sum(l.green_total_cost or 0 for l in lines), 2)
    payload = build_roasted_bill_payload(order)

    try:
        resp = requests.post(
            f"{XERO_BASE}/Invoices",
            headers=_xero_headers(),
            json={"Invoices": [payload]},
            timeout=30,
        )
        if resp.status_code in (200, 201):
            data    = resp.json()
            inv     = data["Invoices"][0]
            bill_id = inv["InvoiceID"]

            # ─ إغلاق Bill بـ Payment على الحساب الوسيط ───────────────────────
            clearing = _get_setting("xero_clearing_account", "851")
            if clearing and bill_id:
                _apply_payment(bill_id, total, order.order_date, clearing)

            return True, bill_id, None
        else:
            # التعافي الذكي في حال كانت الفاتورة موجودة مسبقاً في زيرو ومسددة
            existing = _find_existing_xero_document("Invoices", order.order_number, expected_total=total)
            if existing and existing.get("InvoiceID"):
                bill_id = existing.get("InvoiceID")
                amt_due = float(existing.get("AmountDue") or 0.0)
                clearing = _get_setting("xero_clearing_account", "851")
                if clearing and amt_due > 0:
                    _apply_payment(bill_id, amt_due, order.order_date, clearing)
                logger.info("Found existing Xero Bill %s for order %s, linked successfully.", bill_id, order.order_number)
                return True, bill_id, None

            err = _parse_xero_error(resp)
            return False, None, err

    except Exception as e:
        return False, None, str(e)


def _apply_payment(bill_id: str, amount: float, txn_date, account_code: str):
    """يُغلق Bill بـ Payment (Amount Due = 0)."""
    payload = {
        "Payments": [{
            "Invoice": {"InvoiceID": bill_id},
            "Account": {"Code": account_code},
            "Date":    _fmt_date(txn_date),
            "Amount":  round(amount, 2),
        }]
    }
    try:
        requests.post(
            f"{XERO_BASE}/Payments",
            headers=_xero_headers(),
            json=payload,
            timeout=20,
        )
    except Exception as e:
        print(f"[Xero] تحذير — Bill Payment فشل: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# الدالة الرئيسية: ترحيل أمر تحميص كامل
# ─────────────────────────────────────────────────────────────────────────────

def post_roasting_order_to_xero(order) -> dict:
    """
    يُرحّل أمر تحميص مكتمل إلى Xero.
    يعيد dict يصف الحالة النهائية.
    يدعم الترحيل الجزئي: إذا نجح أحد المستندين فقط → xero_status = 'partial'.
    """
    from models import db
    from models_coffee import log_coffee_audit
    from flask import request as flask_request, session

    result = {
        "credit_note_success": False,
        "bill_success":        False,
        "credit_note_id":      None,
        "bill_id":             None,
        "errors":              [],
        "xero_status":         "failed",
        "attachment_success":  False,
        "attachment_errors":   [],
    }

    # ─ التحقق من الحالة ────────────────────────────────────────────────────
    if order.status != "completed":
        result["errors"].append("الأمر لم يكتمل بعد — لا يمكن الترحيل")
        return result

    # ─ منع إعادة ترحيل المستند الناجح ─────────────────────────────────────
    cn_needed   = not bool(order.xero_credit_note_id)
    bill_needed = not bool(order.xero_bill_id)

    if not cn_needed and not bill_needed:
        result["xero_status"]        = "posted"
        result["credit_note_success"] = True
        result["bill_success"]        = True
        result["credit_note_id"]      = order.xero_credit_note_id
        result["bill_id"]             = order.xero_bill_id
        return result

    # ─ إرسال Credit Note ───────────────────────────────────────────────────
    if cn_needed:
        ok, cn_id, err = post_credit_note(order)
        result["credit_note_success"] = ok
        result["credit_note_id"]      = cn_id
        if ok:
            order.xero_credit_note_id  = cn_id
            order.xero_credit_note_url = f"https://go.xero.com/AccountsPayable/View.aspx?creditnoteID={cn_id}"
        else:
            result["errors"].append(f"Credit Note: {err}")
    else:
        result["credit_note_success"] = True
        result["credit_note_id"]      = order.xero_credit_note_id

    # ─ إرسال Bill ──────────────────────────────────────────────────────────
    if bill_needed:
        ok, bill_id, err = post_bill(order)
        result["bill_success"] = ok
        result["bill_id"]      = bill_id
        if ok:
            order.xero_bill_id  = bill_id
            order.xero_bill_url = f"https://go.xero.com/AccountsPayable/Edit.aspx?InvoiceID={bill_id}"
        else:
            result["errors"].append(f"Bill: {err}")
    else:
        result["bill_success"] = True
        result["bill_id"]      = order.xero_bill_id

    # ─ تحديد الحالة الإجمالية ─────────────────────────────────────────────
    if result["credit_note_success"] and result["bill_success"]:
        order.xero_status    = "posted"
        order.xero_posted_at = datetime.utcnow()
        order.xero_error_message = None
        result["xero_status"] = "posted"
    elif result["credit_note_success"] or result["bill_success"]:
        order.xero_status        = "partial"
        order.xero_error_message = " | ".join(result["errors"])
        result["xero_status"]    = "partial"
    else:
        order.xero_status        = "failed"
        order.xero_error_message = " | ".join(result["errors"])
        result["xero_status"]    = "failed"

    db.session.commit()

    # ─ إرفاق مستند أمر التحميص الرسمي في Xero (فاتورة ناتج المحمص وإشعار الأخضر) ──
    if order.xero_bill_id:
        try:
            att_b = attach_voucher_to_xero("Invoices", order.xero_bill_id, "roasting_order", order.id, order.order_number)
            if att_b.get("success"):
                result["attachment_success"] = True
                try:
                    uid = session.get("user_id") if flask_request else None
                    log_coffee_audit(
                        event_type  = "xero_attachment",
                        entity_type = "roasting_order",
                        entity_id   = order.id,
                        user_id     = uid,
                        new_value   = {"filename": att_b.get("filename"), "target": "Bill", "invoice_id": order.xero_bill_id},
                        notes       = f"إرفاق نسخة أمر التحميص {att_b.get('filename')} بفاتورة ناتج التحميص في زيرو"
                    )
                    db.session.commit()
                except Exception:
                    pass
            else:
                result["attachment_errors"].append(f"فاتورة المحمص: {att_b.get('error')}")
        except Exception as e:
            logger.warning("Failed to attach roasting voucher to bill: %s", e)
            result["attachment_errors"].append(f"فاتورة المحمص: {e}")

    if order.xero_credit_note_id:
        try:
            att_cn = attach_voucher_to_xero("CreditNotes", order.xero_credit_note_id, "roasting_order", order.id, order.order_number)
            if att_cn.get("success"):
                result["attachment_success"] = True
            else:
                result["attachment_errors"].append(f"إشعار الأخضر: {att_cn.get('error')}")
        except Exception as e:
            logger.warning("Failed to attach roasting voucher to credit note: %s", e)
            result["attachment_errors"].append(f"إشعار الأخضر: {e}")

    # ─ تسجيل في سجل التدقيق ───────────────────────────────────────────────
    try:
        uid = session.get("user_id") if flask_request else None
        log_coffee_audit(
            event_type  = "post_xero" if result["xero_status"] == "posted" else "partial_xero",
            entity_type = "roasting_order",
            entity_id   = order.id,
            user_id     = uid,
            new_value   = {
                "xero_status":      order.xero_status,
                "credit_note_id":   order.xero_credit_note_id,
                "bill_id":          order.xero_bill_id,
                "errors":           result["errors"],
            },
            notes = f"ترحيل أمر تحميص {order.order_number} إلى Xero"
        )
        db.session.commit()
    except Exception:
        pass

    return result


# ─────────────────────────────────────────────────────────────────────────────
# اختبار الاتصال بـ Xero
# ─────────────────────────────────────────────────────────────────────────────

def test_xero_connection() -> dict:
    """يختبر الاتصال بـ Xero ويعيد dict بالنتيجة."""
    try:
        resp = _xero_request("GET", f"{XERO_BASE}/Organisation", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        org_name = data.get("Organisations", [{}])[0].get("Name", "—")
        return {"success": True, "org_name": org_name, "status_code": resp.status_code}
    except requests.HTTPError as e:
        return {"success": False, "error": _parse_xero_error(e), "status_code": e.response.status_code if e.response else 0}
    except Exception as e:
        return {"success": False, "error": str(e), "status_code": 0}



# ─────────────────────────────────────────────────────────────────────────────
# ترحيل سند صرف البن الأخضر بالبيع كفاتورة مبيعات مسودة لـ Xero
# ─────────────────────────────────────────────────────────────────────────────

def build_green_issue_sale_invoice_payload(issue) -> dict:
    """
    يبني payload فاتورة مبيعات مسودة (Draft Receivable Invoice) لسند صرف البن الأخضر بالبيع.
    Reference: issue.doc_number (مثال: GB 0001)
    """
    sales_account = _get_setting("xero_green_sales_account", "201")
    customer_name = (issue.customer_name or "").strip() or "عميل بيع بن أخضر"
    tracking_cat  = _get_setting("xero_tracking_category", "Branches")
    green_tv      = _get_setting("xero_green_tracking_val", "Green Coffee")

    tax_type = _get_setting("xero_sales_tax_type", "OUTPUT")

    line_items = []
    for line in issue.all_lines:
        citem = line.coffee_item
        if not citem:
            continue
        uprice = float(line.sale_price_per_kg or (citem.sale_price_per_kg or 0.0))
        q = float(line.qty_kg or 0.0)
        lamount = round(q * uprice, 2)
        line_items.append({
            "ItemCode": citem.xero_item_code.strip() if citem.xero_item_code else "",
            "Description": f"بيع بن أخضر - {citem.name_ar or citem.xero_item_code}",
            "Quantity": q,
            "UnitAmount": uprice,
            "LineAmount": lamount,
            "AccountCode": sales_account,
            "TaxType": tax_type,
            "Tracking": [{
                "Name": tracking_cat,
                "Option": green_tv
            }]
        })

    if not line_items and issue.coffee_item:
        qty = round(issue.qty_kg or 0, 4)
        uprice = round(issue.sale_price_per_kg or (issue.coffee_item.sale_price_per_kg or 0), 2)
        line_items.append({
            "ItemCode": issue.coffee_item.xero_item_code.strip(),
            "Description": f"بيع بن أخضر - {issue.coffee_item.name_ar or issue.coffee_item.xero_item_code}",
            "Quantity": qty,
            "UnitAmount": uprice,
            "LineAmount": round(qty * uprice, 2),
            "AccountCode": sales_account,
            "TaxType": tax_type,
            "Tracking": [{
                "Name": tracking_cat,
                "Option": green_tv
            }]
        })

    # إضافة بند مصاريف الشحن والتوصيل (حساب 261) في حالة السندات المشحونة
    if getattr(issue, "delivery_method", None) == "shipping":
        from models_coffee import calculate_shipping_fee
        ship_fee = float(getattr(issue, "shipping_fee", 0.0) or 0.0)
        if ship_fee <= 0:
            ship_fee = calculate_shipping_fee(issue.total_qty_kg, issue.shipping_company)
        if ship_fee > 0:
            ship_account = _get_setting("xero_shipping_account", "261")
            ship_desc = _get_setting("xero_shipping_description", "ايرادات شحن وتوصيل")
            ship_line = {
                "Description": ship_desc,
                "Quantity": 1.0,
                "UnitAmount": round(ship_fee, 2),
                "LineAmount": round(ship_fee, 2),
                "AccountCode": ship_account,
                "TaxType": tax_type,
            }
            ship_item_code = _get_setting("xero_shipping_item_code", "").strip()
            if ship_item_code and ship_item_code != "مصاريف شحن":
                ship_line["ItemCode"] = ship_item_code
            if tracking_cat and green_tv:
                ship_line["Tracking"] = [{
                    "Name": tracking_cat,
                    "Option": green_tv
                }]
            line_items.append(ship_line)

    return {
        "Type": "ACCREC",
        "Contact": {
            "Name": customer_name
        },
        "Date": _fmt_date(issue.issue_date),
        "DueDate": _fmt_date(issue.issue_date),
        "Status": "DRAFT",
        "LineAmountTypes": "Exclusive",
        "Reference": issue.doc_number,
        "LineItems": line_items
    }


def _generate_voucher_pdf(doc_type: str, entity_id: int) -> Tuple[Optional[bytes], Optional[str]]:
    """
    يولد مستند PDF للسند الرسمي المطبوع شاملاً الترويسة والأصناف والأختام والشعار والتوقيع الحي.
    يعيد: (pdf_bytes, mime_type)
    إذا تعذر تشغيل المتصفح، يعيد صورة التوقيع الحي PNG كخيار احتياطي موثوق.
    """
    from flask import current_app, render_template
    from models_coffee import RoastedCoffeeDispatch, GreenCoffeeIssue, RoastingOrder

    html_content = ""
    signature_data = None
    b64_logo = ""

    # 1. تجهيز الشعار كـ base64 مسبقاً وتمريره للقالب وتضمينه في الـ HTML
    try:
        root_dir = current_app.root_path if current_app else os.path.dirname(os.path.abspath(__file__))
        logo_path = os.path.join(root_dir, "static", "img", "logo.png")
        if not os.path.exists(logo_path):
            logo_path = os.path.join(root_dir, "static", "img", "logo.jpg")
        if os.path.exists(logo_path):
            with open(logo_path, "rb") as lf:
                b64_logo = base64.b64encode(lf.read()).decode("ascii")
    except Exception as e:
        logger.debug("Could not read logo: %s", e)

    try:
        ctx = current_app.test_request_context() if current_app else None
        if ctx:
            ctx.push()

        try:
            if doc_type == "roasted_dispatch":
                disp = RoastedCoffeeDispatch.query.get(entity_id)
                if disp:
                    html_content = render_template("coffee/dispatch_print.html", dispatch=disp, logo_b64=b64_logo)
                    signature_data = disp.customer_signature
            elif doc_type == "green_issue":
                issue = GreenCoffeeIssue.query.get(entity_id)
                if issue:
                    html_content = render_template("coffee/green_issue_print.html", issue=issue, logo_b64=b64_logo)
                    signature_data = issue.customer_signature
            elif doc_type == "roasting_order":
                order = RoastingOrder.query.get(entity_id)
                if order:
                    html_content = render_template("coffee/roasting_print.html", order=order, gw=order.green_warehouse, rw=order.roasted_warehouse, logo_b64=b64_logo)
        finally:
            if ctx:
                ctx.pop()
    except Exception as e:
        logger.warning("Error rendering print HTML for %s #%s: %s", doc_type, entity_id, e)

    # استبدال أي مسار متبقي للشعار بـ Data URI لضمان ظهوره في المتصفح الرأسي
    if html_content and b64_logo:
        data_uri = f"data:image/png;base64,{b64_logo}"
        html_content = re.sub(
            r'(src|href)=["\'][^"\']*(?:logo\.(?:png|jpg|jpeg))["\']',
            rf'\1="{data_uri}"',
            html_content,
            flags=re.IGNORECASE
        )
        html_content = html_content.replace("/static/img/logo.png", data_uri)
        html_content = html_content.replace("/static/img/logo.jpg", data_uri)

    if html_content:
        # مسارات المتصفح المدعومة (بيئات Linux/Docker/Nix وحاويات CranL بالإضافة لبيئات Windows)
        browser_paths = [
            os.environ.get("PUPPETEER_EXECUTABLE_PATH", ""),
            os.environ.get("CHROME_BIN", ""),
            os.environ.get("CHROMIUM_PATH", ""),
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/root/.nix-profile/bin/chromium",
            "/nix/var/nix/profiles/default/bin/chromium",
            "/usr/local/bin/chromium",
            "/usr/local/bin/chrome",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]
        browser_exe = next((p for p in browser_paths if p and os.path.exists(p)), None)
        if not browser_exe:
            for name in ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome", "msedge"]:
                w = shutil.which(name)
                if w:
                    browser_exe = w
                    break

        if browser_exe:
            temp_html = None
            temp_pdf = None
            user_data_dir = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8") as f:
                    f.write(html_content)
                    temp_html = f.name
                temp_pdf = temp_html.replace(".html", ".pdf")
                user_data_dir = tempfile.mkdtemp(prefix="chrome_pdf_")

                # تجربة --headless=new أولاً (المعيار الحديث) ثم --headless التقليدي
                for headless_flag in ["--headless=new", "--headless"]:
                    cmd = [
                        browser_exe,
                        headless_flag,
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-software-rasterizer",
                        "--run-all-compositor-stages-before-draw",
                        "--no-pdf-header-footer",
                        f"--user-data-dir={user_data_dir}",
                        f"--print-to-pdf={temp_pdf}",
                        f"file://{os.path.abspath(temp_html)}"
                    ]
                    try:
                        res = subprocess.run(cmd, capture_output=True, timeout=30)
                        if os.path.exists(temp_pdf) and os.path.getsize(temp_pdf) > 0:
                            with open(temp_pdf, "rb") as pf:
                                pdf_bytes = pf.read()
                            return pdf_bytes, "application/pdf"
                        else:
                            err_msg = res.stderr.decode('utf-8', errors='replace')[:400] if res.stderr else ''
                            logger.warning("Chromium %s returned code %s: %s", headless_flag, res.returncode, err_msg)
                    except Exception as sub_e:
                        logger.warning("Headless attempt with %s failed: %s", headless_flag, sub_e)
            finally:
                if temp_html and os.path.exists(temp_html):
                    try: os.remove(temp_html)
                    except Exception: pass
                if temp_pdf and os.path.exists(temp_pdf):
                    try: os.remove(temp_pdf)
                    except Exception: pass
                if user_data_dir and os.path.exists(user_data_dir):
                    try: shutil.rmtree(user_data_dir, ignore_errors=True)
                    except Exception: pass

    logger.warning("PDF voucher generation failed for %s #%s (no suitable headless browser or render error)", doc_type, entity_id)
    return None, None


def attach_voucher_to_xero(endpoint: str, record_id: str, doc_type: str, entity_id: int, doc_number: str) -> dict:
    """
    يرفع نسخة السند الرسمي (PDF المعتمد) كمرفق مباشر في زيرو:
    - endpoint: 'Invoices' أو 'CreditNotes'
    - record_id: المعرف الفريد للفاتورة أو الإشعار في Xero (InvoiceID أو CreditNoteID)
    """
    token = _get_setting("xero_bearer_token").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    tenant_id = _get_setting("xero_tenant_id").strip()

    if not token or not tenant_id or not record_id:
        return {"success": False, "error": "بيانات التوثيق أو معرف زيرو غير متوفرة"}

    content_bytes, mime_type = _generate_voucher_pdf(doc_type, entity_id)
    if not content_bytes or mime_type != "application/pdf":
        return {"success": False, "error": "تعذر توليد ملف السند الرسمي كـ PDF للإرفاق في زيرو"}

    ext = "pdf"
    safe_doc = re.sub(r"[^\w\-]", "_", (doc_number or "voucher").strip())
    filename = f"Voucher_{safe_doc}.pdf"
    quoted_filename = urllib.parse.quote(filename)

    url = f"{XERO_BASE}/{endpoint}/{record_id}/Attachments/{quoted_filename}"
    headers = {
        "Authorization": f"Bearer {token}",
        "xero-tenant-id": tenant_id,
        "Content-Type": mime_type,
        "Content-Length": str(len(content_bytes)),
        "Accept": "application/json",
        "X-Include-On-Online": "true",
    }

    try:
        resp = requests.post(
            url,
            data=content_bytes,
            headers=headers,
            timeout=30
        )
        if resp.status_code in (200, 201):
            return {"success": True, "filename": filename, "mime_type": mime_type}
        else:
            err = _parse_xero_error(resp)
            return {"success": False, "error": f"فشل إرفاق الملف (HTTP {resp.status_code}): {err}"}
    except Exception as e:
        return {"success": False, "error": f"استثناء أثناء إرفاق الملف بـ Xero: {e}"}


def build_green_issue_credit_note_payload(issue) -> dict:
    """
    يبني payload إشعار دائن مشتريات (Payable Credit Note - ACCPAYCREDIT)
    لسند صرف البن الأخضر لغرض عينات فحص وتذوق (sample) أو استخدام داخلي تجارب (internal_use).
    - الجهة Contact Name: بنفس الاسم ('عينات فحص وتذوق' أو 'استخدام داخلي تجارب').
    - المرجع Reference: رقم السند (مثل GB 001).
    - كود الإشعار CreditNoteNumber: رقم السند (مثل GB 001).
    - الحساب AccountCode: مخزون الأخضر 635.
    - الضريبة TaxType: NONE.
    - التتبع Tracking: Branches / Green Coffee.
    """
    if issue.issue_type == "sample":
        contact_name = _get_setting("xero_green_sample_contact", "عينات فحص وتذوق")
    elif issue.issue_type == "internal_use":
        contact_name = _get_setting("xero_green_internal_contact", "استخدام داخلي تجارب")
    else:
        contact_name = issue.issue_reason or "سند صرف بن اخضر"

    green_account = _get_setting("xero_green_account", "635")
    tracking_cat  = _get_setting("xero_tracking_category", "Branches")
    green_tv      = _get_setting("xero_green_tracking_val", "Green Coffee")

    line_items = []
    for line in issue.all_lines:
        citem = line.coffee_item
        if not citem:
            continue
        q = round(float(line.qty_kg or 0.0), 4)
        # اعتماد سعر تكلفة الصنف على Xero
        ucost = citem.cost_price if (citem and citem.cost_price > 0) else (round(float(line.unit_cost or 0.0), 4))
        total = round(q * ucost, 2)
        icode = (citem.xero_item_code or f"GB-{citem.base_code}").strip()
        desc = f"{line.notes} - {citem.name_ar}" if line.notes else (citem.name_ar or citem.name_en or icode)
        tracking_list = [{"Name": tracking_cat, "Option": green_tv}] if tracking_cat else []

        line_items.append({
            "ItemCode": icode,
            "Description": desc,
            "Quantity": q,
            "UnitAmount": ucost,
            "LineAmount": total,
            "AccountCode": green_account,
            "TaxType": "NONE",
            "Tracking": tracking_list
        })

    if not line_items and issue.coffee_item:
        q = round(float(issue.qty_kg or 0.0), 4)
        total = round(float(issue.total_cost or (q * (issue.unit_cost or 0.0))), 2)
        ucost = round(total / q, 4) if q > 0 else round(float(issue.unit_cost or 0.0), 4)
        icode = (issue.coffee_item.xero_item_code or f"GB-{issue.coffee_item.base_code}").strip()
        desc = issue.coffee_item.name_ar or issue.coffee_item.name_en or icode
        tracking_list = [{"Name": tracking_cat, "Option": green_tv}] if tracking_cat else []

        line_items.append({
            "ItemCode": icode,
            "Description": desc,
            "Quantity": q,
            "UnitAmount": ucost,
            "LineAmount": total,
            "AccountCode": green_account,
            "TaxType": "NONE",
            "Tracking": tracking_list
        })

    return {
        "Type": "ACCPAYCREDIT",
        "Contact": {"Name": contact_name},
        "Date": _fmt_date(issue.issue_date),
        "CreditNoteNumber": issue.doc_number,
        "Reference": issue.doc_number,
        "Status": "AUTHORISED",
        "LineAmountTypes": "Exclusive",
        "CurrencyCode": "SAR",
        "LineItems": line_items
    }


def post_green_issue_to_xero(issue_id: int) -> dict:
    """
    يرحل سند صرف البن الأخضر إلى Xero:
    1) بالبيع ('sale'): كفاتورة مبيعات مسودة (Draft ACCREC Invoice) على حساب 201 مع 15% ضريبة.
    2) عينات فحص وتذوق ('sample') أو استخدام داخلي تجارب ('internal_use'):
       كإشعار دائن (ACCPAYCREDIT) بنفس الاسم، ويُقفل بحساب عينات قهوة (كود 345) بـ Cash Refund ليكون Paid.
    ويرفق نسخة السند الرسمي الموقعة تلقائياً في زيرو.
    """
    from models import db
    from models_coffee import GreenCoffeeIssue, log_coffee_audit

    issue = GreenCoffeeIssue.query.get(issue_id)
    if not issue:
        return {"success": False, "error": f"سند الصرف #{issue_id} غير موجود"}

    if issue.status != "approved":
        return {"success": False, "error": "لا يمكن ترحيل السند إلى Xero لأنه مسودة — يجب اعتماد السند أولاً"}

    token = _get_setting("xero_bearer_token")
    if not token and _get_setting("xero_refresh_token"):
        refresh_xero_token()
        token = _get_setting("xero_bearer_token")
    if not token:
        issue.xero_status = "failed"
        issue.xero_error = "رمز Xero Token غير مضبوط في الإعدادات"
        db.session.commit()
        return {"success": False, "error": issue.xero_error}

    if issue.issue_type == "sale":
        payload = build_green_issue_sale_invoice_payload(issue)
        try:
            resp = requests.post(
                f"{XERO_BASE}/Invoices",
                json={"Invoices": [payload]},
                headers=_xero_headers(),
                timeout=25
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                inv = data.get("Invoices", [{}])[0]
                issue.xero_invoice_id = inv.get("InvoiceID")
                issue.xero_invoice_number = inv.get("InvoiceNumber")
                issue.xero_status = "posted"
                issue.xero_posted_at = datetime.utcnow()
                issue.xero_error = None
                db.session.commit()

                # إرفاق نسخة السند الرسمي (PDF / التوقيع) كمرفق مباشر في Xero
                att_res = attach_voucher_to_xero("Invoices", issue.xero_invoice_id, "green_issue", issue.id, issue.doc_number)
                if att_res.get("success"):
                    try:
                        log_coffee_audit(
                            event_type="xero_attachment",
                            entity_type="green_issue",
                            entity_id=issue.id,
                            new_value={"filename": att_res.get("filename")},
                            notes=f"إرفاق نسخة السند الرسمي {att_res.get('filename')} بفاتورة مبيعات زيرو {issue.xero_invoice_number}"
                        )
                        db.session.commit()
                    except Exception:
                        pass

                try:
                    log_coffee_audit(
                        event_type="post_xero",
                        entity_type="green_issue",
                        entity_id=issue.id,
                        new_value={"invoice_id": issue.xero_invoice_id, "invoice_number": issue.xero_invoice_number},
                        notes=f"ترحيل سند صرف الأخضر {issue.doc_number} كفاتورة مبيعات مسودة لـ Xero"
                    )
                    db.session.commit()
                except Exception:
                    pass

                return {"success": True, "invoice_id": issue.xero_invoice_id, "invoice_number": issue.xero_invoice_number, "type": "invoice"}
            else:
                existing = _find_existing_xero_document("Invoices", issue.doc_number)
                if existing and existing.get("InvoiceID"):
                    inv_id = existing.get("InvoiceID")
                    inv_num = existing.get("InvoiceNumber") or issue.doc_number
                    issue.xero_invoice_id = inv_id
                    issue.xero_invoice_number = inv_num
                    issue.xero_status = "posted"
                    issue.xero_posted_at = datetime.utcnow()
                    issue.xero_error = None
                    db.session.commit()
                    try:
                        attach_voucher_to_xero("Invoices", inv_id, "green_issue", issue.id, issue.doc_number)
                    except Exception:
                        pass
                    return {"success": True, "invoice_id": inv_id, "invoice_number": inv_num, "type": "invoice"}

                issue.xero_status = "failed"
                issue.xero_error = _parse_xero_error(resp)
                db.session.commit()
                return {"success": False, "error": issue.xero_error}
        except Exception as e:
            issue.xero_status = "failed"
            issue.xero_error = str(e)[:300]
            db.session.commit()
            return {"success": False, "error": issue.xero_error}

    elif issue.issue_type in ("sample", "internal_use"):
        payload = build_green_issue_credit_note_payload(issue)
        total_amount = round(sum(item["LineAmount"] for item in payload["LineItems"]), 2)

        try:
            resp = _post_credit_note_resilient(payload, timeout=25)

            if resp.status_code in (200, 201):
                data = resp.json()
                cn = data.get("CreditNotes", [{}])[0]
                cn_id = cn.get("CreditNoteID")
                cn_num = cn.get("CreditNoteNumber")
                actual_total = float(cn.get("Total") or total_amount)

                # سداد وإقفال الإشعار بـ Cash Refund على حساب عينات قهوة (كود 345)
                samples_acc = _get_setting("xero_samples_account", "345")
                if samples_acc and cn_id and actual_total > 0:
                    _apply_cash_refund(cn_id, actual_total, issue.issue_date, samples_acc)

                issue.xero_invoice_id = cn_id
                issue.xero_invoice_number = cn_num or issue.doc_number
                issue.xero_status = "posted"
                issue.xero_posted_at = datetime.utcnow()
                issue.xero_error = None
                db.session.commit()

                # إرفاق نسخة السند الرسمي (PDF / التوقيع) كمرفق مباشر في Xero
                att_res = attach_voucher_to_xero("CreditNotes", cn_id, "green_issue", issue.id, issue.doc_number)
                if att_res.get("success"):
                    try:
                        log_coffee_audit(
                            event_type="xero_attachment",
                            entity_type="green_issue",
                            entity_id=issue.id,
                            new_value={"filename": att_res.get("filename")},
                            notes=f"إرفاق نسخة السند الرسمي {att_res.get('filename')} بإشعار دائن زيرو {issue.xero_invoice_number}"
                        )
                        db.session.commit()
                    except Exception:
                        pass

                try:
                    type_label = "عينات فحص وتذوق" if issue.issue_type == "sample" else "استخدام داخلي تجارب"
                    log_coffee_audit(
                        event_type="post_xero",
                        entity_type="green_issue",
                        entity_id=issue.id,
                        new_value={"credit_note_id": cn_id, "credit_note_number": issue.xero_invoice_number},
                        notes=f"ترحيل سند صرف الأخضر ({type_label}) {issue.doc_number} كإشعار دائن مقفل بحساب عينات قهوة (345) لـ Xero"
                    )
                    db.session.commit()
                except Exception:
                    pass

                return {"success": True, "invoice_id": cn_id, "invoice_number": issue.xero_invoice_number, "type": "credit_note"}
            else:
                existing = _find_existing_xero_document("CreditNotes", issue.doc_number, expected_total=total_amount)
                if existing and existing.get("CreditNoteID"):
                    cn_id = existing.get("CreditNoteID")
                    cn_num = existing.get("CreditNoteNumber") or issue.doc_number
                    rem_credit = float(existing.get("RemainingCredit") or 0.0)
                    samples_acc = _get_setting("xero_samples_account", "345")
                    if samples_acc and rem_credit > 0:
                        _apply_cash_refund(cn_id, rem_credit, issue.issue_date, samples_acc)

                    issue.xero_invoice_id = cn_id
                    issue.xero_invoice_number = cn_num
                    issue.xero_status = "posted"
                    issue.xero_posted_at = datetime.utcnow()
                    issue.xero_error = None
                    db.session.commit()
                    try:
                        attach_voucher_to_xero("CreditNotes", cn_id, "green_issue", issue.id, issue.doc_number)
                    except Exception:
                        pass
                    return {"success": True, "invoice_id": cn_id, "invoice_number": cn_num, "type": "credit_note"}

                issue.xero_status = "failed"
                issue.xero_error = _parse_xero_error(resp)
                db.session.commit()
                return {"success": False, "error": issue.xero_error}
        except Exception as e:
            issue.xero_status = "failed"
            issue.xero_error = str(e)[:300]
            db.session.commit()
            return {"success": False, "error": issue.xero_error}

    else:
        return {"success": False, "error": f"نوع الصرف '{issue.issue_type_label_ar}' غير مربوط بترحيل Xero"}


def build_roasted_issue_sale_invoice_payload(dispatch) -> dict:
    """
    يبني payload فاتورة مبيعات مسودة (Draft ACCREC) لسند صرف البن المحمص لغرض البيع.
    المرجع Reference هو رقم السند doc_number (مثل RB 0001).
    الضريبة 15% (OUTPUT2 / VAT on Income).
    """
    contact_name = dispatch.customer_name or _get_setting("xero_roasted_contact", "عميل بيع بن محمص")
    sales_account = _get_setting("xero_roasted_sales_account", "202")
    if "سلة" in (dispatch.customer_name or "") or (dispatch.issue_reason and "سلة" in dispatch.issue_reason):
        sales_account = _get_setting("xero_salla_sales_account", "203")
    tracking_cat  = _get_setting("xero_tracking_category", "Branches")
    roasted_tv    = _get_setting("xero_roasted_tracking_val", "Roasted Coffee")
    tax_type      = _get_setting("xero_sales_tax_type", "OUTPUT")

    line_items = []
    for line in dispatch.all_lines:
        citem = line.coffee_item
        if not citem:
            continue
        uprice = float(line.sale_price_per_kg or (citem.sale_price_per_kg or 0.0))
        q = float(line.qty_kg or 0.0)
        lamount = round(q * uprice, 2)
        icode = (citem.xero_item_code or f"RB-{citem.base_code}").strip()
        line_items.append({
            "ItemCode": icode,
            "Description": f"بيع بن محمص - {citem.name_ar or icode}",
            "Quantity": q,
            "UnitAmount": uprice,
            "LineAmount": lamount,
            "AccountCode": sales_account,
            "TaxType": tax_type,
            "Tracking": [{
                "Name": tracking_cat,
                "Option": roasted_tv
            }]
        })

    if not line_items and dispatch.coffee_item:
        unit_price = float(dispatch.sale_price_per_kg or (dispatch.coffee_item.sale_price_per_kg or 0.0))
        qty        = float(dispatch.qty_kg or 0.0)
        line_amount = round(qty * unit_price, 2)
        item_code = (dispatch.coffee_item.xero_item_code or f"RB-{dispatch.coffee_item.base_code}").strip()
        line_items.append({
            "ItemCode": item_code,
            "Description": f"بيع بن محمص - {dispatch.coffee_item.name_ar or item_code}",
            "Quantity": qty,
            "UnitAmount": unit_price,
            "LineAmount": line_amount,
            "AccountCode": sales_account,
            "TaxType": tax_type,
            "Tracking": [{
                "Name": tracking_cat,
                "Option": roasted_tv
            }]
        })

    # إضافة بند مصاريف الشحن والتوصيل (حساب 261) في حالة السندات المشحونة
    if getattr(dispatch, "delivery_method", None) == "shipping":
        from models_coffee import calculate_shipping_fee
        ship_fee = float(getattr(dispatch, "shipping_fee", 0.0) or 0.0)
        if ship_fee <= 0:
            ship_fee = calculate_shipping_fee(dispatch.total_qty_kg, dispatch.shipping_company)
        if ship_fee > 0:
            ship_account = _get_setting("xero_shipping_account", "261")
            ship_desc = _get_setting("xero_shipping_description", "ايرادات شحن وتوصيل")
            ship_line = {
                "Description": ship_desc,
                "Quantity": 1.0,
                "UnitAmount": round(ship_fee, 2),
                "LineAmount": round(ship_fee, 2),
                "AccountCode": ship_account,
                "TaxType": tax_type,
            }
            ship_item_code = _get_setting("xero_shipping_item_code", "").strip()
            if ship_item_code and ship_item_code != "مصاريف شحن":
                ship_line["ItemCode"] = ship_item_code
            if tracking_cat and roasted_tv:
                ship_line["Tracking"] = [{
                    "Name": tracking_cat,
                    "Option": roasted_tv
                }]
            line_items.append(ship_line)

    return {
        "Type": "ACCREC",
        "Contact": {"Name": contact_name.strip()},
        "Date": _fmt_date(dispatch.dispatch_date),
        "DueDate": _fmt_date(dispatch.dispatch_date),
        "Reference": dispatch.doc_number,
        "Status": "DRAFT",
        "LineAmountTypes": "Exclusive",
        "CurrencyCode": "SAR",
        "LineItems": line_items
    }


def build_roasted_dispatch_branch_credit_note_payload(dispatch) -> dict:
    """
    يبني payload إشعار دائن مشتريات (Payable Credit Note - ACCPAYCREDIT)
    لسند صرف البن المحمص لفرع (سلسلة RB).
    الجهة Contact Name: سند صرف قهوه محموصه {اسم الفرع}
    المرجع Reference: رقم السند (مثل RB 0001 أو MP 3584)
    حساب البنود AccountCode: مخزون البن المحمص 636
    الضريبة TaxType: NONE (معفى Tax Exempt)
    التتبع Tracking: Branches / اسم الفرع
    """
    branch_name = ""
    if dispatch.branch_location:
        branch_name = dispatch.branch_location.name_ar or dispatch.branch_location.name_en or ""
    elif dispatch.branch:
        branch_name = dispatch.branch.name_ar or dispatch.branch.name_en or ""

    contact_name = f"سند صرف قهوه محموصه {branch_name}".strip() if branch_name else _get_setting("xero_dispatch_contact_name", "سند صرف قهوة محموصة للفرع")
    roasted_account = _get_setting("xero_roasted_account", "636")
    tracking_cat = _get_setting("xero_tracking_category", "Branches")
    roasted_tv   = _get_setting("xero_roasted_tracking_val", "Roasted Coffee")

    line_items = []
    for line in dispatch.all_lines:
        citem = line.coffee_item
        if not citem:
            continue
        q = round(float(line.qty_kg or 0.0), 4)
        # اعتماد سعر تكلفة الصنف على Xero
        ucost = citem.cost_price if (citem and citem.cost_price > 0) else (round(float(line.unit_cost or 0.0), 4))
        total = round(q * ucost, 2)
        icode = (citem.xero_item_code or f"RB-{citem.base_code}").strip()
        desc = citem.name_ar or citem.name_en or icode
        if hasattr(line, "package_type") and line.package_type and line.package_type != "bulk":
            if line.package_type == "250g":
                pkg_count = int(line.package_qty) if (line.package_qty and float(line.package_qty).is_integer()) else (line.package_qty or 0)
                desc = f"{desc} ({pkg_count} كيس ربع كيلو 250g)"
            elif line.package_type == "1kg":
                pkg_count = int(line.package_qty) if (line.package_qty and float(line.package_qty).is_integer()) else (line.package_qty or 0)
                desc = f"{desc} ({pkg_count} كيس 1 كجم)"
            else:
                pkg_count = int(line.package_qty) if (line.package_qty and float(line.package_qty).is_integer()) else (line.package_qty or 0)
                desc = f"{desc} ({pkg_count} عبوة {line.package_type})"
        tracking_list = [{"Name": tracking_cat, "Option": roasted_tv}] if (tracking_cat and roasted_tv) else []

        line_items.append({
            "ItemCode": icode,
            "Description": desc,
            "Quantity": q,
            "UnitAmount": ucost,
            "LineAmount": total,
            "AccountCode": roasted_account,
            "TaxType": "NONE",
            "Tracking": tracking_list
        })

    if not line_items and dispatch.coffee_item:
        q = round(float(dispatch.qty_kg or 0.0), 4)
        citem = dispatch.coffee_item
        ucost = citem.cost_price if (citem and citem.cost_price > 0) else (round(float(dispatch.unit_cost or 0.0), 4))
        total = round(q * ucost, 2)
        icode = (dispatch.coffee_item.xero_item_code or f"RB-{dispatch.coffee_item.base_code}").strip()
        desc = dispatch.coffee_item.name_ar or dispatch.coffee_item.name_en or icode
        tracking_list = [{"Name": tracking_cat, "Option": roasted_tv}] if (tracking_cat and roasted_tv) else []

        line_items.append({
            "ItemCode": icode,
            "Description": desc,
            "Quantity": q,
            "UnitAmount": ucost,
            "LineAmount": total,
            "AccountCode": roasted_account,
            "TaxType": "NONE",
            "Tracking": tracking_list
        })

    return {
        "Type": "ACCPAYCREDIT",
        "Contact": {"Name": contact_name},
        "Date": _fmt_date(dispatch.dispatch_date),
        "CreditNoteNumber": dispatch.doc_number,
        "Reference": dispatch.doc_number,
        "Status": "AUTHORISED",
        "LineAmountTypes": "Exclusive",
        "CurrencyCode": "SAR",
        "LineItems": line_items
    }


def build_roasted_issue_credit_note_payload(dispatch) -> dict:
    """
    يبني payload إشعار دائن مشتريات (Payable Credit Note - ACCPAYCREDIT)
    لسند صرف البن المحمص لغرض عينات فحص وتذوق (sample) أو استخدام داخلي تجارب (internal_use).
    - الجهة Contact Name: بنفس الاسم ('عينات فحص وتذوق' أو 'استخدام داخلي تجارب').
    - المرجع Reference: رقم السند (مثل RB 0001 أو MP 3584).
    - كود الإشعار CreditNoteNumber: رقم السند (مثل RB 0001 أو MP 3584).
    - الحساب AccountCode: مخزون البن المحمص 636.
    - الضريبة TaxType: NONE.
    - التتبع Tracking: Branches / Roasted Coffee.
    """
    if dispatch.dispatch_type == "sample":
        contact_name = _get_setting("xero_roasted_sample_contact") or _get_setting("xero_green_sample_contact", "عينات فحص وتذوق")
    elif dispatch.dispatch_type == "internal_use":
        contact_name = _get_setting("xero_roasted_internal_contact") or _get_setting("xero_green_internal_contact", "استخدام داخلي تجارب")
    else:
        contact_name = dispatch.issue_reason or "سند صرف بن محمص"

    roasted_account = _get_setting("xero_roasted_account", "636")
    tracking_cat   = _get_setting("xero_tracking_category", "Branches")
    roasted_tv     = _get_setting("xero_roasted_tracking_val", "Roasted Coffee")

    line_items = []
    for line in dispatch.all_lines:
        citem = line.coffee_item
        if not citem:
            continue
        q = round(float(line.qty_kg or 0.0), 4)
        # اعتماد سعر تكلفة الصنف على Xero
        ucost = citem.cost_price if (citem and citem.cost_price > 0) else (round(float(line.unit_cost or 0.0), 4))
        total = round(q * ucost, 2)
        icode = (citem.xero_item_code or f"RB-{citem.base_code}").strip()
        desc = f"{line.notes} - {citem.name_ar}" if line.notes else (citem.name_ar or citem.name_en or icode)
        tracking_list = [{"Name": tracking_cat, "Option": roasted_tv}] if (tracking_cat and roasted_tv) else []

        line_items.append({
            "ItemCode": icode,
            "Description": desc,
            "Quantity": q,
            "UnitAmount": ucost,
            "LineAmount": total,
            "AccountCode": roasted_account,
            "TaxType": "NONE",
            "Tracking": tracking_list
        })

    if not line_items and dispatch.coffee_item:
        q = round(float(dispatch.qty_kg or 0.0), 4)
        citem = dispatch.coffee_item
        ucost = citem.cost_price if (citem and citem.cost_price > 0) else (round(float(dispatch.unit_cost or 0.0), 4))
        total = round(q * ucost, 2)
        icode = (dispatch.coffee_item.xero_item_code or f"RB-{dispatch.coffee_item.base_code}").strip()
        desc = dispatch.coffee_item.name_ar or dispatch.coffee_item.name_en or icode
        tracking_list = [{"Name": tracking_cat, "Option": roasted_tv}] if (tracking_cat and roasted_tv) else []

        line_items.append({
            "ItemCode": icode,
            "Description": desc,
            "Quantity": q,
            "UnitAmount": ucost,
            "LineAmount": total,
            "AccountCode": roasted_account,
            "TaxType": "NONE",
            "Tracking": tracking_list
        })

    return {
        "Type": "ACCPAYCREDIT",
        "Contact": {"Name": contact_name},
        "Date": _fmt_date(dispatch.dispatch_date),
        "CreditNoteNumber": dispatch.doc_number,
        "Reference": dispatch.doc_number,
        "Status": "AUTHORISED",
        "LineAmountTypes": "Exclusive",
        "CurrencyCode": "SAR",
        "LineItems": line_items
    }


def post_roasted_dispatch_to_xero(dispatch_id: int) -> dict:
    """
    يرحل سند صرف البن المحمص لـ Xero:
    1) إذا كان نوع الصرف بيع ('sale'): يرحل كفاتورة مبيعات مسودة (Draft ACCREC) مع 15% ضريبة بمرجع RB.
    2) إذا كان نوع الصرف لفرع ('branch'): يرحل كإشعار دائن (ACCPAYCREDIT) باسم 'سند صرف قهوه محموصه {الفرع}'
       ويسدد بحساب مخزون فودكس (كود 631) بـ Cash Refund ليتحول لـ Paid والرصيد المتبقي 0.00.
    3) إذا كان نوع الصرف عينات وتذوق ('sample') أو استخدام داخلي وتجارب ('internal_use'):
       يرحل كإشعار دائن (ACCPAYCREDIT) بنفس الاسم، ويُقفل بحساب عينات قهوة (كود 345) بـ Cash Refund ليكون Paid،
       مع إرفاق نسخة السند الرسمي (PDF المعتمد) ورقم السند في Reference للاثنين.
    """
    from models import db
    from models_coffee import RoastedCoffeeDispatch, log_coffee_audit

    dispatch = RoastedCoffeeDispatch.query.get(dispatch_id)
    if not dispatch:
        return {"success": False, "error": f"سند الصرف #{dispatch_id} غير موجود"}

    if dispatch.status != "approved":
        return {"success": False, "error": "لا يمكن ترحيل السند إلى Xero لأنه مسودة — يجب اعتماد السند أولاً"}

    token = _get_setting("xero_bearer_token")
    if not token and _get_setting("xero_refresh_token"):
        refresh_xero_token()
        token = _get_setting("xero_bearer_token")
    if not token:
        dispatch.xero_status = "failed"
        dispatch.xero_error = "رمز Xero Token غير مضبوط في الإعدادات"
        db.session.commit()
        return {"success": False, "error": dispatch.xero_error}

    if dispatch.dispatch_type == "sale":
        payload = build_roasted_issue_sale_invoice_payload(dispatch)
        try:
            resp = requests.post(
                f"{XERO_BASE}/Invoices",
                json={"Invoices": [payload]},
                headers=_xero_headers(),
                timeout=25
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                inv = data.get("Invoices", [{}])[0]
                dispatch.xero_invoice_id = inv.get("InvoiceID")
                dispatch.xero_invoice_number = inv.get("InvoiceNumber")
                dispatch.xero_status = "posted"
                dispatch.xero_posted_at = datetime.utcnow()
                dispatch.xero_error = None
                db.session.commit()

                # إرفاق نسخة السند الرسمي (PDF / التوقيع) كمرفق مباشر في Xero
                att_res = attach_voucher_to_xero("Invoices", dispatch.xero_invoice_id, "roasted_dispatch", dispatch.id, dispatch.doc_number)
                if att_res.get("success"):
                    try:
                        log_coffee_audit(
                            event_type="xero_attachment",
                            entity_type="dispatch",
                            entity_id=dispatch.id,
                            new_value={"filename": att_res.get("filename")},
                            notes=f"إرفاق نسخة السند الرسمي {att_res.get('filename')} بفاتورة مبيعات زيرو {dispatch.xero_invoice_number}"
                        )
                        db.session.commit()
                    except Exception:
                        pass

                try:
                    log_coffee_audit(
                        event_type="post_xero",
                        entity_type="dispatch",
                        entity_id=dispatch.id,
                        new_value={"invoice_id": dispatch.xero_invoice_id, "invoice_number": dispatch.xero_invoice_number},
                        notes=f"ترحيل سند صرف المحمص للبيع {dispatch.doc_number} كفاتورة مبيعات مسودة لـ Xero"
                    )
                    db.session.commit()
                except Exception:
                    pass

                return {"success": True, "invoice_id": dispatch.xero_invoice_id, "invoice_number": dispatch.xero_invoice_number, "type": "invoice"}
            else:
                sale_total = round(sum(item.get("LineAmount", 0.0) for item in payload.get("LineItems", [])), 2)
                existing = _find_existing_xero_document("Invoices", dispatch.doc_number, expected_total=sale_total)
                if existing and existing.get("InvoiceID"):
                    inv_id = existing.get("InvoiceID")
                    inv_num = existing.get("InvoiceNumber") or dispatch.doc_number
                    dispatch.xero_invoice_id = inv_id
                    dispatch.xero_invoice_number = inv_num
                    dispatch.xero_status = "posted"
                    dispatch.xero_posted_at = datetime.utcnow()
                    dispatch.xero_error = None
                    db.session.commit()
                    try:
                        attach_voucher_to_xero("Invoices", inv_id, "roasted_dispatch", dispatch.id, dispatch.doc_number)
                    except Exception:
                        pass
                    return {"success": True, "invoice_id": inv_id, "invoice_number": inv_num, "type": "invoice"}

                dispatch.xero_status = "failed"
                dispatch.xero_error = _parse_xero_error(resp)
                db.session.commit()
                return {"success": False, "error": dispatch.xero_error}
        except Exception as e:
            dispatch.xero_status = "failed"
            dispatch.xero_error = str(e)[:300]
            db.session.commit()
            return {"success": False, "error": dispatch.xero_error}

    elif dispatch.dispatch_type == "branch":
        payload = build_roasted_dispatch_branch_credit_note_payload(dispatch)
        total_amount = round(sum(item["LineAmount"] for item in payload["LineItems"]), 2)

        try:
            resp = _post_credit_note_resilient(payload, timeout=25)

            if resp.status_code in (200, 201):
                data = resp.json()
                cn = data.get("CreditNotes", [{}])[0]
                cn_id = cn.get("CreditNoteID")
                cn_num = cn.get("CreditNoteNumber")
                actual_total = float(cn.get("Total") or total_amount)

                # سداد الإشعار بـ Cash Refund على حساب مخزون فودكس (كود 631)
                foodics_acc = _get_setting("xero_foodics_inventory_account", "631")
                if foodics_acc and cn_id and actual_total > 0:
                    _apply_cash_refund(cn_id, actual_total, dispatch.dispatch_date, foodics_acc)

                dispatch.xero_invoice_id = cn_id
                dispatch.xero_invoice_number = cn_num or dispatch.doc_number
                dispatch.xero_status = "posted"
                dispatch.xero_posted_at = datetime.utcnow()
                dispatch.xero_error = None
                db.session.commit()

                # إرفاق نسخة السند الرسمي (PDF / التوقيع) كمرفق مباشر في Xero
                att_res = attach_voucher_to_xero("CreditNotes", cn_id, "roasted_dispatch", dispatch.id, dispatch.doc_number)
                if att_res.get("success"):
                    try:
                        log_coffee_audit(
                            event_type="xero_attachment",
                            entity_type="dispatch",
                            entity_id=dispatch.id,
                            new_value={"filename": att_res.get("filename")},
                            notes=f"إرفاق نسخة السند الرسمي {att_res.get('filename')} بإشعار دائن زيرو {dispatch.xero_invoice_number}"
                        )
                        db.session.commit()
                    except Exception:
                        pass

                try:
                    log_coffee_audit(
                        event_type="post_xero",
                        entity_type="dispatch",
                        entity_id=dispatch.id,
                        new_value={"credit_note_id": cn_id, "credit_note_number": cn_num},
                        notes=f"ترحيل سند صرف المحمص للفرع {dispatch.doc_number} كإشعار دائن لـ Xero وسداده بحساب مخزون فودكس {foodics_acc}"
                    )
                    db.session.commit()
                except Exception:
                    pass

                return {"success": True, "invoice_id": cn_id, "credit_note_id": cn_id, "invoice_number": cn_num, "type": "credit_note"}
            else:
                existing = _find_existing_xero_document("CreditNotes", dispatch.doc_number, expected_total=total_amount)
                if existing and existing.get("CreditNoteID"):
                    cn_id = existing.get("CreditNoteID")
                    cn_num = existing.get("CreditNoteNumber") or dispatch.doc_number
                    rem_credit = float(existing.get("RemainingCredit") or 0.0)
                    foodics_acc = _get_setting("xero_foodics_inventory_account", "631")
                    if foodics_acc and rem_credit > 0:
                        _apply_cash_refund(cn_id, rem_credit, dispatch.dispatch_date, foodics_acc)

                    dispatch.xero_invoice_id = cn_id
                    dispatch.xero_invoice_number = cn_num
                    dispatch.xero_status = "posted"
                    dispatch.xero_posted_at = datetime.utcnow()
                    dispatch.xero_error = None
                    db.session.commit()

                    try:
                        attach_voucher_to_xero("CreditNotes", cn_id, "roasted_dispatch", dispatch.id, dispatch.doc_number)
                    except Exception:
                        pass

                    try:
                        log_coffee_audit(
                            event_type="post_xero",
                            entity_type="dispatch",
                            entity_id=dispatch.id,
                            new_value={"credit_note_id": cn_id, "credit_note_number": cn_num},
                            notes=f"ربط سند صرف المحمص للفرع {dispatch.doc_number} بإشعار دائن موجود مسبقاً في Xero ({cn_num})"
                        )
                        db.session.commit()
                    except Exception:
                        pass

                    return {"success": True, "invoice_id": cn_id, "credit_note_id": cn_id, "invoice_number": cn_num, "type": "credit_note"}

                dispatch.xero_status = "failed"
                dispatch.xero_error = _parse_xero_error(resp)
                db.session.commit()
                return {"success": False, "error": dispatch.xero_error}
        except Exception as e:
            dispatch.xero_status = "failed"
            dispatch.xero_error = str(e)[:300]
            db.session.commit()
            return {"success": False, "error": dispatch.xero_error}

    elif dispatch.dispatch_type in ("sample", "internal_use"):
        payload = build_roasted_issue_credit_note_payload(dispatch)
        total_amount = round(sum(item["LineAmount"] for item in payload["LineItems"]), 2)

        try:
            resp = _post_credit_note_resilient(payload, timeout=25)

            if resp.status_code in (200, 201):
                data = resp.json()
                cn = data.get("CreditNotes", [{}])[0]
                cn_id = cn.get("CreditNoteID")
                cn_num = cn.get("CreditNoteNumber") or dispatch.doc_number
                actual_total = float(cn.get("Total") or total_amount)

                # سداد وإقفال الإشعار بـ Cash Refund على حساب عينات قهوة (كود 345)
                samples_acc = _get_setting("xero_samples_account", "345")
                if samples_acc and cn_id and actual_total > 0:
                    _apply_cash_refund(cn_id, actual_total, dispatch.dispatch_date, samples_acc)

                dispatch.xero_invoice_id = cn_id
                dispatch.xero_invoice_number = cn_num
                dispatch.xero_status = "posted"
                dispatch.xero_posted_at = datetime.utcnow()
                dispatch.xero_error = None
                db.session.commit()

                # إرفاق نسخة السند الرسمي (PDF المعتمد مع التوقيع) تلقائياً في زيرو
                att_res = attach_voucher_to_xero("CreditNotes", cn_id, "roasted_dispatch", dispatch.id, dispatch.doc_number)
                if att_res.get("success"):
                    try:
                        log_coffee_audit(
                            event_type="xero_attachment",
                            entity_type="dispatch",
                            entity_id=dispatch.id,
                            new_value={"filename": att_res.get("filename")},
                            notes=f"إرفاق نسخة السند الرسمي {att_res.get('filename')} بإشعار دائن زيرو {dispatch.xero_invoice_number}"
                        )
                        db.session.commit()
                    except Exception:
                        pass

                try:
                    type_label = "عينات فحص وتذوق" if dispatch.dispatch_type == "sample" else "استخدام داخلي تجارب"
                    log_coffee_audit(
                        event_type="post_xero",
                        entity_type="dispatch",
                        entity_id=dispatch.id,
                        new_value={"credit_note_id": cn_id, "credit_note_number": cn_num},
                        notes=f"ترحيل سند صرف المحمص ({type_label}) {dispatch.doc_number} كإشعار دائن مقفل بحساب عينات قهوة (345) لـ Xero"
                    )
                    db.session.commit()
                except Exception:
                    pass

                return {"success": True, "invoice_id": cn_id, "credit_note_id": cn_id, "invoice_number": cn_num, "type": "credit_note_sample"}
            else:
                existing = _find_existing_xero_document("CreditNotes", dispatch.doc_number, expected_total=total_amount)
                if existing and existing.get("CreditNoteID"):
                    cn_id = existing.get("CreditNoteID")
                    cn_num = existing.get("CreditNoteNumber") or dispatch.doc_number
                    rem_credit = float(existing.get("RemainingCredit") or 0.0)
                    samples_acc = _get_setting("xero_samples_account", "345")
                    if samples_acc and rem_credit > 0:
                        _apply_cash_refund(cn_id, rem_credit, dispatch.dispatch_date, samples_acc)

                    dispatch.xero_invoice_id = cn_id
                    dispatch.xero_invoice_number = cn_num
                    dispatch.xero_status = "posted"
                    dispatch.xero_posted_at = datetime.utcnow()
                    dispatch.xero_error = None
                    db.session.commit()

                    try:
                        attach_voucher_to_xero("CreditNotes", cn_id, "roasted_dispatch", dispatch.id, dispatch.doc_number)
                    except Exception:
                        pass

                    return {"success": True, "invoice_id": cn_id, "credit_note_id": cn_id, "invoice_number": cn_num, "type": "credit_note_sample"}

                dispatch.xero_status = "failed"
                dispatch.xero_error = _parse_xero_error(resp)
                db.session.commit()
                return {"success": False, "error": dispatch.xero_error}
        except Exception as e:
            dispatch.xero_status = "failed"
            dispatch.xero_error = str(e)[:300]
            db.session.commit()
            return {"success": False, "error": dispatch.xero_error}

    else:
        return {"success": False, "error": f"نوع الصرف '{dispatch.dispatch_type}' غير مدعوم للترحيل إلى Xero (المدعوم: بيع، صرف لفرع، عينات، أو استخدام داخلي تجارب)"}


def test_xero_connection() -> dict:
    """
    يفحص الاتصال بـ Xero API عبر جلب بيانات المنشأة (Organisation).
    يعيد: {"success": bool, "org_name": str, "error": str}
    """
    token = _get_setting("xero_bearer_token")
    tenant_id = _get_setting("xero_tenant_id")
    if not token:
        return {"success": False, "error": "رمز الدخول (Bearer Token) غير مسجل في الإعدادات"}
    if not tenant_id:
        return {"success": False, "error": "معرف المنشأة (Tenant ID) غير مسجل في الإعدادات"}

    try:
        resp = requests.get(
            f"{XERO_BASE}/Organisation",
            headers=_xero_headers(),
            timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            orgs = data.get("Organisations", [])
            org_name = orgs[0].get("Name") if orgs else "منشأة غير مسماة"
            return {"success": True, "org_name": org_name}
        elif resp.status_code == 401:
            return {"success": False, "error": "رمز الدخول (Token) منتهي الصلاحية أو غير صحيح (HTTP 401 Unauthorized)"}
        elif resp.status_code == 403:
            return {"success": False, "error": "لا توجد صلاحية للوصول إلى المنشأة (HTTP 403 Forbidden) - تحقق من Tenant ID"}
        else:
            return {"success": False, "error": f"خطأ من Xero ({resp.status_code}): {resp.text[:200]}"}
    except Exception as e:
        return {"success": False, "error": f"تعذر الاتصال بـ Xero: {str(e)}"}


# ─────────────────────────────────────────────────────────────────────────────
# مزامنة واكتشاف أصناف القهوة من Xero
# ─────────────────────────────────────────────────────────────────────────────

def fetch_xero_items_diff(only_with_stock: bool = False) -> dict:
    """
    يجلب الأصناف من Xero ويقارنها مع الأصناف المسجلة محلياً في جدول CoffeeItem و Item.
    يدعم تصفية الأصناف التي لها رصيد متوفر فقط (QuantityOnHand > 0).
    يعيد:
      {
        "success": bool,
        "total_xero_coffee_items": int,
        "existing_count": int,
        "new_count": int,
        "new_with_stock_count": int,
        "new_zero_stock_count": int,
        "only_with_stock": bool,
        "new_items": list of dicts,
        "error": Optional[str]
      }
    """
    from models import Item
    from models_coffee import CoffeeItem

    try:
        resp = _xero_request("GET", f"{XERO_BASE}/Items", timeout=25)
    except Exception as e:
        return {
            "success": False,
            "error": f"تعذر الاتصال بـ Xero: {str(e)}",
            "new_items": [],
            "new_count": 0,
            "new_with_stock_count": 0,
            "new_zero_stock_count": 0,
            "only_with_stock": only_with_stock,
            "total_xero_coffee_items": 0,
            "existing_count": 0
        }

    if resp.status_code != 200:
        err = _parse_xero_error(resp)
        return {
            "success": False,
            "error": f"فشل جلب الأصناف من Xero: {err}",
            "new_items": [],
            "new_count": 0,
            "new_with_stock_count": 0,
            "new_zero_stock_count": 0,
            "only_with_stock": only_with_stock,
            "total_xero_coffee_items": 0,
            "existing_count": 0
        }

    data = resp.json()
    items = data.get("Items", [])

    # تجميع الأكواد المسجلة مسبقاً محلياً للمقارنة السريعة
    local_codes = set()
    for ci in CoffeeItem.query.all():
        if ci.xero_item_code:
            local_codes.add(ci.xero_item_code.strip().upper())
        if ci.item and ci.item.sku:
            local_codes.add(ci.item.sku.strip().upper())
        if ci.prefix and ci.base_code:
            local_codes.add(f"{ci.prefix}{ci.base_code}".upper())
            local_codes.add(f"{ci.prefix} {ci.base_code}".upper())

    for itm in Item.query.all():
        if itm.sku:
            local_codes.add(itm.sku.strip().upper())

    new_items = []
    total_coffee = 0
    existing_count = 0
    existing_green_bases = {
        ci.base_code.strip().upper()
        for ci in CoffeeItem.query.filter_by(coffee_type="green").all()
        if ci.base_code
    }

    for it in items:
        code = (it.get("Code") or "").strip()
        if not code:
            continue

        name = (it.get("Name") or "").strip()
        desc = (it.get("Description") or "").strip()

        # استخراج البادئة والكود
        p, b = CoffeeItem.extract_prefix_and_base(code)
        is_coffee = (p in ("GB", "RB")) or any(w in code.lower() for w in ["gb", "rb"]) or any(w in name for w in ["بن", "قهو", "coffee"]) or any(w in desc for w in ["بن", "قهو", "coffee"])
        if not is_coffee:
            continue

        total_coffee += 1

        # فحص هل الصنف مسجل مسبقاً
        norm_code = code.upper()
        norm_key = f"{p}{b}".upper() if p and b else norm_code
        if norm_code in local_codes or norm_key in local_codes:
            existing_count += 1
            continue

        # تحديد النوع
        if p == "GB":
            c_type = "green"
        elif p == "RB":
            c_type = "roasted"
        elif any(w in name for w in ["محمص", "محموص", "roasted"]):
            c_type = "roasted"
            p = "RB"
        else:
            c_type = "green"
            p = "GB"

        # استخراج الاسم العربي النظيف
        has_ar_in_code = bool(re.search(r"[\u0600-\u06FF]", code))
        has_ar_in_name = bool(re.search(r"[\u0600-\u06FF]", name))
        has_ar_in_desc = bool(re.search(r"[\u0600-\u06FF]", desc))

        if has_ar_in_code and "-" in code:
            name_ar = code.split("-", 1)[1].strip()
        elif has_ar_in_name:
            parts = name.split("-", 1)
            name_ar = parts[1].strip() if len(parts) > 1 and re.search(r"[\u0600-\u06FF]", parts[1]) else name
        elif has_ar_in_desc:
            parts = desc.split("-", 1)
            name_ar = parts[1].strip() if len(parts) > 1 and re.search(r"[\u0600-\u06FF]", parts[1]) else desc
        else:
            parts = name.split("-", 1)
            name_ar = parts[1].strip() if len(parts) > 1 else name

        name_en = name or name_ar

        # قراءة الأسعار
        p_details = it.get("PurchaseDetails") or {}
        s_details = it.get("SalesDetails") or {}
        cost = float(p_details.get("UnitPrice") or 0.0)
        sale = float(s_details.get("UnitPrice") or 0.0)

        # قراءة الرصيد الفعلي في Xero وحالة تتبع المخزون
        qoh = float(it.get("QuantityOnHand") or 0.0)
        is_tracked = bool(it.get("IsTrackedAsInventory"))

        # استنتاج وزن الخيشة إن وجد في النص (مثل 60k أو 30k أو bags30)
        sack_weight = 60.0 if c_type == "green" else 0.0
        combined_text = f"{name} {desc} {code}".lower()
        if any(w in combined_text for w in ["bag30", "bags30", "30k", "30 كجم", "30ك"]):
            sack_weight = 30.0
        else:
            m_sack = re.search(r"(\d+)\s*(?:kg|k|كجم|كيلو)", name, re.IGNORECASE)
            if m_sack and c_type == "green":
                try:
                    sw = float(m_sack.group(1))
                    if 10.0 <= sw <= 100.0:
                        sack_weight = sw
                except ValueError:
                    pass

        has_green_pair = (c_type == "roasted") and bool(b and b.strip().upper() in existing_green_bases)

        new_items.append({
            "item_id": it.get("ItemID"),
            "code": code,
            "prefix": p or ("GB" if c_type == "green" else "RB"),
            "base_code": b or code,
            "coffee_type": c_type,
            "name_ar": name_ar,
            "name_en": name_en,
            "unit_cost": cost,
            "sale_price": sale,
            "quantity_on_hand": qoh,
            "is_tracked": is_tracked,
            "default_sack_weight": sack_weight,
            "has_green_pair": has_green_pair,
            "description": desc
        })

    new_with_stock = [it for it in new_items if it.get("quantity_on_hand", 0) > 0]
    new_zero_stock = [it for it in new_items if it.get("quantity_on_hand", 0) <= 0]
    returned_items = new_with_stock if only_with_stock else new_items

    return {
        "success": True,
        "total_xero_coffee_items": total_coffee,
        "existing_count": existing_count,
        "new_count": len(returned_items),
        "new_with_stock_count": len(new_with_stock),
        "new_zero_stock_count": len(new_zero_stock),
        "only_with_stock": only_with_stock,
        "new_items": returned_items
    }


def import_xero_items(selected_items: list, user_id=None) -> dict:
    """
    يستورد الأصناف المحددة من Xero، وينشئ لها سجلات Item و CoffeeItem،
    ويسجل الأرصدة الافتتاحية في مستودع البن الأخضر أو المحمص،
    ويربط آلياً بين صنف البن الأخضر والمحمص المقابل إن وجد.
    """
    from models import db, Item, Location, StockBalance, StockMovement
    from models_coffee import CoffeeItem, CoffeeItemLink, log_coffee_audit

    gw = Location.query.filter_by(name_ar="مستودع البن الأخضر").first()
    if not gw:
        gw = Location.query.filter(Location.name_ar.like("%أخضر%") | Location.name_ar.like("%اخضر%")).first()

    rw = Location.query.filter_by(name_ar="مستودع البن المحمص").first()
    if not rw:
        rw = Location.query.filter(Location.name_ar.like("%محمص%") | Location.name_ar.like("%محموص%")).first()

    added = []
    skipped = []
    linked = []

    for item_data in selected_items:
        code = (item_data.get("code") or "").strip()
        if not code:
            continue

        prefix = item_data.get("prefix") or ""
        base_code = item_data.get("base_code") or ""
        p_clean, b_clean = CoffeeItem.extract_prefix_and_base(code)
        if p_clean and not prefix:
            prefix = p_clean
        if b_clean:
            base_code = b_clean
        elif not base_code:
            base_code = code

        c_type = item_data.get("coffee_type") or item_data.get("category")
        if not c_type:
            c_type = "roasted" if prefix == "RB" else "green"
        if not prefix:
            prefix = "GB" if c_type == "green" else "RB"

        qoh = float(item_data.get("quantity_on_hand") or item_data.get("qoh") or 0.0)

        # التحقق من عدم التكرار
        existing = CoffeeItem.query.filter(
            (CoffeeItem.xero_item_code == code) |
            ((CoffeeItem.prefix == prefix) & (CoffeeItem.base_code == base_code))
        ).first()

        name_raw = (item_data.get("name") or "").strip()
        name_ar = (item_data.get("name_ar") or "").strip()
        if not name_ar:
            if "-" in code and any("\u0600" <= c <= "\u06ff" for c in code):
                name_ar = code.split("-", 1)[1].strip()
            elif "-" in name_raw and any("\u0600" <= c <= "\u06ff" for c in name_raw):
                name_ar = name_raw.split("-", 1)[1].strip()
            elif name_raw:
                name_ar = name_raw
            else:
                name_ar = code
        name_en = item_data.get("name_en") or name_raw or name_ar
        unit_cost = float(item_data.get("unit_cost") or 0.0)
        sale_price = float(item_data.get("sale_price") or 0.0)
        sack_weight = float(item_data.get("default_sack_weight") or 30.0) if c_type == "green" else 0.0

        if existing:
            # تحديث سعر التكلفة المعتمد إذا توفر
            if unit_cost > 0:
                existing.xero_cost_price = unit_cost
                if existing.item:
                    existing.item.cost = unit_cost
            if name_ar and not any("\u0600" <= c <= "\u06ff" for c in (existing.name_ar or "")):
                existing.name_ar = name_ar
                if existing.item:
                    existing.item.name_ar = name_ar
            # تحديث الرصيد المخزني إذا كان صفر أو غير مسجل
            target_loc = gw if c_type == "green" else rw
            if target_loc and qoh > 0:
                bal = StockBalance.query.filter_by(item_id=existing.item_id, location_id=target_loc.id).first()
                if not bal:
                    bal = StockBalance(item_id=existing.item_id, location_id=target_loc.id, quantity=qoh)
                    db.session.add(bal)
                    db.session.flush()
                    mov = StockMovement(
                        item_id=existing.item_id,
                        location_id=target_loc.id,
                        movement_type="initial",
                        reference_type="xero_sync",
                        quantity_change=qoh,
                        balance_after=qoh,
                        notes=f"رصيد افتتاحي مستورد من Xero ({code})",
                        created_by=user_id
                    )
                    db.session.add(mov)
                elif bal.quantity is None or bal.quantity == 0.0:
                    bal.quantity = qoh
                    mov = StockMovement(
                        item_id=existing.item_id,
                        location_id=target_loc.id,
                        movement_type="initial",
                        reference_type="xero_sync",
                        quantity_change=qoh,
                        balance_after=qoh,
                        notes=f"تحديث الرصيد الافتتاحي من Xero ({code})",
                        created_by=user_id
                    )
                    db.session.add(mov)

            db.session.add(existing)
            skipped.append(code)
            continue

        # إنشاء أو جلب Item الأساسي
        base_item = Item.query.filter_by(sku=code).first()
        if not base_item:
            base_item = Item(
                sku=code,
                name_ar=name_ar,
                name_en=name_en,
                cost=unit_cost,
                storage_unit="kg",
                ingredient_unit="kg",
                storage_to_ingredient_factor=1.0,
                sync_status="synced"
            )
            db.session.add(base_item)
            db.session.flush()

        ci = CoffeeItem(
            item_id=base_item.id,
            xero_item_code=code,
            prefix=prefix,
            base_code=base_code,
            coffee_type=c_type,
            name_ar=name_ar,
            name_en=name_en,
            sack_allowed=(c_type == "green" and sack_weight > 0),
            default_sack_weight=sack_weight,
            allowed_sack_weights=json.dumps([int(sack_weight), 60] if sack_weight > 0 else [30, 60]),
            sale_price_per_kg=sale_price,
            xero_cost_price=unit_cost,
            reference_loss_pct=18.0 if c_type == "green" else 0.0,
            data_source="xero_sync",
            active=True
        )
        db.session.add(ci)
        db.session.flush()
        added.append(code)

        # تسجيل الرصيد الافتتاحي في المستودع المناسب
        target_loc = gw if c_type == "green" else rw
        if target_loc and qoh > 0:
            bal = StockBalance.query.filter_by(item_id=base_item.id, location_id=target_loc.id).first()
            if not bal:
                bal = StockBalance(item_id=base_item.id, location_id=target_loc.id, quantity=qoh)
                db.session.add(bal)
                db.session.flush()
            else:
                bal.quantity = qoh

            mov = StockMovement(
                item_id=base_item.id,
                location_id=target_loc.id,
                movement_type="initial",
                reference_type="xero_sync",
                quantity_change=qoh,
                balance_after=qoh,
                notes=f"رصيد افتتاحي مستورد من Xero ({code})",
                created_by=user_id
            )
            db.session.add(mov)

        # محاولة الربط التلقائي بين الأخضر والمحمص إذا وُجد الصنف المقابل بنفس base_code
        if c_type == "green":
            opp_item = CoffeeItem.query.filter_by(prefix="RB", base_code=base_code).first()
            if opp_item:
                existing_link = CoffeeItemLink.query.filter_by(green_item_id=ci.id, roasted_item_id=opp_item.id).first()
                if not existing_link:
                    lnk = CoffeeItemLink(green_item_id=ci.id, roasted_item_id=opp_item.id, link_type="auto")
                    db.session.add(lnk)
                    linked.append(f"{code} ↔ {opp_item.xero_item_code}")
        elif c_type == "roasted":
            opp_item = CoffeeItem.query.filter_by(prefix="GB", base_code=base_code).first()
            if opp_item:
                existing_link = CoffeeItemLink.query.filter_by(green_item_id=opp_item.id, roasted_item_id=ci.id).first()
                if not existing_link:
                    lnk = CoffeeItemLink(green_item_id=opp_item.id, roasted_item_id=ci.id, link_type="auto")
                    db.session.add(lnk)
                    linked.append(f"{opp_item.xero_item_code} ↔ {code}")

    db.session.commit()

    if added:
        try:
            log_coffee_audit(
                event_type="xero_items_imported",
                entity_type="CoffeeItem",
                entity_id=0,
                notes=f"الأصناف المضافة: {', '.join(added[:10])}{' ...' if len(added) > 10 else ''} | الروابط المنشأة: {len(linked)}",
                user_id=user_id
            )
            db.session.commit()
        except Exception:
            pass

    return {
        "success": True,
        "imported_count": len(added),
        "skipped_count": len(skipped),
        "linked_count": len(linked),
        "imported_items": added,
        "linked_items": linked
    }





