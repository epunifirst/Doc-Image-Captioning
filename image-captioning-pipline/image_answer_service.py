import json
import os

from openai import OpenAI


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


def synthesize_answer(question: str, vision_result: dict) -> dict:
    prompt = {
        "question": question,
        "batch_analyses": vision_result["analyses"],
        "instructions": [
            "Use only the supplied visual evidence.",
            "Cite supporting images with their exact labels, for example [Image 1].",
            "If evidence is insufficient, say so plainly.",
            "Do not treat caption hints as proof of visual content.",
            "Return strict JSON with keys answer, cited_image_references, and confidence.",
        ],
    }
    response = _client().chat.completions.create(
        model=os.environ["FOUNDRY_MODEL_DEPLOYMENT"],
        messages=[
            {
                "role": "system",
                "content": "You synthesize grounded answers from structured visual evidence. Return valid JSON only.",
            },
            {
                "role": "user",
                "content": json.dumps(prompt, ensure_ascii=False),
            },
        ],
        response_format={"type": "json_object"},
    )
    result = json.loads(response.choices[0].message.content)
    result.setdefault("cited_image_references", [])
    result.setdefault("confidence", "unknown")
    return result
