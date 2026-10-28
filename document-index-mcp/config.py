from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    mcp_api_key: str
    search_service_endpoint: str
    search_index_name: str
    search_api_key: str
    foundry_project_endpoint: str
    foundry_api_key: str
    foundry_embedding_deployment: str
    blob_account_name: str
    blob_account_url: str
    blob_account_key: str
    image_container: str
    source_document_account_name: str
    source_document_account_url: str
    source_document_account_key: str
    source_document_container: str
    max_returned_images: int

    @classmethod
    def from_environment(cls) -> Settings:
        try:
            max_returned_images = int(
                os.environ.get("MAX_RETURNED_IMAGES", "20")
            )
        except ValueError as exc:
            raise ValueError("MAX_RETURNED_IMAGES must be an integer.") from exc
        if not 1 <= max_returned_images <= 20:
            raise ValueError("MAX_RETURNED_IMAGES must be between 1 and 20.")

        return cls(
            mcp_api_key=os.environ.get("MCP_API_KEY", ""),
            search_service_endpoint=os.environ.get("SEARCH_SERVICE_ENDPOINT", ""),
            search_index_name=os.environ.get("SEARCH_INDEX_NAME", "image-captions"),
            search_api_key=os.environ.get("SEARCH_API_KEY", ""),
            foundry_project_endpoint=os.environ.get("FOUNDRY_PROJECT_ENDPOINT", ""),
            foundry_api_key=os.environ.get("FOUNDRY_API_KEY", ""),
            foundry_embedding_deployment=os.environ.get(
                "FOUNDRY_EMBEDDING_DEPLOYMENT", ""
            ),
            blob_account_name=os.environ.get("BLOB_ACCOUNT_NAME", ""),
            blob_account_url=os.environ.get("BLOB_ACCOUNT_URL", ""),
            blob_account_key=os.environ.get("BLOB_ACCOUNT_KEY", ""),
            image_container=os.environ.get("IMAGE_CONTAINER", "output-images"),
            source_document_account_name=os.environ.get(
                "SOURCE_DOCUMENT_ACCOUNT_NAME", ""
            ),
            source_document_account_url=os.environ.get(
                "SOURCE_DOCUMENT_ACCOUNT_URL", ""
            ),
            source_document_account_key=os.environ.get(
                "SOURCE_DOCUMENT_ACCOUNT_KEY", ""
            ),
            source_document_container=os.environ.get(
                "SOURCE_DOCUMENT_CONTAINER", "incoming"
            ),
            max_returned_images=max_returned_images,
        )

    def validate_azure_services(self) -> None:
        required = {
            "SEARCH_SERVICE_ENDPOINT": self.search_service_endpoint,
            "SEARCH_API_KEY": self.search_api_key,
            "FOUNDRY_PROJECT_ENDPOINT": self.foundry_project_endpoint,
            "FOUNDRY_API_KEY": self.foundry_api_key,
            "FOUNDRY_EMBEDDING_DEPLOYMENT": self.foundry_embedding_deployment,
            "BLOB_ACCOUNT_NAME": self.blob_account_name,
            "BLOB_ACCOUNT_URL": self.blob_account_url,
            "BLOB_ACCOUNT_KEY": self.blob_account_key,
            "SOURCE_DOCUMENT_ACCOUNT_NAME": self.source_document_account_name,
            "SOURCE_DOCUMENT_ACCOUNT_URL": self.source_document_account_url,
            "SOURCE_DOCUMENT_ACCOUNT_KEY": self.source_document_account_key,
            "SOURCE_DOCUMENT_CONTAINER": self.source_document_container,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ValueError(
                "Azure image retrieval is not configured. Set: "
                + ", ".join(missing)
            )
