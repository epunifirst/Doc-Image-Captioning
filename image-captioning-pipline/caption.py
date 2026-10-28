"""
caption.py

Sends a cropped figure image, along with its surrounding document context,
to a vision-capable model deployed in a Microsoft Foundry PROJECT, and asks
for a structured JSON caption back.

Uses AIProjectClient (azure-ai-projects) to get a properly-authenticated
OpenAI-compatible client for the project's deployed models -- this is the
supported path when FOUNDRY_PROJECT_ENDPOINT looks like:
  https://<resource>.services.ai.azure.com/api/projects/<project-name>

(If you're instead pointed at a resource-level endpoint like
.../services.ai.azure.com/models directly, without /api/projects/, you'd
use azure-ai-inference's ChatCompletionsClient instead -- but that's not
the setup you're on.)
"""

import os
import json
import base64

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

SYSTEM_PROMPT = """You are captioning images extracted from a technical/instructional \
document for use by another AI agent. That agent will later need to decide which \
images are relevant to show alongside a piece of text.

Given an image and the text context around where it appeared in the source document, \
respond with ONLY a JSON object (no markdown fences, no preamble) with these fields:

{
  "caption": "one or two sentences describing what the image shows, in context",
  "entities": ["short", "list", "of", "key", "objects/concepts", "shown"],
  "keywords": ["short", "list", "of", "search", "keywords"],
  "related_text": "the single sentence or instruction from the context that this image most directly illustrates, or empty string if none"
}
"""
def _resource_openai_endpoint() -> str:
    project_endpoint = os.environ[
        "FOUNDRY_PROJECT_ENDPOINT"
    ].rstrip("/")

    project_path = "/api/projects/"

    if project_path not in project_endpoint:
        raise ValueError(
            "FOUNDRY_PROJECT_ENDPOINT must contain "
            "'/api/projects/'."
        )

    resource_endpoint = project_endpoint.split(
        project_path,
        maxsplit=1,
    )[0]

    return f"{resource_endpoint}/openai/v1/"

_openai_client = None


def _client() -> OpenAI:
    global _openai_client

    if _openai_client is None:
        _openai_client = OpenAI(
            base_url=_resource_openai_endpoint(),
            api_key=os.environ[
                "FOUNDRY_API_KEY"
            ],
        )

    return _openai_client

def generate_caption(image_bytes: bytes, context) -> dict:
    """
    context is a FigureContext from extract.py (or anything with
    .section_path, .caption, .context_before, .context_after).
    """
    user_text = (
        f"Section: {' > '.join(context.section_path) if context.section_path else '(none)'}\n"
        f"Detected caption near image: {context.caption or '(none)'}\n"
        f"Text before the image:\n{context.context_before}\n\n"
        f"Text after the image:\n{context.context_after}"
    )

    b64_image = base64.b64encode(image_bytes).decode("utf-8")

    client = _client()
    response = client.chat.completions.create(
        model=os.environ["FOUNDRY_MODEL_DEPLOYMENT"],
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64_image}"},
                    },
                ],
            },
        ],
    )

    raw = response.choices[0].message.content.strip()

    # Models occasionally wrap JSON in ```json fences despite instructions -- strip if present
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Fall back gracefully rather than crashing the whole batch on one bad response
        return {
            "caption": raw,
            "entities": [],
            "keywords": [],
            "related_text": "",
            "_parse_warning": "model did not return valid JSON",
        }