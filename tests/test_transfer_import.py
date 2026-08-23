"""``POST /media/v1/import`` — manifest and archive import (`U9`).

The inbound half of the transfer surface. One file for both formats, because
they share everything that matters: the same uploaded document shape, the same
idempotent tree recreation, the same per-row report. They differ in exactly one
place — whether bytes came with it — and that difference is what most of these
tests pin down.

Two properties get the most attention here, because they are the ones a
regression would silently break:

* **An imported verdict is never trusted.** Bytes arriving in an archive are
  re-driven through the real upload pipeline and come out ``UPLOADED`` +
  ``PENDING`` with a scan job queued, no matter what ``scan_status`` the
  manifest claimed — and the pipeline's own refusals (SHA-256, magic bytes,
  size, quota) reach the caller as `U1`'s reason tokens.
* **Import converges.** Running the same import twice recreates no categories
  and duplicates no media; the second pass links what the first created.

Storage is a hand-written double rather than the shared ``mock_storage``
fixture, for the same reason ``tests/test_export_archive.py`` uses one: these
tests assert on the bytes. It stores whatever the streaming put writes and
serves it back to the completion path's stat / magic-byte / SHA-256 reads, so
an imported object really does travel through storage.
"""

import hashlib
import io
import json
import uuid
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, col, select

from media_service.controllers.export_archive import (
    ExportPrincipal,
    FILES_PREFIX,
    MANIFEST_ENTRY,
    write_archive,
)
from media_service.controllers.transfer_import import (
    ImportController,
    _best_effort_remove,
    _reason_for,
    drain_to_tempfile,
    path_chain,
)
from media_service.core.config import settings
from media_service.db_models.categories import Category
from media_service.db_models.media_object_categories import MediaObjectCategoryLink
from media_service.db_models.media_objects import (
    MediaCategory,
    MediaObject,
    MediaObjectStatus,
    MediaVisibility,
    ScanStatus,
    utcnow,
)
from media_service.db_models.storage_usage import StorageUsage
from media_service.db_models.upload_sessions import UploadSession
from media_service.schemas.objects import ObjectListParams

IMPORT_URL = "/media/v1/import"
BUCKET = "private-media"
TEXT = b"the quick brown fox jumps over the lazy dog\n"
PDF = b"%PDF-1.7\n" + b"0" * 64


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# ── doubles ──────────────────────────────────────────────────────────────────


class _Stat:
    def __init__(self, size: int) -> None:
        self.size = size
        self.etag = "etag-%d" % size


class _FakeStorage:
    """An in-memory object store the completion path can really read back."""

    def __init__(self, *, fail_put: bool = False, fail_remove: bool = False) -> None:
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.content_types: dict[tuple[str, str], str] = {}
        self.removed: list[tuple[str, str]] = []
        self.fail_put = fail_put
        self.fail_remove = fail_remove

    def put_object_stream(self, *, bucket, object_key, data, length, content_type):
        if self.fail_put:
            raise OSError("bucket is unavailable")
        payload = data.read(length)
        assert len(payload) == length, "declared length must match the bytes written"
        self.blobs[(bucket, object_key)] = payload
        self.content_types[(bucket, object_key)] = content_type

    def stat_object(self, *, bucket, object_key):
        return _Stat(len(self.blobs[(bucket, object_key)]))

    def get_object_head(self, *, bucket, object_key, length: int = 512):
        return self.blobs[(bucket, object_key)][:length]

    def stream_object(self, *, bucket, object_key, chunk_size=1024):
        data = self.blobs[(bucket, object_key)]
        for start in range(0, len(data), chunk_size):
            yield data[start : start + chunk_size]

    def set_object_content_type(self, *, bucket, object_key, content_type):
        self.content_types[(bucket, object_key)] = content_type

    def remove_object(self, *, bucket, object_key):
        if self.fail_remove:
            raise OSError("remove failed")
        self.removed.append((bucket, object_key))
        self.blobs.pop((bucket, object_key), None)


@pytest.fixture
def mock_storage() -> _FakeStorage:
    """Override the shared ``MagicMock`` double with a real little store."""
    return _FakeStorage()


# ── builders ─────────────────────────────────────────────────────────────────


def _node(name: str, slug: str | None = None, children=()) -> dict:
    """An exported ``CategoryNode``, extra derived fields and all."""
    return {
        "id": 999,
        "parent_id": None,
        "owner_id": str(uuid.uuid4()),
        "tenant_id": None,
        "name": name,
        "slug": slug or name.lower(),
        "object_count": 0,
        "total_object_count": 0,
        "children": list(children),
    }


def _entry(
    *,
    object_id: uuid.UUID | None = None,
    filename: str = "notes.txt",
    mime_type: str = "text/plain",
    payload: bytes = TEXT,
    category: str = "document",
    visibility: str = "private",
    category_paths=(),
    sha256: str | None = None,
    size_bytes: int | None = None,
) -> dict:
    """One exported ``ManifestObjectEntry``, verdicts included.

    The verdicts are deliberately the *cleared* ones an exporting service would
    have written, so every archive test proves they are discarded.
    """
    now = utcnow().isoformat()
    return {
        "id": str(object_id or uuid.uuid4()),
        "filename": filename,
        "category": category,
        "category_paths": list(category_paths),
        "visibility": visibility,
        "size_bytes": len(payload) if size_bytes is None else size_bytes,
        "sha256": _sha(payload) if sha256 is None else sha256,
        "mime_type": mime_type,
        "status": "ready",
        "scan_status": "clean",
        "created_at": now,
        "updated_at": now,
    }


def _manifest(objects=(), tree=()) -> dict:
    return {"category_tree": list(tree), "objects": list(objects)}


def _post(client: TestClient, fmt: str, raw: bytes, name: str = "manifest.json"):
    return client.post(
        IMPORT_URL, data={"format": fmt}, files={"file": (name, raw, "application/*")}
    )


def _post_manifest(client: TestClient, document: dict):
    return _post(client, "manifest", json.dumps(document).encode("utf-8"))


def _zip_bytes(manifest: dict, files: dict[str, bytes], extras=()) -> bytes:
    """Build an archive in the layout ``export_archive`` writes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(MANIFEST_ENTRY, json.dumps(manifest))
        for name, payload in files.items():
            archive.writestr(name, payload)
        for name in extras:
            archive.writestr(name, b"ignored")
    return buffer.getvalue()


def _files_entry(entry: dict, payload: bytes) -> dict[str, bytes]:
    return {f"{FILES_PREFIX}/{entry['id']}/{entry['filename']}": payload}


def _post_archive(client: TestClient, manifest: dict, files, extras=()):
    return _post(
        client, "archive", _zip_bytes(manifest, files, extras), name="export.zip"
    )


def _make_object(
    session: Session,
    owner_id: uuid.UUID,
    *,
    object_id: uuid.UUID | None = None,
    filename: str = "notes.txt",
    status: MediaObjectStatus = MediaObjectStatus.READY,
    deleted: bool = False,
) -> MediaObject:
    oid = object_id or uuid.uuid4()
    obj = MediaObject(
        id=oid,
        owner_user_id=owner_id,
        category=MediaCategory.DOCUMENT,
        visibility=MediaVisibility.PRIVATE,
        storage_bucket=BUCKET,
        object_key=f"users/{owner_id}/document/{oid}/original/{filename}",
        original_filename=filename,
        mime_type="text/plain",
        size_bytes=len(TEXT),
        sha256=_sha(TEXT),
        status=status,
        scan_status=ScanStatus.CLEAN,
        deleted_at=utcnow() if deleted else None,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


def _paths(session: Session, obj_id: uuid.UUID) -> set[str]:
    """The slugs of every category one object is filed into."""
    links = session.exec(
        select(MediaObjectCategoryLink).where(
            col(MediaObjectCategoryLink.media_object_id) == obj_id
        )
    ).all()
    return {session.get(Category, link.category_id).slug for link in links}


def _by_source(report: dict) -> dict[str, dict]:
    return {row["source_id"]: row for row in report["objects"]}


# ── manifest format ──────────────────────────────────────────────────────────


def test_manifest_import_recreates_the_tree_and_reports_missing_bytes(
    client: TestClient, session: Session, current_user
):
    """A manifest carries no bytes, so it restores the tree and says so per row."""
    tree = [_node("Docs", children=[_node("Invoices")])]
    entry = _entry(category_paths=["docs/invoices"])
    response = _post_manifest(client, _manifest([entry], tree))

    assert response.status_code == 200
    report = response.json()
    assert report["format"] == "manifest"
    assert (report["categories_created"], report["categories_reused"]) == (2, 0)
    assert (report["created"], report["linked"], report["skipped"]) == (0, 0, 1)
    row = report["objects"][0]
    assert (row["status"], row["reason"]) == ("skipped", "missing_bytes")
    assert row["media_object_id"] is None
    assert row["category_paths"] == ["docs/invoices"]

    rows = session.exec(select(Category)).all()
    assert {c.slug for c in rows} == {"docs", "invoices"}
    child = next(c for c in rows if c.slug == "invoices")
    parent = next(c for c in rows if c.slug == "docs")
    assert child.parent_id == parent.id
    assert child.owner_id == current_user.id and child.tenant_id is None


def test_manifest_import_round_trips_the_tree_and_assignments(
    client: TestClient, session: Session, current_user
):
    """Export a filed collection, re-import it, and get the filing back."""
    root = Category(name="Docs", slug="docs", owner_id=current_user.id)
    session.add(root)
    session.commit()
    session.refresh(root)
    obj = _make_object(session, current_user.id)
    session.add(MediaObjectCategoryLink(media_object_id=obj.id, category_id=root.id))
    session.commit()

    exported = client.post("/media/v1/export", json={"format": "manifest"}).json()
    # Drop the filing, keep the object: the manifest is now the only record of
    # where it lived.
    for link in session.exec(select(MediaObjectCategoryLink)).all():
        session.delete(link)
    session.commit()
    assert _paths(session, obj.id) == set()

    report = _post_manifest(client, exported).json()
    assert (report["created"], report["linked"], report["skipped"]) == (0, 1, 0)
    row = report["objects"][0]
    assert (row["status"], row["reason"]) == ("linked", "already_exists")
    assert row["media_object_id"] == str(obj.id)
    assert _paths(session, obj.id) == {"docs"}


def test_manifest_import_is_idempotent(client: TestClient, session: Session):
    """Re-running an import creates nothing the first run already made."""
    document = _manifest(
        [_entry(category_paths=["docs/invoices"])],
        [_node("Docs", children=[_node("Invoices")])],
    )
    first = _post_manifest(client, document).json()
    second = _post_manifest(client, document).json()

    assert (first["categories_created"], first["categories_reused"]) == (2, 0)
    assert (second["categories_created"], second["categories_reused"]) == (0, 2)
    assert len(session.exec(select(Category)).all()) == 2


def test_manifest_import_keeps_a_reused_categorys_local_name(
    client: TestClient, session: Session, current_user
):
    """An import may not rename a category the tenant already shares."""
    session.add(Category(name="Local Name", slug="docs", owner_id=current_user.id))
    session.commit()

    report = _post_manifest(client, _manifest([], [_node("Foreign Name", "docs")]))
    assert report.json()["categories_reused"] == 1
    rows = session.exec(select(Category)).all()
    assert [(row.name, row.slug) for row in rows] == [("Local Name", "docs")]


def test_manifest_import_creates_paths_the_tree_omitted(
    client: TestClient, session: Session
):
    """An object filed into a branch the tree forgot still gets its branch."""
    entry = _entry(category_paths=["archive/2026/q1"])
    report = _post_manifest(client, _manifest([entry])).json()
    # Every level is counted, because every level is a category that now exists.
    assert (report["categories_created"], report["categories_reused"]) == (3, 0)
    rows = {row.slug: row for row in session.exec(select(Category)).all()}
    assert rows["q1"].parent_id == rows["2026"].id
    assert rows["2026"].parent_id == rows["archive"].id
    # A path segment has no display name of its own, so the slug doubles as it.
    assert rows["q1"].name == "q1"


def test_manifest_import_adds_to_an_existing_filing_without_removing_any(
    client: TestClient, session: Session, current_user
):
    """A manifest is a partial view; it must not unfile what it never saw."""
    kept = Category(name="Kept", slug="kept", owner_id=current_user.id)
    session.add(kept)
    session.commit()
    session.refresh(kept)
    obj = _make_object(session, current_user.id)
    session.add(MediaObjectCategoryLink(media_object_id=obj.id, category_id=kept.id))
    session.commit()

    entry = _entry(object_id=obj.id, category_paths=["docs"])
    _post_manifest(client, _manifest([entry], [_node("Docs")]))
    assert _paths(session, obj.id) == {"kept", "docs"}


def test_manifest_import_relinking_the_same_categories_changes_nothing(
    client: TestClient, session: Session, current_user
):
    """The second pass over an already-filed object is a no-op, not a duplicate."""
    obj = _make_object(session, current_user.id)
    entry = _entry(object_id=obj.id, category_paths=["docs", "docs"])
    document = _manifest([entry], [_node("Docs")])
    _post_manifest(client, document)
    report = _post_manifest(client, document).json()
    assert report["objects"][0]["status"] == "linked"
    assert _paths(session, obj.id) == {"docs"}


@pytest.mark.parametrize(
    ("status", "deleted"),
    [
        (MediaObjectStatus.READY, True),
        (MediaObjectStatus.REJECTED, False),
    ],
)
def test_manifest_import_refuses_an_id_held_by_a_non_object(
    client: TestClient, session: Session, current_user, status, deleted
):
    """A tombstone or a rejected upload holds its id; it is not re-filed onto."""
    obj = _make_object(session, current_user.id, status=status, deleted=deleted)
    entry = _entry(object_id=obj.id, category_paths=["docs"])
    report = _post_manifest(client, _manifest([entry], [_node("Docs")])).json()
    row = report["objects"][0]
    assert (row["status"], row["reason"]) == ("failed", "id_conflict")
    assert report["failed"] == 1
    assert _paths(session, obj.id) == set()


def test_manifest_import_refuses_an_id_owned_by_somebody_else(
    client: TestClient, session: Session
):
    """Read access is not enough: re-filing is a write on somebody's object."""
    stranger = _make_object(session, uuid.uuid4())
    entry = _entry(object_id=stranger.id, category_paths=["docs"])
    report = _post_manifest(client, _manifest([entry], [_node("Docs")])).json()
    assert report["objects"][0]["reason"] == "id_conflict"
    assert _paths(session, stranger.id) == set()


# ── document-level refusals ──────────────────────────────────────────────────


def test_import_rejects_an_unknown_format(client: TestClient):
    assert _post(client, "csv", b"{}").status_code == 422


def test_import_rejects_a_manifest_that_is_not_json(client: TestClient):
    assert _post(client, "manifest", b"\xff\xfe not json").status_code == 422


def test_import_rejects_a_manifest_that_is_not_an_object(client: TestClient):
    assert _post(client, "manifest", b"[]").status_code == 422


def test_import_rejects_a_manifest_whose_sections_are_not_arrays(client: TestClient):
    body = json.dumps({"category_tree": {"Docs": []}, "objects": []}).encode()
    assert _post(client, "manifest", body).status_code == 422


def test_import_rejects_a_tree_node_that_is_not_an_object(client: TestClient):
    body = json.dumps({"category_tree": ["nope"], "objects": []}).encode()
    assert _post(client, "manifest", body).status_code == 422


def test_import_rejects_children_that_are_not_an_array(client: TestClient):
    node = _node("Docs")
    node["children"] = "nope"
    assert _post_manifest(client, _manifest([], [node])).status_code == 422


def test_import_rejects_a_manifest_of_the_wrong_shape(client: TestClient):
    entry = _entry()
    entry["visibility"] = "invisible"
    assert _post_manifest(client, _manifest([entry])).status_code == 422


def test_import_rejects_a_manifest_nested_past_python_itself(client: TestClient):
    """A tree built to exhaust the stack is refused by the decoder, not by a crash."""
    depth = 100_000
    raw = (
        b'{"category_tree":'
        + b'[{"name":"a","slug":"a","children":' * depth
        + b"[]"
        + b"}]" * depth
        + b',"objects":[]}'
    )
    assert _post(client, "manifest", raw).status_code == 422


def test_import_rejects_too_many_objects(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_IMPORT_MAX_OBJECTS", 1)
    document = _manifest([_entry(), _entry()])
    assert _post_manifest(client, document).status_code == 422


def test_import_rejects_too_many_tree_nodes(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_IMPORT_MAX_CATEGORIES", 1)
    tree = [_node("Docs"), _node("Assets")]
    assert _post_manifest(client, _manifest([], tree)).status_code == 422


def test_import_rejects_too_many_category_paths(client: TestClient, monkeypatch):
    """The ceiling covers the union of tree paths and paths only objects name."""
    monkeypatch.setattr(settings, "MEDIA_IMPORT_MAX_CATEGORIES", 2)
    document = _manifest(
        [_entry(category_paths=["a", "b"])], [_node("Docs"), _node("Assets")]
    )
    assert _post_manifest(client, document).status_code == 422


def test_import_rejects_a_tree_deeper_than_the_ceiling(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_IMPORT_MAX_CATEGORY_DEPTH", 2)
    tree = [_node("A", children=[_node("B", children=[_node("C")])])]
    assert _post_manifest(client, _manifest([], tree)).status_code == 422


def test_import_rejects_a_path_deeper_than_the_ceiling(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_IMPORT_MAX_CATEGORY_DEPTH", 2)
    document = _manifest([_entry(category_paths=["a/b/c"])])
    assert _post_manifest(client, document).status_code == 422


def test_import_counts_every_level_of_a_deep_path_against_the_ceiling(
    client: TestClient, monkeypatch
):
    """A single three-level path is three categories, not one."""
    monkeypatch.setattr(settings, "MEDIA_IMPORT_MAX_CATEGORIES", 2)
    document = _manifest([_entry(category_paths=["a/b/c"])])
    assert _post_manifest(client, document).status_code == 422


def test_import_rejects_an_empty_category_path(client: TestClient):
    document = _manifest([_entry(category_paths=["///"])])
    assert _post_manifest(client, document).status_code == 422


def test_import_rejects_a_name_that_slugifies_to_nothing(client: TestClient):
    assert (
        _post_manifest(client, _manifest([], [_node("!!!", "???")])).status_code == 422
    )


def test_import_rejects_an_upload_over_the_byte_ceiling(
    client: TestClient, monkeypatch
):
    monkeypatch.setattr(settings, "MEDIA_IMPORT_MAX_MANIFEST_BYTES", 8)
    assert _post_manifest(client, _manifest()).status_code == 422


def test_import_rejects_an_upload_that_is_not_a_zip(client: TestClient):
    assert _post(client, "archive", b"not a zip", name="x.zip").status_code == 422


def test_import_rejects_an_archive_with_no_manifest(client: TestClient):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", b"hello")
    assert _post(client, "archive", buffer.getvalue(), "x.zip").status_code == 422


def test_import_rejects_an_archive_whose_manifest_is_too_large(
    client: TestClient, monkeypatch
):
    monkeypatch.setattr(settings, "MEDIA_IMPORT_MAX_MANIFEST_BYTES", 8)
    assert _post_archive(client, _manifest(), {}).status_code == 422


def test_import_rejects_an_archive_with_too_many_files(client: TestClient, monkeypatch):
    """Counted over the zip, not the manifest: the two need not agree."""
    monkeypatch.setattr(settings, "MEDIA_IMPORT_MAX_OBJECTS", 1)
    listed, unlisted = _entry(), _entry()
    files = {**_files_entry(listed, TEXT), **_files_entry(unlisted, TEXT)}
    assert _post_archive(client, _manifest([listed]), files).status_code == 422


def test_import_rejects_an_archive_over_the_total_byte_ceiling(
    client: TestClient, monkeypatch
):
    monkeypatch.setattr(settings, "MEDIA_IMPORT_MAX_TOTAL_BYTES", 1)
    entry = _entry()
    assert (
        _post_archive(client, _manifest([entry]), _files_entry(entry, TEXT)).status_code
        == 422
    )


# ── archive format ───────────────────────────────────────────────────────────


def test_archive_import_recreates_objects_and_never_trusts_the_scan_verdict(
    client: TestClient, session: Session, mock_storage, fake_arq_pool, current_user
):
    """The exported ``clean`` verdict is discarded; a fresh scan is queued."""
    entry = _entry(category_paths=["docs/invoices"])
    tree = [_node("Docs", children=[_node("Invoices")])]
    response = _post_archive(
        client, _manifest([entry], tree), _files_entry(entry, TEXT)
    )

    assert response.status_code == 200
    report = response.json()
    assert (report["created"], report["linked"], report["skipped"]) == (1, 0, 0)
    row = report["objects"][0]
    assert row["status"] == "created" and row["reason"] is None
    # The source id is reused, which is what makes a repeat import converge.
    assert row["media_object_id"] == entry["id"]
    assert row["scan_queued"] is True

    stored = session.get(MediaObject, uuid.UUID(entry["id"]))
    assert stored.status == MediaObjectStatus.UPLOADED
    assert stored.scan_status == ScanStatus.PENDING
    assert stored.owner_user_id == current_user.id
    assert stored.sha256 == _sha(TEXT)
    assert stored.size_bytes == len(TEXT)
    assert _paths(session, stored.id) == {"invoices"}
    assert mock_storage.blobs[(BUCKET, stored.object_key)] == TEXT

    payload = fake_arq_pool.enqueue_job.await_args.args[1]
    assert payload.object_id == stored.id
    assert (payload.bucket, payload.object_key) == (BUCKET, stored.object_key)


def test_archive_import_round_trips_a_real_exported_archive(
    client: TestClient, session: Session, mock_storage, current_user
):
    """The document the exporter writes is the document the importer reads."""
    root = Category(name="Docs", slug="docs", owner_id=current_user.id)
    session.add(root)
    session.commit()
    session.refresh(root)
    source = _make_object(session, current_user.id, filename="notes.txt")
    session.add(MediaObjectCategoryLink(media_object_id=source.id, category_id=root.id))
    session.commit()
    mock_storage.blobs[(source.storage_bucket, source.object_key)] = TEXT

    buffer = io.BytesIO()
    write_archive(
        session=session,
        storage=mock_storage,
        principal=ExportPrincipal(
            id=uuid.UUID(str(current_user.id)), is_superuser=False, tenant_id=None
        ),
        filters=ObjectListParams(),
        fh=buffer,
    )

    # Simulate importing into a clean collection: the ids are free again.
    # Deleting the object takes its link rows with it (the M2M is a secondary
    # relationship), so the tree survives and the media does not.
    session.delete(session.get(MediaObject, source.id))
    session.commit()
    assert session.exec(select(MediaObjectCategoryLink)).all() == []

    report = _post(client, "archive", buffer.getvalue(), "export.zip").json()
    assert (report["created"], report["categories_reused"]) == (1, 1)
    restored = session.get(MediaObject, source.id)
    assert restored is not None
    assert restored.original_filename == "notes.txt"
    assert restored.scan_status == ScanStatus.PENDING
    assert _paths(session, restored.id) == {"docs"}


def test_archive_import_links_an_object_already_present(
    client: TestClient, session: Session, mock_storage, current_user
):
    """A second run files the object again rather than uploading a duplicate."""
    entry = _entry(category_paths=["docs"])
    document = _manifest([entry], [_node("Docs")])
    files = _files_entry(entry, TEXT)
    _post_archive(client, document, files)
    stored_before = len(mock_storage.blobs)

    report = _post_archive(client, document, files).json()
    assert (report["created"], report["linked"]) == (0, 1)
    assert report["objects"][0]["reason"] == "already_exists"
    assert len(mock_storage.blobs) == stored_before
    assert len(session.exec(select(MediaObject)).all()) == 1


def test_archive_import_refuses_an_id_held_by_another_owner(
    client: TestClient, session: Session
):
    stranger = _make_object(session, uuid.uuid4())
    entry = _entry(object_id=stranger.id)
    report = _post_archive(client, _manifest([entry]), _files_entry(entry, TEXT)).json()
    assert report["objects"][0]["reason"] == "id_conflict"


def test_archive_import_reports_a_row_the_archive_carries_no_bytes_for(
    client: TestClient,
):
    """The export lists quarantined rows without their bytes; import says why."""
    entry = _entry()
    report = _post_archive(client, _manifest([entry]), {}).json()
    row = report["objects"][0]
    assert (row["status"], row["reason"]) == ("skipped", "missing_bytes")


def test_archive_import_ignores_entries_that_are_not_object_files(
    client: TestClient, session: Session
):
    """Only ``files/{uuid}/{name}`` counts; anything else is inert, not a path."""
    entry = _entry()
    extras = (
        f"{FILES_PREFIX}/not-a-uuid/x.txt",
        "elsewhere/x.txt",
        f"{FILES_PREFIX}/{entry['id']}/nested/deep.txt",
        "../../etc/passwd",
    )
    report = _post_archive(client, _manifest([entry]), {}, extras).json()
    assert report["objects"][0]["reason"] == "missing_bytes"
    assert session.exec(select(MediaObject)).all() == []


def test_archive_import_ignores_directory_entries(client: TestClient):
    entry = _entry()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(MANIFEST_ENTRY, json.dumps(_manifest([entry])))
        archive.writestr(f"{FILES_PREFIX}/{entry['id']}/", b"")
    report = _post(client, "archive", buffer.getvalue(), "x.zip").json()
    assert report["objects"][0]["reason"] == "missing_bytes"


def test_archive_import_re_verifies_the_declared_sha256(
    client: TestClient, session: Session, mock_storage
):
    """Bytes that do not match the manifest digest are refused, not stored."""
    entry = _entry()
    tampered = _files_entry(entry, TEXT.replace(b"quick", b"slow!"))
    report = _post_archive(client, _manifest([entry]), tampered).json()
    row = report["objects"][0]
    assert (row["status"], row["reason"]) == ("failed", "sha256_mismatch")
    assert row["message"] == "Upload rejected: sha256_mismatch."
    # The pipeline's own reject path removed the bytes and kept the audit row.
    assert mock_storage.blobs == {}
    stored = session.get(MediaObject, uuid.UUID(entry["id"]))
    assert stored.status == MediaObjectStatus.REJECTED


def test_archive_import_re_sniffs_the_magic_bytes(client: TestClient):
    """A row declaring PDF but carrying something else is a mime mismatch."""
    entry = _entry(filename="doc.pdf", mime_type="application/pdf", payload=TEXT)
    report = _post_archive(client, _manifest([entry]), _files_entry(entry, TEXT)).json()
    assert report["objects"][0]["reason"] == "mime_mismatch"


def test_archive_import_accepts_a_row_whose_bytes_match_their_type(
    client: TestClient, session: Session
):
    entry = _entry(filename="doc.pdf", mime_type="application/pdf", payload=PDF)
    report = _post_archive(client, _manifest([entry]), _files_entry(entry, PDF)).json()
    assert report["objects"][0]["status"] == "created"
    assert session.get(MediaObject, uuid.UUID(entry["id"])).mime_type == (
        "application/pdf"
    )


def test_archive_import_refuses_a_content_type_off_the_allowlist(
    client: TestClient, session: Session
):
    entry = _entry(filename="page.html", mime_type="text/html")
    report = _post_archive(client, _manifest([entry]), _files_entry(entry, TEXT)).json()
    row = report["objects"][0]
    assert (row["status"], row["reason"]) == ("failed", "unsupported_mime")
    assert session.exec(select(UploadSession)).all() == []


def test_archive_import_refuses_a_file_over_its_category_cap(
    client: TestClient, monkeypatch
):
    monkeypatch.setattr(settings, "MEDIA_MAX_UPLOAD_SIZE_BYTES", 4)
    entry = _entry()
    report = _post_archive(client, _manifest([entry]), _files_entry(entry, TEXT)).json()
    assert report["objects"][0]["reason"] == "size_exceeded"


def test_archive_import_refuses_an_empty_file(client: TestClient):
    """The pipeline's floor is one byte, so a zero-byte row cannot be uploaded."""
    entry = _entry(payload=b"")
    report = _post_archive(client, _manifest([entry]), _files_entry(entry, b"")).json()
    row = report["objects"][0]
    assert (row["status"], row["reason"]) == ("failed", "invalid_metadata")


@pytest.mark.parametrize(
    ("quota_field", "value", "reason"),
    [
        ("quota_bytes", 1, "quota_bytes_exceeded"),
        ("quota_objects", 0, "quota_objects_exceeded"),
    ],
)
def test_archive_import_enforces_the_owners_quota(
    client: TestClient, session: Session, current_user, quota_field, value, reason
):
    """Import is not a way around the ceiling a normal upload is held to."""
    usage = StorageUsage(owner_user_id=uuid.UUID(str(current_user.id)))
    setattr(usage, quota_field, value)
    session.add(usage)
    session.commit()

    entry = _entry()
    report = _post_archive(client, _manifest([entry]), _files_entry(entry, TEXT)).json()
    assert report["objects"][0]["reason"] == reason


def test_archive_import_reports_a_storage_failure_and_cleans_up(
    client: TestClient, session: Session, mock_storage
):
    mock_storage.fail_put = True
    entry = _entry()
    report = _post_archive(client, _manifest([entry]), _files_entry(entry, TEXT)).json()
    row = report["objects"][0]
    assert (row["status"], row["reason"]) == ("failed", "storage_error")
    assert len(mock_storage.removed) == 1
    assert session.exec(select(UploadSession)).all() == []


def test_archive_import_survives_a_cleanup_that_also_fails(
    client: TestClient, mock_storage
):
    mock_storage.fail_put = True
    mock_storage.fail_remove = True
    entry = _entry()
    report = _post_archive(client, _manifest([entry]), _files_entry(entry, TEXT)).json()
    assert report["objects"][0]["reason"] == "storage_error"


def test_archive_import_reports_a_scan_that_could_not_be_queued(
    client: TestClient, session: Session, fake_arq_pool
):
    """The object is safe (still PENDING); the row says the scan needs re-driving."""
    fake_arq_pool.enqueue_job = AsyncMock(side_effect=OSError("redis is gone"))
    entry = _entry()
    report = _post_archive(client, _manifest([entry]), _files_entry(entry, TEXT)).json()
    row = report["objects"][0]
    assert (row["status"], row["scan_queued"]) == ("created", False)
    assert session.get(MediaObject, uuid.UUID(entry["id"])).scan_status == (
        ScanStatus.PENDING
    )


def test_archive_import_mixes_outcomes_in_one_report(
    client: TestClient, session: Session
):
    """One refused row does not cost the collection the rows that were fine."""
    good = _entry(filename="good.txt")
    tampered = _entry(filename="bad.txt")
    absent = _entry(filename="gone.txt")
    files = {
        **_files_entry(good, TEXT),
        **_files_entry(tampered, TEXT.replace(b"quick", b"slow!")),
    }
    report = _post_archive(
        client, _manifest([good, tampered, absent], [_node("Docs")]), files
    ).json()

    rows = _by_source(report)
    assert rows[good["id"]]["status"] == "created"
    assert rows[tampered["id"]]["reason"] == "sha256_mismatch"
    assert rows[absent["id"]]["reason"] == "missing_bytes"
    assert (report["created"], report["failed"], report["skipped"]) == (1, 1, 1)
    assert report["categories_created"] == 1


def test_archive_import_files_an_unnamed_object_under_a_stable_key(
    client: TestClient, session: Session
):
    entry = _entry(filename="")
    entry["filename"] = None
    files = {f"{FILES_PREFIX}/{entry['id']}/blob": TEXT}
    report = _post_archive(client, _manifest([entry]), files).json()
    assert report["objects"][0]["status"] == "created"
    stored = session.get(MediaObject, uuid.UUID(entry["id"]))
    assert stored.object_key.endswith("/original/file")


# ── unit-level ───────────────────────────────────────────────────────────────


def test_drain_to_tempfile_closes_the_handle_when_the_source_fails():
    """A read that blows up must not leak the temporary file it was filling."""

    class _Exploding:
        def read(self, _size):
            raise OSError("disk went away")

    with pytest.raises(OSError):
        drain_to_tempfile(_Exploding(), limit=1024)


def test_path_chain_slugifies_each_segment():
    assert path_chain("Docs/My Invoices") == (("docs", "docs"), ("my-invoices",) * 2)


def test_reason_for_falls_back_to_a_storage_failure():
    """Anything the pipeline raises that is not a known refusal is storage."""
    reason, message = _reason_for(HTTPException(status_code=422, detail="odd"))
    assert (reason, message) == ("storage_error", "odd")


def test_best_effort_remove_swallows_storage_errors():
    storage = _FakeStorage(fail_remove=True)
    _best_effort_remove(storage, bucket=BUCKET, object_key="k")
    assert storage.removed == []


def test_import_controller_runs_against_a_tenanted_principal(
    session: Session, mock_storage
):
    """Categories an import creates are stamped with the caller's tenant (`D4`)."""
    tenant_id = uuid.uuid4()
    principal = SimpleNamespace(
        id=uuid.uuid4(), is_superuser=False, tenant_id=tenant_id
    )
    raw = json.dumps(_manifest([], [_node("Docs")])).encode()
    outcome = ImportController.run(
        session=session,
        current_user=principal,
        storage=mock_storage,
        fmt="manifest",
        source=io.BytesIO(raw),
    )
    assert outcome.report.categories_created == 1
    row = session.exec(select(Category)).one()
    assert (row.tenant_id, row.owner_id) == (tenant_id, principal.id)
