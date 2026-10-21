"""Compose explicit primary, derivative and summary artifacts."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass

from ._channels import Channel
from ._core import (
    AVAILABLE_STATUSES,
    PAYLOAD_SCHEMA,
    Access,
    Artifact,
    FormatError,
    IncompleteRecordingError,
    insert_unique,
    match_identity,
    read_json,
)
from ._features import Features, TemporalFeature


@dataclass(frozen=True)
class MemoryRecording:
    """Detached samples, event tables, feature rows and metadata in Python memory."""

    signals: dict
    annotations: dict
    features: dict
    metadata: dict
    availability: dict


class Recording:
    """A lazy, read-only logical recording over separate artifacts.

    Use :func:`open_recording` and a context manager. No inference, calibration,
    resampling, cloud discovery or fallback to the archived H5 occurs here.
    """

    def __init__(self, primary, derivatives, summary_json, require_complete):
        self._access = Access()
        self._artifacts = []
        self.signals = {}
        self.annotations = {}
        self.metadata = {}
        issues = []
        try:
            primary_artifact = self._open(primary)
            derivative_artifact = (
                self._open(derivatives) if derivatives is not None else None
            )
            if derivative_artifact:
                match_identity(primary_artifact.identity, derivative_artifact.identity)
                p_source = primary_artifact.metadata.get("source", {}).get("file")
                d_source = derivative_artifact.metadata.get("source", {}).get("file")
                if p_source and d_source and p_source != d_source:
                    raise FormatError(
                        "Primary and derivative declare different source files"
                    )
            summary = read_json(summary_json) if summary_json is not None else None
            self.features = Features(summary)
            if summary is not None:
                try:
                    subject = summary["subject"]["label"]
                    session = summary["subject"]["session"]
                    duration = float(summary["recording"]["duration_s"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise FormatError(
                        "Summary lacks recording identity or duration"
                    ) from exc
                match_identity(primary_artifact.identity, (subject, session, duration))
                if summary.get("recording_id") != f"{subject}_{session}":
                    raise FormatError(
                        "Summary recording_id contradicts its subject/session"
                    )
                if summary.get("counts", {}).get("summary_features") != len(
                    self.features.summary
                ):
                    raise FormatError(
                        "Summary row count disagrees with counts.summary_features"
                    )
                if summary.get("counts", {}).get("not_scalar", 0):
                    issues.append(
                        "Summary declares non-scalar outputs not represented by its vectors"
                    )
                for key, row in self.features.summary.raw.items():
                    if (
                        row["status"] in AVAILABLE_STATUSES
                        and not self.features.summary[key]["available"]
                    ):
                        issues.append(
                            "An available summary row has no supported exported value"
                        )
                        break
            else:
                issues.append("Summary JSON was not supplied")

            schema = None
            skipped = {}
            for label, artifact in (
                ("primary", primary_artifact),
                ("derivatives", derivative_artifact),
            ):
                if artifact is None:
                    issues.append("Derivative Zarr was not supplied")
                    continue
                self.metadata[label] = artifact.metadata
                source = artifact.metadata.get("source", {})
                if "skipped" not in source:
                    issues.append(f"{label} has no skipped-export inventory")
                skipped[label] = source.get("skipped", {})
                if any(skipped[label].values()):
                    issues.append(f"{label} records skipped exports")
                if label == "derivatives":
                    schema = source.get("feature_payload_schema")
                    if schema is None:
                        issues.append(
                            "Derivative lacks the lossless temporal payload schema marker"
                        )
                    elif schema != PAYLOAD_SCHEMA:
                        raise FormatError(
                            "Unsupported derivative feature payload schema"
                        )
                self._index_channels(artifact, label, issues)
            if not self.signals:
                issues.append("Primary has no physiological signal channels")
            if summary is not None:
                self.metadata["summary"] = summary
            self.availability = {
                "primary": True,
                "derivatives": derivatives is not None,
                "summary": summary is not None,
                "feature_payload_schema": schema,
                "temporal_families": len(self.features.time_series),
                "feature_projections": len(self.features.projections),
                "skipped": skipped,
                "complete": not issues,
                "issues": tuple(issues),
                "identity_binding": "metadata_only",
                "completeness_scope": "declared_exported_signals_annotations_temporal_payloads_summary_scalars_vectors",
            }
            if require_complete and issues:
                raise IncompleteRecordingError("; ".join(dict.fromkeys(issues)))
        except BaseException:
            self.close()
            raise

    def _open(self, reference):
        artifact = Artifact(reference)
        self._artifacts.append(artifact)
        return artifact

    def _index_channels(self, artifact, label, issues):
        payload_paths = {}
        channel_metadata = self.metadata.setdefault("channels", {}).setdefault(
            label, {}
        )
        for group in artifact.channels():
            channel = Channel(group, self._access, label)
            insert_unique(channel_metadata, channel.id, channel.metadata)
            origin = channel.provenance.get("origin")
            if origin == "signal":
                if label != "primary" or channel.kind != "continuous":
                    raise FormatError(
                        "Physiological signals must be continuous primary channels"
                    )
                insert_unique(self.signals, channel.name, channel)
            elif origin == "annotation":
                scorer = channel.provenance.get("annotator")
                if (
                    channel.kind != "event"
                    or not scorer
                    or not channel.name.startswith(f"{scorer}/")
                ):
                    raise FormatError("Annotation channel identity is inconsistent")
                kind = channel.name[len(scorer) + 1 :]
                insert_unique(self.annotations.setdefault(scorer, {}), kind, channel)
            elif origin == "feature":
                if label != "derivatives":
                    raise FormatError("Feature channels must be in the derivative")
                feature_id = channel.provenance.get("feature_id")
                family = channel.provenance.get("family")
                if not feature_id or not family:
                    raise FormatError(
                        "Feature projection requires feature_id and family"
                    )
                insert_unique(self.features.projections, feature_id, channel)
                reference = channel.provenance.get("feature_payload")
                if reference is None:
                    issues.append(
                        "Feature projection lacks its original temporal payload"
                    )
                    continue
                path = reference.get("path", "")
                if (
                    reference.get("schema") != PAYLOAD_SCHEMA
                    or not isinstance(path, str)
                    or not re.fullmatch(r"[0-9]+/feature_payload", path)
                ):
                    raise FormatError(
                        "Unsupported or invalid feature payload reference"
                    )
                if path in payload_paths:
                    if payload_paths[path] != family:
                        raise FormatError(
                            "Different families reference the same payload"
                        )
                    continue
                if path not in artifact.root:
                    raise FormatError("Feature payload reference does not exist")
                payload = artifact.root[path]
                if (
                    payload.attrs.get("schema") != PAYLOAD_SCHEMA
                    or "source" not in payload
                ):
                    raise FormatError(
                        "Feature payload has an invalid schema or no source"
                    )
                feature = TemporalFeature(payload["source"], self._access)
                feature.export_metadata = dict(payload.attrs)
                if feature.feature_id != family:
                    raise FormatError(
                        "Payload identity differs from its projection family"
                    )
                insert_unique(self.features.time_series, family, feature)
                payload_paths[path] = family
                if feature.storage == "numeric_array":
                    self.features.arrays[family] = feature
                elif feature.storage == "categorical_json":
                    self.features.structured[family] = feature
            else:
                raise FormatError("Channel has an unsupported provenance origin")

    def materialize(self):
        """Explicitly load all exposed values; the result survives closing stores.

        Original masks/attrs remain available on lazy ``TemporalFeature.raw``.
        Materialized tables use None for absent values and retain row statuses.
        This may require substantial RAM for overnight signals/spectrograms.
        """
        self._access.check()
        return MemoryRecording(
            signals={name: channel.read() for name, channel in self.signals.items()},
            annotations={
                scorer: {kind: channel.read() for kind, channel in tracks.items()}
                for scorer, tracks in self.annotations.items()
            },
            features={
                "summary": copy.deepcopy(dict(self.features.summary)),
                "vectors": {
                    name: values.copy()
                    for name, values in self.features.vectors.items()
                },
                "time_series": {
                    name: family.read()
                    for name, family in self.features.time_series.items()
                },
                "metadata": {
                    name: copy.deepcopy(
                        {
                            "source": family.metadata,
                            "export": family.export_metadata,
                            "information_schema": family.information_schema,
                        }
                    )
                    for name, family in self.features.time_series.items()
                },
                "projections": {
                    name: channel.read()
                    for name, channel in self.features.projections.items()
                },
            },
            metadata=copy.deepcopy(self.metadata),
            availability=copy.deepcopy(self.availability),
        )

    def close(self):
        if not self._access.closed:
            self._access.closed = True
            for artifact in reversed(self._artifacts):
                artifact.close()

    def __enter__(self):
        self._access.check()
        return self

    def __exit__(self, *exc):
        self.close()


def open_recording(
    *, primary, derivatives=None, summary_json=None, require_complete=True
):
    """Open explicit local paths or caller-configured Zarr v3 Stores read-only.

    ``summary_json`` accepts a local JSON path or a decoded mapping. Remote
    stores must contain recording.json and be configured/authenticated by the
    caller. Caller-owned Stores are not closed by this reader.

    By default require derivatives, JSON, lossless temporal payloads and no
    declared skipped exports. Set require_complete=False to inspect a legacy or
    primary-only recording and check ``availability`` before using its features.
    Completeness is limited to the supported export schema, not all possible
    producer outputs; matching metadata does not prove common content identity.
    """
    return Recording(primary, derivatives, summary_json, require_complete)
