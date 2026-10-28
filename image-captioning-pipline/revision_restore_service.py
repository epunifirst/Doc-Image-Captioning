import copy
import logging

from azure.core.exceptions import (
    ResourceModifiedError,
)
from embeddings import generate_embedding
from revision_manager import (
    build_restore_revision,
    build_revision_blob_path,
)
from retrieval_text import build_retrieval_text
from search_client import upload_document
from search_projection import (
    build_search_document,
    validate_search_document,
)
from caption_update_service import (
    download_current_record,
    download_current_record_with_etag,
    raise_revision_conflict_from_storage,
    upload_current_record,
    upload_revision_once,
    utc_now,
)
from revision_history_service import (
    download_json_blob,
    revision_folder_prefix,
)
from revision_conflict import (
    ensure_expected_active_revision,
)

EMBEDDING_DIMENSIONS = 1536


def validate_revision_number(
    value,
) -> int:
    if isinstance(value, bool):
        raise ValueError(
            "revision_number must be an integer."
        )

    if not isinstance(value, int):
        raise ValueError(
            "revision_number must be an integer."
        )

    if value < 1:
        raise ValueError(
            "revision_number must be greater "
            "than or equal to 1."
        )

    return value


def source_revision_blob_path(
    document_id: str,
    image_id: str,
    revision_number: int,
) -> str:
    prefix = revision_folder_prefix(
        document_id=document_id,
        image_id=image_id,
    )

    return (
        f"{prefix}"
        f"{revision_number:06d}.json"
    )


def download_source_revision(
    document_id: str,
    image_id: str,
    revision_number: int,
) -> dict:
    blob_path = source_revision_blob_path(
        document_id=document_id,
        image_id=image_id,
        revision_number=revision_number,
    )

    try:
        revision = download_json_blob(
            blob_path
        )
    except RuntimeError as exc:
        raise FileNotFoundError(
            "Requested revision was not found: "
            f"{blob_path}"
        ) from exc

    if (
        revision.get("document_id")
        != document_id
    ):
        raise ValueError(
            "Requested revision document_id does "
            "not match the requested document."
        )

    if (
        revision.get("image_id")
        != image_id
    ):
        raise ValueError(
            "Requested revision image_id does not "
            "match the requested image."
        )

    if (
        revision.get("revision_number")
        != revision_number
    ):
        raise ValueError(
            "Requested revision number does not "
            "match its blob filename."
        )

    return revision


def rebuild_restored_retrieval_text(
    current_record: dict,
    restore_revision: dict,
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
        caption=restore_revision[
            "caption"
        ],
        entities=restore_revision.get(
            "entities",
            [],
        ),
        keywords=restore_revision.get(
            "keywords",
            [],
        ),
        related_text=restore_revision.get(
            "related_text",
            "",
        ),
        context_before=location.get(
            "context_before",
            "",
        ),
        context_after=location.get(
            "context_after",
            "",
        ),
    )


def restore_revision(
    document_id: str,
    image_id: str,
    revision_number: int,
    change_reason: str,
    expected_active_revision: int,
    actor_object_id: str | None = None,
    actor_email: str | None = None,
    actor_display_name: str | None = None,
) -> dict:
    if not isinstance(document_id, str):
        raise ValueError(
            "document_id must be a string."
        )

    if not isinstance(image_id, str):
        raise ValueError(
            "image_id must be a string."
        )

    if not isinstance(change_reason, str):
        raise ValueError(
            "change_reason must be a string."
        )

    cleaned_document_id = (
        document_id.strip()
    )

    cleaned_image_id = image_id.strip()

    cleaned_reason = (
        change_reason.strip()
    )

    if not cleaned_document_id:
        raise ValueError(
            "document_id cannot be empty."
        )

    if "/" in cleaned_document_id:
        raise ValueError(
            "document_id cannot contain '/'."
        )

    if not cleaned_image_id:
        raise ValueError(
            "image_id cannot be empty."
        )

    if "/" in cleaned_image_id:
        raise ValueError(
            "image_id cannot contain '/'."
        )

    if not cleaned_reason:
        raise ValueError(
            "change_reason cannot be empty."
        )

    source_revision_number = (
        validate_revision_number(
            revision_number
        )
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

    source_revision = (
        download_source_revision(
            document_id=cleaned_document_id,
            image_id=cleaned_image_id,
            revision_number=(
                source_revision_number
            ),
        )
    )

    restore_revision_record = (
        build_restore_revision(
            current_record=current_record,
            source_revision=source_revision,
            change_reason=cleaned_reason,
            actor_object_id=actor_object_id,
            actor_email=actor_email,
            actor_display_name=(
                actor_display_name
            ),
        )
    )

    new_revision_number = (
        restore_revision_record[
            "revision_number"
        ]
    )

    new_revision_blob_path = (
        build_revision_blob_path(
            document_id=cleaned_document_id,
            image_id=cleaned_image_id,
            revision_number=(
                new_revision_number
            ),
        )
    )

    retrieval_text = (
        rebuild_restored_retrieval_text(
            current_record=current_record,
            restore_revision=(
                restore_revision_record
            ),
        )
    )

    embedding_data = generate_embedding(
        retrieval_text
    )

    if (
        embedding_data["dimensions"]
        != EMBEDDING_DIMENSIONS
    ):
        raise ValueError(
            "Restored caption embedding must "
            f"contain {EMBEDDING_DIMENSIONS} "
            "dimensions."
        )

    updated_record = copy.deepcopy(
        current_record
    )

    updated_record["active_revision"] = {
        "revision_number":
            new_revision_number,
        "revision_blob_path":
            new_revision_blob_path,
        "revision_type":
            restore_revision_record[
                "revision_type"
            ],
        "caption":
            restore_revision_record[
                "caption"
            ],
        "entities":
            restore_revision_record[
                "entities"
            ],
        "keywords":
            restore_revision_record[
                "keywords"
            ],
        "related_text":
            restore_revision_record[
                "related_text"
            ],
        "created_at":
            restore_revision_record[
                "created_at"
            ],
        "created_by":
            restore_revision_record[
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
        "display_name":
            actor_display_name,
        "actor_type": "user",
    }

    search_document = build_search_document(
        updated_record
    )

    validate_search_document(
        search_document
    )

    # Write the immutable restore revision first.
    # If this fails, current.json and Search remain
    # unchanged.
    upload_revision_once(
        revision=restore_revision_record,
        revision_blob_path=(
            new_revision_blob_path
        ),
    )

    # Persist the restored active revision with a
    # pending Search state.
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
    ] = new_revision_number

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
                new_revision_number
            ),
        )

    return {
        "document_id":
            cleaned_document_id,
        "image_id":
            cleaned_image_id,
        "revision_number":
            new_revision_number,
        "revision_blob_path":
            new_revision_blob_path,
        "revision_type":
            restore_revision_record[
                "revision_type"
            ],
        "restored_from_revision":
            source_revision_number,
        "caption":
            restore_revision_record[
                "caption"
            ],
        "embedding_dimensions":
            embedding_data["dimensions"],
        "index_status":
            updated_record["retrieval"][
                "index_status"
            ],
        "indexed_revision":
            updated_record["retrieval"][
                "indexed_revision"
            ],
        "indexed_at":
            updated_record["retrieval"][
                "indexed_at"
            ],
    }