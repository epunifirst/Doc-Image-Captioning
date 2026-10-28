from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_mcp_requires_configured_api_key(monkeypatch) -> None:
    monkeypatch.delenv("MCP_API_KEY", raising=False)

    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})

    assert response.status_code == 503
    assert "MCP authentication is not configured" in response.json()["detail"]


def test_mcp_rejects_missing_or_invalid_api_key(monkeypatch) -> None:
    valid_key = "k" * 32
    monkeypatch.setenv("MCP_API_KEY", valid_key)
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

    missing_key = client.post("/mcp", json=body)
    invalid_key = client.post(
        "/mcp", json=body, headers={"X-API-Key": "x" * 32}
    )

    assert missing_key.status_code == 401
    assert invalid_key.status_code == 401


def test_authenticated_client_can_initialize_and_list_tools(monkeypatch) -> None:
    valid_key = "k" * 32
    monkeypatch.setenv("MCP_API_KEY", valid_key)
    headers = {"X-API-Key": valid_key}

    initialized = client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
    )
    tools = client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )

    assert initialized.status_code == 200
    assert initialized.json()["result"]["serverInfo"]["name"] == "document-index-mcp"
    assert tools.status_code == 200
    advertised_tools = tools.json()["result"]["tools"]
    assert [tool["name"] for tool in advertised_tools] == [
        "retrieve_relevant_images"
    ]
    image_limit = advertised_tools[0]["inputSchema"]["properties"]["max_images"]
    assert image_limit["maximum"] == 20
    assert image_limit["default"] == 20


def test_authenticated_tool_call_returns_only_image_urls_and_metadata(
    monkeypatch,
) -> None:
    valid_key = "k" * 32
    monkeypatch.setenv("MCP_API_KEY", valid_key)

    class FakeRetrievalService:
        def retrieve(self, question, max_images):
            assert question == "show the machine"
            assert max_images == 20
            return {
                "metadata": {
                    "returned_count": 1,
                    "results": [
                        {
                            "image_id": "image-1",
                            "content_type": "image/png",
                            "blob_uri": "https://blob.example/image.png",
                            "sas_url": "https://blob.example/image.png?sig=example",
                            "image_url": "https://blob.example/image.png?sig=example",
                            "markdown_image": (
                                "![Caption](https://blob.example/image.png?sig=example)"
                            ),
                        },
                    ],
                },
            }

    monkeypatch.setattr(
        "main.get_image_retrieval_service", lambda: FakeRetrievalService()
    )
    response = client.post(
        "/mcp",
        headers={"X-API-Key": valid_key},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "retrieve_relevant_images",
                "arguments": {"question": "show the machine"},
            },
        },
    )

    content = response.json()["result"]["content"]
    assert response.status_code == 200
    assert content[0]["type"] == "text"
    assert '"image_id": "image-1"' in content[0]["text"]
    assert '"sas_url": "https://blob.example/image.png?sig=example"' in content[0]["text"]
    assert len(content) == 1


def test_authenticated_tool_call_accepts_multiple_images(monkeypatch) -> None:
    valid_key = "k" * 32
    monkeypatch.setenv("MCP_API_KEY", valid_key)

    class FakeRetrievalService:
        def retrieve(self, question, max_images):
            assert question == "show multiple images"
            assert max_images == 4
            return {
                "metadata": {"returned_count": 0, "results": []},
            }

    monkeypatch.setattr(
        "main.get_image_retrieval_service", lambda: FakeRetrievalService()
    )
    response = client.post(
        "/mcp",
        headers={"X-API-Key": valid_key},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "retrieve_relevant_images",
                "arguments": {
                    "question": "show multiple images",
                    "max_images": 4,
                },
            },
        },
    )

    result = response.json()["result"]
    assert response.status_code == 200
    assert result["content"][0]["type"] == "text"


def test_health_does_not_require_or_report_api_key(monkeypatch) -> None:
    monkeypatch.delenv("MCP_API_KEY", raising=False)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "name": "document-index-mcp"}
    assert "key" not in response.text.lower()


def test_mcp_rejects_short_configured_api_key(monkeypatch) -> None:
    monkeypatch.setenv("MCP_API_KEY", "short")

    response = client.post(
        "/mcp",
        headers={"X-API-Key": "short"},
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "MCP_API_KEY must be at least 32 bytes."


def test_notification_does_not_execute_tool(monkeypatch) -> None:
    monkeypatch.setenv("MCP_API_KEY", "k" * 32)

    def unexpected_tool_call():
        raise AssertionError("notifications must not execute tool calls")

    monkeypatch.setattr("main.get_image_retrieval_service", unexpected_tool_call)
    response = client.post(
        "/mcp",
        headers={"X-API-Key": "k" * 32},
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "retrieve_relevant_images",
                "arguments": {"question": "find a process"},
            },
        },
    )

    assert response.status_code == 202
    assert response.content == b""


def test_mcp_rejects_invalid_jsonrpc_id(monkeypatch) -> None:
    monkeypatch.setenv("MCP_API_KEY", "k" * 32)

    response = client.post(
        "/mcp",
        headers={"X-API-Key": "k" * 32},
        json={"jsonrpc": "2.0", "id": [], "method": "ping"},
    )

    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32600
