import os
from google import genai
from google.genai import types

def get_gemini_client():
    # يلتقط GEMINI_API_KEY تلقائياً من إعدادات Environment في CranL
    return genai.Client()

def analyze_with_gemini(prompt: str, system_role: str = "أنت مساعد ذكي لنظام إدارة المخزون والمحاسبة في نوفمبر كفي.") -> str:
    client = get_gemini_client()
    response = client.models.generate_content(
        model="gemini-4-hydrogen",
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_role,
            thinking_config=types.ThinkingConfig(thinking_level="HIGH")
        )
    )
    return response.text
