@echo off
cd /d "%~dp0"

:: التحقق من وجود أي تعديلات جديدة أو ملفات غير متتبعة
git status --porcelain | findstr . >nul
if %errorlevel% neq 0 (
    exit /b 0
)

:: اعتماد التعديلات، سحب أي تحديثات بعيدة، ثم الرفع إلى GitHub
git add .
git commit -m "chore: daily auto-sync %date% %time%"
git pull --rebase origin main
git push origin main