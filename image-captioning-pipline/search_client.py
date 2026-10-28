import os

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from dotenv import load_dotenv

load_dotenv()

_client = None


def get_search_client() -> SearchClient:
    global _client

    if _client is None:
        _client = SearchClient(
            endpoint=os.environ[
                "SEARCH_SERVICE_ENDPOINT"
            ],
            index_name=os.environ[
                "SEARCH_INDEX_NAME"
            ],
            credential=AzureKeyCredential(
                os.environ[
                    "SEARCH_ADMIN_KEY"
                ]
            ),
        )

    return _client


def upload_document(
    search_document: dict,
) -> None:
    print(
        sorted(search_document.keys())
    )
    result = get_search_client().upload_documents(
        documents=[search_document]
    )

    if not result[0].succeeded:
        raise RuntimeError(
            f"Search upload failed: "
            f"{result[0].error_message}"
        )