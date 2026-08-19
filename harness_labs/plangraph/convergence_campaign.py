"""Campaign checkpoint, artifact store, and target pin (CC-02).

Two durability primitives for one campaign, sitting beside the append-only
ledger (``harness_labs/plangraph/convergence_ledger.py``, CC-01) under the
same campaign root (``state-checkpoint-store``):

* :class:`CampaignCheckpointStore` — ``convergence-campaign-checkpoint/1``.
  Atomic replace (write-temp + fsync + rename + directory fsync), a
  monotonically increasing sequence number, a freeform ``lifecycle`` field
  (the closed vocabulary belongs to the CC-04 driver, not this module), and
  an owner/liveness (heartbeat) stamp. The checkpoint names the base commit
  it believes current; a load presented with a different repository head
  raises :class:`CampaignCheckpointStaleError`, a distinct, typed refusal.
* :class:`CampaignArtifactStore` — content-addressed (``objects/<digest>``),
  recording size, media type, and retention. Sealing copies bytes in
  atomically (temp + rename within the store, same discipline as the
  checkpoint); the store owns its own copy, so a later deletion of the
  source worktree leaves every lookup by digest succeeding.
  :meth:`CampaignArtifactStore.seal_audit_result` walks an ``audit_result``'s
  findings' ``evidence_refs`` and seals every one, refusing (rather than
  silently skipping) any ref with no matching source.

Also implements the campaign-config and target-pin surface
(``contracts-target``): :func:`build_campaign_config` records the
``pre_journal_sanitizer`` hook and the recall/amendment-ratio thresholds;
:func:`pin_target` copies the target file into the campaign root (rejecting a
``snapshot_relative_path`` that escapes it) and records ``campaign_opened``
(delegating the record itself to :meth:`ConvergenceLedger.open_campaign`,
CC-01); :func:`reject_target_grant` refuses the pinned target path as a
repair-node grant, reusing ``plan_graph_contract.path_is_allowed`` rather
than reimplementing path containment. It falls back to the target's
``snapshot_path`` when the module-added ``path`` field is absent, and raises
rather than permitting when neither is present, so a target built to the
plan's literal ``kind``/``digest``/``snapshot_path`` contract is still
checked. The scopeless-``target_amended`` blocked state itself is already
derived by :meth:`ConvergenceLedger.is_blocked` (CC-01); this module adds no
second copy of that logic.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import mimetypes
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from harness_labs.plangraph.convergence_ledger import ConvergenceLedger
from harness_labs.plangraph.plan_graph_contract import path_is_allowed

CHECKPOINT_PROTOCOL = "convergence-campaign-checkpoint/1"
ARTIFACT_STORE_PROTOCOL = "convergence-campaign-artifact-store/1"

CONFIG_SANITIZER_KEY = "pre_journal_sanitizer"
CONFIG_RECALL_THRESHOLD_KEY = "inspector_recall_threshold"
CONFIG_AMENDMENT_RATIO_THRESHOLD_KEY = "amendment_ratio_threshold"

_DEFAULT_MEDIA_TYPE = "application/octet-stream"
_DEFAULT_RETENTION = "campaign"


class ConvergenceCampaignError(ValueError):
    """Raised on a malformed checkpoint, artifact-store operation, target
    pin, or campaign config."""


class CampaignCheckpointSequenceError(ConvergenceCampaignError):
    """Raised when a checkpoint save or load observes a non-monotonic
    sequence number (a save that does not advance past the stored sequence,
    or a load whose sequence regresses from one this store already
    observed)."""


class CampaignCheckpointStaleError(ConvergenceCampaignError):
    """Raised when a loaded checkpoint's recorded ``base_commit`` does not
    match the repository head presented at load."""


# -- checkpoint --------------------------------------------------------------


@dataclass(frozen=True)
class CampaignCheckpoint:
    campaign_id: str
    lifecycle: str
    sequence: int
    base_commit: str
    liveness_at: str
    owner: str | None
    state: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol": CHECKPOINT_PROTOCOL,
            "campaign_id": self.campaign_id,
            "lifecycle": self.lifecycle,
            "sequence": self.sequence,
            "base_commit": self.base_commit,
            "liveness_at": self.liveness_at,
            "owner": self.owner,
            "state": dict(self.state),
        }


class CampaignCheckpointStore:
    """One campaign's checkpoint file at ``path`` (``state-checkpoint-store``)."""

    protocol = CHECKPOINT_PROTOCOL

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._last_sequence: int | None = None

    def save(
        self,
        *,
        campaign_id: str,
        lifecycle: str,
        base_commit: str,
        state: Mapping[str, Any] | None = None,
        owner: str | None = None,
        sequence: int | None = None,
        liveness_at: str | None = None,
    ) -> CampaignCheckpoint:
        """Atomically replace the checkpoint file with a new sequence.

        ``sequence`` defaults to one past whatever is currently on disk (or
        ``1`` for a first save). Passing an explicit ``sequence`` that does
        not advance past the stored one -- including a caller trying to go
        backwards -- raises :class:`CampaignCheckpointSequenceError` without
        touching the file.
        """

        if not isinstance(campaign_id, str) or not campaign_id.strip():
            raise ConvergenceCampaignError("checkpoint campaign_id must be a non-empty string")
        if not isinstance(lifecycle, str) or not lifecycle.strip():
            raise ConvergenceCampaignError("checkpoint lifecycle must be a non-empty string")
        if not isinstance(base_commit, str) or not base_commit.strip():
            raise ConvergenceCampaignError("checkpoint base_commit must be a non-empty string")

        previous = self._read_raw()
        previous_sequence = previous["sequence"] if previous is not None else 0
        next_sequence = previous_sequence + 1 if sequence is None else sequence
        if next_sequence <= previous_sequence:
            raise CampaignCheckpointSequenceError(
                "checkpoint sequence must advance past "
                f"{previous_sequence}; got {next_sequence}"
            )

        checkpoint = CampaignCheckpoint(
            campaign_id=campaign_id,
            lifecycle=lifecycle,
            sequence=next_sequence,
            base_commit=base_commit,
            liveness_at=liveness_at or _now_iso(),
            owner=owner,
            state=dict(state or {}),
        )
        _atomic_write(self.path, _encode_json(checkpoint.as_dict()))
        self._last_sequence = next_sequence
        return checkpoint

    def load(self, *, repository_head: str | None = None) -> CampaignCheckpoint:
        """Read the current checkpoint.

        Raises :class:`CampaignCheckpointSequenceError` if this store
        instance previously observed a higher sequence than the one now on
        disk (a regression -- e.g. an older checkpoint file swapped in
        underneath it), and :class:`CampaignCheckpointStaleError` if
        ``repository_head`` is given and differs from the checkpoint's
        recorded ``base_commit``.
        """

        raw = self._read_raw()
        if raw is None:
            raise ConvergenceCampaignError(f"no checkpoint found at {self.path}")

        sequence = raw["sequence"]
        if self._last_sequence is not None and sequence < self._last_sequence:
            raise CampaignCheckpointSequenceError(
                "checkpoint load observed a sequence regression: "
                f"{sequence} < {self._last_sequence}"
            )
        self._last_sequence = sequence

        if repository_head is not None and raw["base_commit"] != repository_head:
            raise CampaignCheckpointStaleError(
                f"checkpoint base_commit {raw['base_commit']!r} does not match "
                f"repository head {repository_head!r} presented at load"
            )

        return CampaignCheckpoint(
            campaign_id=raw["campaign_id"],
            lifecycle=raw["lifecycle"],
            sequence=sequence,
            base_commit=raw["base_commit"],
            liveness_at=raw["liveness_at"],
            owner=raw.get("owner"),
            state=dict(raw.get("state", {})),
        )

    def _read_raw(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if raw.get("protocol") != self.protocol:
            raise ConvergenceCampaignError(
                f"checkpoint at {self.path} has an unrecognized protocol"
            )
        return raw


# -- content-addressed artifact store -----------------------------------


@dataclass(frozen=True)
class ArtifactRecord:
    digest: str
    algorithm: str
    size_bytes: int
    media_type: str
    retention: str = _DEFAULT_RETENTION

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol": ARTIFACT_STORE_PROTOCOL,
            "digest": self.digest,
            "algorithm": self.algorithm,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "retention": self.retention,
        }


class CampaignArtifactStore:
    """Content-addressed store under ``root/objects/<digest>``.

    Sealing copies bytes in atomically (temp + rename, same as the
    checkpoint) and records size and media type alongside the content, so a
    lookup by digest never needs the original source file: the store owns
    its own copy.
    """

    protocol = ARTIFACT_STORE_PROTOCOL

    def __init__(self, root: Path, *, algorithm: str = "sha256") -> None:
        self.root = Path(root)
        self.algorithm = algorithm

    def seal(
        self,
        source: Path,
        *,
        media_type: str | None = None,
        retention: str | None = None,
    ) -> ArtifactRecord:
        """Copy ``source`` into the store keyed by its content digest."""

        source = Path(source)
        data = source.read_bytes()
        digest = hashlib.new(self.algorithm, data).hexdigest()
        resolved_media_type = media_type or _guess_media_type(source)

        content_path = self._content_path(digest)
        if not content_path.exists():
            _atomic_write(content_path, data)
        record = ArtifactRecord(
            digest=digest,
            algorithm=self.algorithm,
            size_bytes=len(data),
            media_type=resolved_media_type,
            retention=retention or _DEFAULT_RETENTION,
        )
        _atomic_write(self._metadata_path(digest), _encode_json(record.as_dict()))
        return record

    def seal_many(
        self,
        sources: Sequence[Path] | Mapping[str, Path],
        *,
        media_type_of: "Mapping[str, str] | None" = None,
    ) -> dict[str, ArtifactRecord]:
        """Seal every referenced evidence file; returns ``{ref: record}``.

        ``sources`` may be a bare sequence of paths (keyed by their own
        string form) or a mapping of caller-chosen refs to paths, matching
        how an ``audit_result``'s evidence references are carried.
        """

        items = (
            sources.items()
            if isinstance(sources, Mapping)
            else ((str(path), path) for path in sources)
        )
        media_types = media_type_of or {}
        return {
            ref: self.seal(path, media_type=media_types.get(ref))
            for ref, path in items
        }

    def seal_audit_result(
        self,
        audit_result: Mapping[str, Any],
        *,
        evidence_sources: Mapping[str, Path],
        media_type_of: "Mapping[str, str] | None" = None,
    ) -> dict[str, ArtifactRecord]:
        """Seal every evidence file referenced by ``audit_result``'s findings.

        Walks ``audit_result["findings"][*]["evidence_refs"]`` (the CC-01
        ledger's finding-ingest shape,
        ``harness_labs/plangraph/convergence_ledger.py``) collecting the
        distinct refs, then seals the source path each ref names via
        ``evidence_sources`` -- a caller-supplied ref-to-path resolution,
        since a ref is an opaque string, not itself a filesystem path. A ref
        with no matching entry in ``evidence_sources`` raises rather than
        being silently dropped, so a missing evidence file cannot pass this
        gate unnoticed.
        """

        findings = audit_result.get("findings", [])
        if not isinstance(findings, list):
            raise ConvergenceCampaignError("audit_result 'findings' must be a list")

        refs: list[str] = []
        for finding in findings:
            for ref in finding.get("evidence_refs") or []:
                if ref not in refs:
                    refs.append(ref)

        missing = [ref for ref in refs if ref not in evidence_sources]
        if missing:
            raise ConvergenceCampaignError(
                f"audit_result evidence_refs missing from evidence_sources: {missing!r}"
            )

        return self.seal_many(
            {ref: evidence_sources[ref] for ref in refs}, media_type_of=media_type_of
        )

    def contains(self, digest: str) -> bool:
        return self._content_path(digest).exists()

    def lookup(self, digest: str) -> Path:
        path = self._content_path(digest)
        if not path.exists():
            raise ConvergenceCampaignError(f"no artifact stored for digest {digest!r}")
        return path

    def open_bytes(self, digest: str) -> bytes:
        return self.lookup(digest).read_bytes()

    def metadata(self, digest: str) -> ArtifactRecord:
        meta_path = self._metadata_path(digest)
        if not meta_path.exists():
            raise ConvergenceCampaignError(
                f"no artifact metadata stored for digest {digest!r}"
            )
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        return ArtifactRecord(
            digest=raw["digest"],
            algorithm=raw["algorithm"],
            size_bytes=raw["size_bytes"],
            media_type=raw["media_type"],
            retention=raw.get("retention", _DEFAULT_RETENTION),
        )

    def _content_path(self, digest: str) -> Path:
        return self.root / "objects" / digest

    def _metadata_path(self, digest: str) -> Path:
        return self.root / "objects" / f"{digest}.json"


# -- campaign config, target pin, and grant refusal (contracts-target) ------


def build_campaign_config(
    *,
    pre_journal_sanitizer: str,
    recall_threshold: float,
    amendment_ratio_threshold: float,
) -> dict[str, Any]:
    """Validate and assemble the campaign config surface.

    Records the sanitizer hook (a reference the driver later resolves and
    invokes; this module only validates and carries it) and the two
    calibration thresholds referenced at ``bounds-termination``: inspector
    recall and amendment ratio.
    """

    if not isinstance(pre_journal_sanitizer, str) or not pre_journal_sanitizer.strip():
        raise ConvergenceCampaignError(
            "campaign config pre_journal_sanitizer hook must be a non-empty string"
        )
    for field, value in (
        ("recall_threshold", recall_threshold),
        ("amendment_ratio_threshold", amendment_ratio_threshold),
    ):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not (0.0 <= float(value) <= 1.0)
        ):
            raise ConvergenceCampaignError(
                f"campaign config {field} must be a number between 0 and 1"
            )
    return {
        CONFIG_SANITIZER_KEY: pre_journal_sanitizer,
        CONFIG_RECALL_THRESHOLD_KEY: float(recall_threshold),
        CONFIG_AMENDMENT_RATIO_THRESHOLD_KEY: float(amendment_ratio_threshold),
    }


def pin_target(
    ledger: ConvergenceLedger,
    *,
    campaign_root: Path,
    domain: str,
    source_path: Path,
    target_kind: str,
    snapshot_relative_path: str,
    base_commit: str,
    pre_journal_sanitizer: str,
    recall_threshold: float,
    amendment_ratio_threshold: float,
    merge_base: str | None = None,
    predecessor_graph_id: str | None = None,
    seed_audit_digest: str | None = None,
    repo_identity_branch: str | None = None,
) -> dict[str, Any]:
    """Pin the campaign target and record ``campaign_opened``.

    Copies ``source_path`` into ``campaign_root / snapshot_relative_path``
    (atomically, same discipline as the checkpoint and artifact store),
    computes its digest, and delegates the ``campaign_opened`` record itself
    to :meth:`ConvergenceLedger.open_campaign` (CC-01). The target dict
    carries an extra ``path`` field (the original source path) beyond the
    ledger's required ``kind``/``digest``/``snapshot_path`` -- the ledger
    passes extra target keys through unvalidated, and :func:`reject_target_grant`
    reads it back to refuse the target as a repair-node grant.
    """

    source_path = Path(source_path)
    if not source_path.is_file():
        raise ConvergenceCampaignError(f"target source path does not exist: {source_path}")
    if not isinstance(snapshot_relative_path, str) or not snapshot_relative_path.strip():
        raise ConvergenceCampaignError("target snapshot_relative_path must be a non-empty string")
    relative_parts = PurePosixPath(snapshot_relative_path)
    if relative_parts.is_absolute() or ".." in relative_parts.parts:
        raise ConvergenceCampaignError(
            "target snapshot_relative_path must stay inside the campaign root, "
            f"got {snapshot_relative_path!r}"
        )

    content = source_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()

    campaign_root = Path(campaign_root)
    snapshot_dest = campaign_root / snapshot_relative_path
    _atomic_write(snapshot_dest, content)

    config = build_campaign_config(
        pre_journal_sanitizer=pre_journal_sanitizer,
        recall_threshold=recall_threshold,
        amendment_ratio_threshold=amendment_ratio_threshold,
    )
    target = {
        "kind": target_kind,
        "digest": digest,
        "snapshot_path": snapshot_relative_path,
        "path": str(source_path),
    }
    return ledger.open_campaign(
        domain=domain,
        target=target,
        base_commit=base_commit,
        merge_base=merge_base,
        predecessor_graph_id=predecessor_graph_id,
        seed_audit_digest=seed_audit_digest,
        repo_identity_branch=repo_identity_branch,
        config=config,
    )


def reject_target_grant(target: Mapping[str, Any], allowed_paths: Sequence[str]) -> None:
    """Refuse ``allowed_paths`` (a repair node's grant) when it covers the
    pinned target's path.

    Reuses ``plan_graph_contract.path_is_allowed`` -- the same containment
    rule admission and refinement already use for grant/intent subset
    checks -- rather than a second copy of path overlap logic. Prefers the
    module-added ``path`` field (the original source path) but falls back to
    ``snapshot_path`` -- the one path field the ledger's target contract
    actually guarantees (``ConvergenceLedger.open_campaign``) -- so a target
    built to that literal contract is still checked. A target with neither
    field is malformed and raises rather than being permitted by default.
    """

    path = target.get("path") or target.get("snapshot_path")
    if not path:
        raise ConvergenceCampaignError(
            "target mapping must include a 'path' or 'snapshot_path' to check "
            "against a repair-node grant"
        )
    if path_is_allowed(str(path), list(allowed_paths)):
        raise ConvergenceCampaignError(
            f"repair-node grant {list(allowed_paths)!r} must not cover the "
            f"pinned target path {path!r}"
        )


# -- shared atomic-replace primitive (write-temp + fsync + rename + dir fsync) -


def _atomic_write(path: Path, data: bytes) -> None:
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    _fsync_directory(directory)


def _fsync_directory(directory: Path) -> None:
    directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _encode_json(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _guess_media_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or _DEFAULT_MEDIA_TYPE


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
