from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from core.errors import ResumeStateError


@contextmanager
def output_job_lock(state_dir: Path):
    """Hold an OS-backed lock for the whole job, including all artifact writes."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "job.lock"
    # Keep the inode in place after release: deleting a lock file can allow two
    # processes to acquire different inodes for the same job directory.
    with path.open("a+b") as handle:
        acquired = False
        try:
            try:
                if os.name == "nt":
                    import msvcrt

                    if path.stat().st_size == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                raise ResumeStateError(
                    f"Output job is already in use: {state_dir.parent}",
                    remediation="Wait for the other AutoAudio process to finish, or choose a different output directory.",
                ) from exc
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({"pid": os.getpid()}).encode("utf-8"))
            handle.flush()
            yield
        finally:
            if acquired:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
