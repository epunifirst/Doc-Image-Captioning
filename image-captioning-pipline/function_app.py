import logging
import tempfile
import os
from pathlib import Path
from image_ask_service import ask_images
import azure.functions as func
import json
from caption_update_service import update_caption
from revision_restore_service import restore_revision
from main import process_document
from revision_history_service import list_revisions
from revision_conflict import (
    RevisionConflictError,
)
app = func.FunctionApp()


@app.blob_trigger(
    arg_name="blob",
    path="incoming/{name}",
    connection="AzureWebJobsDocumentStorage",
    source=func.BlobSource.EVENT_GRID,
)
def document_trigger(blob: func.InputStream):

    logging.info(
        f"Processing: {blob.name}"
    )

    with tempfile.NamedTemporaryFile(
        delete=False,
        suffix=Path(blob.name).suffix
    ) as temp:
        temp.write(blob.read())
        local_path = temp.name

    process_document(
        file_path=Path(local_path),
        source_blob_name=blob.name,
    )

@app.route(
    route="captions/update",
    methods=["POST"],
    auth_level=func.AuthLevel.FUNCTION,
)
def update_caption_http(
    req: func.HttpRequest,
) -> func.HttpResponse:
    try:
        request_body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps(
                {
                    "error": (
                        "Request body must contain "
                        "valid JSON."
                    )
                }
            ),
            status_code=400,
            mimetype="application/json",
        )

    required_fields = [
        "document_id",
        "image_id",
        "caption",
        "change_reason",
        "expected_active_revision",
    ]

    missing_fields = [
        field_name
        for field_name in required_fields
        if request_body.get(field_name) is None
        or request_body.get(field_name) == ""
    ]

    if missing_fields:
        return func.HttpResponse(
            json.dumps(
                {
                    "error": (
                        "Missing required fields."
                    ),
                    "missing_fields":
                        missing_fields,
                }
            ),
            status_code=400,
            mimetype="application/json",
        )

    try:
        result = update_caption(
            document_id=request_body[
                "document_id"
            ],
            image_id=request_body[
                "image_id"
            ],
            caption=request_body[
                "caption"
            ],
            change_reason=request_body[
                "change_reason"
            ],
            entities=request_body.get(
                "entities"
            ),
            keywords=request_body.get(
                "keywords"
            ),
            related_text=request_body.get(
                "related_text"
            ),
            actor_object_id=request_body.get(
                "actor_object_id"
            ),
            actor_email=request_body.get(
                "actor_email"
            ),
            actor_display_name=request_body.get(
                "actor_display_name"
            ),
            expected_active_revision=request_body[
                "expected_active_revision"
            ],
        )

        return func.HttpResponse(
            json.dumps(
                result,
                indent=2,
                ensure_ascii=False,
            ),
            status_code=200,
            mimetype="application/json",
        )

    except FileNotFoundError as exc:
        logging.warning(
            "Caption update record not found: %s",
            exc,
        )

        return func.HttpResponse(
            json.dumps(
                {
                    "error": str(exc)
                }
            ),
            status_code=404,
            mimetype="application/json",
        )
    except RevisionConflictError as exc:
        logging.warning(
            "Caption update revision conflict: %s",
            exc,
        )

        return func.HttpResponse(
            json.dumps(
                {
                    "error": str(exc),
                    "expected_active_revision": (
                        exc.expected_active_revision
                    ),
                    "actual_active_revision": (
                        exc.actual_active_revision
                    ),
                }
            ),
            status_code=409,
            mimetype="application/json",
        )
    except (
        ValueError,
        FileExistsError,
    ) as exc:
        logging.warning(
            "Caption update rejected: %s",
            exc,
        )

        return func.HttpResponse(
            json.dumps(
                {
                    "error": str(exc)
                }
            ),
            status_code=400,
            mimetype="application/json",
        )

    except Exception:
        logging.exception(
            "Caption update failed."
        )

        return func.HttpResponse(
            json.dumps(
                {
                    "error": (
                        "Caption update failed. "
                        "Review the Function App logs "
                        "for details."
                    )
                }
            ),
            status_code=500,
            mimetype="application/json",
        )

@app.route(
    route="captions/revisions",
    methods=["GET"],
    auth_level=func.AuthLevel.FUNCTION,
)
def list_caption_revisions_http(
    req: func.HttpRequest,
) -> func.HttpResponse:
    document_id = req.params.get(
        "document_id",
        "",
    ).strip()

    image_id = req.params.get(
        "image_id",
        "",
    ).strip()

    missing_fields = []

    if not document_id:
        missing_fields.append(
            "document_id"
        )

    if not image_id:
        missing_fields.append(
            "image_id"
        )

    if missing_fields:
        return func.HttpResponse(
            json.dumps(
                {
                    "error": (
                        "Missing required query "
                        "parameters."
                    ),
                    "missing_fields":
                        missing_fields,
                }
            ),
            status_code=400,
            mimetype="application/json",
        )

    try:
        result = list_revisions(
            document_id=document_id,
            image_id=image_id,
        )

        return func.HttpResponse(
            json.dumps(
                result,
                indent=2,
                ensure_ascii=False,
            ),
            status_code=200,
            mimetype="application/json",
        )

    except FileNotFoundError as exc:
        logging.warning(
            "Revision history was not found: %s",
            exc,
        )

        return func.HttpResponse(
            json.dumps(
                {
                    "error": str(exc)
                }
            ),
            status_code=404,
            mimetype="application/json",
        )

    except ValueError as exc:
        logging.warning(
            "Revision history request was "
            "rejected: %s",
            exc,
        )

        return func.HttpResponse(
            json.dumps(
                {
                    "error": str(exc)
                }
            ),
            status_code=400,
            mimetype="application/json",
        )

    except Exception:
        logging.exception(
            "Revision history request failed."
        )

        return func.HttpResponse(
            json.dumps(
                {
                    "error": (
                        "Revision history request "
                        "failed. Review the Function "
                        "App logs for details."
                    )
                }
            ),
            status_code=500,
            mimetype="application/json",
        )

@app.route(
    route="captions/revisions/restore",
    methods=["POST"],
    auth_level=func.AuthLevel.FUNCTION,
)
def restore_caption_revision_http(
    req: func.HttpRequest,
) -> func.HttpResponse:
    try:
        request_body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps(
                {
                    "error": (
                        "Request body must contain "
                        "valid JSON."
                    )
                }
            ),
            status_code=400,
            mimetype="application/json",
        )

    required_fields = [
        "document_id",
        "image_id",
        "revision_number",
        "change_reason",
        "expected_active_revision",
    ]

    missing_fields = [
        field_name
        for field_name in required_fields
        if request_body.get(field_name) is None
        or request_body.get(field_name) == ""
    ]

    if missing_fields:
        return func.HttpResponse(
            json.dumps(
                {
                    "error": (
                        "Missing required fields."
                    ),
                    "missing_fields":
                        missing_fields,
                }
            ),
            status_code=400,
            mimetype="application/json",
        )

    try:
        result = restore_revision(
            document_id=request_body[
                "document_id"
            ],
            image_id=request_body[
                "image_id"
            ],
            revision_number=request_body[
                "revision_number"
            ],
            change_reason=request_body[
                "change_reason"
            ],
            expected_active_revision=request_body[
                "expected_active_revision"
            ],
            actor_object_id=request_body.get(
                "actor_object_id"
            ),
            actor_email=request_body.get(
                "actor_email"
            ),
            actor_display_name=request_body.get(
                "actor_display_name"
            ),
        )

        return func.HttpResponse(
            json.dumps(
                result,
                indent=2,
                ensure_ascii=False,
            ),
            status_code=200,
            mimetype="application/json",
        )

    except FileNotFoundError as exc:
        logging.warning(
            "Revision restore source was not "
            "found: %s",
            exc,
        )

        return func.HttpResponse(
            json.dumps(
                {
                    "error": str(exc)
                }
            ),
            status_code=404,
            mimetype="application/json",
        )
    except RevisionConflictError as exc:
        logging.warning(
            "Revision restore conflict: %s",
            exc,
        )

        return func.HttpResponse(
            json.dumps(
                {
                    "error": str(exc),
                    "expected_active_revision": (
                        exc.expected_active_revision
                    ),
                    "actual_active_revision": (
                        exc.actual_active_revision
                    ),
                }
            ),
            status_code=409,
            mimetype="application/json",
        )
    except (
        ValueError,
        FileExistsError,
    ) as exc:
        logging.warning(
            "Revision restore was rejected: %s",
            exc,
        )

        return func.HttpResponse(
            json.dumps(
                {
                    "error": str(exc)
                }
            ),
            status_code=400,
            mimetype="application/json",
        )

    except Exception:
        logging.exception(
            "Revision restore failed."
        )

        return func.HttpResponse(
            json.dumps(
                {
                    "error": (
                        "Revision restore failed. "
                        "Review the Function App logs "
                        "for details."
                    )
                }
            ),
            status_code=500,
            mimetype="application/json",
        )


@app.route(
    route="images/ask",
    methods=["POST"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def ask_images_http(
    req: func.HttpRequest,
) -> func.HttpResponse:
    try:
        request_body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"error": "Request body must contain valid JSON."}),
            status_code=400,
            mimetype="application/json",
        )

    question = request_body.get("question")
    if not isinstance(question, str) or not question.strip():
        return func.HttpResponse(
            json.dumps({"error": "question is required and must be a nonempty string."}),
            status_code=400,
            mimetype="application/json",
        )

    candidate_count = request_body.get("candidate_count")
    if candidate_count is not None:
        if isinstance(candidate_count, bool) or not isinstance(candidate_count, int) or candidate_count < 1:
            return func.HttpResponse(
                json.dumps({"error": "candidate_count must be a positive integer."}),
                status_code=400,
                mimetype="application/json",
            )

    try:
        result = ask_images(
            question=question,
            candidate_count=candidate_count,
        )
        return func.HttpResponse(
            json.dumps(result, indent=2, ensure_ascii=False),
            status_code=200,
            mimetype="application/json",
        )
    except ValueError as exc:
        logging.warning("Image question rejected: %s", exc)
        return func.HttpResponse(
            json.dumps({"error": str(exc)}),
            status_code=400,
            mimetype="application/json",
        )
    except Exception:
        logging.exception("Image question failed.")
        return func.HttpResponse(
            json.dumps({"error": "Image question failed. Review Function App logs for details."}),
            status_code=500,
            mimetype="application/json",
        )
