import copy
import json
import os
import logging
from datetime import datetime, timezone

from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv

from embeddings import generate_embedding
from retrieval_text import build_retrieval_text
from azure.core import MatchConditions
from azure.core.exceptions import (
    ResourceModifiedError,
)
from revision_manager import (
    build_revision,
    build_revision_blob_path,
)
from search_client import upload_document
from search_projection import (
    build_search_document,
    validate_search_document,
)
from revision_conflict import (
    RevisionConflictError,
    ensure_expected_active_revision,
)

load_dotenv()


METADATA_CONTAINER = "output-metadata"

_blob_service_client = None


def utc_now() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def get_blob_service_client() -> BlobServiceClient:
    global _blob_service_client

    if _blob_service_client is None:
        _blob_service_client = (
            BlobServiceClient.from_connection_string(
                os.environ[
                    "OUTPUT_STORAGE_CONNECTION_STRING"
                ]
            )
        )

    return _blob_service_client


def get_metadata_container_client():
    return (
        get_blob_service_client()
        .get_container_client(
            METADATA_CONTAINER
        )
    )


def current_metadata_blob_path(
    document_id: str,
    image_id: str,
) -> str:
    return (
        f"{document_id}/"
        f"{image_id}/"
        "current.json"
    )


def download_current_record(
    document_id: str,
    image_id: str,
) -> dict:
    container_client = (
        get_metadata_container_client()
    )

    blob_path = current_metadata_blob_path(
        document_id=document_id,
        image_id=image_id,
    )

    blob_client = container_client.get_blob_client(
        blob_path
    )

    if not blob_client.exists():
        raise FileNotFoundError(
            "Current metadata record was not found: "
            f"{METADATA_CONTAINER}/{blob_path}"
        )

    raw_data = (
        blob_client
        .download_blob()
        .readall()
    )

    current_record = json.loads(
        raw_data.decode("utf-8")
    )

    if (
        current_record.get("document_id")
        != document_id
    ):
        raise ValueError(
            "Downloaded record document_id does not "
            "match the requested document_id."
        )

    if (
        current_record.get("image_id")
        != image_id
    ):
        raise ValueError(
            "Downloaded record image_id does not "
            "match the requested image_id."
        )

    return current_record

def download_current_record_with_etag(
    document_id: str,
    image_id: str,
) -> tuple[dict, str]:
    container_client = (
        get_metadata_container_client()
    )

    blob_path = current_metadata_blob_path(
        document_id=document_id,
        image_id=image_id,
    )

    blob_client = container_client.get_blob_client(
        blob_path
    )

    if not blob_client.exists():
        raise FileNotFoundError(
            "Current metadata record was not found: "
            f"{METADATA_CONTAINER}/{blob_path}"
        )

    downloader = blob_client.download_blob()

    raw_data = downloader.readall()

    current_record = json.loads(
        raw_data.decode("utf-8")
    )

    if (
        current_record.get("document_id")
        != document_id
    ):
        raise ValueError(
            "Downloaded record document_id does not "
            "match the requested document_id."
        )

    if (
        current_record.get("image_id")
        != image_id
    ):
        raise ValueError(
            "Downloaded record image_id does not "
            "match the requested image_id."
        )

    etag = downloader.properties.etag

    if not etag:
        raise ValueError(
            "Current metadata record did not "
            "return an ETag."
        )

    return current_record, etag

def upload_current_record(
    current_record: dict,
    expected_etag: str | None = None,
) -> str:
    blob_path = current_metadata_blob_path(
        document_id=current_record[
            "document_id"
        ],
        image_id=current_record[
            "image_id"
        ],
    )

    blob_client = (
        get_metadata_container_client()
        .get_blob_client(
            blob_path
        )
    )

    upload_options = {
        "overwrite": True,
    }

    if expected_etag is not None:
        upload_options["etag"] = expected_etag
        upload_options["match_condition"] = (
            MatchConditions.IfNotModified
        )

    response = blob_client.upload_blob(
        json.dumps(
            current_record,
            indent=2,
            ensure_ascii=False,
        ).encode("utf-8"),
        **upload_options,
    )

    new_etag = response.get("etag")

    if not new_etag:
        properties = (
            blob_client.get_blob_properties()
        )

        new_etag = properties.etag

    if not new_etag:
        raise ValueError(
            "Updated metadata record did not "
            "return an ETag."
        )

    return new_etag

def upload_revision_once(
    revision: dict,
    revision_blob_path: str,
) -> None:
    blob_client = (
        get_metadata_container_client()
        .get_blob_client(
            revision_blob_path
        )
    )

    if blob_client.exists():
        raise FileExistsError(
            "Revision already exists and will not "
            "be overwritten: "
            f"{METADATA_CONTAINER}/"
            f"{revision_blob_path}"
        )

    blob_client.upload_blob(
        json.dumps(
            revision,
            indent=2,
            ensure_ascii=False,
        ).encode("utf-8"),
        overwrite=False,
    )


def rebuild_retrieval_text(
    current_record: dict,
    revision: dict,
) -> str:
    source_document = current_record[
        "source_document"
    ]

    image = current_record["image"]
    location = current_record["location"]

    return build_retrieval_text(
        source_filename=source_document[
            "filename"
        ],
        source_type=image["source_type"],
        section_path=location.get(
            "section_path",
            [],
        ),
        detected_caption=location.get(
            "detected_caption"
        ),
        caption=revision["caption"],
        entities=revision["entities"],
        keywords=revision["keywords"],
        related_text=revision[
            "related_text"
        ],
        context_before=location.get(
            "context_before",
            "",
        ),
        context_after=location.get(
            "context_after",
            "",
        ),
    )


def validate_string_list(
    value,
    field_name: str,
) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(
            f"{field_name} must be a list."
        )

    if not all(
        isinstance(item, str)
        for item in value
    ):
        raise ValueError(
            f"Every value in {field_name} "
            "must be a string."
        )

    return value

def raise_revision_conflict_from_storage(
    document_id: str,
    image_id: str,
    expected_active_revision: int,
) -> None:
    latest_record = download_current_record(
        document_id=document_id,
        image_id=image_id,
    )

    actual_active_revision = latest_record[
        "active_revision"
    ]["revision_number"]

    raise RevisionConflictError(
        expected_active_revision=(
            expected_active_revision
        ),
        actual_active_revision=(
            actual_active_revision
        ),
    )

def update_caption(
    document_id: str,
    image_id: str,
    caption: str,
    change_reason: str,
    expected_active_revision: int,
    entities: list[str] | None = None,
    keywords: list[str] | None = None,
    related_text: str | None = None,
    actor_object_id: str | None = None,
    actor_email: str | None = None,
    actor_display_name: str | None = None,
) -> dict:
    cleaned_document_id = document_id.strip()
    cleaned_image_id = image_id.strip()
    cleaned_caption = caption.strip()
    cleaned_reason = change_reason.strip()

    if not cleaned_document_id:
        raise ValueError(
            "document_id cannot be empty."
        )

    if not cleaned_image_id:
        raise ValueError(
            "image_id cannot be empty."
        )

    if not cleaned_caption:
        raise ValueError(
            "caption cannot be empty."
        )

    if not cleaned_reason:
        raise ValueError(
            "change_reason cannot be empty."
        )

    (
        current_record,
        current_record_etag,
    ) = download_current_record_with_etag(
        document_id=cleaned_document_id,
        image_id=cleaned_image_id,
    )


    ensure_expected_active_revision(
        current_record=current_record,
        expected_active_revision=(
            expected_active_revision
        ),
    )

    active_revision = current_record[
        "active_revision"
    ]

    if entities is None:
        next_entities = active_revision.get(
            "entities",
            [],
        )
    else:
        next_entities = validate_string_list(
            entities,
            "entities",
        )

    if keywords is None:
        next_keywords = active_revision.get(
            "keywords",
            [],
        )
    else:
        next_keywords = validate_string_list(
            keywords,
            "keywords",
        )

    if related_text is None:
        next_related_text = (
            active_revision.get(
                "related_text",
                "",
            )
        )
    else:
        if not isinstance(
            related_text,
            str,
        ):
            raise ValueError(
                "related_text must be a string."
            )

        next_related_text = related_text.strip()

    revision = build_revision(
        current_record=current_record,
        caption=cleaned_caption,
        entities=next_entities,
        keywords=next_keywords,
        related_text=next_related_text,
        change_reason=cleaned_reason,
        actor_email=actor_email,
    )

    revision["created_by"] = {
        "object_id": actor_object_id,
        "email": actor_email,
        "display_name": actor_display_name,
        "actor_type": "user",
    }

    revision_blob_path = (
        build_revision_blob_path(
            document_id=cleaned_document_id,
            image_id=cleaned_image_id,
            revision_number=revision[
                "revision_number"
            ],
        )
    )

    retrieval_text = rebuild_retrieval_text(
        current_record=current_record,
        revision=revision,
    )

    embedding_data = generate_embedding(
        retrieval_text
    )

    if (
        embedding_data["dimensions"]
        != 1536
    ):
        raise ValueError(
            "Updated caption embedding must contain "
            "1536 dimensions."
        )

    updated_record = copy.deepcopy(
        current_record
    )

    updated_record["active_revision"] = {
        "revision_number": revision[
            "revision_number"
        ],
        "revision_blob_path":
            revision_blob_path,
        "revision_type": revision[
            "revision_type"
        ],
        "caption": revision["caption"],
        "entities": revision["entities"],
        "keywords": revision["keywords"],
        "related_text": revision[
            "related_text"
        ],
        "created_at": revision[
            "created_at"
        ],
        "created_by": revision[
            "created_by"
        ],
    }

    updated_record["retrieval"]["text"] = (
        retrieval_text
    )

    updated_record["retrieval"]["vector"] = (
        embedding_data["vector"]
    )

    updated_record["retrieval"][
        "embedding_model"
    ] = embedding_data["model"]

    updated_record["retrieval"][
        "embedding_dimensions"
    ] = embedding_data["dimensions"]

    updated_record["retrieval"][
        "embedding_version"
    ] = embedding_data["version"]

    updated_record["retrieval"][
        "index_status"
    ] = "pending"

    updated_record["retrieval"][
        "index_error"
    ] = None

    updated_record["lifecycle"][
        "updated_at"
    ] = utc_now()

    updated_record["lifecycle"][
        "updated_by"
    ] = {
        "object_id": actor_object_id,
        "email": actor_email,
        "display_name": actor_display_name,
        "actor_type": "user",
    }

    search_document = build_search_document(
        updated_record
    )

    validate_search_document(
        search_document
    )

    # The immutable revision is written first. If this
    # fails, current.json and Search remain unchanged.
    upload_revision_once(
        revision=revision,
        revision_blob_path=revision_blob_path,
    )

    # Persist the new active revision with pending
    # indexing state before attempting Search.
    try:
        current_record_etag = (
            upload_current_record(
                updated_record,
                expected_etag=(
                    current_record_etag
                ),
            )
        )
    except ResourceModifiedError:
        raise_revision_conflict_from_storage(
            document_id=cleaned_document_id,
            image_id=cleaned_image_id,
            expected_active_revision=(
                expected_active_revision
            ),
        )
    try:
        upload_document(
            search_document
        )

    except Exception as exc:
        updated_record["retrieval"][
            "index_status"
        ] = "failed"

        updated_record["retrieval"][
            "index_error"
        ] = str(exc)

        try:
            upload_current_record(
                updated_record,
                expected_etag=(
                    current_record_etag
                ),
            )
        except ResourceModifiedError:
            logging.warning(
                "The failed indexing status was not "
                "written because current.json changed."
            )

        raise

    updated_record["retrieval"][
        "indexed_revision"
    ] = revision["revision_number"]

    updated_record["retrieval"][
        "indexed_at"
    ] = utc_now()

    updated_record["retrieval"][
        "index_status"
    ] = "indexed"

    updated_record["retrieval"][
        "index_error"
    ] = None

    try:
        current_record_etag = (
            upload_current_record(
                updated_record,
                expected_etag=(
                    current_record_etag
                ),
            )
        )
    except ResourceModifiedError:
        raise_revision_conflict_from_storage(
            document_id=cleaned_document_id,
            image_id=cleaned_image_id,
            expected_active_revision=(
                revision["revision_number"]
            ),
        )
    return {
        "document_id": cleaned_document_id,
        "image_id": cleaned_image_id,
        "revision_number": revision[
            "revision_number"
        ],
        "revision_blob_path":
            revision_blob_path,
        "caption": revision["caption"],
        "embedding_dimensions":
            embedding_data["dimensions"],
        "index_status": updated_record[
            "retrieval"
        ]["index_status"],
        "indexed_revision": updated_record[
            "retrieval"
        ]["indexed_revision"],
        "indexed_at": updated_record[
            "retrieval"
        ]["indexed_at"],
    }