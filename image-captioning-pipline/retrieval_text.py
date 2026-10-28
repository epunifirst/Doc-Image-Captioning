def build_retrieval_text(
    source_filename: str,
    source_type: str,
    section_path: list[str],
    detected_caption: str | None,
    caption: str,
    entities: list[str],
    keywords: list[str],
    related_text: str,
    context_before: str,
    context_after: str,
) -> str:
    section = " > ".join(section_path)

    parts = [
        ("Document", source_filename),
        ("Content type", source_type),
        ("Section", section),
        ("Generated caption", caption),
        (
            "Detected source caption",
            detected_caption or "",
        ),
        (
            "Related instruction",
            related_text,
        ),
        (
            "Entities",
            ", ".join(entities),
        ),
        (
            "Keywords",
            ", ".join(keywords),
        ),
        (
            "Context before",
            context_before,
        ),
        (
            "Context after",
            context_after,
        ),
    ]

    lines = []

    for label, value in parts:
        if isinstance(value, str) and value.strip():
            lines.append(
                f"{label}: {value.strip()}"
            )

    return "\n".join(lines)