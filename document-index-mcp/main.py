from __future__ import annotations

import hmac
import json
import logging
from typing import Any

from azure.core.exceptions import AzureError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from openai import OpenAIError
from starlette.concurrency import run_in_threadpool

from config import Settings
from image_retrieval import ImageRetrievalService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "document-index-mcp", "version": "0.1.0"}
TOOL_DEFINITIONS = [
    {
        "name": "retrieve_relevant_images",
        "description": (
            "Retrieve prompt-relevant images and their metadata from the indexed "
            "image-captioning pipeline. Each result includes a short-lived "
            "read-only SAS URL and Markdown image syntax for a client UI to "
            "render. The response also contains one read-only source-document "
            "SAS URL per unique original document. No image bytes are embedded "
            "in the tool response."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The user's image retrieval question.",
                },
                "max_images": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 20,
                    "description": (
                        "Optional maximum images to return. Subject to the "
                        "server-configured limit, up to 20."
                    ),
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    }
]

app = FastAPI(
    title="Document Image Retrieval MCP",
    version=SERVER_INFO["version"],
    docs_url=None,
    redoc_url=None,
)
_image_retrieval_service: ImageRetrievalService | None = None


def get_image_retrieval_service() -> ImageRetrievalService:
    global _image_retrieval_service
    if _image_retrieval_service is None:
        _image_retrieval_service = ImageRetrievalService(
            Settings.from_environment()
        )
    return _image_retrieval_service


def jsonrpc_error(
    request_id: Any, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def jsonrpc_success(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


async def process_request(body: Any) -> dict[str, Any] | None:
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        request_id = body.get("id") if isinstance(body, dict) else None
        return jsonrpc_error(request_id, -32600, "Invalid Request")

    request_id = body.get("id")
    if "id" not in body:
        return None
    if request_id is not None and (
        isinstance(request_id, bool)
        or not isinstance(request_id, (str, int, float))
    ):
        return jsonrpc_error(None, -32600, "Invalid Request")

    method = body.get("method")
    params = body.get("params", {})
    if not isinstance(method, str) or not isinstance(params, dict):
        return jsonrpc_error(request_id, -32600, "Invalid Request")

    if method == "notifications/initialized":
        return None
    if method == "initialize":
        result = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }
    elif method == "tools/list":
        result = {"tools": TOOL_DEFINITIONS}
    elif method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments", {})
        if name != "retrieve_relevant_images" or not isinstance(arguments, dict):
            return jsonrpc_error(request_id, -32602, "Invalid tool call parameters")
        try:
            max_images = arguments.get("max_images", 20)
            retrieval = await run_in_threadpool(
                get_image_retrieval_service().retrieve,
                arguments.get("question", ""),
                max_images,
            )
        except ValueError as exc:
            result = {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            }
        except (AzureError, OpenAIError):
            logger.exception("Image retrieval service request failed")
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": "Image retrieval failed while accessing an Azure service.",
                    }
                ],
                "isError": True,
            }
        else:
            metadata = retrieval["metadata"]

            content = [
                {
                    "type": "text",
                    "text": (
                        f"Retrieved {metadata['returned_count']} image(s) "
                        f"for query: {metadata['question']}"
                    ),
                }
            ]

            result = {
                "content": content,
                "structuredContent": metadata,
            }
    elif method == "ping":
        result = {}
    else:
        return jsonrpc_error(request_id, -32601, f"Method not found: {method}")

    return jsonrpc_success(request_id, result)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "name": SERVER_INFO["name"]}


@app.post("/mcp")
async def mcp_endpoint(request: Request) -> Response:
    try:
        settings = Settings.from_environment()
    except ValueError as exc:
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc)},
        )
    if not settings.mcp_api_key:
        return JSONResponse(
            status_code=503,
            content={"detail": "MCP authentication is not configured."},
        )
    if len(settings.mcp_api_key.encode("utf-8")) < 32:
        return JSONResponse(
            status_code=503,
            content={"detail": "MCP_API_KEY must be at least 32 bytes."},
        )

    supplied_key = request.headers.get("X-API-Key", "")
    if not hmac.compare_digest(
        supplied_key.encode("utf-8"),
        settings.mcp_api_key.encode("utf-8"),
    ):
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API key."},
        )

    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            status_code=400,
            content=jsonrpc_error(None, -32700, "Parse error"),
        )

    if isinstance(body, list):
        if not body:
            return JSONResponse(
                content=jsonrpc_error(None, -32600, "Invalid Request"),
            )
        responses = [await process_request(item) for item in body]
        responses = [item for item in responses if item is not None]
        if not responses:
            return Response(status_code=202)
        return JSONResponse(content=responses)

    response = await process_request(body)
    if response is None:
        return Response(status_code=202)
    return JSONResponse(content=response)
