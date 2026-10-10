"""Host-side bounded verification recording; never invoked by an offline evaluator.

The caller supplies a real oracle and an immutable environment identity. Exclusivity
is a host guarantee, not a claim inferred from a successful command or unchanged hash.
"""

import hashlib
from pathlib import Path


def capture_manifest(root, paths):
    root = Path(root).resolve()
    manifest = {}
    for name in sorted(set(paths)):
        path = (root / name).resolve()
        if not path.is_relative_to(root) or path == root or not path.is_file():
            raise ValueError("Verification scope must contain existing files inside the workspace")
        manifest[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not manifest:
        raise ValueError("Verification requires an explicit nonempty bounded scope")
    return manifest


def verification_receipt(*, criterion_id, goal_revision, scope, before, after, oracle, environment, passed, exclusive=False, coverage="partial"):
    if not oracle or not environment or not scope or not before or not after or type(passed) is not bool:
        raise ValueError("Verification receipt requires a real oracle, scope, manifests, environment and boolean result")
    return {
        "type": "verification.recorded",
        "producer": "loom.verification.v1",
        "criterion_id": criterion_id,
        "goal_revision": goal_revision,
        "scope": scope,
        "before": before,
        "after": after,
        "oracle": oracle,
        "environment": environment,
        "exclusive": exclusive,
        "coverage": coverage,
        "status": "supported" if passed else "contradicted",
    }


def artifact_boundary(*, scope, manifest, environment, exclusive=False):
    return {"type": "artifact.version.recorded", "scope": scope, "manifest": manifest, "environment": environment, "exclusive": exclusive}
