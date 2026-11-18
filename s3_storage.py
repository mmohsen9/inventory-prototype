import os
import uuid
import logging
from werkzeug.utils import secure_filename

logger = logging.getLogger(__name__)

S3_ENDPOINT = os.environ.get("S3_ENDPOINT")
S3_BUCKET = os.environ.get("S3_BUCKET")
S3_REGION = os.environ.get("S3_REGION", "auto")
S3_ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID")
S3_SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY")
S3_CDN_URL = os.environ.get("S3_CDN_URL", "https://storage-novmer-cofee.cranl.net").rstrip("/")


def is_s3_configured() -> bool:
    return bool(
        os.environ.get("S3_ENDPOINT")
        and os.environ.get("S3_BUCKET")
        and os.environ.get("S3_ACCESS_KEY_ID")
        and os.environ.get("S3_SECRET_ACCESS_KEY")
    )


def get_s3_client():
    if not is_s3_configured():
        return None
    try:
        import boto3
        from botocore.config import Config
        return boto3.client(
            "s3",
            endpoint_url=os.environ.get("S3_ENDPOINT"),
            aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY"),
            region_name=os.environ.get("S3_REGION", "auto"),
            config=Config(signature_version="s3v4")
        )
    except Exception as e:
        logger.warning(f"Failed to initialize S3 client: {e}")
        return None


def upload_to_s3_or_local(file_storage, subfolder: str, upload_dir: str) -> str:
    """
    يرفع الملف إلى سلة CranL S3 / Cloudflare R2 إذا كانت الإعدادات متوفرة،
    أو يحفظه محلياً في مجلد المرفقات كمسار احتياطي موثوق.
    """
    if not file_storage or not file_storage.filename:
        return None

    _, ext = os.path.splitext(file_storage.filename)
    ext = ext.lower()
    subfolder_parts = [secure_filename(p) for p in subfolder.replace("\\", "/").split("/") if p]
    clean_subfolder = "/".join(subfolder_parts)
    safe_name = f"{uuid.uuid4().hex}{ext}"
    s3_key = f"{clean_subfolder}/{safe_name}" if clean_subfolder else safe_name

    # محاولة الرفع السحابي أولاً
    client = get_s3_client()
    if client:
        try:
            content_type = "application/octet-stream"
            if ext in (".jpg", ".jpeg"):
                content_type = "image/jpeg"
            elif ext == ".png":
                content_type = "image/png"
            elif ext == ".pdf":
                content_type = "application/pdf"

            file_storage.seek(0)
            bucket = os.environ.get("S3_BUCKET")
            client.upload_fileobj(
                file_storage,
                bucket,
                s3_key,
                ExtraArgs={"ContentType": content_type}
            )
            file_storage.seek(0)
            cdn_base = (os.environ.get("S3_CDN_URL") or "https://storage-novmer-cofee.cranl.net").rstrip("/")
            logger.info(f"Successfully uploaded file to S3 bucket {bucket}: {s3_key}")
            return f"{cdn_base}/{s3_key}"
        except Exception as e:
            logger.error(f"S3 upload error, falling back to local disk: {e}")

    # التخزين المحلي كبديل
    folder = os.path.join(upload_dir, *subfolder_parts)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, safe_name)
    file_storage.seek(0)
    file_storage.save(path)
    return f"/uploads/{clean_subfolder}/{safe_name}"
