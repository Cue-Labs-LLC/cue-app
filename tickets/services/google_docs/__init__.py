"""
Google Docs event knowledge base generator.

Generates and updates a Google Doc with upcoming event information
for use as an AI agent knowledge base.
"""
from .auth import get_google_docs_service
from .event_formatter import EventDocFormatter
from .doc_writer import GoogleDocWriter

__all__ = [
    'get_google_docs_service',
    'EventDocFormatter',
    'GoogleDocWriter',
]
