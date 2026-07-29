# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""ops.py — the five operators behind the panel.

`showcade.send` is the interesting one, and its shape is dictated by one
constraint: **`urllib` is synchronous and Blender's UI thread must not block on
a socket.** So the export runs on the main thread (only it may touch `bpy`
data), the resulting *bytes plus config strings* are handed to a `worker.SendJob`,
and a modal timer drains it — every non-timer event is passed through, so the
viewport stays live for the whole upload.

`execute()` is the same work run to completion; it exists for scripts, for the
save handler (a handler cannot own a modal operator) and because it is the only
path testable without a window. `invoke()` is the interactive one.

Every decision that does not need `bpy` — which objects to export, whether the
size looks wrong, what the result line says — lives in `policy.py`, so the parts
worth testing are tested without Blender.
"""

from __future__ import annotations

import logging

import bpy  # type: ignore[import-not-found]  # provided by Blender

from . import deviceauth, export_glb, exporter, policy, prefs, send, transport, worker

log = logging.getLogger(__name__)

__all__ = ["register", "unregister"]

_TIMER_HZ = 0.1


# ── shared plumbing ──────────────────────────────────────────────────────


class _Aborted(RuntimeError):
    """A pre-flight failure with an operator-facing message."""


def _require_prefs(context):
    settings = prefs.get_prefs(context)
    if settings is None:  # pragma: no cover - only if the addon is half-registered
        raise _Aborted("the addon preferences are unavailable — re-enable the addon")
    if not settings.endpoint.strip():
        raise _Aborted("no endpoint set — open Preferences ▸ Add-ons ▸ Showcade Bridge")
    return settings


def source_objects(context, scope: str):
    """The objects a resolved scope actually covers (for the size warning)."""
    if scope == "SELECTION":
        return list(context.selected_objects or ())
    if scope == "COLLECTION":
        collection = context.collection
        return list(collection.all_objects) if collection else []
    return list(context.scene.objects)


#: Object types that actually contribute geometry to the GLB. Everything else is
#: either excluded by EXPORT_KWARGS (export_lights/export_cameras are False) or
#: has no surface to export. Kept here, next to the measurement, because the
#: measurement is the only thing that cares.
GEOMETRY_TYPES = frozenset({"MESH", "CURVE", "SURFACE", "META", "FONT", "GPENCIL", "GREASEPENCIL"})


def contributes_geometry(obj) -> bool:
    """Would this object put anything in the exported GLB?

    A light or a camera has a ZERO-SIZE bound_box but a real world position, so
    including it in a union AABB moves the box out to wherever it happens to sit
    without adding anything that ships. On Blender's default startup scene —
    2 m Cube, camera at (7.4, -6.9, 5.0), light at (4.1, 5.9, 5.9) — that turns a
    2 m measurement into 12.8 m and fires the "scene-sized, not prop-sized"
    warning on the most ordinary send there is. Which is showcade-x9r7's own
    failure mode inverted: it was "never warns when it should".
    """
    kind = getattr(obj, "type", None)
    return kind is None or kind in GEOMETRY_TYPES


def world_corners(obj):
    """An object's 8 bounding-box corners in WORLD space.

    `bound_box` is local; `matrix_world` is what puts it where the user sees it,
    parents and rotation included. Multiplied here by hand (rather than with
    `mathutils`) only because it keeps the arithmetic identical to what the
    `bpy`-free `policy.bounds_max_dimension_m` consumes.
    """
    box = getattr(obj, "bound_box", None)
    matrix = getattr(obj, "matrix_world", None)
    if box is None or matrix is None:
        return ()
    rows = [[float(matrix[row][column]) for column in range(4)] for row in range(4)]
    corners = []
    for corner in box:
        x, y, z = (float(corner[0]), float(corner[1]), float(corner[2]))
        corners.append(
            tuple(rows[row][0] * x + rows[row][1] * y + rows[row][2] * z + rows[row][3] for row in range(3))
        )
    return tuple(corners)


#: Memo for the panel's per-redraw call. Keyed on a CHEAP signature (name +
#: world translation + local dimensions per contributing object); the expensive
#: part is the 8-corner transform, so recomputing only when that signature moves
#: turns a per-redraw O(8N) into a per-redraw O(N) attribute read.
_bounds_memo: dict[str, object] = {"key": None, "value": 0.0}


def _memo_key(objects, scale_length: float):
    """Cheap enough to build on every redraw; specific enough to notice a move.

    Reads only attributes Blender already has resolved — no matrix arithmetic.
    Translation comes from matrix_world's last COLUMN (index [row][3]), which is
    the object's world position, so a parent transform still moves the key.
    """
    parts = []
    for obj in objects:
        if not contributes_geometry(obj):
            continue
        matrix = getattr(obj, "matrix_world", None)
        where = tuple(float(matrix[row][3]) for row in range(3)) if matrix is not None else ()
        parts.append((getattr(obj, "name", ""), where, tuple(getattr(obj, "dimensions", ()) or ())))
    return (tuple(parts), float(scale_length))


def max_dimension_m(objects, scale_length: float) -> float:
    """Longest edge of the bounding box around ALL of `objects`, in METRES.

    The union, not the per-object maximum: a selection of many small objects
    spread over 30 m is exactly the "I selected the whole level" case the >5 m
    warning exists for, and measuring each object's own box never saw it (bd
    showcade-x9r7). The measuring itself lives in `policy` so it is testable
    without Blender.
    """
    # Called from SHOWCADE_PT_panel.draw(), i.e. on EVERY N-panel redraw, so the
    # 8-corner world transform must not run unless something actually moved.
    key = _memo_key(objects, scale_length)
    if _bounds_memo["key"] == key:
        return float(_bounds_memo["value"])  # type: ignore[arg-type]
    points = []
    for obj in objects:
        if not contributes_geometry(obj):
            continue
        points.extend(world_corners(obj))
    value = policy.bounds_max_dimension_m(points, scale_length)
    _bounds_memo["key"] = key
    _bounds_memo["value"] = value
    return value


def _plan_for(context):
    scene = context.scene
    collection = context.collection
    return policy.resolve_source(
        scene.showcade.source,
        selected_count=len(context.selected_objects or ()),
        collection_name=collection.name if collection else None,
        collection_count=len(collection.all_objects) if collection else 0,
        scene_count=len(scene.objects),
    )


class _JobOperator:
    """Modal drain for a `worker.SendJob`. Subclasses build and report it."""

    _job = None
    _timer = None
    #: The ShowcadeLink this job is FOR, captured when the job was built.
    #
    # A send is multi-second and modal() deliberately PASS_THROUGHs every
    # non-timer event so the viewport stays live — so the user can (and will)
    # click a different collection in the Outliner mid-upload. Re-reading
    # `context.collection` when the result lands would then write PropA's asset
    # id and generation window onto PropB, and PropB's next send would GC-delete
    # the asset it is actually bound to (bd showcade-q42r.5 review). Capture the
    # link once and report against THAT.
    _link = None

    # -- to implement --
    def build_job(self, context):  # pragma: no cover - interface
        raise NotImplementedError

    def report_outcome(self, context, outcome) -> set:  # pragma: no cover - interface
        raise NotImplementedError

    # -- shared --
    def execute(self, context) -> set:
        try:
            job = self.build_job(context)
        except (_Aborted, policy.SourceError, ValueError, RuntimeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        outcome = job.start().wait()
        return self.report_outcome(context, outcome)

    def invoke(self, context, event) -> set:
        try:
            self._job = self.build_job(context)
        except (_Aborted, policy.SourceError, ValueError, RuntimeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self._job.start()
        manager = context.window_manager
        self._timer = manager.event_timer_add(_TIMER_HZ, window=context.window)
        manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event) -> set:
        if event.type != "TIMER":
            # The whole point of the modal: the user keeps orbiting, selecting
            # and typing while the socket is busy.
            return {"PASS_THROUGH"}
        outcome = self._job.poll() if self._job else None
        if outcome is None:
            return {"RUNNING_MODAL"}
        self._release_timer(context)
        return self.report_outcome(context, outcome)

    def _release_timer(self, context) -> None:
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None

    def cancel(self, context) -> None:
        self._release_timer(context)


def _redraw_sidebars(context) -> None:
    """Repaint the N-panel — a modal finishing does not tag anything itself."""
    for area in getattr(context.screen, "areas", ()) or ():
        if area.type == "VIEW_3D":
            area.tag_redraw()


# ── send ─────────────────────────────────────────────────────────────────


class SHOWCADE_OT_send(_JobOperator, bpy.types.Operator):
    bl_idname = "showcade.send"
    bl_label = "Send to Showcade"
    bl_description = "Export this collection to GLB and upload it to showcade's asset catalog"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context) -> bool:
        return context.mode == "OBJECT" and context.collection is not None

    def build_job(self, context):
        settings = _require_prefs(context)
        link = context.collection.showcade
        # Bind the outcome to THIS collection's link for the life of the job —
        # the active collection can change while the socket is busy.
        self._link = link
        # The collection's own name is the default asset name and a good one:
        # it is already stable, and dedupe is (sha256, name).
        name = (link.asset_name or context.collection.name).strip()
        if not name:
            raise _Aborted("give the asset a name before sending")
        link.asset_name = name

        plan = _plan_for(context)
        if plan.fell_back:
            # Never silent: the user asked for the selection and got something else.
            self.report({"INFO"}, f"nothing selected — sending {plan.label}")

        scale_warning = exporter.scale_length_warning(context.scene.unit_settings.scale_length)
        if scale_warning:
            self.report({"WARNING"}, scale_warning)
        size_warning = policy.size_warning(
            max_dimension_m(source_objects(context, plan.scope), context.scene.unit_settings.scale_length)
        )
        if size_warning:
            self.report({"WARNING"}, size_warning)

        if not link.link_id:
            # First send mints the identity; from here on every upload carries
            # blender:<id> and the browser can follow the bytes across re-exports.
            link.link_id = transport.new_link_id()

        # Main thread only: this is the one call that touches scene data.
        data = export_glb.export_glb_bytes(basename=name, source_kwargs=plan.kwargs)

        # From here down it is all plain values — nothing the worker touches can
        # reach `bpy`, which is the actual thread-safety contract.
        endpoint = settings.endpoint.strip()
        # Session-first (bd showcade-fmq8): a signed-in send is attributed to the user, so it
        # lands inside their quota and appears in their own ?mine listing. Falls back to the
        # static token for a bearer-era endpoint.
        token = transport.credential(settings.session_token, settings.token)
        timeout = float(settings.timeout_s)
        insecure = bool(settings.insecure_tls)
        link_id = link.link_id
        device = link.device.strip()
        gc_target = link.prev_asset_id if settings.gc_previous else ""

        def work():
            result = send.send_bytes(
                data,
                endpoint=endpoint,
                token=token,
                name=name,
                link_id=link_id,
                extra_tags=(f"device:{device}",) if device else (),
                timeout=timeout,
                insecure_tls=insecure,
            )
            # A DEDUPE IS NOT A NEW GENERATION, on either axis. status 200 means
            # the catalog returned the existing row, so policy.rotate_generations
            # (correctly) leaves the window alone — and GC'ing here would delete
            # `prev` anyway, collapsing the two-generation safety margin to one
            # and leaving prev_asset_id pointing at a deleted row. The next real
            # send would then try to delete that dangling id, fail, and never
            # reclaim the generation it should have (bd showcade-ldgg).
            deduped = result.status == 200
            if gc_target and not deduped and gc_target != result.asset_id:
                # Two generations back. Failures are logged, never fatal — a GC
                # miss costs disk, a raised exception would cost the upload.
                try:
                    transport.delete_asset(endpoint, token, gc_target, timeout=timeout, insecure_tls=insecure)
                except Exception as exc:
                    log.warning("showcade: could not delete old asset %s: %s", gc_target, exc)
            return result

        return worker.SendJob(work)

    def report_outcome(self, context, outcome) -> set:
        # NOT context.collection: see _JobOperator._link. Falling back to the
        # live collection would reintroduce the cross-collection write, so a
        # missing capture reports against nothing instead.
        link = self._link
        if outcome is None or not outcome.ok:
            message = outcome.error if outcome else "the send did not report a result"
            if link is not None:
                link.last_result = f"failed: {message}"
            self.report({"ERROR"}, message)
            _redraw_sidebars(context)
            return {"CANCELLED"}

        result = outcome.value
        line = policy.summarize_upload(
            link.asset_name if link else "",
            status=result.status,
            sent_bytes=result.sent_bytes,
            asset_id=result.asset_id,
        )
        if link is not None:
            # Rotate AFTER the send; `prev` is what the NEXT send garbage-
            # collects, and a dedupe must not advance it (see the policy note).
            link.last_asset_id, link.prev_asset_id = policy.rotate_generations(
                last=link.last_asset_id, prev=link.prev_asset_id, new=result.asset_id
            )
            link.last_sha256 = str(result.asset.get("sha256", ""))
            link.last_result = line
        self.report({"INFO"}, f"{line} ({outcome.elapsed_s:.1f}s)")
        _redraw_sidebars(context)
        return {"FINISHED"}


# ── connection test ──────────────────────────────────────────────────────


class SHOWCADE_OT_test_connection(_JobOperator, bpy.types.Operator):
    bl_idname = "showcade.test_connection"
    bl_label = "Test Connection"
    bl_description = "Check the endpoint is reachable and the token is accepted — writes nothing"
    bl_options = {"REGISTER"}

    def build_job(self, context):
        settings = _require_prefs(context)
        endpoint = settings.endpoint.strip()
        # The SAME credential a send would use — probing with the other one would report a
        # healthy connection for a send that then 401s.
        token = transport.credential(settings.session_token, settings.token)
        timeout, insecure = float(settings.timeout_s), bool(settings.insecure_tls)

        def work():
            return transport.probe(endpoint, token, timeout=timeout, insecure_tls=insecure)

        return worker.SendJob(work, name="showcade-probe")

    def report_outcome(self, context, outcome) -> set:
        result = outcome.value if outcome and outcome.ok else None
        message = result.message if result is not None else (outcome.error if outcome else "no result")
        ok = bool(result and result.ok)
        context.scene.showcade.status = message
        self.report({"INFO"} if ok else {"ERROR"}, message)
        _redraw_sidebars(context)
        return {"FINISHED"} if ok else {"CANCELLED"}


# ── account / device sign-in (bd showcade-fmq8) ──────────────────────────


class SHOWCADE_OT_sign_in(_JobOperator, bpy.types.Operator):
    """Sign in with the OAuth 2.0 device grant (RFC 8628).

    Blender has no browser and no cookie jar, so it cannot hold the session cookie a
    session-era deployment wants. Instead it asks for a short user code, sends the human to a
    real browser to approve it, and polls for the resulting session token.

    The POLL runs on the worker thread and can last minutes — as long as the human takes.
    That is exactly what `_JobOperator`'s modal drain is for: the viewport stays live because
    modal() passes every non-timer event through, and the worker never touches `bpy`.
    """

    bl_idname = "showcade.sign_in"
    bl_label = "Sign in"
    bl_description = (
        "Sign in to showcade in your browser, so sends are attributed to you. "
        "Needed for a deployment that verifies sessions instead of a shared upload token"
    )
    bl_options = {"REGISTER"}

    def build_job(self, context):
        settings = _require_prefs(context)
        auth_endpoint = settings.auth_endpoint.strip()
        if not auth_endpoint:
            raise ValueError("set an Auth endpoint in the addon preferences first")
        timeout, insecure = float(settings.timeout_s), bool(settings.insecure_tls)

        def work():
            pending = deviceauth.request_device_code(
                auth_endpoint, timeout=timeout, insecure_tls=insecure
            )
            # Open the browser from the WORKER, before the poll blocks: webbrowser is stdlib
            # and touches no bpy data. Best-effort — a headless box has no browser, and
            # failing the sign-in over that would be absurd when the code is on screen.
            try:
                import webbrowser

                webbrowser.open(pending.best_uri)
            except Exception:
                pass
            token = deviceauth.poll_for_token(
                pending, auth_endpoint, timeout=timeout, insecure_tls=insecure
            )
            return (pending, token)

        return worker.SendJob(work, name="showcade-signin")

    def report_outcome(self, context, outcome) -> set:
        if not (outcome and outcome.ok):
            message = outcome.error if outcome else "sign-in produced no result"
            context.scene.showcade.status = message
            self.report({"ERROR"}, message)
            _redraw_sidebars(context)
            return {"CANCELLED"}
        _pending, token = outcome.value
        settings = prefs.get_prefs(context)
        if settings is None:  # pragma: no cover - unregistered mid-flight
            self.report({"ERROR"}, "addon preferences unavailable")
            return {"CANCELLED"}
        # Preferences, never the .blend: props.py identity is per-collection and a .blend
        # gets shared, so a token stored there would be a credential leak.
        settings.session_token = token
        message = "signed in — sends are now attributed to you"
        context.scene.showcade.status = message
        self.report({"INFO"}, message)
        _redraw_sidebars(context)
        return {"FINISHED"}


class SHOWCADE_OT_sign_out(bpy.types.Operator):
    """Forget the stored session token. Local only — it does not revoke the session."""

    bl_idname = "showcade.sign_out"
    bl_label = "Sign out"
    bl_description = "Forget the stored session; sends fall back to the upload token"
    bl_options = {"REGISTER"}

    def execute(self, context) -> set:
        settings = prefs.get_prefs(context)
        if settings is None:  # pragma: no cover - unregistered mid-flight
            self.report({"ERROR"}, "addon preferences unavailable")
            return {"CANCELLED"}
        settings.session_token = ""
        context.scene.showcade.status = "signed out"
        self.report({"INFO"}, "signed out")
        _redraw_sidebars(context)
        return {"FINISHED"}


# ── link management ──────────────────────────────────────────────────────


class SHOWCADE_OT_new_link(bpy.types.Operator):
    bl_idname = "showcade.new_link"
    bl_label = "New Link"
    bl_description = "Mint a fresh blender:<id> identity for this collection (the next send uses it)"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context) -> bool:
        return context.collection is not None

    def execute(self, context) -> set:
        link = context.collection.showcade
        link.link_id = transport.new_link_id()
        # A new identity means the old generations belong to the old id; keeping
        # them would GC assets this link no longer owns.
        link.last_asset_id = link.prev_asset_id = link.last_sha256 = ""
        self.report({"INFO"}, f"new link {transport.link_tag(link.link_id)}")
        return {"FINISHED"}


class SHOWCADE_OT_unlink(bpy.types.Operator):
    bl_idname = "showcade.unlink"
    bl_label = "Unlink"
    bl_description = "Forget this collection's identity — sends still work, but showcade stops following them"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context) -> bool:
        return context.collection is not None and bool(context.collection.showcade.link_id)

    def execute(self, context) -> set:
        link = context.collection.showcade
        link.link_id = link.last_asset_id = link.prev_asset_id = link.last_sha256 = ""
        link.last_result = ""
        self.report({"INFO"}, "unlinked — uploads from here will no longer carry an identity tag")
        return {"FINISHED"}


class SHOWCADE_OT_copy_handle(bpy.types.Operator):
    bl_idname = "showcade.copy_handle"
    bl_label = "Copy Handle"
    bl_description = "Copy a showcade:model handle for the last upload — paste it into showcade to bind"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context) -> bool:
        return context.collection is not None and bool(context.collection.showcade.last_asset_id)

    def execute(self, context) -> set:
        link = context.collection.showcade
        try:
            handle = policy.clipboard_handle(
                asset_id=link.last_asset_id,
                sha256=link.last_sha256,
                name=link.asset_name,
                link_id=link.link_id,
            )
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        context.window_manager.clipboard = handle
        self.report({"INFO"}, "handle copied — paste it into showcade's object editor")
        return {"FINISHED"}


# ── save handler ─────────────────────────────────────────────────────────


@bpy.app.handlers.persistent
def _auto_send_on_save(*_args) -> None:
    """Opt-in re-send after a save. Silent unless it has something to send."""
    context = bpy.context
    settings = prefs.get_prefs(context)
    collection = getattr(context, "collection", None)
    if not settings or not settings.auto_send_on_save or collection is None:
        return
    link = collection.showcade
    if not link.link_id or not link.asset_name:
        # Only an already-linked, already-named collection auto-sends; saving an
        # unrelated .blend must never upload anything.
        return
    try:
        # EXEC_DEFAULT, not INVOKE: a handler cannot own a modal operator.
        bpy.ops.showcade.send("EXEC_DEFAULT")
    except RuntimeError as exc:  # pragma: no cover - operator poll/context failures
        log.warning("showcade: auto-send on save failed: %s", exc)


_CLASSES = (
    SHOWCADE_OT_send,
    SHOWCADE_OT_test_connection,
    SHOWCADE_OT_sign_in,
    SHOWCADE_OT_sign_out,
    SHOWCADE_OT_new_link,
    SHOWCADE_OT_unlink,
    SHOWCADE_OT_copy_handle,
)


def register() -> None:
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    if _auto_send_on_save not in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.append(_auto_send_on_save)


def unregister() -> None:
    if _auto_send_on_save in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.remove(_auto_send_on_save)
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
