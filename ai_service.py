import os
import json
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

class InvoiceItem(BaseModel):
    item_name: str = Field(description="اسم الصنف كما يظهر في الفاتورة")
    quantity: float = Field(description="الكمية المستلمة")
    unit_price: float = Field(description="سعر الوحدة قبل الضريبة")
    total_price: float = Field(description="إجمالي سعر السطر")

class SupplierInvoiceData(BaseModel):
    supplier_name: str = Field(description="اسم المورد أو الشركة المصدرة للفاتورة")
    invoice_number: str = Field(description="رقم الفاتورة مثل INV-00054")
    invoice_date: str = Field(description="تاريخ الفاتورة بصيغة YYYY-MM-DD")
    total_amount: float = Field(description="إجمالي الفاتورة النهائي")
    items: list[InvoiceItem] = Field(description="قائمة الأصناف والكميات والأسعار في الفاتورة")

def extract_invoice_from_image(file_bytes: bytes, mime_type: str) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("متغير GEMINI_API_KEY غير موجود في إعدادات البيئة في CranL")

    client = genai.Client(api_key=api_key)
    prompt = (
        "استخرج بيانات الفاتورة الضريبية المرفقة بدقة بصيغة JSON: "
        "supplier_name (اسم المورد بالعربية إن وجد)، invoice_number (رقم الفاتورة)، "
        "invoice_date (التاريخ بصيغة YYYY-MM-DD)، total_amount (الإجمالي النهائي)، "
        "وقائمة items (الأصناف مع الكمية وسعر الوحدة والإجمالي)."
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash", # أسرع وأضمن نموذج لقراءة ملفات PDF والفواتير لحظياً
        contents=[
            types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
            prompt
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SupplierInvoiceData,
        )
    )
    return json.loads(response.text)
