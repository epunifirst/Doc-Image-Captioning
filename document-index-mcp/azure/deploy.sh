#!/usr/bin/env bash
set -euo pipefail

require_value() {
    if [[ -z "$2" ]]; then
        printf 'Error: %s must be set.\n' "$1" >&2
        exit 1
    fi
}

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        printf 'Error: required command not found: %s\n' "$1" >&2
        exit 1
    fi
}

require_command az

require_value AZURE_SUBSCRIPTION "${AZURE_SUBSCRIPTION:-}"
require_value AZURE_RESOURCE_GROUP "${AZURE_RESOURCE_GROUP:-}"
require_value AZURE_CONTAINER_REGISTRY "${AZURE_CONTAINER_REGISTRY:-}"
require_value MCP_API_KEY "${MCP_API_KEY:-}"
if [[ ${#MCP_API_KEY} -lt 32 ]]; then
    printf 'Error: MCP_API_KEY must be at least 32 bytes.\n' >&2
    exit 1
fi
require_value SEARCH_SERVICE_ENDPOINT "${SEARCH_SERVICE_ENDPOINT:-}"
require_value SEARCH_API_KEY "${SEARCH_API_KEY:-}"
require_value FOUNDRY_PROJECT_ENDPOINT "${FOUNDRY_PROJECT_ENDPOINT:-}"
require_value FOUNDRY_API_KEY "${FOUNDRY_API_KEY:-}"
require_value FOUNDRY_EMBEDDING_DEPLOYMENT "${FOUNDRY_EMBEDDING_DEPLOYMENT:-}"
require_value BLOB_ACCOUNT_NAME "${BLOB_ACCOUNT_NAME:-}"
require_value BLOB_ACCOUNT_URL "${BLOB_ACCOUNT_URL:-}"
require_value BLOB_ACCOUNT_KEY "${BLOB_ACCOUNT_KEY:-}"
require_value SOURCE_DOCUMENT_ACCOUNT_NAME "${SOURCE_DOCUMENT_ACCOUNT_NAME:-}"
require_value SOURCE_DOCUMENT_ACCOUNT_URL "${SOURCE_DOCUMENT_ACCOUNT_URL:-}"
require_value SOURCE_DOCUMENT_ACCOUNT_KEY "${SOURCE_DOCUMENT_ACCOUNT_KEY:-}"
require_value ACR_PULL_USERNAME "${ACR_PULL_USERNAME:-}"
require_value ACR_PULL_PASSWORD "${ACR_PULL_PASSWORD:-}"

APP_NAME="${APP_NAME:-doc-image-mcp}"
IMAGE_REPOSITORY="${IMAGE_REPOSITORY:-document-index-mcp}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
SEARCH_INDEX_NAME="${SEARCH_INDEX_NAME:-image-captions}"
IMAGE_CONTAINER="${IMAGE_CONTAINER:-output-images}"
SOURCE_DOCUMENT_CONTAINER="${SOURCE_DOCUMENT_CONTAINER:-incoming}"
MAX_RETURNED_IMAGES="${MAX_RETURNED_IMAGES:-20}"

ACR_LOGIN_SERVER=""
FULL_IMAGE_NAME=""

secret_args=(
    "mcp-api-key=${MCP_API_KEY}"
    "search-api-key=${SEARCH_API_KEY}"
    "foundry-api-key=${FOUNDRY_API_KEY}"
    "blob-account-key=${BLOB_ACCOUNT_KEY}"
    "source-document-account-key=${SOURCE_DOCUMENT_ACCOUNT_KEY}"
)

env_args=(
    "MCP_API_KEY=secretref:mcp-api-key"
    "SEARCH_SERVICE_ENDPOINT=${SEARCH_SERVICE_ENDPOINT}"
    "SEARCH_INDEX_NAME=${SEARCH_INDEX_NAME}"
    "SEARCH_API_KEY=secretref:search-api-key"
    "FOUNDRY_PROJECT_ENDPOINT=${FOUNDRY_PROJECT_ENDPOINT}"
    "FOUNDRY_API_KEY=secretref:foundry-api-key"
    "FOUNDRY_EMBEDDING_DEPLOYMENT=${FOUNDRY_EMBEDDING_DEPLOYMENT}"
    "BLOB_ACCOUNT_NAME=${BLOB_ACCOUNT_NAME}"
    "BLOB_ACCOUNT_URL=${BLOB_ACCOUNT_URL}"
    "BLOB_ACCOUNT_KEY=secretref:blob-account-key"
    "IMAGE_CONTAINER=${IMAGE_CONTAINER}"
    "SOURCE_DOCUMENT_ACCOUNT_NAME=${SOURCE_DOCUMENT_ACCOUNT_NAME}"
    "SOURCE_DOCUMENT_ACCOUNT_URL=${SOURCE_DOCUMENT_ACCOUNT_URL}"
    "SOURCE_DOCUMENT_ACCOUNT_KEY=secretref:source-document-account-key"
    "SOURCE_DOCUMENT_CONTAINER=${SOURCE_DOCUMENT_CONTAINER}"
    "MAX_RETURNED_IMAGES=${MAX_RETURNED_IMAGES}"
)

az account set --subscription "$AZURE_SUBSCRIPTION"
ACR_LOGIN_SERVER="$(az acr show \
    --name "$AZURE_CONTAINER_REGISTRY" \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --query loginServer \
    --output tsv)"
require_value ACR_LOGIN_SERVER "$ACR_LOGIN_SERVER"
FULL_IMAGE_NAME="${ACR_LOGIN_SERVER}/${IMAGE_REPOSITORY}:${IMAGE_TAG}"

if ! az containerapp show \
    --name "$APP_NAME" \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --output none >/dev/null 2>&1; then
    printf 'Error: Container App "%s" was not found in resource group "%s". Create it in the portal first or set APP_NAME and AZURE_RESOURCE_GROUP correctly.\n' \
        "$APP_NAME" "$AZURE_RESOURCE_GROUP" >&2
    exit 1
fi

az containerapp secret set \
    --name "$APP_NAME" \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --secrets "${secret_args[@]}"
az containerapp registry set \
    --name "$APP_NAME" \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --server "$ACR_LOGIN_SERVER" \
    --username "$ACR_PULL_USERNAME" \
    --password "$ACR_PULL_PASSWORD"
az containerapp update \
    --name "$APP_NAME" \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --image "$FULL_IMAGE_NAME" \
    --set-env-vars "${env_args[@]}"

az containerapp show \
    --name "$APP_NAME" \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --query properties.configuration.ingress.fqdn \
    --output tsv
