from __future__ import annotations

import hashlib
import logging
import mimetypes
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import AzureError
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from azure.storage.blob import BlobSasPermissions, BlobServiceClient, generate_blob_sas
from openai import OpenAI, OpenAIError

from config import Settings

logger = logging.getLogger(__name__)

VECTOR_DIMENSIONS = 1536
IMAGE_SAS_LIFETIME = timedelta(minutes=15)
SEARCH_SELECT_FIELDS = [
    "image_id",
    "image_storage_account",
    "image_container",
    "image_blob_path",
    "image_content_type",
    "source_type",
    "source_document_name",
    "source_document_storage_account",
    "source_document_container",
    "source_document_blob_path",
    "page_number",
    "figure_id",
    "section_path",
    "caption",
    "entities",
    "keywords",
    "related_text",
    "retrieval_text",
    "sibling_image_ids",
    "is_ignored",
]


@dataclass
class Candidate:
    record: dict[str, Any]
    group_ids: list[str] = field(default_factory=list)
    is_direct_match: bool = False
    search_rank: int | None = None
    search_score: float | None = None


class ImageRetrievalService:
    def __init__(
        self,
        settings: Settings,
        *,
        search_client: Any | None = None,
        embedding_client: Any | None = None,
        blob_service_client: Any | None = None,
        source_document_blob_service_client: Any | None = None,
    ) -> None:
        self.settings = settings
        self._search_client = search_client
        self._embedding_client = embedding_client
        self._blob_service_client = blob_service_client
        self._source_document_blob_service_client = (
            source_document_blob_service_client
        )

    def retrieve(
        self, question: str, max_images: int | None = None
    ) -> dict[str, Any]:
        cleaned_question = question.strip() if isinstance(question, str) else ""
        if not cleaned_question:
            raise ValueError("question must be a non-empty string.")
        if max_images is not None and (
            isinstance(max_images, bool)
            or not isinstance(max_images, int)
            or not 1 <= max_images <= 20
        ):
            raise ValueError("max_images must be an integer between 1 and 20.")
        if max_images is not None and max_images > self.settings.max_returned_images:
            raise ValueError(
                "max_images cannot exceed the configured MAX_RETURNED_IMAGES "
                f"limit of {self.settings.max_returned_images}."
            )

        self.settings.validate_azure_services()
        result_limit = max_images or self.settings.max_returned_images
        direct_matches, total_count = self._search(cleaned_question, result_limit)
        candidates, count_truncated = self._expand_groups(
            direct_matches, result_limit
        )
        truncated = count_truncated or (
            total_count is not None and total_count > len(direct_matches)
        )
        truncation_reasons: list[str] = []
        if count_truncated or (total_count is not None and total_count > len(direct_matches)):
            truncation_reasons.append("image_count_limit")

        image_items = self._create_candidate_links(candidates)
        source_documents, document_ids = self._create_source_document_links(
            candidates
        )

        metadata_results = []
        for candidate in image_items:
            source_document_id = document_ids.get(candidate["image_id"])
            metadata_results.append(
                {
                    "image_id": candidate["image_id"],
                    "relationship": (
                        "direct" if candidate["is_direct_match"] else "sibling"
                    ),
                    "group_ids": candidate["group_ids"],
                    "search_rank": candidate["search_rank"],
                    "search_score": candidate["search_score"],
                    "metadata": candidate["metadata"],
                    "source_document_id": source_document_id,
                    "content_type": candidate["mime_type"],
                    "blob_uri": candidate["blob_uri"],
                    "sas_url": candidate["sas_url"],
                    "image_url": candidate["sas_url"],
                    "markdown_image": (
                        f"![{_markdown_alt(candidate['metadata'].get('caption'))}]"
                        f"({candidate['sas_url']})"
                    ),
                }
            )

        return {
            "metadata": {
                "question": cleaned_question,
                "direct_match_count": len(direct_matches),
                "candidate_count": len(candidates),
                "returned_count": len(image_items),
                "max_images": result_limit,
                "truncated": truncated,
                "truncation_reasons": truncation_reasons,
                "source_documents": source_documents,
                "results": metadata_results,
            },
        }

    def _search(
        self, question: str, result_limit: int
    ) -> tuple[list[dict[str, Any]], int | None]:
        embedding = self._embedding_client_instance().embeddings.create(
            model=self.settings.foundry_embedding_deployment,
            input=question,
        )
        if not embedding.data or not embedding.data[0].embedding:
            raise ValueError("Embedding deployment returned an empty vector.")
        vector = embedding.data[0].embedding
        if len(vector) != VECTOR_DIMENSIONS:
            raise ValueError(
                "Embedding deployment returned "
                f"{len(vector)} dimensions; the image index requires "
                f"{VECTOR_DIMENSIONS}."
            )

        vector_query = VectorizedQuery(
            vector=vector,
            k_nearest_neighbors=result_limit,
            fields="caption_vector",
        )
        results = self._search_client_instance().search(
            search_text=question,
            vector_queries=[vector_query],
            filter="is_ignored eq false",
            select=SEARCH_SELECT_FIELDS,
            top=result_limit,
            include_total_count=True,
        )
        direct_matches = [
            dict(result) for result in results if not result.get("is_ignored")
        ]
        total_count = results.get_count()
        return direct_matches, total_count

    def _expand_groups(
        self, direct_matches: list[dict[str, Any]], result_limit: int
    ) -> tuple[list[Candidate], bool]:
        candidates: list[Candidate] = []
        by_image_id: dict[str, Candidate] = {}
        truncated = False

        for rank, record in enumerate(direct_matches, start=1):
            image_id = _required_string(record, "image_id")
            score = record.get("@search.score")
            candidate = by_image_id.get(image_id)
            if candidate is None:
                candidate = Candidate(record=record)
                by_image_id[image_id] = candidate
                candidates.append(candidate)
            candidate.is_direct_match = True
            candidate.search_rank = rank
            candidate.search_score = score
            _add_group(candidate, image_id)

            sibling_ids = record.get("sibling_image_ids") or []
            if not isinstance(sibling_ids, list):
                raise ValueError(
                    f"Image index record {image_id} has invalid sibling_image_ids."
                )
            for sibling_id in sibling_ids:
                if not isinstance(sibling_id, str) or not sibling_id:
                    continue
                sibling = by_image_id.get(sibling_id)
                if sibling is not None:
                    _add_group(sibling, image_id)
                    continue

                sibling_record = self._get_sibling(sibling_id)
                if sibling_record is None:
                    continue
                if len(candidates) >= result_limit:
                    truncated = True
                    break

                sibling = Candidate(
                    record=sibling_record,
                    search_rank=rank,
                    search_score=score,
                )
                _add_group(sibling, image_id)
                by_image_id[sibling_id] = sibling
                candidates.append(sibling)

            if truncated:
                break

        return candidates, truncated

    def _get_sibling(self, image_id: str) -> dict[str, Any] | None:
        try:
            record = dict(
                self._search_client_instance().get_document(
                    key=image_id,
                    selected_fields=SEARCH_SELECT_FIELDS,
                )
            )
        except AzureError as exc:
            if getattr(exc, "status_code", None) == 404:
                return None
            raise
        if record.get("image_id") != image_id:
            raise ValueError(
                f"Image index returned an unexpected record for {image_id}."
            )
        if record.get("is_ignored"):
            return None
        return record

    def _create_candidate_links(
        self, candidates: list[Candidate]
    ) -> list[dict[str, Any]]:
        linked: list[dict[str, Any]] = []

        for candidate in candidates:
            record = candidate.record
            image_id = _required_string(record, "image_id")
            account_name = _required_string(record, "image_storage_account")
            if account_name.casefold() != self.settings.blob_account_name.casefold():
                raise ValueError(
                    f"Image {image_id} refers to an unexpected storage account."
                )

            container = _required_string(record, "image_container")
            if container != self.settings.image_container:
                raise ValueError(
                    f"Image {image_id} refers to an unexpected storage container."
                )
            blob_path = _required_string(record, "image_blob_path")
            mime_type = _required_string(record, "image_content_type")
            if not mime_type.startswith("image/"):
                raise ValueError(f"Image {image_id} has an invalid content type.")

            blob_client = self._blob_service_client_instance().get_blob_client(
                container=container,
                blob=blob_path,
            )

            sas_url = _create_readonly_sas_url(
                blob_client=blob_client,
                account_name=self.settings.blob_account_name,
                account_key=self.settings.blob_account_key,
                container=container,
                blob_path=blob_path,
                content_type=mime_type,
            )
            linked.append(
                {
                    "image_id": image_id,
                    "mime_type": mime_type,
                    "blob_uri": blob_client.url,
                    "sas_url": sas_url,
                    "is_direct_match": candidate.is_direct_match,
                    "group_ids": candidate.group_ids,
                    "search_rank": candidate.search_rank,
                    "search_score": candidate.search_score,
                    "metadata": _public_metadata(record),
                }
            )

        return linked

    def _create_source_document_links(
        self, candidates: list[Candidate]
    ) -> tuple[list[dict[str, str]], dict[str, str]]:
        source_documents: list[dict[str, str]] = []
        document_ids: dict[str, str] = {}
        documents_by_identity: dict[tuple[str, str, str], str] = {}

        for candidate in candidates:
            record = candidate.record
            image_id = _required_string(record, "image_id")
            account_name = _required_string(
                record, "source_document_storage_account"
            )
            if (
                account_name.casefold()
                != self.settings.source_document_account_name.casefold()
            ):
                raise ValueError(
                    f"Image {image_id} refers to an unexpected source "
                    "document storage account."
                )

            container = _required_string(record, "source_document_container")
            if container != self.settings.source_document_container:
                raise ValueError(
                    f"Image {image_id} refers to an unexpected source "
                    "document container."
                )
            blob_path = _required_string(record, "source_document_blob_path")
            identity = (account_name.casefold(), container, blob_path)
            source_document_id = documents_by_identity.get(identity)
            if source_document_id is None:
                source_document_id = hashlib.sha256(
                    "\0".join(identity).encode("utf-8")
                ).hexdigest()
                blob_client = (
                    self._source_document_blob_service_client_instance()
                    .get_blob_client(container=container, blob=blob_path)
                )
                document_name = record.get("source_document_name")
                content_type = mimetypes.guess_type(
                    document_name
                    if isinstance(document_name, str)
                    else blob_path
                )[0] or "application/octet-stream"
                sas_url = _create_readonly_sas_url(
                    blob_client=blob_client,
                    account_name=self.settings.source_document_account_name,
                    account_key=self.settings.source_document_account_key,
                    container=container,
                    blob_path=blob_path,
                    content_type=content_type,
                )
                source_documents.append(
                    {
                        "source_document_id": source_document_id,
                        "name": (
                            document_name.strip()
                            if isinstance(document_name, str)
                            and document_name.strip()
                            else blob_path.rsplit("/", maxsplit=1)[-1]
                        ),
                        "content_type": content_type,
                        "sas_url": sas_url,
                    }
                )
                documents_by_identity[identity] = source_document_id
            document_ids[image_id] = source_document_id

        return source_documents, document_ids

    def _search_client_instance(self) -> SearchClient:
        if self._search_client is None:
            self._search_client = SearchClient(
                endpoint=self.settings.search_service_endpoint,
                index_name=self.settings.search_index_name,
                credential=AzureKeyCredential(self.settings.search_api_key),
            )
        return self._search_client

    def _embedding_client_instance(self) -> OpenAI:
        if self._embedding_client is None:
            self._embedding_client = OpenAI(
                base_url=_openai_base_url(self.settings.foundry_project_endpoint),
                api_key=self.settings.foundry_api_key,
            )
        return self._embedding_client

    def _blob_service_client_instance(self) -> BlobServiceClient:
        if self._blob_service_client is None:
            self._blob_service_client = BlobServiceClient(
                account_url=self.settings.blob_account_url,
                credential=self.settings.blob_account_key,
            )
        return self._blob_service_client

    def _source_document_blob_service_client_instance(self) -> BlobServiceClient:
        if self._source_document_blob_service_client is None:
            self._source_document_blob_service_client = BlobServiceClient(
                account_url=self.settings.source_document_account_url,
                credential=self.settings.source_document_account_key,
            )
        return self._source_document_blob_service_client


def _openai_base_url(project_endpoint: str) -> str:
    project_path = "/api/projects/"
    normalized = project_endpoint.rstrip("/")
    if project_path not in normalized:
        raise ValueError(
            "FOUNDRY_PROJECT_ENDPOINT must contain '/api/projects/'."
        )
    resource_endpoint = normalized.split(project_path, maxsplit=1)[0]
    return f"{resource_endpoint}/openai/v1/"


def _create_readonly_sas_url(
    *,
    blob_client: Any,
    account_name: str,
    account_key: str,
    container: str,
    blob_path: str,
    content_type: str,
) -> str:
    sas_token = generate_blob_sas(
        account_name=account_name,
        container_name=container,
        blob_name=blob_path,
        account_key=account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(UTC) + IMAGE_SAS_LIFETIME,
        protocol="https",
        content_type=content_type,
        content_disposition="inline",
    )
    return f"{blob_client.url}?{sas_token}"


def _public_metadata(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "caption": record.get("caption"),
        "entities": record.get("entities") or [],
        "keywords": record.get("keywords") or [],
        "related_text": record.get("related_text") or "",
        "source_document_name": record.get("source_document_name"),
        "page_number": record.get("page_number"),
        "section_path": record.get("section_path") or [],
        "figure_id": record.get("figure_id"),
        "source_type": record.get("source_type"),
    }


def _add_group(candidate: Candidate, group_id: str) -> None:
    if group_id not in candidate.group_ids:
        candidate.group_ids.append(group_id)


def _required_string(record: dict[str, Any], field_name: str) -> str:
    value = record.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"Image index record is missing required field: {field_name}."
        )
    return value.strip()


def _markdown_alt(caption: Any) -> str:
    if not isinstance(caption, str) or not caption.strip():
        return "Retrieved image"
    return caption.replace("[", "(").replace("]", ")").replace("\n", " ").strip()
