import os
import sys

# Make the package importable when running the tests without `pip install -e .`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def make_doc(name: str, base64: str = None, document_index: int = None) -> dict:
    """Build a minimal RenderDocumentDto-shaped dict for tests.

    Args:
        name: The document name.
        base64: Optional inline base64 content.
        document_index: Optional zero-based position in the render request's
            data array. Set it so batch tests can place documents out of
            order and assert index-based correlation.

    Returns:
        A dict matching the API's rendered-document shape.
    """
    doc = {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "documentName": name,
        "templateId": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
        "versionNumber": 1,
        "environment": "test",
        "fileSizeBytes": 1024,
        "pageCount": 1,
        "renderedAt": "2026-06-10T12:34:56",
        "renderDuration": 12.5,
        "viewUrl": "https://example.test/doc",
        "documentType": "Invoice",
    }
    if base64 is not None:
        doc["documentBase64"] = base64
    if document_index is not None:
        doc["documentIndex"] = document_index
    return doc
