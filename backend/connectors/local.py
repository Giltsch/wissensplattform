"""Local filesystem connector.

Wraps the existing ``ingest.iter_files`` walk so the CLI's directory-ingest
behaviour is preserved exactly: ``source`` is the bare filename (the
versioning key the prior CLI used), and ``content_type`` is empty so
``parse_bytes`` dispatches on the suffix — identical to the pre-Phase-3
``SourceItem(source=path.name, raw_bytes=..., content_type="")`` the CLI
constructed inline.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

from connectors import SourceItem


class LocalFileConnector:
    """Yield one ``SourceItem`` per supported file under ``root``.

    ``root`` may be a single file or a directory; the suffix allow-list and
    sorted traversal live in ``ingest.iter_files`` (reused, not
    reimplemented). File reads are offloaded to a worker thread so the walk
    doesn't block the event loop when driven from an async context.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    async def iter_items(self) -> AsyncIterator[SourceItem]:
        # Local import: ingest imports connectors (for the SourceItem
        # re-export), so importing it at module top would create a cycle.
        from ingest import iter_files

        for path in iter_files(self._root):
            raw = await asyncio.to_thread(path.read_bytes)
            yield SourceItem(
                source=path.name,
                raw_bytes=raw,
                content_type="",  # local files dispatch by suffix
            )
