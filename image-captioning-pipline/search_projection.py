
def build_search_document(
    current_record: dict,
) -> dict:
    source_document = current_record["source_document"]
    image = current_record["image"]
    location = current_record["location"]
    active_revision = current_record["active_revision"]
    retrieval = current_record["retrieval"]
    relationships = current_record["relationships"]
    search_control = current_record["search_control"]
    access = current_record["access"]
    lifecycle = current_record["lifecycle"]

    return {
        "image_id": current_record["image_id"],
        "document_id": current_record["document_id"],
        "source_version_id": current_record["source_version_id"],
        "schema_version": current_record["schema_version"],
        "extraction_version": current_record[
            "extraction_version"
        ],

        "source_document_name": source_document["filename"],
        "source_document_storage_account": source_document[
            "storage_account"
        ],
        "source_document_container": source_document["container"],
        "source_document_blob_path": source_document["blob_path"],
        "page_number": source_document["page_number"],

        "image_storage_account": image["storage_account"],
        "image_container": image["container"],
        "image_blob_path": image["blob_path"],
        "image_content_type": image["content_type"],
        "source_type": image["source_type"],

        "figure_id": location["figure_id"],
        "section_path": location["section_path"],
        "detected_caption": location["detected_caption"],

        "caption": active_revision["caption"],
        "caption_revision": active_revision["revision_number"],
        "caption_revision_type": active_revision["revision_type"],
        "caption_updated_at": active_revision["created_at"],

        "entities": active_revision["entities"],
        "keywords": active_revision["keywords"],
        "related_text": active_revision["related_text"],

        "retrieval_text": retrieval["text"],
        "caption_vector": retrieval["vector"],
        "sibling_image_ids": relationships[
            "sibling_image_ids"
        ],

        "is_ignored": search_control["is_ignored"],
        "ignore_reason": search_control["ignore_reason"],

        "allowed_user_ids": access["allowed_user_ids"],
        "allowed_group_ids": access["allowed_group_ids"],

        "record_created_at": lifecycle["created_at"],
        "record_updated_at": lifecycle["updated_at"],
    }

def validate_search_document(
    search_document: dict,
) -> None:
    required_fields = [
        "image_id",
        "document_id",
        "source_version_id",
        "source_document_name",
        "source_document_container",
        "source_document_blob_path",
        "image_container",
        "image_blob_path",
        "source_type",
        "caption",
        "caption_revision",
        "retrieval_text",
        "caption_vector",
        "is_ignored",
        "allowed_user_ids",
        "allowed_group_ids",
    ]

    for field_name in required_fields:
        if field_name not in search_document:
            raise ValueError(
                "Search document is missing required field: "
                f"{field_name}"
            )
    if not isinstance(
        search_document["caption_vector"],
        list,
    ):
        raise ValueError(
            "Search document caption_vector must be a list."
        )
    if len(search_document["caption_vector"]) != 1536:
        raise ValueError(
            "Search document caption_vector must contain "
            "1536 dimensions."
        )
    if not search_document["source_version_id"]:
        raise ValueError(
            "Search document source_version_id cannot be empty."
        )
    if not search_document["image_id"]:
        raise ValueError(
            "Search document image_id cannot be empty."
        )

    if not search_document["document_id"]:
        raise ValueError(
            "Search document document_id cannot be empty."
        )

    if not search_document["caption"].strip():
        raise ValueError(
            "Search document caption cannot be empty."
        )

    if search_document["caption_revision"] < 1:
        raise ValueError(
            "Search document caption revision must be "
            "greater than or equal to 1."
        )

    if not search_document["retrieval_text"].strip():
        raise ValueError(
            "Search document retrieval text cannot be empty."
        )

    if not isinstance(
        search_document["is_ignored"],
        bool,
    ):
        raise ValueError(
            "Search document is_ignored must be a Boolean."
        )

    if not isinstance(
        search_document["allowed_user_ids"],
        list,
    ):
        raise ValueError(
            "Search document allowed_user_ids must be a list."
        )

    if not isinstance(
        search_document["allowed_group_ids"],
        list,
    ):
        raise ValueError(
            "Search document allowed_group_ids must be a list."
        )