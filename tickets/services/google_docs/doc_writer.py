"""Write content to Google Docs via the API."""
import logging

from googleapiclient.errors import HttpError

from .auth import get_google_docs_service

logger = logging.getLogger(__name__)


class GoogleDocWriter:
    """Replaces the full content of a Google Doc."""

    def __init__(self, doc_id, service=None):
        self.doc_id = doc_id
        self.service = service or get_google_docs_service()

    def clear_document(self):
        """Delete all existing content from the document."""
        doc = self.service.documents().get(documentId=self.doc_id).execute()
        content = doc.get('body', {}).get('content', [])

        if len(content) <= 1:
            return  # Document is empty or has only the required trailing newline

        end_index = content[-1]['endIndex'] - 1
        if end_index <= 1:
            return

        requests = [{
            'deleteContentRange': {
                'range': {
                    'startIndex': 1,
                    'endIndex': end_index,
                }
            }
        }]

        self.service.documents().batchUpdate(
            documentId=self.doc_id,
            body={'requests': requests}
        ).execute()
        logger.info("Cleared document %s", self.doc_id)

    def write_content(self, text):
        """Replace all document content with the given text."""
        self.clear_document()

        requests = [{
            'insertText': {
                'location': {'index': 1},
                'text': text,
            }
        }]

        self.service.documents().batchUpdate(
            documentId=self.doc_id,
            body={'requests': requests}
        ).execute()
        logger.info("Wrote %d characters to document %s", len(text), self.doc_id)

    def update_document(self, text):
        """Full update cycle with error handling. Returns a result dict."""
        try:
            self.write_content(text)
            return {
                'success': True,
                'doc_id': self.doc_id,
                'characters_written': len(text),
            }
        except HttpError as e:
            logger.error("Google Docs API error: %s", e)
            return {
                'success': False,
                'error': str(e),
                'doc_id': self.doc_id,
            }
        except Exception as e:
            logger.error("Unexpected error writing to Google Doc: %s", e)
            return {
                'success': False,
                'error': str(e),
                'doc_id': self.doc_id,
            }
