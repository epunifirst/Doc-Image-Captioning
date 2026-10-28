import json
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def next_revision_number(
    revisions: list[dict],
) -> int:
    if not revisions:
        return 1

    return (
        max(
            revision["revision_number"]
            for revision in revisions
        )
        + 1
    )


def build_revision(
    current_record: dict,
    caption: str,
    entities: list[str],
    keywords: list[str],
    related_text: str,
    change_reason: str,
    actor_email: str | None = None,
) -> dict:
    active_revision = current_record[
        "active_revision"
    ]

    revision_number = (
        active_revision["revision_number"]
        + 1
    )

    return {
        "schema_version": current_record[
            "schema_version"
        ],
        "image_id": current_record[
            "image_id"
        ],
        "document_id": current_record[
            "document_id"
        ],
        "source_version_id": current_record[
            "source_version_id"
        ],
        "revision_number": revision_number,
        "revision_type": "manual_edit",
        "restored_from_revision": None,
        "caption": caption,
        "entities": entities,
        "keywords": keywords,
        "related_text": related_text,
        "change_reason": change_reason,
        "created_at": utc_now(),
        "created_by": {
            "object_id": None,
            "email": actor_email,
            "display_name": actor_email,
            "actor_type": "user",
        },
    }

def build_restore_revision(
    current_record: dict,
    source_revision: dict,
    change_reason: str,
    actor_object_id: str | None = None,
    actor_email: str | None = None,
    actor_display_name: str | None = None,
) -> dict:
    cleaned_reason = change_reason.strip()

    if not cleaned_reason:
        raise ValueError(
            "Change reason cannot be empty."
        )

    if (
        source_revision.get("document_id")
        != current_record.get("document_id")
    ):
        raise ValueError(
            "Source revision document_id does not "
            "match the current record."
        )

    if (
        source_revision.get("image_id")
        != current_record.get("image_id")
    ):
        raise ValueError(
            "Source revision image_id does not "
            "match the current record."
        )

    source_revision_number = (
        source_revision.get(
            "revision_number"
        )
    )

    if not isinstance(
        source_revision_number,
        int,
    ):
        raise ValueError(
            "Source revision number must be "
            "an integer."
        )

    if source_revision_number < 1:
        raise ValueError(
            "Source revision number must be "
            "greater than or equal to 1."
        )

    active_revision_number = (
        current_record[
            "active_revision"
        ][
            "revision_number"
        ]
    )

    if source_revision_number == (
        active_revision_number
    ):
        raise ValueError(
            "The requested revision is already "
            "the active revision."
        )

    source_caption = source_revision.get(
        "caption"
    )

    if (
        not isinstance(
            source_caption,
            str,
        )
        or not source_caption.strip()
    ):
        raise ValueError(
            "Source revision caption cannot "
            "be empty."
        )

    source_entities = source_revision.get(
        "entities",
        [],
    )

    if not isinstance(
        source_entities,
        list,
    ):
        raise ValueError(
            "Source revision entities must "
            "be a list."
        )

    if not all(
        isinstance(item, str)
        for item in source_entities
    ):
        raise ValueError(
            "Every source revision entity "
            "must be a string."
        )

    source_keywords = source_revision.get(
        "keywords",
        [],
    )

    if not isinstance(
        source_keywords,
        list,
    ):
        raise ValueError(
            "Source revision keywords must "
            "be a list."
        )

    if not all(
        isinstance(item, str)
        for item in source_keywords
    ):
        raise ValueError(
            "Every source revision keyword "
            "must be a string."
        )

    source_related_text = (
        source_revision.get(
            "related_text",
            "",
        )
    )

    if not isinstance(
        source_related_text,
        str,
    ):
        raise ValueError(
            "Source revision related_text "
            "must be a string."
        )

    new_revision_number = (
        active_revision_number
        + 1
    )

    return {
        "schema_version": current_record[
            "schema_version"
        ],
        "image_id": current_record[
            "image_id"
        ],
        "document_id": current_record[
            "document_id"
        ],
        "source_version_id": current_record[
            "source_version_id"
        ],
        "revision_number":
            new_revision_number,
        "revision_type": "restore",
        "restored_from_revision":
            source_revision_number,
        "caption": source_caption.strip(),
        "entities": list(
            source_entities
        ),
        "keywords": list(
            source_keywords
        ),
        "related_text":
            source_related_text.strip(),
        "change_reason":
            cleaned_reason,
        "created_at": utc_now(),
        "created_by": {
            "object_id":
                actor_object_id,
            "email":
                actor_email,
            "display_name":
                actor_display_name,
            "actor_type": "user",
        },
    }

def build_revision_blob_path(
    document_id: str,
    image_id: str,
    revision_number: int,
) -> str:
    return (
        f"{document_id}/"
        f"{image_id}/"
        "revisions/"
        f"{revision_number:06d}.json"
    )