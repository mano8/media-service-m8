"""Pure assembly helpers for the user-category tree (`U3`).

These functions take rows the caller has already scoped and turn them into the
nested response shapes. They hold no session and no tenant logic on purpose:
scoping every query by tenant is the controller's job (`U4`), and keeping the
assembly pure means the subtree rollup and the path resolution are testable
without a database and reusable by both the tree endpoint and the
``MediaObjectPublic.categories`` projection.
"""

from collections.abc import Iterable, Mapping, Sequence

from media_service.db_models.categories import (
    Category,
    CategoryNode,
    MediaObjectCategoryRef,
)

#: Separator between slugs in a resolved category path.
PATH_SEPARATOR = "/"


def build_category_tree(
    categories: Iterable[Category],
    direct_counts: Mapping[int, int] | None = None,
) -> list[CategoryNode]:
    """Assemble scoped category rows into root-anchored nested nodes.

    ``direct_counts`` maps a category id to the number of media objects filed
    directly on it; a missing id counts as zero, which is what makes the
    empty-node case render as ``0`` rather than being dropped.
    ``total_object_count`` is rolled up post-order, so a parent reports its own
    objects plus every descendant's.

    A row whose ``parent_id`` is not present in ``categories`` is treated as a
    root, so a caller that hands over only a subtree still gets a well-formed
    tree instead of losing the rows.
    """
    counts = direct_counts or {}
    rows = sorted(categories, key=lambda c: (c.name or "").lower())
    nodes: dict[int, CategoryNode] = {}
    for row in rows:
        node = CategoryNode.model_validate(
            row, update={"children": [], "object_count": counts.get(row.id, 0)}
        )
        nodes[row.id] = node

    roots: list[CategoryNode] = []
    for row in rows:
        node = nodes[row.id]
        parent = nodes.get(row.parent_id) if row.parent_id is not None else None
        if parent is None:
            roots.append(node)
        else:
            parent.children.append(node)

    for root in roots:
        _roll_up_counts(root)
    return roots


def count_category_nodes(nodes: Sequence[CategoryNode]) -> int:
    """Return the total number of nodes in ``nodes`` at every depth."""
    return sum(1 + count_category_nodes(node.children) for node in nodes)


def resolve_category_paths(categories: Iterable[Category]) -> dict[int, str]:
    """Map each category id to its slash-joined slug path from the tree root.

    A parent missing from ``categories`` ends the walk, so a partially-scoped
    set resolves to the deepest path it can prove rather than raising. A cycle
    (which `U4`'s CRUD guards reject on write) is broken instead of looping
    forever, so a corrupt row cannot hang a response.
    """
    rows = {c.id: c for c in categories}
    paths: dict[int, str] = {}
    for category_id, row in rows.items():
        segments = [row.slug]
        seen = {category_id}
        parent_id = row.parent_id
        while parent_id is not None and parent_id in rows and parent_id not in seen:
            seen.add(parent_id)
            parent = rows[parent_id]
            segments.append(parent.slug)
            parent_id = parent.parent_id
        paths[category_id] = PATH_SEPARATOR.join(reversed(segments))
    return paths


def category_refs(
    categories: Sequence[Category], scope: Iterable[Category]
) -> list[MediaObjectCategoryRef]:
    """Project ``categories`` into public refs, resolving paths against ``scope``.

    ``scope`` is the row set the paths are resolved through — normally every
    category visible to the caller, loaded once per page so the projection does
    not become an N+1 (`U4`).
    """
    paths = resolve_category_paths(scope)
    return [
        MediaObjectCategoryRef(
            id=category.id,
            name=category.name,
            path=paths.get(category.id, category.slug),
        )
        for category in categories
    ]


def _roll_up_counts(node: CategoryNode) -> int:
    """Set ``total_object_count`` on ``node`` and its subtree; return the total."""
    total = node.object_count
    for child in node.children:
        total += _roll_up_counts(child)
    node.total_object_count = total
    return total
