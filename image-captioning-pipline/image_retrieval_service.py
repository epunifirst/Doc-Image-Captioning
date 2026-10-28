import os

from azure.search.documents.models import VectorizedQuery

from embeddings import generate_embedding
from search_client import get_search_client


DEFAULT_CANDIDATE_COUNT = 20
DEFAULT_MAX_EXPANDED_IMAGES = 100

SELECT_FIELDS = [
    "image_id",
    "document_id",
    "source_version_id",
    "source_document_name",
    "page_number",
    "image_storage_account",
    "image_container",
    "image_blob_path",
    "image_content_type",
    "source_type",
    "figure_id",
    "section_path",
    "caption",
    "entities",
    "keywords",
    "related_text",
    "retrieval_text",
    "sibling_image_ids",
    "is_ignored",
    "allowed_user_ids",
    "allowed_group_ids",
]


def _positive_int_setting(name: str, default: int) -> int:
    raw_value = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if value < 1:
        raise ValueError(f"{name} must be at least 1.")
    return value


def _normalize_document(document: dict) -> dict:
    normalized = {field: document.get(field) for field in SELECT_FIELDS}
    normalized["sibling_image_ids"] = normalized.get("sibling_image_ids") or []
    normalized["section_path"] = normalized.get("section_path") or []
    normalized["entities"] = normalized.get("entities") or []
    normalized["keywords"] = normalized.get("keywords") or []
    normalized["allowed_user_ids"] = normalized.get("allowed_user_ids") or []
    normalized["allowed_group_ids"] = normalized.get("allowed_group_ids") or []
    return normalized


def _get_document(image_id: str) -> dict | None:
    try:
        document = get_search_client().get_document(
            key=image_id,
            selected_fields=SELECT_FIELDS,
        )
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        if status_code == 404:
            return None
        raise
    result = _normalize_document(dict(document))
    if result.get("is_ignored"):
        return None
    return result


def retrieve_image_groups(
    question: str,
    candidate_count: int | None = None,
    max_expanded_images: int | None = None,
) -> dict:
    cleaned_question = question.strip()
    if not cleaned_question:
        raise ValueError("question cannot be empty.")

    candidate_count = candidate_count or _positive_int_setting(
        "IMAGE_SEARCH_CANDIDATE_COUNT",
        DEFAULT_CANDIDATE_COUNT,
    )
    max_expanded_images = max_expanded_images or _positive_int_setting(
        "MAX_EXPANDED_IMAGES",
        DEFAULT_MAX_EXPANDED_IMAGES,
    )

    query_embedding = generate_embedding(cleaned_question)
    vector_query = VectorizedQuery(
        vector=query_embedding["vector"],
        k_nearest_neighbors=candidate_count,
        fields="caption_vector",
    )

    search_results = get_search_client().search(
        search_text=cleaned_question,
        vector_queries=[vector_query],
        filter="is_ignored eq false",
        select=SELECT_FIELDS,
        top=candidate_count,
        include_total_count=True,
    )

    primary_results = []
    for rank, result in enumerate(search_results, start=1):
        record = _normalize_document(dict(result))
        record["search_rank"] = rank
        record["search_score"] = result.get("@search.score")
        primary_results.append(record)

    expanded_by_id = {}
    groups = []
    truncated = False
    omitted_image_ids = []

    for primary in primary_results:
        ordered_ids = [primary["image_id"]]
        ordered_ids.extend(primary.get("sibling_image_ids") or [])
        seen_in_group = set()
        group_images = []

        for image_id in ordered_ids:
            if not image_id or image_id in seen_in_group:
                continue
            seen_in_group.add(image_id)

            record = expanded_by_id.get(image_id)
            if record is None:
                if len(expanded_by_id) >= max_expanded_images:
                    truncated = True
                    omitted_image_ids.append(image_id)
                    continue
                record = primary if image_id == primary["image_id"] else _get_document(image_id)
                if record is None:
                    continue
                expanded_by_id[image_id] = record

            group_images.append({
                "relationship": "primary" if image_id == primary["image_id"] else "sibling",
                "record": record,
            })

        groups.append({
            "group_id": primary["image_id"],
            "primary_image_id": primary["image_id"],
            "search_rank": primary["search_rank"],
            "search_score": primary.get("search_score"),
            "images": group_images,
        })

    return {
        "question": cleaned_question,
        "candidate_count_requested": candidate_count,
        "candidate_count_returned": len(primary_results),
        "expanded_image_count": len(expanded_by_id),
        "max_expanded_images": max_expanded_images,
        "truncated": truncated,
        "omitted_image_ids": omitted_image_ids,
        "groups": groups,
    }
