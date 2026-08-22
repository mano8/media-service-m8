"""Business logic for exporting/importing a media collection (`U9`).

Only the ``manifest`` export path is implemented here. It reuses the same
scoping and filter helpers as the objects list (`_scoped_query`,
`_apply_filters`, `_apply_category_filter` from
:mod:`media_service.controllers.objects`) so a caller cannot export another
tenant's media by passing a foreign `category_id`/`owner_user_id` filter — the
export surface can never see rows the list surface would refuse. It also
reuses :meth:`media_service.controllers.category.CategoryController.get_category_tree`
for the tree, rather than a second count-rollup implementation.

Every query this step needs — resolving the filter (which may 404/403 on a
foreign `category_id`), loading the matching objects, and building the
category tree — runs **before** the response starts streaming
(:meth:`TransferController.export`). A refusal must come back as a normal
404/403, not as a corrupted body after a 200 has already gone out; only the
JSON *serialization* of already-fetched rows happens lazily, in
:func:`stream_manifest`, which never runs a query and so can never raise an
``HTTPException``.

The ``archive`` format is accepted by the request schema (both are locked as
export formats, 2026-07-04) but is not assembled by this step — building the
zip is async, off the request path, and is the next `U9` checkbox — so it is
refused with 501 rather than silently downgraded to a manifest.
"""

import uuid
from collections.abc import Iterator, Mapping, Sequence

from fastapi import HTTPException, status
from sqlmodel import Session, col

from fastapi_m8 import UserModel

from media_service.controllers.category import (
    CategoryController,
    category_refs_by_object,
)
from media_service.controllers.objects import (
    _apply_category_filter,
    _apply_filters,
    _scoped_query,
)
from media_service.db_models.categories import MediaObjectCategoryRef
from media_service.db_models.media_objects import MediaObject
from media_service.schemas.objects import ObjectListParams
from media_service.schemas.transfer import ExportRequest, ManifestObjectEntry


def _manifest_objects(
    session: Session, current_user: UserModel, filters: ObjectListParams
) -> list[MediaObject]:
    """Return every object matching ``filters`` in the caller's scope, ordered.

    Deliberately not paginated: a manifest exports the whole filtered
    collection, not one page of it, so ``filters.limit``/``filters.cursor`` are
    read by every other export-adjacent surface but not by this one. Ordered by
    id for a deterministic stream. A ``category_id`` outside the caller's scope
    raises here (via ``_apply_category_filter`` -> ``branch_category_ids``),
    the same 404/403 the objects list itself would answer.
    """
    statement = _apply_filters(_scoped_query(current_user, filters), filters)
    statement = _apply_category_filter(session, current_user, statement, filters)
    statement = statement.order_by(col(MediaObject.id))
    return list(session.exec(statement).all())


def _manifest_entry(obj: MediaObject, category_paths: list[str]) -> ManifestObjectEntry:
    """Project one ``MediaObject`` row into its manifest shape. No bytes."""
    return ManifestObjectEntry(
        id=obj.id,
        filename=obj.original_filename,
        category=obj.category,
        category_paths=category_paths,
        visibility=obj.visibility,
        size_bytes=obj.size_bytes,
        sha256=obj.sha256,
        mime_type=obj.mime_type,
        status=obj.status,
        scan_status=obj.scan_status,
        created_at=obj.created_at,
        updated_at=obj.updated_at,
    )


def _category_tree_json(session: Session, current_user: UserModel) -> str:
    """Serialize the caller's full category tree, independent of any filter.

    The manifest always carries the whole tree, not just the branches the
    exported objects happen to touch, so an import can recreate the tree
    structure (parents included) even when a filter narrowed the objects.
    Reuses :meth:`CategoryController.get_category_tree` — the same counts the
    ``GET /category/tree/`` endpoint serves — rather than a second rollup.
    """
    tree = CategoryController.get_category_tree(
        session=session, current_user=current_user
    )
    return "[" + ",".join(node.model_dump_json() for node in tree.data) + "]"


def stream_manifest(
    tree_json: str,
    objects: Sequence[MediaObject],
    refs_by_object: Mapping[uuid.UUID, list[MediaObjectCategoryRef]],
) -> Iterator[str]:
    """Yield a ``manifest`` export as a JSON document, one object at a time.

    Takes already-fetched rows, not a session: every query that could refuse
    the request has already run by the time this is called (see the module
    docstring), so this function only serializes and can never raise. Streamed
    rather than built as one Python string up front so response assembly cost
    stays flat per object instead of growing with the whole collection held in
    memory as one string at once.
    """
    yield '{"category_tree":' + tree_json + ',"objects":['
    first = True
    for obj in objects:
        paths = [ref.path for ref in refs_by_object.get(obj.id, [])]
        entry = _manifest_entry(obj, paths)
        prefix = "" if first else ","
        first = False
        yield prefix + entry.model_dump_json()
    yield "]}"


class TransferController:
    """Handle export/import of a caller's media collection."""

    @staticmethod
    def export(
        *,
        session: Session,
        current_user: UserModel,
        body: ExportRequest,
    ) -> Iterator[str]:
        """Resolve an export request and return its streamed body.

        Only ``format="manifest"`` is implemented; ``format="archive"`` is a
        locked, valid request shape whose assembly has not landed yet (501,
        not 422 — the request itself is well-formed). Every query — and thus
        every possible 404/403 from an out-of-scope filter — runs here, before
        the generator is handed to the response, so a refusal is a normal
        error response rather than a truncated 200 body.
        """
        if body.format == "archive":
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="Archive export is not yet implemented.",
            )
        filters = body.filters if body.filters is not None else ObjectListParams()
        objects = _manifest_objects(session, current_user, filters)
        refs_by_object = category_refs_by_object(
            session, current_user, [obj.id for obj in objects]
        )
        tree_json = _category_tree_json(session, current_user)
        return stream_manifest(tree_json, objects, refs_by_object)
