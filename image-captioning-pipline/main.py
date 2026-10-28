"""
main.py

Ties extract.py and caption.py together:
  1. For every PDF/DOCX in input_docs/
  2. Extract figures + context (extract.py)
  3. Pull each figure's cropped image bytes
  4. Caption each figure with its context (caption.py)
  5. Embed immutable source provenance into the PNG metadata.
  6. Store the active caption state in current.json.
  7. Store immutable caption revisions under revisions/.

Run with:  python main.py
"""
from azure.storage.blob import BlobServiceClient
import argparse
import os
import json
import hashlib
from io import BytesIO
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from search_client import upload_document
from retrieval_text import build_retrieval_text

from extract import (
    analyze_document,
    build_figure_contexts,
    get_figure_image_bytes,
    get_table_image_bytes,
)
from caption import generate_caption
from embeddings import generate_embedding
from search_projection import (
    build_search_document,
    validate_search_document,
)
SCHEMA_VERSION = "1.0"
EXTRACTION_VERSION = 1

INPUT_DIR = Path("input_docs")
IMAGE_DIR = Path("output/images")
METADATA_DIR = Path("output/metadata")

SUPPORTED_EXTENSIONS = {".pdf", ".docx"}

# The key under which the JSON blob is stored inside the PNG's text chunks.
# Any tool that reads PNG metadata (Pillow, exiftool, etc.) can pull this
# key back out later.
METADATA_CHUNK_KEY = "document_context"

OUTPUT_STORAGE_CONNECTION_STRING = os.environ[
    "OUTPUT_STORAGE_CONNECTION_STRING"
]

INPUT_STORAGE_ACCOUNT = "docimagecaptionstore"
INPUT_CONTAINER = "incoming"

IMAGE_CONTAINER = "output-images"
METADATA_CONTAINER = "output-metadata"

def stable_id(*parts: object) -> str:
    normalized = "|".join(
        str(part).strip().casefold()
        for part in parts
        if part is not None
    )

    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

def compute_file_hash(file_path: Path) -> str:
    digest = hashlib.sha256()

    with file_path.open("rb") as source_file:
        for chunk in iter(
            lambda: source_file.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def build_image_provenance(record: dict) -> dict:
    source_document = record["source_document"]
    image = record["image"]
    location = record["location"]

    return {
        "schema_version": record["schema_version"],
        "image_id": record["image_id"],
        "document_id": record["document_id"],
        "source_version_id": record["source_version_id"],
        "extraction_version": record["extraction_version"],
        "source_document": {
            "filename": source_document["filename"],
            "storage_account": source_document["storage_account"],
            "container": source_document["container"],
            "blob_path": source_document["blob_path"],
            "page_number": source_document["page_number"],
            "content_hash": source_document["content_hash"],
        },
        "image": {
            "storage_account": image["storage_account"],
            "container": image["container"],
            "blob_path": image["blob_path"],
            "content_type": image["content_type"],
            "source_type": image["source_type"],
            "figure_id": location["figure_id"],
        },
    }


def validate_current_record(record: dict) -> None:
    required_top_level_fields = [
        "schema_version",
        "image_id",
        "document_id",
        "source_version_id",
        "extraction_version",
        "source_document",
        "image",
        "location",
        "active_revision",
        "retrieval",
        "relationships",
        "search_control",
        "access",
        "lifecycle",
        "storage",
        "_initial_revision",
    ]

    for field_name in required_top_level_fields:
        if field_name not in record:
            raise ValueError(
                f"Current record is missing required field: "
                f"{field_name}"
            )
    if not record["source_version_id"]:
        raise ValueError(
        "Current record has an empty source_version_id."
        )
    if not record["image_id"]:
        raise ValueError("Current record has an empty image_id.")

    if not record["document_id"]:
        raise ValueError("Current record has an empty document_id.")

    source_document = record["source_document"]

    for field_name in [
        "filename",
        "storage_account",
        "container",
        "blob_path",
    ]:
        if not source_document.get(field_name):
            raise ValueError(
                "Source document is missing required field: "
                f"{field_name}"
            )

    image = record["image"]

    for field_name in [
        "storage_account",
        "container",
        "blob_path",
        "content_type",
        "source_type",
    ]:
        if not image.get(field_name):
            raise ValueError(
                f"Image is missing required field: {field_name}"
            )

    active_revision = record["active_revision"]

    if active_revision.get("revision_number") != 1:
        raise ValueError(
            "Initial active revision must have revision number 1."
        )

    if not active_revision.get("revision_blob_path"):
        raise ValueError(
            "Active revision is missing its revision Blob path."
        )

    if not active_revision.get("caption", "").strip():
        raise ValueError(
            "Active revision caption cannot be empty."
        )

    retrieval = record["retrieval"]

    if not retrieval.get("text", "").strip():
        raise ValueError("Retrieval text cannot be empty.")

    storage = record["storage"]

    if not storage.get("current_metadata_blob_path"):
        raise ValueError(
            "Current metadata Blob path cannot be empty."
        )

    initial_revision = record["_initial_revision"]
    if (
        initial_revision.get("source_version_id")
        != record["source_version_id"]
    ):
        raise ValueError(
            "Initial revision source_version_id does not match "
            "the current record."
        )
    if initial_revision.get("image_id") != record["image_id"]:
        raise ValueError(
            "Initial revision image_id does not match "
            "the current record."
        )

    if initial_revision.get("document_id") != record["document_id"]:
        raise ValueError(
            "Initial revision document_id does not match "
            "the current record."
        )

    if initial_revision.get("revision_number") != 1:
        raise ValueError(
            "Initial revision must have revision number 1."
        )

    if (
        initial_revision.get("caption")
        != active_revision.get("caption")
    ):
        raise ValueError(
            "Initial revision caption does not match "
            "the active caption."
        )
    
def get_blob_client(
    container_name: str,
    blob_name: str,
):
    service_client = BlobServiceClient.from_connection_string(
        OUTPUT_STORAGE_CONNECTION_STRING
    )

    return service_client.get_blob_client(
        container=container_name,
        blob=blob_name,
    )

def blob_exists(
    container_name: str,
    blob_name: str,
) -> bool:
    blob_client = get_blob_client(
        container_name=container_name,
        blob_name=blob_name,
    )

    return blob_client.exists()

def upload_image_once(
    container_name: str,
    blob_name: str,
    data: bytes,
):
    blob_client = get_blob_client(
        container_name=container_name,
        blob_name=blob_name,
    )

    if blob_client.exists():
        print(
            f"Image already exists and will not be replaced: "
            f"{container_name}/{blob_name}"
        )
        return

    blob_client.upload_blob(
        data,
        overwrite=False,
    )


def upload_metadata(
    container_name: str,
    blob_name: str,
    data: bytes,
):
    blob_client = get_blob_client(
        container_name=container_name,
        blob_name=blob_name,
    )

    blob_client.upload_blob(
        data,
        overwrite=True,
    )

def upload_revision_once(
    container_name: str,
    blob_name: str,
    data: bytes,
) -> bool:
    blob_client = get_blob_client(
        container_name=container_name,
        blob_name=blob_name,
    )

    if blob_client.exists():
        print(
            "Revision already exists and will not be replaced: "
            f"{container_name}/{blob_name}"
        )
        return False

    blob_client.upload_blob(
        data,
        overwrite=False,
    )

    return True

def embed_metadata_into_png(image_bytes: bytes, metadata: dict) -> bytes:
    """
    Returns new PNG bytes with `metadata` (as a JSON string) embedded in a
    PNG text chunk. The pixel data is untouched -- this only adds metadata,
    same idea as writing EXIF tags onto a JPEG.
    """
    img = Image.open(BytesIO(image_bytes))

    png_info = PngInfo()
    png_info.add_text(METADATA_CHUNK_KEY, json.dumps(metadata))

    out = BytesIO()
    img.save(out, format="PNG", pnginfo=png_info)
    return out.getvalue()


def read_metadata_from_png(image_path: Path) -> dict | None:
    """Utility for later: pull the embedded metadata back out of a saved image."""
    img = Image.open(image_path)
    raw = img.text.get(METADATA_CHUNK_KEY)  # type: ignore[attr-defined]
    return json.loads(raw) if raw else None


def process_document(
    file_path: Path,
    source_blob_name: str | None = None,
    limit: int | None = None,
):
    source_content_hash = compute_file_hash(file_path)

    source_blob_path = source_blob_name or file_path.name

    container_prefix = f"{INPUT_CONTAINER}/"

    if source_blob_path.startswith(container_prefix):
        source_blob_path = source_blob_path.removeprefix(
            container_prefix
        )

    document_id = stable_id(
        INPUT_STORAGE_ACCOUNT,
        INPUT_CONTAINER,
        source_blob_path,
    )

    source_version_id = stable_id(
        document_id,
        source_content_hash,
    )

    source_filename = Path(source_blob_path).name
    print(f"\n=== Processing {source_filename} ===")

    result, operation_id = analyze_document(str(file_path))
    contexts = build_figure_contexts(result)

    print(f"Found {len(contexts)} figures")

    if limit is not None:
        contexts = contexts[:limit]
        print(f"Limiting to first {limit} figures for this run")

    # Group by section so we can record sibling_image_ids afterward
    records_by_section = defaultdict(list)
    all_records = []
    raw_image_bytes_by_id = {}  # kept out of the JSON-serializable record

    for ctx in contexts:
        if ctx.source_type == "table":
            image_bytes = get_table_image_bytes(str(file_path), result, ctx)
        elif ctx.image_bytes is not None:
            image_bytes = ctx.image_bytes
        else:
            image_bytes = get_figure_image_bytes(result, operation_id, ctx.figure_id)

        image_id = stable_id(
            document_id,
            source_version_id,
            EXTRACTION_VERSION,
            ctx.source_type,
            ctx.figure_id,
            ctx.page_number,
        )

        image_blob_path = f"{document_id}/{image_id}.png"

        current_metadata_blob_path = (
            f"{document_id}/{image_id}/current.json"
        )

        initial_revision_blob_path = (
            f"{document_id}/{image_id}/revisions/000001.json"
        )

        print(f"  - figure {ctx.figure_id} -> {image_id}.png "
              f"(section: {' > '.join(ctx.section_path) or '(none)'})")
        
        caption_data = generate_caption(
            image_bytes,
            ctx,
        )

        retrieval_text = build_retrieval_text(
            source_filename=source_filename,
            source_type=ctx.source_type,
            section_path=ctx.section_path,
            detected_caption=ctx.caption,
            caption=caption_data.get(
                "caption",
                "",
            ),
            entities=caption_data.get(
                "entities",
                [],
            ),
            keywords=caption_data.get(
                "keywords",
                [],
            ),
            related_text=caption_data.get(
                "related_text",
                "",
            ),
            context_before=ctx.context_before,
            context_after=ctx.context_after,
        )

        embedding_data = generate_embedding(
            retrieval_text
        )

        created_at = utc_now()

        initial_revision = {
            "schema_version": SCHEMA_VERSION,
            "image_id": image_id,
            "document_id": document_id,
            "source_version_id": source_version_id,
            "revision_number": 1,
            "revision_type": "initial_generation",
            "restored_from_revision": None,
            "caption": caption_data.get("caption", ""),
            "entities": caption_data.get("entities", []),
            "keywords": caption_data.get("keywords", []),
            "related_text": caption_data.get("related_text", ""),
            "change_reason": None,
            "created_at": created_at,
            "created_by": {
                "object_id": None,
                "email": None,
                "display_name": None,
                "actor_type": "system",
            },
        }

        current_record = {
            "schema_version": SCHEMA_VERSION,
            "image_id": image_id,
            "document_id": document_id,
            "source_version_id": source_version_id,
            "extraction_version": EXTRACTION_VERSION,

            "source_document": {
                "filename": source_filename,
                "storage_account": INPUT_STORAGE_ACCOUNT,
                "container": INPUT_CONTAINER,
                "blob_path": source_blob_path,
                "page_number": ctx.page_number,
                "etag": None,
                "content_hash": source_content_hash,
            },

            "image": {
                "storage_account": "docimagecaptionfuncstore",
                "container": IMAGE_CONTAINER,
                "blob_path": image_blob_path,
                "content_type": "image/png",
                "source_type": ctx.source_type,
            },

            "location": {
                "figure_id": ctx.figure_id,
                "section_path": ctx.section_path,
                "detected_caption": ctx.caption,
                "context_before": ctx.context_before,
                "context_after": ctx.context_after,
            },

            "active_revision": {
                "revision_number": initial_revision["revision_number"],
                "revision_blob_path": initial_revision_blob_path,
                "revision_type": initial_revision["revision_type"],
                "caption": initial_revision["caption"],
                "entities": initial_revision["entities"],
                "keywords": initial_revision["keywords"],
                "related_text": initial_revision["related_text"],
                "created_at": initial_revision["created_at"],
                "created_by": initial_revision["created_by"],
            },

            "retrieval": {
                "text": retrieval_text,
                "vector": embedding_data["vector"],
                "embedding_model": embedding_data["model"],
                "embedding_dimensions": embedding_data["dimensions"],
                "embedding_version": embedding_data["version"],
                "indexed_revision": None,
                "indexed_at": None,
                "index_status": "pending",
                "index_error": None,
            },

            "relationships": {
                "sibling_image_ids": [],
            },

            "search_control": {
                "is_ignored": False,
                "ignored_at": None,
                "ignored_by": None,
                "ignore_reason": None,
            },

            "access": {
                "allowed_user_ids": [],
                "allowed_group_ids": [],
            },

            "lifecycle": {
                "created_at": created_at,
                "updated_at": created_at,
                "updated_by": {
                    "object_id": None,
                    "email": None,
                    "display_name": None,
                    "actor_type": "system",
                },
            },

            "storage": {
                "current_metadata_blob_path": current_metadata_blob_path,
            },

            "_initial_revision": initial_revision,
        }

        section_key = tuple(ctx.section_path)

        validate_current_record(current_record)
        search_document = build_search_document(
            current_record
        )

        validate_search_document(
            search_document
        )

        try:
            upload_document(
                search_document
            )

            current_record["retrieval"][
                "indexed_revision"
            ] = current_record[
                "active_revision"
            ][
                "revision_number"
            ]

            current_record["retrieval"][
                "indexed_at"
            ] = utc_now()

            current_record["retrieval"][
                "index_status"
            ] = "indexed"

            current_record["retrieval"][
                "index_error"
            ] = None

        except Exception as exc:
            current_record["retrieval"][
                "index_status"
            ] = "failed"

            current_record["retrieval"][
                "index_error"
            ] = str(exc)

            raise

        records_by_section[section_key].append(
            current_record
        )

        all_records.append(current_record)

        raw_image_bytes_by_id[image_id] = image_bytes

    # Add sibling_image_ids: every other image in the same section.
    # Done after the loop above because we need every image in a section
    # collected first before we know who its siblings are.
    for section_key, records in records_by_section.items():
        ids_in_section = [
            record["image_id"]
            for record in records
        ]

        for record in records:
            record["relationships"]["sibling_image_ids"] = [
                sibling_id
                for sibling_id in ids_in_section
                if sibling_id != record["image_id"]
            ]
    # Now that records are complete (siblings included), embed each one
    # into its own image file and write it out.
    for record in all_records:
        image_id = record["image_id"]

        current_metadata_blob_path = record["storage"][
            "current_metadata_blob_path"
        ]

        current_record_exists = blob_exists(
            container_name=METADATA_CONTAINER,
            blob_name=current_metadata_blob_path,
        )

        if current_record_exists:
            print(
                "Existing caption history preserved: "
                f"{METADATA_CONTAINER}/"
                f"{current_metadata_blob_path}"
            )
            continue

        image_provenance = build_image_provenance(record)

        final_bytes = embed_metadata_into_png(
            raw_image_bytes_by_id[image_id],
            image_provenance,
        )

        upload_image_once(
            container_name=record["image"]["container"],
            blob_name=record["image"]["blob_path"],
            data=final_bytes,
        )

        initial_revision = record["_initial_revision"]
        initial_revision_blob_path = record[
            "active_revision"
        ]["revision_blob_path"]

        revision_created = upload_revision_once(
            container_name=METADATA_CONTAINER,
            blob_name=initial_revision_blob_path,
            data=json.dumps(
                initial_revision,
                indent=2,
                ensure_ascii=False,
            ).encode("utf-8"),
        )

        if not revision_created:
            print(
                "Initial revision already exists. "
                "Current metadata was not created."
            )
            continue

        persisted_current_record = {
            key: value
            for key, value in record.items()
            if key != "_initial_revision"
        }

        upload_metadata(
            container_name=METADATA_CONTAINER,
            blob_name=current_metadata_blob_path,
            data=json.dumps(
                persisted_current_record,
                indent=2,
                ensure_ascii=False,
            ).encode("utf-8"),
        )
    print(
        f"Done: {len(all_records)} images processed "
        f"for {source_filename}"
    )

def main():
    parser = argparse.ArgumentParser(description="Extract and caption images from documents.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N figures per document (useful while testing/debugging).",
    )
    args = parser.parse_args()

    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    METADATA_DIR.mkdir(parents=True, exist_ok=True)

    docs = [
        p for p in INPUT_DIR.iterdir()
        if p.suffix.lower() in SUPPORTED_EXTENSIONS
    ]

    if not docs:
        print(f"No PDF/DOCX files found in {INPUT_DIR}/ -- add one and re-run.")
        return

    for doc_path in docs:
        process_document(doc_path, limit=args.limit)


if __name__ == "__main__":
    main()
