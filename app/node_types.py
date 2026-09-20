"""Backward-compatible facade for the versioned runtime Node Registry."""

from __future__ import annotations

from app.runtime import NODE_REGISTRY
from app.runtime.registry import NodeManifest as NodeTypeManifest, NodeRegistration


def _latest() -> dict[str, NodeTypeManifest]:
    result: dict[str, NodeTypeManifest] = {}
    for (node_type, _), registration in NODE_REGISTRY._nodes.items():
        current = result.get(node_type)
        if current is None or registration.manifest.schema_version > current.schema_version:
            result[node_type] = registration.manifest
    return result


NODE_TYPES: dict[str, NodeTypeManifest] = _latest()


def register_node_type(manifest: NodeTypeManifest) -> None:
    NODE_REGISTRY.register(NodeRegistration(manifest))
    NODE_TYPES[manifest.type] = manifest
