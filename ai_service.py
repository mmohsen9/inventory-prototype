import os
import json
import re
from datetime import datetime
from pydantic import BaseModel, Field
from google import genai
from google.genai import types


class InvoiceItem(BaseModel):
    item_code: str = Field(default="", description="كود الصنف أو الباركود أو SKU إن وجد")
    item_name: str = Field(description="اسم الصنف واضحاً (يفضل الاسم العربي إن توفر مع الإنجليزي)")
    unit: str = Field(default="", description="وحدة القياس مثل كجم، حبة، كرتون، Pcs, KG, Portion")
    quantity: float = Field(description="الكمية الفعلية المستلمة كرقم")
    unit_price: float = Field(description="سعر الوحدة الصافي قبل الضريبة")
    tax_amount: float = Field(default=0.0, description="قيمة الضريبة للسطر إن وجدت")
    total_price: float = Field(description="إجمالي السطر قبل الضريبة (الكمية × سعر الوحدة)")


class UniversalInvoiceData(BaseModel):
    document_type: str = Field(description="نوع المستند: فاتورة ضريبية، فاتورة مشتريات، أو إشعار دائن")
    supplier_name: str = Field(description="اسم المورد أو الشركة البائعة (الاسم التجاري العربي أو الإنجليزي)")
    supplier_vat_no: str = Field(default="", description="الرقم الضريبي للمورد إن وجد (15 رقماً)")
    branch_name: str = Field(default="", description="اسم الفرع المستلم أو عنوان التوصيل/المشروع إن ذكر في الفاتورة")
    invoice_number: str = Field(description="رقم الفاتورة المرجعي")
    invoice_date: str = Field(description="تاريخ الفاتورة بصيغة YYYY-MM-DD حصراً")
    subtotal_amount: float = Field(default=0.0, description="الإجمالي قبل الضريبة والخصم")
    discount_amount: float = Field(default=0.0, description="إجمالي الخصم إن وجد")
    vat_amount: float = Field(default=0.0, description="إجمالي ضريبة القيمة المضافة")
    total_amount: float = Field(description="الإجمالي النهائي المستحق شامل الضريبة")
    items: list[InvoiceItem] = Field(description="جميع سطور الأصناف الفعلية في الفاتورة")


def get_gemini_client():
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        raise ValueError("مفتاح GEMINI_API_KEY غير مضاف في إعدادات Environment في CranL")
    
    # يدعم تلقائياً مفاتيح Vertex (التي تبدأ بـ AQ.) ومفاتيح AI Studio (التي تبدأ بـ AIza)
    if api_key.startswith("AQ."):
        return genai.Client(vertexai=True, api_key=api_key)
    return genai.Client(api_key=api_key)


def normalize_date(date_str: str) -> str:
    """تحويل أي صيغة تاريخ مستخرجة إلى YYYY-MM-DD لتعمل مع حقل التاريخ في المتصفح."""
    if not date_str:
        return datetime.today().strftime("%Y-%m-%d")
    clean = date_str.strip().split(" ")[0].replace(".", "/").replace("-", "/")
    for fmt in ("%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(clean, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return date_str


def extract_invoice_from_image(file_bytes: bytes, mime_type: str) -> dict:
    client = get_gemini_client()

    universal_prompt = """
    أنت نظام قراءة بصري ومحاسبي خبير (Universal OCR & Accounting Parser) قادر على قراءة كافة أشكال الفواتير (صور، مسح ضوئي، PDF متعدد الصفحات، عربي أو إنجليزي).

    القواعد الذهبية للاستخراج الدقيق:
    1. التمييز بين البائع والمشتري:
       - المشتري/العميل هو دائماً: "نوفمبر كفي / November Cafe / شركة الكوب الأول الدولية" (رقمه الضريبي 310205775600003). لا تضع اسم المشتري أبداً في حقل المورد (supplier_name).
       - المورد (supplier_name) هو الجهة المصدرة للفاتورة (البائع)، ويفضل استخراج اسمه العربي الرسمي إن وجد في الترويسة أو التذييل أو الختم.
    2. الملفات متعددة الصفحات:
       - إذا تضمن الملف فاتورة ضريبية (VAT Invoice / Tax Invoice) ومعها إشعار دائن (Credit Note) أو سند توصيل، استخرج بيانات وأصناف الفاتورة الضريبية الأساسية، أو ادمج صفحات الفاتورة الواحدة إذا كانت ممتدة على أكثر من صفحة.
    3. الفرع المستلم (branch_name):
       - ابحث في الفاتورة عن أي إشارة للفرع أو المشروع أو موقع التسليم (مثل: الياسمين، التخصصي، حطين، الصحافة، النزهة، رفاء، الخبر، القصيم، المستودع الرئيسي) واكتبه في branch_name.
    4. التاريخ (invoice_date):
       - حول أي تاريخ (سواء كان DD/MM/YYYY أو YYYY/MM/DD) إلى الصيغة القياسية YYYY-MM-DD.
    5. جدول الأصناف (items):
       - استخرج كل صنف فعلي مع الكود، الاسم، الوحدة، الكمية، سعر الوحدة قبل الضريبة، وإجمالي السطر.
       - تجاهل السطور الفارغة أو رسوم الشحن والتوصيل الصفرية (0.00).
       - نظّف الأرقام من أي رموز عملات (SR, ريال, SAR) أو فواصل آلاف وحولها إلى أرقام عشرية دقيقة.
    """

    last_error = None
    for model_name in ("gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[
                    types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
                    universal_prompt
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=UniversalInvoiceData,
                    temperature=0.1,
                ),
            )
            if response.text:
                data = json.loads(response.text)
                data["invoice_date"] = normalize_date(data.get("invoice_date", ""))
                return data
        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(f"تعذر استخراج بيانات الفاتورة عبر نماذج الذكاء الاصطناعي: {last_error}")
