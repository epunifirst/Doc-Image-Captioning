# Document Intelligence Ingestion and Indexing

## Overview

This application uses **Azure AI Document Intelligence** as the document-understanding layer of an ingestion pipeline that converts PDFs and DOCX files into structured, searchable image records.

Document Intelligence does not perform the final image captioning, embedding generation, or search indexing. Its role is to analyze each source document and recover the structure needed by those downstream stages: paragraphs, titles, section headings, figures, tables, captions, page locations, bounding regions, spans, and reading order.

The ingestion pipeline then combines that structural output with image extraction, multimodal captioning, embedding generation, Blob Storage, and a search index. The result is not simply a collection of extracted images. Each indexed image carries enough document context and provenance to support semantic retrieval and later use in a RAG or agent workflow.

## Why Document Intelligence Matters

A PDF or DOCX file is visually organized for a person, but most search and indexing systems cannot directly infer the relationships among its text, figures, tables, headings, and page layout.

A basic image-extraction process could recover embedded image files, but it would lose critical meaning:

- The section in which an image appeared
- The nearby explanatory paragraphs
- A detected figure caption
- The image's page number and location
- Whether an image is a standalone figure or part of a table
- The document's reading order
- The relationship between a visual element and surrounding text

Document Intelligence provides this structural interpretation. In this project, that interpretation is what allows downstream models to create context-aware captions and what allows the search index to represent each image using both visual and textual evidence.

This matters for indexation because an image by itself contains limited searchable text. Document Intelligence supplies the semantic bridge between the source document and the searchable image record.

## Ingestion Architecture

```text
PDF or DOCX in Blob Storage
        |
        v
Azure Function / ingestion entry point
        |
        v
Azure AI Document Intelligence
prebuilt-layout + figure output
        |
        +--> paragraphs and reading order
        +--> titles and section headings
        +--> detected captions
        +--> figures and figure identifiers
        +--> tables and table cells
        +--> page numbers and bounding regions
        |
        v
Context construction in extract.py
        |
        +--> FigureContext for each figure or table
        +--> nearby paragraphs
        +--> section path
        +--> source location and type
        |
        v
Figure retrieval or table crop generation
        |
        v
Multimodal caption generation through Azure AI Foundry
        |
        +--> caption
        +--> entities
        +--> keywords
        +--> related text
        |
        v
Retrieval-text construction and embedding generation
        |
        v
Search-document projection and validation
        |
        +--> Azure AI Search index
        +--> image and metadata blobs
        +--> immutable source and revision metadata
```

## Role of Each Connected Resource

### Azure Blob Storage

Blob Storage supplies source documents and stores generated artifacts.

The ingestion design distinguishes among:

- The source document location
- The extracted image location
- The current metadata record
- Immutable caption-revision records

The source blob name is passed into document processing so the generated records can retain document provenance. Output image and metadata paths are deterministic and derived from stable identifiers.

### Azure Functions

The Function App is the intended ingestion host. A blob trigger receives a document placed in the configured `incoming` container, writes the incoming stream to a temporary local file, and passes that file to the document-processing pipeline.

Document Intelligence itself does not monitor Blob Storage. The Azure Function connects the storage event to the analysis workflow.

### Azure AI Document Intelligence

Document Intelligence analyzes the source file with the `prebuilt-layout` model. It returns structured document elements and produces identifiers that the application uses to retrieve cropped figure images.

Document Intelligence is the source of document structure and layout semantics used during ingestion.

### Azure AI Foundry and the multimodal model

The captioning component receives both:

- The extracted or rendered image
- The context created from Document Intelligence output

The model is asked to return structured JSON containing a caption, entities, keywords, and the related source text. Document Intelligence therefore improves caption generation indirectly by giving the multimodal model information about where and why the image appears.

### Embedding service

The application builds retrieval text from the generated caption, entities, keywords, detected caption, section path, related text, and surrounding document context. An embedding is generated from this combined text.

Document Intelligence affects embedding quality because much of the retrieval text originates from its layout and paragraph analysis.

### Azure AI Search

The final search document includes image identifiers, source provenance, document location, active caption data, retrieval text, vector data, lifecycle state, access-control fields, and relationships to sibling images.

Document Intelligence does not write to the index directly. Its output is transformed by the application into records suitable for search indexing.

## Current Implementation

### Configuration

The Document Intelligence client is created in `extract.py` from two environment variables:

```text
DOCINTEL_ENDPOINT
DOCINTEL_KEY
```

The client uses `AzureKeyCredential`:

```python
from azure.core.credentials import AzureKeyCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient


def get_client() -> DocumentIntelligenceClient:
    return DocumentIntelligenceClient(
        endpoint=os.environ["DOCINTEL_ENDPOINT"],
        credential=AzureKeyCredential(os.environ["DOCINTEL_KEY"]),
    )
```

### Document analysis

The file is submitted asynchronously to the `prebuilt-layout` model:

```python
poller = client.begin_analyze_document(
    "prebuilt-layout",
    body=f,
    content_type="application/octet-stream",
    output=["figures"],
)

result = poller.result()
operation_id = poller.details["operation_id"]
```

The analysis returns two values used by later ingestion stages:

- `result`: the structured analysis response
- `operation_id`: the result identifier used to retrieve figure crops

### Figure extraction

For a detected figure, the application retrieves the cropped image bytes from Document Intelligence:

```python
stream = client.get_analyze_result_figure(
    model_id=result.model_id,
    result_id=operation_id,
    figure_id=figure_id,
)

image_bytes = b"".join(stream)
```

This keeps figure extraction aligned with the figures identified by the layout analysis.

### Table extraction

Tables are read from `result.tables`. For each cell, the application preserves:

- Row index
- Column index
- Row span
- Column span
- Extracted cell content

For the current ingestion flow, a table becomes an image-like asset so that it can pass through the same captioning and indexing pipeline as a figure.

The preferred table-image path crops the table region from the original page. This preserves visual content embedded within the table. Azure bounding coordinates are scaled to the PDF page coordinate system before PyMuPDF renders the crop as PNG.

For DOCX table cropping, the code converts the DOCX file to a temporary PDF with LibreOffice before using PyMuPDF.

The code also contains a table renderer that can create a synthetic PNG from extracted cell content. This is useful when a normalized, text-focused table representation is preferred over a source-faithful crop.

### Reading order and spans

Document elements contain spans into the top-level analyzed content. The application uses the first span offset to place paragraphs, figures, and tables into a shared timeline:

```python
timeline.sort(key=lambda pair: _offset_of(pair[1]))
```

This shared reading-order timeline is what allows the ingestion pipeline to associate a visual element with the body text that appears before and after it.

### Titles, headings, and skipped roles

The implementation treats the following paragraph roles as structural headings:

```python
HEADING_ROLES = {"title", "sectionHeading"}
```

The following roles are excluded from body-context windows:

```python
SKIP_ROLES = {
    "pageHeader",
    "pageFooter",
    "pageNumber",
    "footnote",
}
```

The application maintains the current title and section heading while walking the document timeline. Each visual element receives a `section_path` constructed from those values.

Continuation labels such as `(continued)`, `cont.`, and `cont'd` are normalized when comparing headings. This reduces duplicate or fragmented heading paths across pages.

### Context windows

Each visual element receives nearby paragraphs in reading order. The current settings are:

```python
CONTEXT_PARAGRAPHS_BEFORE = 2
CONTEXT_PARAGRAPHS_AFTER = 2
```

The context builder stops forward collection when it reaches another heading, title, or skipped structural role. This avoids pulling unrelated content from the next section into the figure context.

### Figure and table de-duplication

Document Intelligence can report a figure whose bounding region lies inside a detected table. The implementation compares figure and table bounds and suppresses the standalone figure when the figure center is inside the table.

This avoids representing the same visual content twice, once as a figure and once as part of a table.

### `FigureContext`

The Document Intelligence layer normalizes figures and tables into a shared application object:

```python
@dataclass
class FigureContext:
    figure_id: str
    page_number: Optional[int]
    caption: Optional[str]
    section_path: list
    context_before: str
    context_after: str
    image_bytes: Optional[bytes]
    source_type: str
    table_cells: Optional[list[dict]]
    bounding_box: Optional[tuple[float, float, float, float]]
```

This abstraction decouples downstream captioning and indexing from the raw SDK response. Later stages do not need to understand Document Intelligence response classes. They consume a stable application-level representation.

## Data Produced for Indexation

Document Intelligence contributes directly or indirectly to the following indexed fields:

- Source filename
- Source blob path
- Page number
- Figure identifier
- Source type, such as `figure` or `table`
- Section path
- Detected caption
- Context before the visual
- Context after the visual
- Structured table cells
- Image bytes or a table crop

These fields are combined with downstream outputs:

- Generated caption
- Entities
- Keywords
- Related text
- Retrieval text
- Embedding vector
- Stable document, version, and image identifiers
- Blob paths for image and metadata storage
- Revision and lifecycle metadata
- Search state and access-control fields

The final index record is therefore a projection of the full ingestion process, not a direct serialization of the Document Intelligence result.

## Why the Combination Improves Search

Document Intelligence and the multimodal model solve different parts of the problem:

- Document Intelligence explains the **document structure and local textual context**.
- The multimodal model explains the **visual content of the extracted image**.
- The embedding model converts the combined textual representation into a vector.
- Azure AI Search stores both structured metadata and retrieval vectors.

If only OCR text were indexed, visual concepts that are present only in diagrams or screenshots could be lost.

If only image captions were indexed, section names, source instructions, and nearby terminology could be lost.

Combining both creates a richer retrieval record that can match queries against document language, visual content, structural location, entities, and semantically related concepts.

## Features Currently Used

The application currently uses these Document Intelligence capabilities:

- The `prebuilt-layout` model
- Asynchronous document analysis
- PDF and DOCX ingestion
- Paragraph extraction
- Reading-order spans
- Paragraph roles
- Titles and section headings
- Figure detection
- Figure captions
- Figure identifiers
- Figure image retrieval
- Table detection
- Structured table cells
- Bounding regions and polygons
- Page number, width, and height information
- Document model and result identifiers

## Features Available for Future Use

The following are extension opportunities, not current application behavior.

### Markdown-formatted layout output

The Layout API can return Markdown that preserves headings, paragraphs, tables, figures, selection marks, formulas, barcodes, and page-level elements. This could support document-level chunk construction or provide a normalized textual representation alongside the current figure-centric records.

### Sections from the analysis response

The SDK exposes extracted document sections. The current implementation derives a section path from title and `sectionHeading` paragraph roles. Native sections could be evaluated as an alternative for more complex heading hierarchies.

### Key-value pairs

Key-value extraction could enrich ingestion for forms, specification sheets, checklists, or documents where labels and values are more important than prose reading order.

### Selection marks

Selection marks could preserve checkbox and option state in forms or procedural documents.

### Formula extraction

Formula add-on capabilities could make equations searchable and could improve ingestion of technical reports, engineering documents, and scientific material.

### Barcode and QR-code extraction

Barcode and QR-code extraction could capture identifiers, asset references, URLs, or workflow data embedded in source documents.

### High-resolution analysis

High-resolution analysis could be evaluated for small text, dense diagrams, detailed drawings, or low-quality source material.

### Language and style information

Detected languages and text styles could support multilingual indexing, language-aware analyzers, heading-quality checks, or decisions about how content should be chunked.

### Confidence and warning handling

The analysis response can include warnings, and extracted elements may expose confidence information depending on the model and feature. The ingestion pipeline could persist these signals and route low-confidence content for review instead of treating all extraction results equally.

### Custom extraction models

Custom extraction models could be used when the organization needs domain-specific fields that the general Layout model does not represent directly.

### Custom classification models

A classifier could route different document categories to specialized ingestion paths before extraction. For example, manuals, reports, forms, and engineering documents could use different extraction or indexing strategies.

### Batch analysis

Batch document analysis could be evaluated for scheduled or high-volume ingestion rather than processing one source blob per function invocation.

### Microsoft Entra ID authentication

The current implementation uses an endpoint and API key. A future production implementation could evaluate managed identity and Microsoft Entra ID authentication to reduce direct secret handling.

## Operational Considerations

### Source formats

The application currently declares PDF and DOCX as supported source extensions. Document Intelligence can analyze those formats, but local table cropping for DOCX also depends on LibreOffice because PyMuPDF reads the temporary PDF representation.

### Asynchronous processing

Document analysis is asynchronous. The SDK poller waits for the operation to complete before the pipeline starts constructing contexts and retrieving figure crops.

### Temporary files

The Azure Function writes the incoming blob stream to a temporary local file. Document Intelligence reads that file as a binary stream. DOCX table cropping may create an additional temporary PDF.

### Cost and throughput

The current implementation performs one layout-analysis operation per source-document processing run. Reprocessing the same document will invoke Document Intelligence again unless analysis results are cached or reuse is added.

The pipeline also retrieves figure crops individually and invokes downstream captioning and embedding operations for each processed visual element.

### Failure boundaries

Useful failure boundaries include:

1. Source blob reception
2. Temporary-file creation
3. Document Intelligence analysis
4. Figure retrieval or table cropping
5. Context construction
6. Caption generation
7. Embedding generation
8. Search projection and validation
9. Search upload
10. Image and metadata persistence

These stages should be logged separately so extraction failures are distinguishable from captioning, embedding, indexing, or storage failures.

### Idempotency and provenance

The broader ingestion pipeline computes stable hashes and identifiers from the source location, content hash, extraction version, visual type, figure identifier, and page number. This preserves source provenance and supports deterministic storage paths.

Document Intelligence contributes the figure identifier, page number, source type, layout context, and extracted visual content used in those records.

## Limitations of the Current Implementation

- The heading hierarchy is limited to the current title and current section heading rather than an arbitrary nested outline.
- Context windows use fixed paragraph counts.
- Reading order does not itself establish semantic relevance; proximity is used as the working heuristic.
- Figures embedded in tables are filtered using geometric containment of the figure center.
- DOCX table cropping depends on LibreOffice being present in the runtime environment.
- Document Intelligence credentials are supplied through key-based environment variables.
- Analysis results are not cached for reuse.
- The pipeline does not currently persist the complete raw Document Intelligence response.
- The ingestion flow is optimized around figures and tables rather than general-purpose text chunk indexing.

## Environment Variables Relevant to This Flow

```text
DOCINTEL_ENDPOINT
DOCINTEL_KEY
FOUNDRY_PROJECT_ENDPOINT
FOUNDRY_API_KEY
FOUNDRY_MODEL_DEPLOYMENT
OUTPUT_STORAGE_CONNECTION_STRING
```

Additional embedding, search, and Function binding settings are required by their respective components but are outside the Document Intelligence client configuration itself.

## Primary Code Paths

```text
extract.py
  get_client()
  analyze_document()
  get_figure_image_bytes()
  get_table_image_bytes()
  build_figure_contexts()

caption.py
  generate_caption()

main.py
  process_document()
  build retrieval text
  generate embeddings
  build and upload search records
  persist images and metadata
```

## References

- [Document layout analysis](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/prebuilt/layout?view=doc-intel-4.0.0)
- [Analyze document API response](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/concept/analyze-document-response?view=doc-intel-4.0.0)
- [Azure AI Document Intelligence Python client library](https://learn.microsoft.com/en-us/python/api/overview/azure/ai-documentintelligence-readme?view=azure-python)
- [Document Intelligence documentation](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/?view=doc-intel-4.0.0)
- [Layout API Markdown output](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/concept/markdown-elements?view=doc-intel-4.0.0)

## Summary

Azure AI Document Intelligence is the ingestion pipeline's structural document-understanding component. It transforms a source document into ordered, localized, and typed content that downstream services can caption, embed, persist, and index.

Its primary value is context preservation. The application does not index an extracted image as an isolated file. It indexes the image as a visual element belonging to a specific document version, page, section, and surrounding body of text. That context is what makes the resulting image records useful for semantic search, RAG, and agent-driven retrieval.
