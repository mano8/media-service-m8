"""Business logic for importing a media collection (`U9`).

The inbound half of the transfer surface, and the mirror image of
:mod:`media_service.controllers.transfer`: that module projects rows this
service already owns into a document, this one reads such a document back —
uploaded by a caller, therefore **untrusted in every field**.

Three properties this module exists to hold:

* **The tree comes first, and is idempotent by scope + slug path.** Every
  category path the document names — the ones in ``category_tree`` and any a
  media row is filed into — is resolved through
  :func:`media_service.controllers.category.ensure_category_path` before a
  single object is touched, so a filing always has somewhere to land. A path
  that already exists in the importing scope is *reused*, never duplicated and
  never renamed, which is what makes importing the same document twice a no-op
  for the tree.

* **Imported verdicts are never trusted.** The parsed
  :class:`~media_service.schemas.transfer.ImportObjectEntry` has no
  ``status``/``scan_status`` fields at all, so there is nothing to trust: bytes
  arriving in an archive are re-driven through the *normal* upload pipeline —
  the same :func:`~media_service.controllers.uploads.stage_upload` quota and
  allowlist checks, the same
  :meth:`~media_service.controllers.uploads.UploadsController.complete_upload`
  size/magic-byte/SHA-256/content-type enforcement — and land ``UPLOADED`` +
  ``PENDING``, undownloadable until this service's own scanner clears them.
  Only the presigned-URL step is skipped, because the server already holds the
  bytes.

* **A row's failure is a row's failure.** Import is a batch: one refused object
  is reported in that row of :class:`~media_service.schemas.transfer.ImportReport`
  and the rest of the collection still lands. Only a refusal of the *document*
  — unreadable, or over one of the ``MEDIA_IMPORT_*`` ceilings — is a 4xx, and
  every one of those is checked before any write happens.

Correlation is by the source row id the export carries for exactly this
purpose: an object already present in the caller's own collection is re-filed
into the recreated categories rather than uploaded again, so a repeated import
converges instead of duplicating.
"""

import json
import logging
import tempfile
import uuid
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import IO, Any

from fastapi import HTTPException, status
from pydantic import ValidationError
from slugify import slugify
from sqlmodel import Session

from fastapi_m8 import UserModel

from media_service.controllers.category import ensure_category_path
from media_service.controllers.export_archive import FILES_PREFIX, MANIFEST_ENTRY
from media_service.controllers.uploads import (
    UploadsController,
    record_upload_session,
    stage_upload,
)
from media_service.core.category_tree import PATH_SEPARATOR
from media_service.core.config import settings
from media_service.core.validation import (
    is_allowed_declared_mime,
    max_size_for_category,
)
from media_service.db_models.categories import Category
from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectPublic,
    MediaObjectStatus,
    utcnow,
)
from media_service.schemas.transfer import (
    ImportCategoryNode,
    ImportFormat,
    ImportManifest,
    ImportObjectEntry,
    ImportObjectResult,
    ImportReport,
    ImportRowReason,
    ImportRowStatus,
)
from media_service.schemas.uploads import UploadCompleteRequest, UploadInitiateRequest
from media_service.storage.client import ObjectStorage

_logger = logging.getLogger(__name__)

#: Fallback filename for a manifest row that carries none. Matches the fallback
#: :func:`media_service.storage.keys._safe_filename` already applies, so an
#: unnamed object gets the same key it would have got through a normal upload.
_FALLBACK_FILENAME = "file"


def _reject(message: str) -> HTTPException:
    """Refuse the whole document. Always raised before anything is written."""
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=message
    )


# ── reading the upload ───────────────────────────────────────────────────────


def drain_to_tempfile(source: IO[bytes], *, limit: int) -> IO[bytes]:
    """Copy *source* into a rewound temporary file, refusing to exceed *limit*.

    The uploaded body is attacker-sized, so it is never read with a bare
    ``read()``: bytes are moved in fixed chunks and the copy is abandoned the
    moment it passes the ceiling. A temporary *file* rather than a buffer
    because the archive path needs random access into the zip central
    directory, and because neither format should cost memory proportional to
    what was uploaded.
    """
    chunk_size = settings.MEDIA_EXPORT_STREAM_CHUNK_SIZE
    handle = tempfile.TemporaryFile()
    copied = 0
    try:
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            copied += len(chunk)
            if copied > limit:
                raise _reject(f"Uploaded file exceeds the {limit}-byte import ceiling.")
            handle.write(chunk)
    except BaseException:
        handle.close()
        raise
    handle.seek(0)
    return handle


def parse_manifest_document(raw: bytes) -> ImportManifest:
    """Parse and bound an uploaded manifest before it becomes models.

    Three passes, in this order and for this reason:

    1. ``json.loads`` — which is also the depth guard, since CPython's decoder
       raises :class:`RecursionError` long before a hostile nesting could reach
       the recursive Pydantic model below.
    2. :func:`_assert_document_within_ceilings` — an *iterative* walk of the
       plain structure, so the object count, category count and tree depth are
       all refused before any recursive validation runs.
    3. Pydantic — by which point the document is known to be small and shallow.
    """
    try:
        document: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise _reject("Manifest is not readable JSON.") from exc
    _assert_document_within_ceilings(document)
    try:
        return ImportManifest.model_validate(document)
    except ValidationError as exc:
        raise _reject("Manifest does not match the expected export shape.") from exc


def _assert_document_within_ceilings(document: Any) -> None:
    """Refuse a manifest above any ``MEDIA_IMPORT_*`` ceiling.

    Walks the raw structure with an explicit stack rather than recursion: the
    point of a depth ceiling is to be enforceable on input that was built to
    exhaust the stack, which a recursive check could not survive doing.
    """
    if not isinstance(document, dict):
        raise _reject("Manifest must be a JSON object.")
    objects = document.get("objects") or []
    tree = document.get("category_tree") or []
    if not isinstance(objects, list) or not isinstance(tree, list):
        raise _reject("Manifest `objects` and `category_tree` must be arrays.")
    if len(objects) > settings.MEDIA_IMPORT_MAX_OBJECTS:
        raise _reject(
            f"Manifest carries {len(objects)} objects, above the "
            f"{settings.MEDIA_IMPORT_MAX_OBJECTS} import ceiling."
        )
    nodes = 0
    pending: list[tuple[Any, int]] = [(node, 1) for node in tree]
    while pending:
        node, depth = pending.pop()
        if not isinstance(node, dict):
            raise _reject("Every category tree node must be a JSON object.")
        nodes += 1
        if nodes > settings.MEDIA_IMPORT_MAX_CATEGORIES:
            raise _reject(
                f"Manifest carries more than {settings.MEDIA_IMPORT_MAX_CATEGORIES} "
                "categories."
            )
        if depth > settings.MEDIA_IMPORT_MAX_CATEGORY_DEPTH:
            raise _reject(
                "Category tree nests deeper than the "
                f"{settings.MEDIA_IMPORT_MAX_CATEGORY_DEPTH}-level import ceiling."
            )
        children = node.get("children") or []
        if not isinstance(children, list):
            raise _reject("Category tree `children` must be an array.")
        pending.extend((child, depth + 1) for child in children)


# ── recreating the tree ──────────────────────────────────────────────────────

#: One root-down chain of ``(name, slug)`` pairs naming a category path.
CategoryChain = tuple[tuple[str, str], ...]


def _slug_of(*candidates: str) -> str:
    """Return the first candidate that slugifies to something usable.

    Foreign slugs are re-slugified rather than taken at face value: the value
    reaches the database and the resolved path a client renders, and nothing
    guarantees another service's "slug" is one.
    """
    for candidate in candidates:
        slug = slugify(candidate or "")[:50]
        if slug:
            return slug
    raise _reject("A category has no usable name or slug.")


def tree_chains(nodes: Sequence[ImportCategoryNode]) -> Iterator[CategoryChain]:
    """Yield every node's root-down chain, parents strictly before children.

    Pre-order matters: :func:`ensure_category_path` reuses whatever already
    sits at a path, so visiting a parent first is what lets the child's chain
    resolve onto the row the parent's own visit just created — with the
    parent's real name, not the slug an implicit ancestor would fall back to.
    """
    stack: list[tuple[ImportCategoryNode, CategoryChain]] = [
        (node, ()) for node in reversed(nodes)
    ]
    while stack:
        node, prefix = stack.pop()
        chain = prefix + ((node.name, _slug_of(node.slug, node.name)),)
        yield chain
        stack.extend((child, chain) for child in reversed(node.children))


def path_chain(path: str) -> CategoryChain:
    """Turn a slash-joined slug path from a manifest row into a chain.

    A path segment carries no display name of its own, so the slug doubles as
    the name for any level the tree did not already declare — which is only
    ever reached by a manifest whose ``category_paths`` name a branch its own
    ``category_tree`` omitted.
    """
    segments = [segment for segment in path.split(PATH_SEPARATOR) if segment.strip()]
    if not segments:
        raise _reject("A category path is empty.")
    if len(segments) > settings.MEDIA_IMPORT_MAX_CATEGORY_DEPTH:
        raise _reject(
            "A category path nests deeper than the "
            f"{settings.MEDIA_IMPORT_MAX_CATEGORY_DEPTH}-level import ceiling."
        )
    return tuple((_slug_of(segment), _slug_of(segment)) for segment in segments)


def _chain_key(chain: CategoryChain) -> str:
    """The slug path a chain resolves to — the map key rows are looked up by."""
    return PATH_SEPARATOR.join(slug for _, slug in chain)


def _collect_chain(chains: dict[str, CategoryChain], chain: CategoryChain) -> None:
    """Record ``chain`` and every ancestor prefix of it, first spelling winning.

    Every level becomes an entry of its own so the ceiling counts the rows that
    will actually exist — a single deep path is several categories — and so
    each :func:`ensure_category_path` call below resolves exactly one level,
    which is what lets the created/reused tallies stay a simple count. "First
    spelling wins" is why the tree is walked before the object paths: a level
    the tree named keeps its display name instead of the slug an object path
    would fall back to.
    """
    for depth in range(1, len(chain) + 1):
        prefix = chain[:depth]
        chains.setdefault(_chain_key(prefix), prefix)


@dataclass
class RecreatedTree:
    """The importing scope's categories, keyed by the manifest's slug paths."""

    by_path: dict[str, Category] = field(default_factory=dict)
    created: int = 0
    reused: int = 0


def recreate_tree(
    session: Session,
    current_user: UserModel,
    manifest: ImportManifest,
) -> RecreatedTree:
    """Resolve every category path the document names, creating what is absent.

    The path set is the union of the declared tree and every path a media row
    is filed into, so an object can never reference a branch the import failed
    to make. Bounded by ``MEDIA_IMPORT_MAX_CATEGORIES`` over that union, not
    over the tree alone — the object rows can name paths the tree does not.
    """
    chains: dict[str, CategoryChain] = {}
    for chain in tree_chains(manifest.category_tree):
        _collect_chain(chains, chain)
    for entry in manifest.objects:
        for path in entry.category_paths:
            _collect_chain(chains, path_chain(path))
    if len(chains) > settings.MEDIA_IMPORT_MAX_CATEGORIES:
        raise _reject(
            f"Import names {len(chains)} category paths, above the "
            f"{settings.MEDIA_IMPORT_MAX_CATEGORIES} ceiling."
        )
    tree = RecreatedTree()
    # Shallowest first, so every chain's ancestors are already resolved when it
    # is reached and each call creates at most the one level it names.
    for key in sorted(chains, key=lambda k: (k.count(PATH_SEPARATOR), k)):
        row, created = ensure_category_path(session, current_user, chains[key])
        tree.by_path[key] = row
        tree.created += created
        tree.reused += 1 - created
    return tree


def _categories_for(tree: RecreatedTree, entry: ImportObjectEntry) -> list[Category]:
    """The recreated rows one manifest entry should be filed into."""
    rows: list[Category] = []
    seen: set[int] = set()
    for path in entry.category_paths:
        row = tree.by_path[_chain_key(path_chain(path))]
        if row.id not in seen:
            seen.add(row.id)
            rows.append(row)
    return rows


# ── per-row import ───────────────────────────────────────────────────────────


#: Lifecycle states in which a local row is a real object of the collection.
#: A ``REJECTED`` or ``FAILED`` row is an audit record of an upload that never
#: became an object, and a ``DELETED`` one is a tombstone; none of them is
#: something an import may quietly re-file onto, and none of them may have its
#: id reused either — so a source id landing on one is refused, not overwritten.
_IMPORTABLE_STATUSES = frozenset(
    {
        MediaObjectStatus.PENDING_UPLOAD,
        MediaObjectStatus.UPLOADED,
        MediaObjectStatus.PROCESSING,
        MediaObjectStatus.READY,
    }
)


def _owner_id(current_user: UserModel) -> uuid.UUID:
    return uuid.UUID(str(current_user.id))


def _importable_match(
    session: Session, current_user: UserModel, source_id: uuid.UUID
) -> tuple[MediaObject | None, bool]:
    """Resolve a source id locally: ``(row, usable)``.

    ``usable`` is true only for a live, real object the *caller themself*
    owns. Read access is deliberately not enough — filing an object into
    categories is a write, and a public or tenant-visible object belonging to
    somebody else is not the importer's to re-file. A superuser gets no
    widening here either: an import writes into the importer's own collection,
    whoever they are.
    """
    row = session.get(MediaObject, source_id)
    if row is None:
        return None, False
    usable = (
        row.owner_user_id == _owner_id(current_user)
        and row.deleted_at is None
        and row.status in _IMPORTABLE_STATUSES
    )
    return row, usable


def _file_into(
    session: Session, media_object: MediaObject, categories: Sequence[Category]
) -> None:
    """Add ``categories`` to an object's filing, keeping what is already there.

    Additive on purpose. A manifest is a *partial* view — export accepts the
    same filters the library does — so treating it as the object's whole filing
    would unfile it from categories the export never looked at.
    """
    existing = {row.id for row in media_object.user_categories}
    added = [row for row in categories if row.id not in existing]
    if not added:
        return
    media_object.user_categories = list(media_object.user_categories) + added
    media_object.updated_at = utcnow()
    session.add(media_object)
    session.commit()


def _reason_for(exc: HTTPException) -> tuple[ImportRowReason, str]:
    """Map a pipeline refusal onto the report's vocabulary.

    A structured `U1` reject detail is passed straight through — the whole
    point of reusing that vocabulary is that the token a client already knows
    survives the trip. The two quota refusals are recognised by their status
    codes, which :func:`media_service.core.quotas.check_quota` fixes. Anything
    else reaching here is the pipeline failing to read back what was just
    written, so it is reported as a storage failure rather than blamed on the
    document.
    """
    detail = exc.detail
    if isinstance(detail, dict) and detail.get("code") == "upload_rejected":
        return detail["reason"], str(detail.get("message") or "")
    if exc.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE:
        return "quota_bytes_exceeded", str(detail)
    if exc.status_code == status.HTTP_409_CONFLICT:
        return "quota_objects_exceeded", str(detail)
    return "storage_error", str(detail)


def _result(
    entry: ImportObjectEntry,
    *,
    row_status: ImportRowStatus,
    reason: ImportRowReason | None = None,
    message: str | None = None,
    media_object_id: uuid.UUID | None = None,
    paths: Sequence[str] = (),
) -> ImportObjectResult:
    """Build one report row."""
    return ImportObjectResult(
        source_id=entry.id,
        filename=entry.filename,
        status=row_status,
        reason=reason,
        message=message,
        media_object_id=media_object_id,
        category_paths=list(paths),
    )


def _link_existing(
    session: Session,
    entry: ImportObjectEntry,
    media_object: MediaObject,
    categories: Sequence[Category],
    paths: Sequence[str],
) -> ImportObjectResult:
    """Re-file an object this collection already holds; never re-upload it."""
    _file_into(session, media_object, categories)
    return _result(
        entry,
        row_status="linked",
        reason="already_exists",
        message="Already in this collection; filed into the imported categories.",
        media_object_id=media_object.id,
        paths=paths,
    )


_ID_CONFLICT_MESSAGE = (
    "This id is already held by a record that is not an importable object of "
    "this collection — another owner's, or one already deleted or rejected."
)


@dataclass
class ImportOutcome:
    """What one import produced: the caller's report, and work for the route.

    ``created_objects`` never reaches the client — it carries the storage
    location the scan job needs, which the report deliberately does not expose.
    The route enqueues from it, exactly as the upload route enqueues from the
    completion response.
    """

    report: ImportReport
    created_objects: list[MediaObjectPublic] = field(default_factory=list)


class ImportController:
    """Recreate a collection from an uploaded manifest or archive."""

    @staticmethod
    def run(
        *,
        session: Session,
        current_user: UserModel,
        storage: ObjectStorage,
        fmt: ImportFormat,
        source: IO[bytes],
    ) -> ImportOutcome:
        """Import one uploaded document in the caller's own scope."""
        if fmt == "archive":
            return _import_archive(
                session=session,
                current_user=current_user,
                storage=storage,
                source=source,
            )
        return _import_manifest(
            session=session, current_user=current_user, source=source
        )


def _import_manifest(
    *, session: Session, current_user: UserModel, source: IO[bytes]
) -> ImportOutcome:
    """Import a metadata-only manifest: the tree, plus filings for what is here.

    A manifest carries no bytes, so it cannot create media. Recording a catalog
    row for one anyway would mean an object whose ``object_key`` points at
    nothing — a download that 404s behind a 200-shaped record, quota charged
    for bytes that do not exist, and a permanent orphan for the reconciler to
    find — and the lifecycle has no state that honestly describes it. So a row
    with no local counterpart is reported ``skipped``/``missing_bytes``: the
    "clear per-row missing-bytes status" this step's alternative, chosen
    deliberately over a hollow record. What a manifest *can* restore is the
    tree and the assignments, and it does.
    """
    with drain_to_tempfile(
        source, limit=settings.MEDIA_IMPORT_MAX_MANIFEST_BYTES
    ) as handle:
        manifest = parse_manifest_document(handle.read())
    tree = recreate_tree(session, current_user, manifest)
    report = ImportReport(
        format="manifest",
        categories_created=tree.created,
        categories_reused=tree.reused,
    )
    for entry in manifest.objects:
        categories = _categories_for(tree, entry)
        paths = [_chain_key(path_chain(path)) for path in entry.category_paths]
        media_object, usable = _importable_match(session, current_user, entry.id)
        if media_object is None:
            report.objects.append(
                _result(
                    entry,
                    row_status="skipped",
                    reason="missing_bytes",
                    message="Manifest carries metadata only; no bytes to import.",
                    paths=paths,
                )
            )
            continue
        if not usable:
            report.objects.append(
                _result(
                    entry,
                    row_status="failed",
                    reason="id_conflict",
                    message=_ID_CONFLICT_MESSAGE,
                    paths=paths,
                )
            )
            continue
        report.objects.append(
            _link_existing(session, entry, media_object, categories, paths)
        )
    return ImportOutcome(report=_tallied(report))


def _import_archive(
    *,
    session: Session,
    current_user: UserModel,
    storage: ObjectStorage,
    source: IO[bytes],
) -> ImportOutcome:
    """Import an exported archive, re-driving every file through the pipeline."""
    with drain_to_tempfile(
        source, limit=settings.MEDIA_IMPORT_MAX_ARCHIVE_BYTES
    ) as handle:
        try:
            archive = zipfile.ZipFile(handle)
        except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError) as exc:
            raise _reject("Uploaded file is not a readable zip archive.") from exc
        with archive:
            return _import_open_archive(
                session=session,
                current_user=current_user,
                storage=storage,
                archive=archive,
            )


def _archive_manifest(archive: zipfile.ZipFile) -> ImportManifest:
    """Read and bound the archive's ``manifest.json`` before anything else."""
    try:
        info = archive.getinfo(MANIFEST_ENTRY)
    except KeyError as exc:
        raise _reject(f"Archive has no {MANIFEST_ENTRY}.") from exc
    if info.file_size > settings.MEDIA_IMPORT_MAX_MANIFEST_BYTES:
        raise _reject(
            f"{MANIFEST_ENTRY} exceeds the "
            f"{settings.MEDIA_IMPORT_MAX_MANIFEST_BYTES}-byte ceiling."
        )
    with archive.open(info) as handle:
        # Bounded twice over: the ceiling checked just above, and ``zipfile``
        # itself, which never hands back more than the declared ``file_size``
        # however the entry was compressed.
        raw = handle.read(info.file_size)
    return parse_manifest_document(raw)


def _file_entries(archive: zipfile.ZipFile) -> dict[uuid.UUID, zipfile.ZipInfo]:
    """Map each ``files/{object_id}/{filename}`` entry to its source object id.

    Entry *names* are never used as paths — nothing is extracted to disk, and
    the stored key is rebuilt from the manifest through
    :func:`media_service.storage.keys.build_object_key` — so a hostile name is
    inert here; it simply fails to parse as an id and is ignored.
    """
    entries: dict[uuid.UUID, zipfile.ZipInfo] = {}
    for info in archive.infolist():
        if info.is_dir():
            continue
        parts = info.filename.split("/")
        if len(parts) != 3 or parts[0] != FILES_PREFIX:
            continue
        try:
            object_id = uuid.UUID(parts[1])
        except ValueError:
            continue
        entries[object_id] = info
    return entries


def _assert_archive_within_ceilings(
    manifest: ImportManifest, entries: dict[uuid.UUID, zipfile.ZipInfo]
) -> None:
    """Refuse an archive that would decompress past the import ceilings.

    Declared sizes, not compressed ones: the point is to bound what will be
    *written*, and the per-entry read below is capped at the same declared
    figure, so a zip bomb can neither inflate past this total nor lie its way
    past the per-object category cap.
    """
    if len(entries) > settings.MEDIA_IMPORT_MAX_OBJECTS:
        raise _reject(
            f"Archive carries {len(entries)} files, above the "
            f"{settings.MEDIA_IMPORT_MAX_OBJECTS} import ceiling."
        )
    total = sum(
        entries[entry.id].file_size for entry in manifest.objects if entry.id in entries
    )
    if total > settings.MEDIA_IMPORT_MAX_TOTAL_BYTES:
        raise _reject(
            f"Archive would import {total} bytes, above the "
            f"{settings.MEDIA_IMPORT_MAX_TOTAL_BYTES} ceiling."
        )


def _import_open_archive(
    *,
    session: Session,
    current_user: UserModel,
    storage: ObjectStorage,
    archive: zipfile.ZipFile,
) -> ImportOutcome:
    """Drive an opened archive, one manifest row at a time."""
    manifest = _archive_manifest(archive)
    entries = _file_entries(archive)
    _assert_archive_within_ceilings(manifest, entries)
    tree = recreate_tree(session, current_user, manifest)
    outcome = ImportOutcome(
        report=ImportReport(
            format="archive",
            categories_created=tree.created,
            categories_reused=tree.reused,
        )
    )
    for entry in manifest.objects:
        categories = _categories_for(tree, entry)
        paths = [_chain_key(path_chain(path)) for path in entry.category_paths]
        result, created = _import_one_file(
            session=session,
            current_user=current_user,
            storage=storage,
            archive=archive,
            info=entries.get(entry.id),
            entry=entry,
            categories=categories,
            paths=paths,
        )
        outcome.report.objects.append(result)
        if created is not None:
            outcome.created_objects.append(created)
    outcome.report = _tallied(outcome.report)
    return outcome


def _import_one_file(
    *,
    session: Session,
    current_user: UserModel,
    storage: ObjectStorage,
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo | None,
    entry: ImportObjectEntry,
    categories: Sequence[Category],
    paths: Sequence[str],
) -> tuple[ImportObjectResult, MediaObjectPublic | None]:
    """Re-drive one archived file through the normal upload pipeline.

    Every step below is the pipeline's own, in the pipeline's own order, with
    exactly one omission: no presigned URL is minted, because the bytes are
    already here. That is what makes the imported object's ``scan_status``
    this service's verdict rather than the exporting service's claim.
    """
    media_object, usable = _importable_match(session, current_user, entry.id)
    if media_object is not None:
        if not usable:
            return (
                _result(
                    entry,
                    row_status="failed",
                    reason="id_conflict",
                    message=_ID_CONFLICT_MESSAGE,
                    paths=paths,
                ),
                None,
            )
        return _link_existing(session, entry, media_object, categories, paths), None
    if info is None:
        return (
            _result(
                entry,
                row_status="skipped",
                reason="missing_bytes",
                message=(
                    "The archive carries no bytes for this object; it was "
                    "listed in the manifest but excluded from the export."
                ),
                paths=paths,
            ),
            None,
        )
    if not is_allowed_declared_mime(entry.mime_type):
        # Checked here rather than left to ``stage_upload`` so every refusal
        # this function reports maps to exactly one reason token.
        return (
            _result(
                entry,
                row_status="failed",
                reason="unsupported_mime",
                message=f"Unsupported content type: {entry.mime_type}",
                paths=paths,
            ),
            None,
        )
    size_bytes = info.file_size
    if size_bytes > max_size_for_category(str(entry.category)):
        return (
            _result(
                entry,
                row_status="failed",
                reason="size_exceeded",
                message="Object is larger than its category allows.",
                paths=paths,
            ),
            None,
        )
    try:
        req = UploadInitiateRequest(
            category=entry.category,
            visibility=entry.visibility,
            original_filename=entry.filename or _FALLBACK_FILENAME,
            mime_type=entry.mime_type,
            expected_size_bytes=size_bytes,
            category_ids=[row.id for row in categories],
        )
    except ValidationError:
        # Reached by a zero-byte entry (the pipeline's floor is one byte) and
        # by anything else the upload request shape refuses outright.
        return (
            _result(
                entry,
                row_status="failed",
                reason="invalid_metadata",
                message="Manifest metadata does not describe an uploadable object.",
                paths=paths,
            ),
            None,
        )
    try:
        staged = stage_upload(
            session=session, current_user=current_user, req=req, media_id=entry.id
        )
    except HTTPException as exc:
        reason, message = _reason_for(exc)
        return (
            _result(
                entry,
                row_status="failed",
                reason=reason,
                message=message,
                paths=paths,
            ),
            None,
        )
    try:
        with archive.open(info) as file_bytes:
            storage.put_object_stream(
                bucket=staged.bucket,
                object_key=staged.object_key,
                data=file_bytes,
                length=size_bytes,
                content_type=entry.mime_type,
            )
    except Exception as exc:  # noqa: BLE001
        _logger.warning("media.import.store_failed %s: %s", entry.id, exc)
        _best_effort_remove(storage, bucket=staged.bucket, object_key=staged.object_key)
        return (
            _result(
                entry,
                row_status="failed",
                reason="storage_error",
                message="The imported bytes could not be stored.",
                paths=paths,
            ),
            None,
        )
    expires_at = utcnow() + timedelta(
        seconds=settings.MINIO_PRESIGNED_URL_EXPIRE_SECONDS
    )
    record_upload_session(
        session=session, staged=staged, req=req, expires_at=expires_at
    )
    try:
        response = UploadsController.complete_upload(
            session=session,
            current_user=current_user,
            session_id=staged.media_id,
            req=UploadCompleteRequest(
                sha256=entry.sha256, category_ids=[row.id for row in categories]
            ),
            storage=storage,
        )
    except HTTPException as exc:
        # ``complete_upload`` already removed the stored bytes and left a
        # REJECTED row behind for the audit trail — the same treatment a
        # rejected browser upload gets.
        reason, message = _reason_for(exc)
        return (
            _result(
                entry,
                row_status="failed",
                reason=reason,
                message=message,
                paths=paths,
            ),
            None,
        )
    return (
        _result(
            entry,
            row_status="created",
            media_object_id=response.media_object.id,
            paths=paths,
        ),
        response.media_object,
    )


def _best_effort_remove(
    storage: ObjectStorage, *, bucket: str, object_key: str
) -> None:
    """Drop bytes a failed put may have left behind, swallowing storage errors."""
    try:
        storage.remove_object(bucket=bucket, object_key=object_key)
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "media.import.cleanup_failed %s/%s: %s", bucket, object_key, exc
        )


def _tallied(report: ImportReport) -> ImportReport:
    """Fill the per-status counters from the rows, so the two cannot disagree."""
    report.created = sum(1 for row in report.objects if row.status == "created")
    report.linked = sum(1 for row in report.objects if row.status == "linked")
    report.skipped = sum(1 for row in report.objects if row.status == "skipped")
    report.failed = sum(1 for row in report.objects if row.status == "failed")
    return report
