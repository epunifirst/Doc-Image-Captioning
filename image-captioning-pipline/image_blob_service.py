import base64
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    generate_blob_sas,
)


_blob_service_client = None


def get_output_blob_service_client() -> BlobServiceClient:
    global _blob_service_client
    if _blob_service_client is None:
        _blob_service_client = BlobServiceClient.from_connection_string(
            os.environ["OUTPUT_STORAGE_CONNECTION_STRING"]
        )
    return _blob_service_client


def download_image(record: dict) -> tuple[bytes, str]:
    container = record["image_container"]
    blob_path = record["image_blob_path"]
    blob_client = get_output_blob_service_client().get_blob_client(
        container=container,
        blob=blob_path,
    )
    data = blob_client.download_blob().readall()
    content_type = record.get("image_content_type") or "image/png"
    max_bytes = int(os.environ.get("MAX_VISION_IMAGE_BYTES", str(20 * 1024 * 1024)))
    if len(data) > max_bytes:
        raise ValueError(
            f"Image {record['image_id']} exceeds MAX_VISION_IMAGE_BYTES."
        )
    return data, content_type


def image_data_url(record: dict) -> str:
    data, content_type = download_image(record)
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def _connection_string_parts() -> dict[str, str]:
    parts = {}
    for segment in os.environ["OUTPUT_STORAGE_CONNECTION_STRING"].split(";"):
        if "=" in segment:
            key, value = segment.split("=", 1)
            parts[key] = value
    return parts


def create_read_only_image_url(record: dict) -> str:
    parts = _connection_string_parts()
    account_name = parts.get("AccountName")
    account_key = parts.get("AccountKey")
    endpoint_suffix = parts.get("EndpointSuffix", "core.windows.net")
    if not account_name or not account_key:
        raise ValueError(
            "OUTPUT_STORAGE_CONNECTION_STRING must include AccountName and AccountKey to create demo SAS URLs."
        )

    lifetime_minutes = int(os.environ.get("IMAGE_SAS_LIFETIME_MINUTES", "15"))
    now = datetime.now(timezone.utc)
    token = generate_blob_sas(
        account_name=account_name,
        container_name=record["image_container"],
        blob_name=record["image_blob_path"],
        account_key=account_key,
        permission=BlobSasPermissions(read=True),
        start=now - timedelta(minutes=1),
        expiry=now + timedelta(minutes=lifetime_minutes),
    )
    encoded_path = quote(record["image_blob_path"], safe="/")
    return (
        f"https://{account_name}.blob.{endpoint_suffix}/"
        f"{record['image_container']}/{encoded_path}?{token}"
    )
