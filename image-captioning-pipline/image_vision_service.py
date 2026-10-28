import json
import os

from openai import OpenAI

from image_blob_service import image_data_url


_client_instance = None


def _resource_openai_endpoint() -> str:
    project_endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/")
    marker = "/api/projects/"
    if marker not in project_endpoint:
        raise ValueError("FOUNDRY_PROJECT_ENDPOINT must contain '/api/projects/'.")
    return f"{project_endpoint.split(marker, 1)[0]}/openai/v1/"


def _client() -> OpenAI:
    global _client_instance
    if _client_instance is None:
        _client_instance = OpenAI(
            base_url=_resource_openai_endpoint(),
            api_key=os.environ["FOUNDRY_API_KEY"],
        )
    return _client_instance


def _positive_int_setting(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be at least 1.")
    return value


def flatten_group_images(groups: list[dict]) -> list[dict]:
    flattened = []
    seen = set()
    for group in groups:
        for group_image in group["images"]:
            record = group_image["record"]
            image_id = record["image_id"]
            if image_id in seen:
                continue
            seen.add(image_id)
            flattened.append({
                "reference": f"Image {len(flattened) + 1}",
                "relationship": group_image["relationship"],
                "group_id": group["group_id"],
                "record": record,
            })
    return flattened


def _batch(items: list[dict], size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def analyze_image_batch(question: str, items: list[dict], batch_number: int) -> dict:
    content = [{
        "type": "text",
        "text": (
            "Answer the user's question by inspecting every supplied image. "
            "Use the actual images as the primary evidence. "
            "Use source_context, related_text, captions, and "
            "section metadata as supporting evidence from the "
            "original document. Do not treat generated captions "
            "as substitutes for visual inspection. "
            "Return strict JSON with keys batch_number, summary, and evidence. "
            "evidence must be a list whose items contain image_reference, image_id, relevance "
            "(high, medium, low, or none), observation, and answer_support. "
            "Do not claim details that are not visible.\n\n"
            f"User question: {question}\nBatch number: {batch_number}"
        ),
    }]

    for item in items:
        record = item["record"]
        metadata = {
            "image_reference": item["reference"],
            "image_id": record["image_id"],
            "relationship": item["relationship"],
            "group_id": item["group_id"],
            "caption_hint": record.get("caption"),
            "document": record.get("source_document_name"),
            "page_number": record.get("page_number"),
            "section_path": record.get("section_path"),
            "related_text": record.get("related_text"),
            "source_context": record.get("retrieval_text"),
        }
        content.append({
            "type": "text",
            "text": json.dumps(metadata, ensure_ascii=False),
        })
        content.append({
            "type": "image_url",
            "image_url": {
                "url": image_data_url(record),
                "detail": os.environ.get("VISION_IMAGE_DETAIL", "auto"),
            },
        })

    response = _client().chat.completions.create(
        model=os.environ["FOUNDRY_MODEL_DEPLOYMENT"],
        messages=[
            {
                "role": "system",
                "content": "You are a careful visual evidence analyst. Return valid JSON only.",
            },
            {"role": "user", "content": content},
        ],
        response_format={"type": "json_object"},
    )
    parsed = json.loads(response.choices[0].message.content)
    parsed["batch_number"] = batch_number
    return parsed


def analyze_retrieved_groups(question: str, groups: list[dict]) -> dict:
    images_per_batch = _positive_int_setting("VISION_IMAGES_PER_BATCH", 10)
    max_batches = _positive_int_setting("MAX_VISION_BATCHES", 10)
    flattened = flatten_group_images(groups)
    analyses = []
    analyzed_count = 0
    truncated = False

    for batch_number, items in enumerate(_batch(flattened, images_per_batch), start=1):
        if batch_number > max_batches:
            truncated = True
            break
        analyses.append(analyze_image_batch(question, items, batch_number))
        analyzed_count += len(items)

    return {
        "image_count_available": len(flattened),
        "image_count_analyzed": analyzed_count,
        "vision_batch_count": len(analyses),
        "vision_images_per_batch": images_per_batch,
        "max_vision_batches": max_batches,
        "truncated": truncated,
        "analyses": analyses,
        "images": flattened,
    }
