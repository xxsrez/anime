"""Atomic publication of fully prepared sibling directories."""

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import tempfile

from scripts.operation_lock import (
    DEFAULT_LOCK_TIMEOUT_SECONDS,
    DatabaseOperationLock,
)


def canonical_publish_target(target):
    """Resolve parent aliases without following the replaceable final path."""
    target = Path(target).expanduser().absolute()
    return target.parent.resolve() / target.name


def publish_lock_path(target):
    target = canonical_publish_target(target)
    return target.parent / f".{target.name}.publish.lock"


@contextmanager
def atomic_publish_directory(
    target,
    *,
    stage_prefix=None,
    previous_prefix=None,
    wait=False,
    timeout=DEFAULT_LOCK_TIMEOUT_SECONDS,
    operation="directory publication",
):
    """Yield a stage directory and publish it over ``target`` on clean exit.

    A canonical sibling lock serializes the complete stage-build-and-swap
    lifecycle. If building or publication fails, the stage is removed and the
    previous target is restored whenever the target path is still free. If a
    competing target prevents rollback, the recovery directory is preserved.
    """
    target = canonical_publish_target(target)
    with DatabaseOperationLock(
        target,
        path=publish_lock_path(target),
        wait=wait,
        timeout=timeout,
        operation=operation,
    ):
        stage = Path(
            tempfile.mkdtemp(
                prefix=stage_prefix or f".{target.name}.stage-",
                dir=target.parent,
            )
        )
        previous = None
        published = False
        try:
            yield stage
            if target.exists():
                previous = Path(
                    tempfile.mkdtemp(
                        prefix=previous_prefix or f".{target.name}.previous-",
                        dir=target.parent,
                    )
                )
                previous.rmdir()
                os.replace(target, previous)
            try:
                os.replace(stage, target)
                published = True
            except Exception:
                if previous is not None and previous.exists() and not target.exists():
                    os.replace(previous, target)
                raise
            if previous is not None:
                shutil.rmtree(previous)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
            if published and previous is not None and previous.exists():
                shutil.rmtree(previous)
