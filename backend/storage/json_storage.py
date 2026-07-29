"""Local JSON-file storage backend.

Stores the whole portfolio in a single JSON file on the user's machine.
Writes are atomic (write-to-temp + replace) so a crash mid-write can't
corrupt the existing data.
"""

import json
import os

from .base import StorageBackend


class JSONStorage(StorageBackend):
    def __init__(self, path: str):
        self.path = path

    def load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        with open(self.path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return {}
            return json.loads(content)

    def save(self, portfolio: dict) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(portfolio, f, indent=2, sort_keys=True)
        os.replace(tmp_path, self.path)  # atomic on same filesystem
