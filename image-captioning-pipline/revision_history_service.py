import json
import os
import re

from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv


load_dotenv()


METADATA_CONTAINER = "output-metadata"

REVISION_FILE_PATTERN = re.compile(
    r"^(?P<number>\d{6})\.json$"
)

_blob_service_client = None


def get_blob_service_client() -> BlobServiceClient:
    global _blob_service_client

    if _blob_service_client is None:
        connection_string = os.environ.get(
            "OUTPUT_STORAGE_CONNECTION_STRING",
            "",
        ).strip()

        if not connection_string:
            raise ValueError(
                "OUTPUT_STORAGE_CONNECTION_STRING "
                "is not configured."
            )

        _blob_service_client = (
            BlobServiceClient.from_connection_string(
                connection_string
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


def validate_identifier(
    value: str,
    field_name: str,
) -> str:
    if not isinstance(value, str):
        raise ValueError(
            f"{field_name} must be a string."
        )

    cleaned_value = value.strip()

    if not cleaned_value:
        raise ValueError(
            f"{field_name} cannot be empty."
        )

    if "/" in cleaned_value:
        raise ValueError(
            f"{field_name} cannot contain '/'."
        )

    return cleaned_value


def current_metadata_blob_path(
    document_id: str,
    image_id: str,
) -> str:
    return (
        f"{document_id}/"
        f"{image_id}/"
        "current.json"
    )


def revision_folder_prefix(
    document_id: str,
    image_id: str,
) -> str:
    return (
        f"{document_id}/"
        f"{image_id}/"
        "revisions/"
    )


def download_json_blob(
    blob_name: str,
) -> dict:
    blob_client = (
        get_metadata_container_client()
        .get_blob_client(
            blob_name
        )
    )

    try:
        raw_data = (
            blob_client
            .download_blob()
            .readall()
        )
    except Exception as exc:
        raise RuntimeError(
            "Unable to download JSON blob: "
            f"{METADATA_CONTAINER}/"
            f"{blob_name}"
        ) from exc

    try:
        decoded_data = raw_data.decode(
            "utf-8"
        )

        value = json.loads(
            decoded_data
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError(
            "Blob does not contain valid UTF-8 "
            "JSON: "
            f"{METADATA_CONTAINER}/"
            f"{blob_name}"
        ) from exc

    if not isinstance(value, dict):
        raise ValueError(
            "JSON blob must contain an object: "
            f"{METADATA_CONTAINER}/"
            f"{blob_name}"
        )

    return value


def download_current_record(
    document_id: str,
    image_id: str,
) -> dict:
    blob_name = current_metadata_blob_path(
        document_id=document_id,
        image_id=image_id,
    )

    blob_client = (
        get_metadata_container_client()
        .get_blob_client(
            blob_name
        )
    )

    if not blob_client.exists():
        raise FileNotFoundError(
            "Current metadata record was not found: "
            f"{METADATA_CONTAINER}/"
            f"{blob_name}"
        )

    current_record = download_json_blob(
        blob_name
    )

    if (
        current_record.get("document_id")
        != document_id
    ):
        raise ValueError(
            "Current metadata document_id does "
            "not match the requested document_id."
        )

    if (
        current_record.get("image_id")
        != image_id
    ):
        raise ValueError(
            "Current metadata image_id does not "
            "match the requested image_id."
        )

    return current_record


def parse_revision_number_from_blob_name(
    blob_name: str,
    prefix: str,
) -> int | None:
    if not blob_name.startswith(prefix):
        return None

    relative_name = blob_name[
        len(prefix):
    ]

    match = REVISION_FILE_PATTERN.fullmatch(
        relative_name
    )

    if match is None:
        return None

    return int(
        match.group("number")
    )


def validate_revision(
    revision: dict,
    expected_document_id: str,
    expected_image_id: str,
    expected_revision_number: int,
    blob_name: str,
) -> None:
    if (
        revision.get("document_id")
        != expected_document_id
    ):
        raise ValueError(
            "Revision document_id does not match "
            "the requested document_id: "
            f"{blob_name}"
        )

    if (
        revision.get("image_id")
        != expected_image_id
    ):
        raise ValueError(
            "Revision image_id does not match "
            "the requested image_id: "
            f"{blob_name}"
        )

    revision_number = revision.get(
        "revision_number"
    )

    if revision_number != expected_revision_number:
        raise ValueError(
            "Revision number inside the JSON does "
            "not match its blob filename: "
            f"{blob_name}"
        )

    if not isinstance(
        revision.get("caption"),
        str,
    ):
        raise ValueError(
            "Revision caption must be a string: "
            f"{blob_name}"
        )

    if not isinstance(
        revision.get("entities", []),
        list,
    ):
        raise ValueError(
            "Revision entities must be a list: "
            f"{blob_name}"
        )

    if not isinstance(
        revision.get("keywords", []),
        list,
    ):
        raise ValueError(
            "Revision keywords must be a list: "
            f"{blob_name}"
        )


def list_revision_blob_names(
    document_id: str,
    image_id: str,
) -> list[tuple[int, str]]:
    prefix = revision_folder_prefix(
        document_id=document_id,
        image_id=image_id,
    )

    container_client = (
        get_metadata_container_client()
    )

    revision_blobs = []

    for blob in container_client.list_blobs(
        name_starts_with=prefix
    ):
        revision_number = (
            parse_revision_number_from_blob_name(
                blob_name=blob.name,
                prefix=prefix,
            )
        )

        if revision_number is None:
            continue

        revision_blobs.append(
            (
                revision_number,
                blob.name,
            )
        )

    revision_blobs.sort(
        key=lambda item: item[0]
    )

    return revision_blobs


def list_revisions(
    document_id: str,
    image_id: str,
) -> dict:
    cleaned_document_id = validate_identifier(
        document_id,
        "document_id",
    )

    cleaned_image_id = validate_identifier(
        image_id,
        "image_id",
    )

    current_record = download_current_record(
        document_id=cleaned_document_id,
        image_id=cleaned_image_id,
    )

    active_revision_number = (
        current_record[
            "active_revision"
        ][
            "revision_number"
        ]
    )

    revision_blob_entries = (
        list_revision_blob_names(
            document_id=cleaned_document_id,
            image_id=cleaned_image_id,
        )
    )

    if not revision_blob_entries:
        raise FileNotFoundError(
            "No revision records were found for "
            "the requested image."
        )

    revisions = []

    for (
        revision_number,
        blob_name,
    ) in revision_blob_entries:
        revision = download_json_blob(
            blob_name
        )

        validate_revision(
            revision=revision,
            expected_document_id=(
                cleaned_document_id
            ),
            expected_image_id=(
                cleaned_image_id
            ),
            expected_revision_number=(
                revision_number
            ),
            blob_name=blob_name,
        )

        revisions.append(
            {
                "revision_number":
                    revision_number,
                "revision_blob_path":
                    blob_name,
                "revision_type":
                    revision.get(
                        "revision_type"
                    ),
                "restored_from_revision":
                    revision.get(
                        "restored_from_revision"
                    ),
                "caption":
                    revision["caption"],
                "entities":
                    revision.get(
                        "entities",
                        [],
                    ),
                "keywords":
                    revision.get(
                        "keywords",
                        [],
                    ),
                "related_text":
                    revision.get(
                        "related_text",
                        "",
                    ),
                "change_reason":
                    revision.get(
                        "change_reason"
                    ),
                "created_at":
                    revision.get(
                        "created_at"
                    ),
                "created_by":
                    revision.get(
                        "created_by"
                    ),
                "is_active": (
                    revision_number
                    == active_revision_number
                ),
            }
        )

    revision_numbers = [
        revision["revision_number"]
        for revision in revisions
    ]

    if (
        active_revision_number
        not in revision_numbers
    ):
        raise ValueError(
            "The active revision does not have a "
            "corresponding immutable revision blob."
        )

    return {
        "document_id": cleaned_document_id,
        "image_id": cleaned_image_id,
        "active_revision_number":
            active_revision_number,
        "revision_count": len(
            revisions
        ),
        "revisions": revisions,
    }