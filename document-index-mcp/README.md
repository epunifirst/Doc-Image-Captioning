# Document Image Retrieval MCP

A small FastAPI service exposing one MCP-style JSON-RPC tool,
`retrieve_relevant_images`. It searches the Azure AI Search index built by the
image-captioning pipeline, then returns metadata with a short-lived read-only
Blob SAS URL. The client UI can render that URL as an image.

The service is retrieval-only. It does not create or revise captions, and it
does not decide which retrieved images the consuming chat displays.

## Retrieval behavior

- Hybrid text and vector search uses the pipeline's `retrieval_text` and
  `caption_vector` fields and the same Foundry embedding deployment.
- Ignored records are filtered out. Sibling images are expanded in ranked
  groups and deduplicated by `image_id`; semantic matches that are not siblings
  remain eligible as their own groups.
- `max_images` may reduce the per-call count. The service hard-caps the result
  count at 20 images.
- Results vary by query. `truncated` and `truncation_reasons` say when the
  result limit prevented returning all discovered candidates.
- Each result contains `content_type`, the unsigned `blob_uri`, and a
  15-minute read-only HTTPS `sas_url` (also exposed as `image_url`), plus a
  `markdown_image` value suitable for a Markdown-aware client UI.
- SAS URLs are bearer credentials: anyone who has one can read that image
  until it expires. Avoid copying them into shared logs or chats.
- The MCP service does not download image bytes. A UI that needs inline display
  should render `sas_url` with an image element or through an authorized image
  proxy, similar to AskAldo.
- The metadata is read-only; there is no caption editing or revision tool.

## Authentication and transport

Every request to `POST /mcp` must include:

```text
X-API-Key: <MCP_API_KEY>
```

The server rejects an unconfigured or shorter-than-32-byte client key.

This shared client key is separate from Azure AI Search, Foundry, Blob Storage,
and ACR credentials. Do not put real values in source control or logs. The
`GET /health` endpoint is unauthenticated for Container Apps health probing and
returns only a status and service name.

To rotate the client key, replace `MCP_API_KEY` in the protected deployment
environment, choose a new `IMAGE_TAG`, and rerun the deployment script. After
the updated revision is healthy, change the client's `X-API-Key` value. The
server accepts one key at a time, so coordinate the switch; requests using the
old key will be rejected after the new secret is active.

The HTTP endpoint implements the JSON-RPC/MCP methods used here directly with
FastAPI: `initialize`, `notifications/initialized`, `tools/list`, `tools/call`,
and `ping`. It is not an SSE transport. Verify that the intended MCP client
supports this HTTP request/response shape and the `X-API-Key` header. This MCP
returns image URLs, not native image blocks; the client is responsible for
rendering them.

## Required configuration

Copy `.env.example` to `.env` and fill the values after the Azure resources and
keys are available. The example intentionally leaves endpoints and credentials
blank. `.env` is ignored by Git.

| Variable | Purpose |
| --- | --- |
| `MCP_API_KEY` | Shared client key for `X-API-Key` |
| `SEARCH_SERVICE_ENDPOINT` | Azure AI Search endpoint |
| `SEARCH_INDEX_NAME` | Pipeline index; defaults to `image-captions` |
| `SEARCH_API_KEY` | Search query key (read-only) |
| `FOUNDRY_PROJECT_ENDPOINT` | Foundry project endpoint used by the pipeline |
| `FOUNDRY_API_KEY` | Key for the compatible embeddings API |
| `FOUNDRY_EMBEDDING_DEPLOYMENT` | Deployment that produced `caption_vector` |
| `BLOB_ACCOUNT_NAME` | Account name stored in indexed image records |
| `BLOB_ACCOUNT_URL` | Blob service URL |
| `BLOB_ACCOUNT_KEY` | Key-based Blob Storage credential |
| `IMAGE_CONTAINER` | Pipeline image container; defaults to `output-images` |
| `SOURCE_DOCUMENT_ACCOUNT_NAME` | Storage account recorded for original source documents |
| `SOURCE_DOCUMENT_ACCOUNT_URL` | Blob service URL for the original source documents |
| `SOURCE_DOCUMENT_ACCOUNT_KEY` | Key used only to create source-document read-only SAS URLs |
| `SOURCE_DOCUMENT_CONTAINER` | Original document container; defaults to `incoming` |
| `MAX_RETURNED_IMAGES` | Configurable cap from 1 to 20; defaults to 20 |

Use keys from the same resources and embedding deployment as the captioning
pipeline. The embedding dimensions must match its 1536-dimensional index.
Runtime data access uses keys, not Entra ID. The Azure CLI login used to deploy
the app is separate from runtime authentication. Each retrieval response
includes a `source_documents` list with one short-lived, read-only SAS link per
unique original document (without an unsigned Blob URL), and image results
reference that list by `source_document_id`. Source-document storage settings
are separate from the generated-image storage settings. Image and source
document SAS URLs override the response content type and disposition to
encourage browsers to display the media inline rather than download it.

## Local development and tests

Use Python 3.13 or another supported Python version:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
```

With `MCP_API_KEY` set, start the service:

```bash
uvicorn main:app --reload --port 8001
```

Run the local tests (they use fake Azure clients and do not need credentials):

```bash
pytest -q
```

List tools:

```bash
curl http://localhost:8001/mcp \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $MCP_API_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Call the retrieval tool:

```bash
curl http://localhost:8001/mcp \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $MCP_API_KEY" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"retrieve_relevant_images","arguments":{"question":"Show the steps in the production process","max_images":20}}}'
```

The local service returns explicit configuration errors until the required
Azure settings are populated; it does not return mock image results.

## Azure Container Apps deployment

The deploy script updates an existing Container App to use the image already
built and pushed to ACR. It stores the MCP, Search, Foundry, and Blob
credentials as Container Apps secrets and configures ACR pull credentials.
The runtime receives secret references rather than literal key values. The
script does not build images, require Docker, or create a Container App.

Prerequisites:

- Azure CLI logged in with permission to use the subscription, resource group,
- ACR, and Container App.
- An existing Container App in the specified resource group.
- The tagged image already pushed to the specified ACR repository.
- An ACR repository-scoped pull token with `content/read` and `metadata/read`
  on the image repository.
- All runtime endpoints, resource names, and keys configured in `.env`.

Load the local values in your shell and run:

```bash
set -a
source .env
set +a
bash azure/deploy.sh
```

Set `APP_NAME` to the existing Container App name and `IMAGE_REPOSITORY` to
the ACR repository name if they differ from their defaults. `AZURE_CONTAINER_APP_ENV`
is needed only when creating the app in the portal; this update script does not
use it. The script asks Azure CLI for the registry's login server, so it also
works when that hostname contains a suffix.

Do not run these commands in a shared shell or enable shell tracing (`set -x`):
the deployment CLI must receive credentials to store them as secrets. The
script does not print those values. It prints only the app's public hostname.
Choose a unique `IMAGE_TAG` instead of `latest` when repeatable rollback is
important.

The ingress is external HTTPS and protected at `/mcp` by the shared API key.
This is suitable for the planned test but is not per-user authorization.

## Manual Azure validation

After deployment, use the returned hostname and a privately-held API key:

1. `GET https://<hostname>/health` returns the minimal health response.
2. `POST https://<hostname>/mcp` with no key or a wrong `X-API-Key` returns
   `401`; the correct key allows `initialize`.
3. Authenticated `tools/list` returns only `retrieve_relevant_images`.
4. Try a narrow question and inspect the raw tool result. Confirm it contains
   metadata and a short-lived SAS URL; the client UI must render the URL to show
   the image inline.
5. Verify ignored records are absent and count/byte truncation is reported.
6. Confirm the intended MCP client supports the custom key header and that a
   client UI can load and render the returned SAS URL.
