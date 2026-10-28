from image_answer_service import synthesize_answer
from image_blob_service import create_read_only_image_url
from image_retrieval_service import retrieve_image_groups
from image_vision_service import analyze_retrieved_groups


def ask_images(
    question: str,
    candidate_count: int | None = None,
) -> dict:
    retrieval = retrieve_image_groups(
        question=question,
        candidate_count=candidate_count,
    )
    if not retrieval["groups"]:
        return {
            "question": question,
            "answer": "No relevant indexed images were found.",
            "confidence": "none",
            "cited_image_references": [],
            "images": [],
            "execution": {
                **retrieval,
                "groups": [],
                "vision_batch_count": 0,
                "analyzed_image_count": 0,
            },
        }

    vision = analyze_retrieved_groups(question, retrieval["groups"])
    answer = synthesize_answer(question, vision)

    image_results = []
    for item in vision["images"]:
        record = item["record"]
        image_results.append({
            "reference": item["reference"],
            "image_id": record["image_id"],
            "relationship": item["relationship"],
            "group_id": item["group_id"],
            "caption": record.get("caption"),
            "document_id": record.get("document_id"),
            "source_document_name": record.get("source_document_name"),
            "page_number": record.get("page_number"),
            "section_path": record.get("section_path") or [],
            "source_type": record.get("source_type"),
            "figure_id": record.get("figure_id"),
            "image_url": create_read_only_image_url(record),
        })

    retrieval_summary = {key: value for key, value in retrieval.items() if key != "groups"}
    return {
        "question": question,
        "answer": answer.get("answer", ""),
        "confidence": answer.get("confidence", "unknown"),
        "cited_image_references": answer.get("cited_image_references", []),
        "images": image_results,
        "execution": {
            **retrieval_summary,
            "image_count_available": vision["image_count_available"],
            "analyzed_image_count": vision["image_count_analyzed"],
            "vision_batch_count": vision["vision_batch_count"],
            "vision_truncated": vision["truncated"],
        },
    }
