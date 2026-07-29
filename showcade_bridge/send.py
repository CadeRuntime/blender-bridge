# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""send.py — compose "export the selection" with "upload it to the catalog".

Split in two on purpose:

* `send_bytes()` is **`bpy`-free** — tags + upload, so the whole send path is
  exercised by `test/` against a catalog on an ephemeral port.
* `send_selection()` is the `bpy` half: it exports on the calling (main) thread
  and then calls `send_bytes()`. The `export_glb` import is deliberately *inside*
  the function so this module stays importable without Blender.

The upload itself is synchronous (`urllib` has no async mode), so `ops.py` calls
`send_bytes()` — never `send_selection()` — from a `worker.SendJob`: it exports on
the main thread, hands the resulting **bytes plus config strings** to the worker,
and drains the outcome from a modal timer. The worker must never touch `bpy` data,
which is exactly why the bytes, not the scene, are what crosses that boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from . import transport
from .transport import UploadResult

__all__ = ["send_bytes", "send_selection"]


def send_bytes(
    data: bytes,
    *,
    endpoint: str = transport.DEFAULT_ENDPOINT,
    token: str,
    name: str,
    link_id: str | None = None,
    extra_tags: Sequence[str] = (),
    timeout: float = transport.DEFAULT_TIMEOUT_S,
    insecure_tls: bool = False,
) -> UploadResult:
    """Upload already-exported GLB bytes, stamped with the Blender tag set.

    `name` must be **stable across re-sends** — dedupe is `(sha256, name)`, so an
    unchanged re-export then costs one 200 and writes nothing. Do not put a
    timestamp or a version in it.
    """
    return transport.upload_model(
        endpoint,
        token,
        name=name,
        data=data,
        tags=transport.model_tags(link_id, extra_tags),
        timeout=timeout,
        insecure_tls=insecure_tls,
    )


def send_selection(
    *,
    endpoint: str = transport.DEFAULT_ENDPOINT,
    token: str,
    name: str,
    link_id: str | None = None,
    use_selection: bool = True,
    source_kwargs: Mapping[str, Any] | None = None,
    extra_tags: Sequence[str] = (),
    timeout: float = transport.DEFAULT_TIMEOUT_S,
    insecure_tls: bool = False,
) -> UploadResult:
    """Export the selection to GLB and upload it. Main thread only (touches `bpy`)."""
    from . import export_glb  # bpy lives here; keep this module import-safe without it

    data = export_glb.export_glb_bytes(
        use_selection=use_selection, basename=name, source_kwargs=source_kwargs
    )
    return send_bytes(
        data,
        endpoint=endpoint,
        token=token,
        name=name,
        link_id=link_id,
        extra_tags=extra_tags,
        timeout=timeout,
        insecure_tls=insecure_tls,
    )
