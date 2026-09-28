"""
foodics_oauth.py
وحدة OAuth 2.0 Authorization Code Grant الخاصة بـ Foodics API
مبنية حرفياً على صفحة Authentication الرسمية (apidocs.foodics.com/core/authentication.html)

ملاحظة مهمة لحالتكم:
هذا التدفق مطلوب فقط إذا طلب منكم دعم فوديكس تسجيل تطبيق (App) رسمي بمتجر التطبيقات.
إذا حصلتم على Access Token مباشرة عبر "Personal/Private Integration" من الدعم،
يمكنكم تجاهل هذا الملف بالكامل والاكتفاء بحفظ التوكن مباشرة في SystemSetting (foodics_token).
"""
import requests
from datetime import datetime
from models import db, SystemSetting

FOODICS_OAUTH_TOKEN_URL_SUFFIX = "/oauth/token"
FOODICS_REVOKE_TOKEN_URL_SUFFIX = "/core/resources/revoke_tokens"


def get_authorization_url(client_id, redirect_uri, scopes=None, base_authorize_url="https://console.foodics.com/oauth/authorize"):
    """
    يبني رابط طلب التفويض الذي يُرسل إليه المستخدم (المالك) ليوافق على الصلاحيات.
    scopes: قائمة نصية مثل ["inventory.read", "inventory.write"] حسب صفحة Scopes الرسمية.
    """
    scope_str = " ".join(scopes) if scopes else ""
    return (
        f"{base_authorize_url}?response_type=code"
        f"&client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&scope={scope_str}"
    )


def exchange_code_for_token(base_url, code, client_id, client_secret, redirect_uri):
    """
    يستبدل authorization code (صالح لمدة 600 ثانية فقط) بـ Access Token فعلي.
    يجب استدعاؤها فوراً بعد استلام الكود من Foodics عبر الـ redirect.
    """
    base_url = base_url.rstrip("/")
    url = f"{base_url}{FOODICS_OAUTH_TOKEN_URL_SUFFIX}"

    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    try:
        res = requests.post(url, json=payload, headers=headers, timeout=15)
    except Exception as exc:
        return False, f"تعذر الاتصال بخادم فوديكس أثناء استبدال الكود: {exc}"

    if res.status_code != 200:
        try:
            err = res.json()
        except Exception:
            err = res.text[:200]
        return False, f"فشل استبدال الكود بتوكن (كود {res.status_code}): {err}"

    data = res.json()
    token_type = data.get("token_type")
    access_token = data.get("access_token")

    if token_type != "Bearer" or not access_token:
        return False, "استجابة غير متوقعة من فوديكس: لم يتم إرجاع access_token صالح"

    # لا يوجد refresh_token أو expires_in موثقين رسمياً في صفحة Authentication،
    # لذا نخزن التوكن فقط ونعتمد على مراقبة 401 في sync_queue.py لاكتشاف انتهائه لاحقاً.
    SystemSetting.set_val("foodics_token", access_token)
    SystemSetting.set_val("foodics_token_obtained_at", datetime.utcnow().isoformat())
    SystemSetting.set_val("foodics_sync_enabled", "1")
    SystemSetting.set_val("foodics_last_auth_error", "")

    return True, "تم الحصول على Access Token وحفظه بنجاح."


def revoke_current_token(base_url, token):
    """
    يلغي التوكن الحالي فوراً عبر endpoint الإلغاء الرسمي.
    استخدمها فوراً عند الشك بتسرّب التوكن أو عند إيقاف الربط نهائياً.
    """
    base_url = base_url.rstrip("/")
    url = f"{base_url}{FOODICS_REVOKE_TOKEN_URL_SUFFIX}"
    headers = {
        "Authorization": f"Bearer {token.strip()}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        res = requests.post(url, headers=headers, timeout=10)
    except Exception as exc:
        return False, f"تعذر الاتصال بخادم فوديكس أثناء الإلغاء: {exc}"

    if res.status_code in (200, 204):
        SystemSetting.set_val("foodics_sync_enabled", "0")
        SystemSetting.set_val("foodics_token", "")
        return True, "تم إلغاء التوكن بنجاح وإيقاف الربط."
    return False, f"فشل إلغاء التوكن (كود {res.status_code}): {res.text[:200]}"
</content>
