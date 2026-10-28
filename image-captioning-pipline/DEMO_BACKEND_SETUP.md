# Azure multimodal image retrieval demo backend

Copy the five service `.py` files into the Azure Functions project root.

Merge `function_app_image_ask_addition.py` into the existing `function_app.py`; it is intentionally an addition rather than a replacement because the current file already contains working ingestion and revision routes.

Recommended Function App settings:

- `IMAGE_SEARCH_CANDIDATE_COUNT=20`
- `MAX_EXPANDED_IMAGES=100`
- `VISION_IMAGES_PER_BATCH=10`
- `MAX_VISION_BATCHES=10`
- `MAX_VISION_IMAGE_BYTES=20971520`
- `VISION_IMAGE_DETAIL=auto`
- `IMAGE_SAS_LIFETIME_MINUTES=15`

Existing required settings:

- `SEARCH_SERVICE_ENDPOINT`
- `SEARCH_ADMIN_KEY`
- `SEARCH_INDEX_NAME=image-captions`
- `FOUNDRY_PROJECT_ENDPOINT`
- `FOUNDRY_API_KEY`
- `FOUNDRY_MODEL_DEPLOYMENT`
- `FOUNDRY_EMBEDDING_DEPLOYMENT`
- `OUTPUT_STORAGE_CONNECTION_STRING`

Compile:

```bash
python -m py_compile image_retrieval_service.py image_blob_service.py image_vision_service.py image_answer_service.py image_ask_service.py function_app.py
```

Run `func start` and confirm `ask_images_http` appears.
