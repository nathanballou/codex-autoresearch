#!/usr/bin/env python3
"""Curated overarching documents shared with every candidate worker."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from autoresearch_core import DOCS_DIR, AutoresearchError, Paths, write_text_atomic

GOAL_FILE = "goal.md"
DECISIONS_FILE = "decisions.md"
COMPUTE_FILE = "compute.json"

# Small on purpose. These are injected into every worker packet, so an unbounded
# file would quietly consume the context the worker needs for its actual task.
MAX_DOC_BYTES = 4096


def doc_path(repo: Path, name: str) -> Path:
    """
    Resolve one curated document inside the repository.
    Args:
    repo: Repository root.
    name: File name within the curated documents directory.
    Return: Absolute path to the document.
    """
    return repo / DOCS_DIR / name


def read_doc(repo: Path, name: str, *, required: bool) -> str:
    """
    Read one curated document and enforce the size cap.
    Args:
    repo: Repository root.
    name: File name within the curated documents directory.
    required: When true, a missing or empty document is an error.
    Return: Document text, or an empty string when an optional document is absent.
    """
    path = doc_path(repo, name)
    if not path.exists():
        if required:
            raise AutoresearchError(
                f"Required curated document is missing: {path}. "
                f"Create it with the overarching goal before initializing a run."
            )
        return ""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AutoresearchError(f"Cannot read {path}: {exc}") from exc
    if len(raw) > MAX_DOC_BYTES:
        raise AutoresearchError(
            f"{path} is {len(raw)} bytes, over the {MAX_DOC_BYTES}-byte limit. "
            f"These documents are injected into every worker packet; keep them short."
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AutoresearchError(f"{path} must be UTF-8: {exc}") from exc
    if required and not text.strip():
        raise AutoresearchError(f"{path} is empty. State the overarching goal before starting.")
    return text


def content_hash(text: str) -> str:
    """
    Hash document text for provenance.
    Args:
    text: Document contents.
    Return: Hex sha256 digest.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot(paths: Paths, name: str, text: str) -> str:
    """
    Store a content-addressed copy of one document under the results directory.
    Args:
    paths: Resolved run paths.
    name: Document file name, used as the snapshot suffix.
    text: Document contents to store.
    Return: The sha256 digest identifying the snapshot.
    """
    digest = content_hash(text)
    directory = paths.root / "docs"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{digest}.{name}"
    if not target.exists():
        write_text_atomic(target, text)
    return digest


def load_and_snapshot(repo: Path, paths: Paths) -> dict[str, Any]:
    """
    Read, cap, and snapshot every curated document for one run.
    Args:
    repo: Repository root.
    paths: Resolved run paths.
    Return: Mapping of document paths and their snapshot digests, shaped for run.json.
    """
    goal = read_doc(repo, GOAL_FILE, required=True)
    decisions = read_doc(repo, DECISIONS_FILE, required=False)
    return {
        "goal_path": f"{DOCS_DIR}/{GOAL_FILE}",
        "decisions_path": f"{DOCS_DIR}/{DECISIONS_FILE}",
        "goal_sha256": snapshot(paths, GOAL_FILE, goal),
        "decisions_sha256": snapshot(paths, DECISIONS_FILE, decisions),
    }


def require_docs_match(repo: Path, expected: dict[str, str]) -> None:
    """
    Refuse to proceed when the curated documents drifted from their recorded snapshots.
    Args:
    repo: Repository root.
    expected: Mapping with goal_sha256 and decisions_sha256 as last recorded.
    Return: None.

    The documents are excluded from experiment change detection so the main thread can
    curate them between candidates, which means an unrecorded edit would otherwise pass
    unnoticed. This turns that silence into an actionable error.
    """
    actual = {
        "goal_sha256": content_hash(read_doc(repo, GOAL_FILE, required=True)),
        "decisions_sha256": content_hash(read_doc(repo, DECISIONS_FILE, required=False)),
    }
    drifted = sorted(key for key, value in actual.items() if value != expected[key])
    if drifted:
        names = {"goal_sha256": GOAL_FILE, "decisions_sha256": DECISIONS_FILE}
        changed = ", ".join(f"{DOCS_DIR}/{names[key]}" for key in drifted)
        raise AutoresearchError(
            f"Curated documents changed without being recorded: {changed}. "
            f"Use 'decide --add' to record a decision, or restore the file."
        )


def append_decision(repo: Path, paths: Paths, note: str) -> tuple[str, str]:
    """
    Append one curated decision and re-snapshot the decisions document.
    Args:
    repo: Repository root.
    paths: Resolved run paths.
    note: Decision text to record as a bullet.
    Return: Tuple of the normalized note and the new decisions digest.
    """
    cleaned = " ".join(note.split())
    if not cleaned:
        raise AutoresearchError("--add cannot be empty")
    existing = read_doc(repo, DECISIONS_FILE, required=False)
    if not existing:
        existing = "# Decisions\n\nNotes every candidate worker receives.\n\n"
    if not existing.endswith("\n"):
        existing += "\n"
    updated = f"{existing}- {cleaned}\n"
    if len(updated.encode("utf-8")) > MAX_DOC_BYTES:
        raise AutoresearchError(
            f"Adding this decision would push {doc_path(repo, DECISIONS_FILE)} over the "
            f"{MAX_DOC_BYTES}-byte limit. Consolidate the existing notes first."
        )
    path = doc_path(repo, DECISIONS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, updated)
    return cleaned, snapshot(paths, DECISIONS_FILE, updated)
