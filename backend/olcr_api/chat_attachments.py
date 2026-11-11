"""Normalize composer attachments at the single chat request boundary."""
from __future__ import annotations

from collections.abc import Mapping

from .working_folders import WorkingFolders


class ChatAttachments:
    """Resolve uploaded files and folders into one chat attachment shape.

    Regular files remain message-scoped. A folder also becomes the current
    conversation's working root so existing coding tools keep their behavior.
    """

    def __init__(self, db):
        self.folders = WorkingFolders(db)

    def prepare(self, conversation_id: str, attachment: Mapping | None):
        value = dict(attachment) if isinstance(attachment, Mapping) else None
        folder = None
        if value and value.get('kind') == 'folder':
            folder = self.folders.attach(conversation_id, value)
            value = {
                'kind': 'folder',
                'name': folder['name'],
                'folder_id': folder['id'],
                'mimeType': 'application/x-olcr-working-folder',
                'content': '',
                'file_count': folder['file_count'],
            }
        active_folder = self.folders.get(conversation_id)
        return value, active_folder
