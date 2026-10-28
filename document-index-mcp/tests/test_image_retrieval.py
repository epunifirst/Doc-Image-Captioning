from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from config import Settings
from image_retrieval import ImageRetrievalService, VECTOR_DIMENSIONS


class FakeSearchResults(list):
    def __init__(self, records, total_count):
        super().__init__(records)
        self.total_count = total_count

    def get_count(self):
        return self.total_count


class FakeSearchClient:
    def __init__(self, records, sibling_records, total_count):
        self.records = records
        self.sibling_records = sibling_records
        self.total_count = total_count
        self.search_arguments = None

    def search(self, **kwargs):
        self.search_arguments = kwargs
        return FakeSearchResults(self.records, self.total_count)

    def get_document(self, key, selected_fields):
        return self.sibling_records[key]


class FakeEmbeddingClient:
    def __init__(self):
        self.embeddings = self
        self.request = None

    def create(self, *, model, input):
        self.request = {"model": model, "input": input}
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.1] * VECTOR_DIMENSIONS)]
        )


class FakeBlobClient:
    def __init__(self, data):
        self.data = data
        self.url = ""
        self.download_called = False
        self.properties_called = False

    def get_blob_properties(self):
        self.properties_called = True
        return SimpleNamespace(size=len(self.data))

    def download_blob(self, *, max_concurrency):
        self.download_called = True
        assert max_concurrency == 1
        return self

    def readall(self):
        return self.data


class FakeBlobServiceClient:
    def __init__(self, blobs, account_name="imageaccount"):
        self.blobs = blobs
        self.account_name = account_name
        self.requested_blobs = []

    def get_blob_client(self, *, container, blob):
        self.requested_blobs.append((container, blob))
        client = self.blobs.setdefault(blob, FakeBlobClient(b""))
        client.url = (
            f"https://{self.account_name}.blob.core.windows.net/{container}/{blob}"
        )
        return client


def settings() -> Settings:
    return Settings(
        mcp_api_key="client-key",
        search_service_endpoint="https://search.example.test",
        search_index_name="image-captions",
        search_api_key="search-key",
        foundry_project_endpoint=(
            "https://foundry.example.test/api/projects/project"
        ),
        foundry_api_key="foundry-key",
        foundry_embedding_deployment="embedding-deployment",
        blob_account_name="imageaccount",
        blob_account_url="https://imageaccount.blob.core.windows.net",
        blob_account_key="AAAAAAAAAAAAAAAAAAAAAA==",
        image_container="output-images",
        source_document_account_name="docimagecaptionstore",
        source_document_account_url=(
            "https://docimagecaptionstore.blob.core.windows.net"
        ),
        source_document_account_key="BBBBBBBBBBBBBBBBBBBBBB==",
        source_document_container="incoming",
        max_returned_images=20,
    )


def record(
    image_id: str,
    *,
    siblings: list[str] | None = None,
    is_ignored: bool = False,
    account: str = "imageaccount",
    container: str = "output-images",
    blob_path: str | None = None,
    source_account: str = "docimagecaptionstore",
    source_container: str = "incoming",
    source_blob_path: str = "manual/manual.pdf",
    source_name: str = "manual.pdf",
) -> dict:
    return {
        "image_id": image_id,
        "image_storage_account": account,
        "image_container": container,
        "image_blob_path": blob_path or f"{image_id}.png",
        "image_content_type": "image/png",
        "source_type": "figure",
        "source_document_name": source_name,
        "source_document_storage_account": source_account,
        "source_document_container": source_container,
        "source_document_blob_path": source_blob_path,
        "page_number": 3,
        "figure_id": f"figure-{image_id}",
        "section_path": ["Production", "Process"],
        "caption": f"Caption for {image_id}",
        "entities": ["machine"],
        "keywords": ["production"],
        "related_text": "Follow the next step.",
        "retrieval_text": f"Context for {image_id}",
        "sibling_image_ids": siblings or [],
        "is_ignored": is_ignored,
        "@search.score": 1.0,
    }


def service(
    direct_records,
    sibling_records,
    blobs,
    *,
    total_count=None,
):
    search = FakeSearchClient(
        direct_records,
        sibling_records,
        len(direct_records) if total_count is None else total_count,
    )
    embedding = FakeEmbeddingClient()
    blob_service = FakeBlobServiceClient(blobs)
    source_records = [*direct_records, *sibling_records.values()]
    source_blobs = {
        item["source_document_blob_path"]: FakeBlobClient(b"document")
        for item in source_records
    }
    source_document_blob_service = FakeBlobServiceClient(
        source_blobs, account_name="docimagecaptionstore"
    )
    return (
        ImageRetrievalService(
            settings(),
            search_client=search,
            embedding_client=embedding,
            blob_service_client=blob_service,
            source_document_blob_service_client=source_document_blob_service,
        ),
        search,
        embedding,
        blob_service,
    )


def test_hybrid_search_expands_sibling_groups_in_rank_order() -> None:
    direct = [
        record("primary-1", siblings=["sibling-1"]),
        record("primary-2"),
    ]
    sibling_records = {"sibling-1": record("sibling-1")}
    blobs = {
        item["image_blob_path"]: FakeBlobClient(item["image_id"].encode())
        for item in [*direct, *sibling_records.values()]
    }
    retrieval, search, embedding, blob_service = service(
        direct, sibling_records, blobs, total_count=2
    )

    result = retrieval.retrieve("how do I run production?")

    metadata = result["metadata"]
    assert len(metadata["results"]) == 3
    assert metadata["results"][0]["content_type"] == "image/png"
    assert metadata["results"][0]["blob_uri"] == (
        "https://imageaccount.blob.core.windows.net/output-images/primary-1.png"
    )
    assert metadata["results"][0]["sas_url"].startswith(
        "https://imageaccount.blob.core.windows.net/output-images/primary-1.png?"
    )
    sas_query = parse_qs(urlsplit(metadata["results"][0]["sas_url"]).query)
    assert sas_query["sp"] == ["r"]
    assert sas_query["spr"] == ["https"]
    assert sas_query["rsct"] == ["image/png"]
    assert sas_query["rscd"] == ["inline"]
    assert metadata["results"][0]["image_url"] == metadata["results"][0]["sas_url"]
    assert metadata["results"][0]["markdown_image"].startswith(
        "![Caption for primary-1]("
    )
    assert len(metadata["source_documents"]) == 1
    source_document = metadata["source_documents"][0]
    assert source_document["name"] == "manual.pdf"
    assert source_document["content_type"] == "application/pdf"
    assert "blob_uri" not in source_document
    assert source_document["sas_url"].startswith(
        "https://docimagecaptionstore.blob.core.windows.net/"
        "incoming/manual/manual.pdf?"
    )
    source_sas_query = parse_qs(urlsplit(source_document["sas_url"]).query)
    assert source_sas_query["sp"] == ["r"]
    assert source_sas_query["spr"] == ["https"]
    assert source_sas_query["rsct"] == ["application/pdf"]
    assert source_sas_query["rscd"] == ["inline"]
    assert {
        item["source_document_id"] for item in metadata["results"]
    } == {source_document["source_document_id"]}
    assert embedding.request == {
        "model": "embedding-deployment",
        "input": "how do I run production?",
    }
    assert search.search_arguments["search_text"] == "how do I run production?"
    assert search.search_arguments["filter"] == "is_ignored eq false"
    assert search.search_arguments["vector_queries"][0].fields == "caption_vector"
    assert [
        item["image_id"] for item in metadata["results"]
    ] == ["primary-1", "sibling-1", "primary-2"]
    assert [item["relationship"] for item in metadata["results"]] == [
        "direct",
        "sibling",
        "direct",
    ]
    assert all(
        not blob.download_called and not blob.properties_called
        for blob in blob_service.blobs.values()
    )
    assert metadata["truncated"] is False


def test_image_link_response_does_not_download_or_embed_image_bytes() -> None:
    primary = record("image")
    blob = FakeBlobClient(b"image")
    blobs = {"image.png": blob}
    retrieval, _, _, _ = service([primary], {}, blobs, total_count=1)

    result = retrieval.retrieve("find this image")

    metadata_result = result["metadata"]["results"][0]
    assert metadata_result["content_type"] == "image/png"
    assert metadata_result["blob_uri"].startswith("https://")
    assert metadata_result["sas_url"].startswith("https://")
    assert "image_base64" not in metadata_result
    assert "image_contents" not in result
    assert blob.download_called is False


def test_source_document_links_are_deduplicated_by_blob_identity() -> None:
    first = record("image-1", source_blob_path="manuals/first.pdf")
    second = record("image-2", source_blob_path="manuals/second.pdf")
    blobs = {
        item["image_blob_path"]: FakeBlobClient(b"image")
        for item in [first, second]
    }
    retrieval, _, _, _ = service([first, second], {}, blobs, total_count=2)

    metadata = retrieval.retrieve("find the manual")["metadata"]

    assert len(metadata["source_documents"]) == 2
    assert len(
        {item["source_document_id"] for item in metadata["results"]}
    ) == 2
    assert len({item["sas_url"] for item in metadata["source_documents"]}) == 2


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_account", "unexpected-account", "unexpected source document storage account"),
        ("source_container", "unexpected-container", "unexpected source document container"),
    ],
)
def test_unexpected_source_document_location_is_rejected(
    field, value, message
) -> None:
    retrieval, _, _, _ = service(
        [record("image", **{field: value})],
        {},
        {"image.png": FakeBlobClient(b"image")},
        total_count=1,
    )

    with pytest.raises(ValueError, match=message):
        retrieval.retrieve("find this image")


def test_sibling_expansion_marks_count_truncation_at_cap() -> None:
    primary = record("primary", siblings=["sibling-1", "sibling-2"])
    sibling_records = {
        "sibling-1": record("sibling-1"),
        "sibling-2": record("sibling-2"),
    }
    blobs = {
        item["image_blob_path"]: FakeBlobClient(b"image")
        for item in [primary, *sibling_records.values()]
    }
    retrieval, _, _, blob_service = service(
        [primary], sibling_records, blobs, total_count=1
    )

    result = retrieval.retrieve("describe the production process", max_images=2)

    assert [item["image_id"] for item in result["metadata"]["results"]] == [
        "primary",
        "sibling-1",
    ]
    assert result["metadata"]["truncated"] is True
    assert result["metadata"]["truncation_reasons"] == ["image_count_limit"]
    assert len(blob_service.requested_blobs) == 2


def test_image_links_do_not_fetch_blob_sizes_or_apply_byte_budget() -> None:
    primary = [record("first"), record("second")]
    blobs = {
        item["image_blob_path"]: FakeBlobClient(b"x" * 100)
        for item in primary
    }
    retrieval, _, _, _ = service(primary, {}, blobs, total_count=2)

    result = retrieval.retrieve("find process images")

    assert result["metadata"]["returned_count"] == 2
    assert result["metadata"]["truncated"] is False
    assert all(not blob.properties_called for blob in blobs.values())
    assert all(not blob.download_called for blob in blobs.values())


def test_unexpected_storage_account_is_rejected_before_blob_access() -> None:
    primary = [record("foreign", account="unexpected-account")]
    retrieval, _, _, blob_service = service(primary, {}, {}, total_count=1)

    with pytest.raises(ValueError, match="unexpected storage account"):
        retrieval.retrieve("find this image")

    assert blob_service.requested_blobs == []


def test_more_direct_matches_than_requested_sets_truncation() -> None:
    primary = [record("first")]
    blobs = {"first.png": FakeBlobClient(b"image")}
    retrieval, _, _, _ = service(
        primary, {}, blobs, total_count=2
    )

    result = retrieval.retrieve("find production", max_images=1)

    assert result["metadata"]["truncated"] is True
    assert result["metadata"]["truncation_reasons"] == ["image_count_limit"]


def test_invalid_question_and_result_limit_are_rejected() -> None:
    retrieval, _, _, _ = service([], {}, {}, total_count=0)

    with pytest.raises(ValueError, match="question must be a non-empty string"):
        retrieval.retrieve("  ")
    with pytest.raises(ValueError, match="between 1 and 20"):
        retrieval.retrieve("find an image", max_images=21)
