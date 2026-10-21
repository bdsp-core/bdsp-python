"""Small format and lifetime helpers; Zarr remains an optional dependency."""

from __future__ import annotations

import json
import math
import sys
import warnings
from pathlib import Path

PAYLOAD_SCHEMA = "bdsp.sleep-feature-payload.v1"
SUMMARY_SCHEMA = "psg_features_sidecar_v0.5"
AVAILABLE_STATUSES = frozenset({"ok", "valid_zero"})


class FormatError(ValueError):
    """An artifact violates a supported storage contract."""


class IncompleteRecordingError(FormatError):
    """The requested enriched export is missing declared representations."""


class Access:
    def __init__(self):
        self.closed = False

    def check(self):
        if self.closed:
            raise RuntimeError(
                "Recording is closed; materialize data before closing it"
            )


def read_json(reference):
    if isinstance(reference, dict):
        return reference
    with Path(reference).open(encoding="utf-8") as stream:
        return json.load(stream)


def time_bounds(start, duration):
    lower = -math.inf if start is None else float(start)
    if start is not None and not math.isfinite(lower):
        raise ValueError("start must be finite")
    if duration is None:
        return lower, math.inf
    duration = float(duration)
    if start is None or not math.isfinite(duration) or duration < 0:
        raise ValueError("A finite nonnegative duration requires start")
    return lower, lower + duration


def overlaps(starts, ends, lower, upper):
    # Include point events at t in [lower, upper); intervals intersect the window.
    return (starts < upper) & ((ends > lower) | ((ends == starts) & (starts >= lower)))


class Artifact:
    def __init__(self, reference):
        if sys.version_info < (3, 11):
            raise ImportError("The Zarr reader requires Python 3.11+ and bdsp[zarr]")
        try:
            import zarr
        except ImportError as exc:
            raise ImportError(
                "Install the optional reader with pip install 'bdsp[zarr]'"
            ) from exc

        self.owned = isinstance(reference, (str, Path))
        if self.owned and "://" in str(reference):
            raise ValueError(
                "Pass an explicitly configured Zarr Store for remote access"
            )
        self.root = zarr.open_group(store=reference, mode="r", use_consolidated=False)
        try:
            if (
                self.root.metadata.zarr_format != 3
                or self.root.attrs.get("tsz", {}).get("version") != "1.0"
            ):
                raise FormatError("Expected Zarr v3 with TS-Zarr version 1.0")
            if self.owned:
                self.metadata = read_json(Path(reference) / "recording.json")
            else:
                from zarr.core.sync import sync

                buffer = sync(self.root.store.get("recording.json"))
                if buffer is None:
                    raise FormatError("Store has no recording.json")
                self.metadata = json.loads(buffer.to_bytes())
            self.identity = identity(self.metadata)
        except BaseException:
            self.close()
            raise

    def channels(self):
        # recording.json is an intentional TS-Zarr sidecar, not a Zarr node.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore", message=r"Object at recording\.json is not recognized.*"
            )
            names = sorted(self.root.group_keys(), key=str)
        for name in names:
            if name.isdecimal():
                yield self.root[name]

    def close(self):
        if self.owned:
            self.root.store.close()


def identity(metadata):
    try:
        subject = metadata["subject"]["id"]
        session = metadata["session"]["id"]
        duration = float(metadata["session"]["duration_s"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FormatError(
            "recording.json needs subject.id, session.id and session.duration_s"
        ) from exc
    if (
        not isinstance(subject, str)
        or not subject
        or not isinstance(session, str)
        or not session
    ):
        raise FormatError("Subject and session IDs must be nonempty strings")
    if not math.isfinite(duration) or duration < 0:
        raise FormatError("Invalid recording duration")
    return subject, session, duration


def match_identity(expected, actual):
    if expected[:2] != actual[:2] or not math.isclose(
        expected[2], actual[2], rel_tol=0, abs_tol=1e-6
    ):
        raise FormatError("Artifacts have different subject, session or duration")


def insert_unique(mapping, key, value):
    if key in mapping:
        raise FormatError("Duplicate channel or family identity")
    mapping[key] = value
