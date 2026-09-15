"""Verify the unchanged legacy export at its pinned public Git commit."""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path

from research_map.development import LEGACY_COMMIT
from research_map.public_release import verify_public_release


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    archive = subprocess.run(
        ["git", "-C", str(root), "archive", "--format=tar", LEGACY_COMMIT],
        check=True,
        capture_output=True,
    ).stdout
    with tempfile.TemporaryDirectory(prefix="arw-legacy-check-") as temporary:
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(temporary, filter="data")
        result = verify_public_release(Path(temporary))
        print(
            json.dumps(
                {
                    "ok": True,
                    "legacy_commit": LEGACY_COMMIT,
                    "tree_sha256": result.tree_sha256,
                    "file_count": result.file_count,
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
