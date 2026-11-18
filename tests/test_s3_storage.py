"""Unit tests for s3_storage helper."""
from unittest.mock import MagicMock, patch
import io
import os
from werkzeug.datastructures import FileStorage
import s3_storage

def test_s3_storage_disabled_by_default(monkeypatch):
    monkeypatch.delenv("S3_ENDPOINT", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.delenv("S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("S3_SECRET_ACCESS_KEY", raising=False)
    assert not s3_storage.is_s3_configured()

def test_s3_storage_upload_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("S3_ENDPOINT", raising=False)
    fs = FileStorage(
        stream=io.BytesIO(b"sample invoice attachment"),
        filename="invoice.jpg",
        content_type="image/jpeg",
    )
    url = s3_storage.upload_to_s3_or_local(
        file_storage=fs,
        subfolder="purchases",
        upload_dir=str(tmp_path),
    )
    assert url.startswith("/uploads/purchases/")
    assert url.endswith(".jpg")

def test_s3_storage_upload_s3_success(tmp_path, monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT", "https://example.r2.cloudflarestorage.com")
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("S3_CDN_URL", "https://cdn.example.com")
    
    mock_client = MagicMock()
    with patch("s3_storage.get_s3_client", return_value=mock_client):
        fs = FileStorage(
            stream=io.BytesIO(b"sample invoice s3"),
            filename="receipt.png",
            content_type="image/png",
        )
        url = s3_storage.upload_to_s3_or_local(
            file_storage=fs,
            subfolder="purchases",
            upload_dir=str(tmp_path),
        )
        assert url.startswith("https://cdn.example.com/purchases/")
        assert url.endswith(".png")
        mock_client.upload_fileobj.assert_called_once()
