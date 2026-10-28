import os

from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()


_openai_client = None


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

def generate_embedding(text: str) -> dict:
    cleaned_text = text.strip()

    if not cleaned_text:
        raise ValueError(
            "Embedding input text cannot be empty."
        )

    deployment_name = os.environ[
        "FOUNDRY_EMBEDDING_DEPLOYMENT"
    ]

    response = _client().embeddings.create(
        model=deployment_name,
        input=cleaned_text,
    )

    vector = response.data[0].embedding

    if not vector:
        raise ValueError(
            "Embedding deployment returned an empty vector."
        )

    return {
        "vector": vector,
        "model": deployment_name,
        "dimensions": len(vector),
        "version": "1",
    }