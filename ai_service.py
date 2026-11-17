import os
import json
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

class InvoiceItem(BaseModel):
    item_name: str = Field(description="اسم الصنف كما يظهر في الفاتورة")
    quantity: float = Field(description="الكمية المستلمة")
    unit_price: float = Field(description="سعر الوحدة قبل الضريبة أو السعر الفردي")
    total_price: float = Field(description="إجمالي سعر السطر")

class SupplierInvoiceData(BaseModel):
    supplier_name: str = Field(description="اسم المورد أو الشركة المصدرة للفاتورة")
    invoice_number: str = Field(description="رقم الفاتورة مثل MP-00002")
    invoice_date: str = Field(description="تاريخ الفاتورة بصيغة YYYY-MM-DD")
    total_amount: float = Field(description="إجمالي الفاتورة النهائي")
    items: list[InvoiceItem] = Field(description="قائمة الأصناف والكميات والأسعار في الفاتورة")

def get_gemini_client():
    # يقرأ مفتاح GEMINI_API_KEY تلقائياً من Environment في CranL
    return genai.Client()

def extract_invoice_from_image(file_bytes: bytes, mime_type: str) -> dict:
    client = get_gemini_client()
    prompt = (
        "أنت محاسب خبير في نظام نوفمبر كفي. استخرج بيانات فاتورة المورد المرفقة بدقة: "
        "اسم المورد، رقم الفاتورة، تاريخ الفاتورة بصيغة YYYY-MM-DD، الإجمالي، "
        "وجميع سطور الأصناف مع الكمية وسعر الوحدة والإجمالي."
    )
    response = client.models.generate_content(
        model="gemini-4-hydrogen",
        contents=[
            types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
            prompt
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SupplierInvoiceData,
            thinking_config=types.ThinkingConfig(thinking_level="LOW")
        )
    )
    return json.loads(response.text)
