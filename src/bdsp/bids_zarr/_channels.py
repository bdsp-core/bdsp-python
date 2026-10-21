"""Native-clock physical signals and microsecond event tables."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ._core import FormatError, overlaps, time_bounds


@dataclass(frozen=True)
class SignalWindow:
    """Samples in their physical units and recording-relative seconds."""

    values: np.ndarray
    times_s: np.ndarray
    rate_hz: float
    unit: str


class Channel:
    def __init__(self, group, access, artifact):
        self.raw = group
        self._access = access
        self.artifact = artifact
        self.metadata = dict(group.attrs.get("tsz", {}))
        self.name = self.metadata.get("name")
        self.id = self.metadata.get("id")
        self.kind = self.metadata.get("kind")
        self.provenance = self.metadata.get("provenance", {})
        if not self.name or not self.id or self.kind not in {"continuous", "event"}:
            raise FormatError("Channel requires name, id and supported kind")
        if self.kind == "continuous":
            self.rate_hz = float(self.metadata.get("rate_hz", 0))
            self.offset_s = float(self.metadata.get("offset_us", 0)) / 1e6
            if (
                not math.isfinite(self.rate_hz)
                or self.rate_hz <= 0
                or not math.isfinite(self.offset_s)
            ):
                raise FormatError("Invalid channel clock")
            if "raw" not in group or group["raw"].ndim != 1:
                raise FormatError(
                    "Continuous channel requires one-dimensional raw samples"
                )
        else:
            if "events" not in group or "durations" not in group:
                raise FormatError("Event channel requires events and durations")
            n = group["events"].shape
            if len(n) != 1 or group["durations"].shape != n:
                raise FormatError("Invalid event time dimensions")
            for key in ("events", "durations"):
                if group[key].dtype.kind not in "iu":
                    raise FormatError("Event times must be integer microseconds")
            for key in ("labels", "values"):
                if key in group and group[key].shape != n:
                    raise FormatError("Event values must match event times")
            if "labels" in group:
                if group["labels"].dtype.kind not in "iu":
                    raise FormatError("Event label indices must be integers")
                names = self.metadata.get("label_names")
                codes = self.provenance.get("label_codes")
                if not isinstance(names, list) or (
                    codes is not None and len(codes) != len(names)
                ):
                    raise FormatError("Invalid label names/original codes mapping")

    @property
    def samples(self):
        self._access.check()
        if self.kind != "continuous":
            raise TypeError("Event channels do not have continuous samples")
        return self.raw["raw"]

    def read(self, start=None, duration=None):
        """Read samples or intersecting events in a half-open window in seconds."""
        self._access.check()
        lower, upper = time_bounds(start, duration)
        if duration == 0:
            upper = lower
        if self.kind == "continuous":
            n = self.samples.shape[0]

            def index(t, default):
                if not math.isfinite(t):
                    return default
                # nextafter avoids losing a sample at an exactly represented boundary.
                x = (t - self.offset_s) * self.rate_hz
                return max(0, min(n, math.ceil(math.nextafter(x, -math.inf))))

            first, last = index(lower, 0), index(upper, n)
            values = np.asarray(self.samples[first:last])
            times = (
                self.offset_s + np.arange(first, last, dtype=np.float64) / self.rate_hz
            )
            return SignalWindow(
                values, times, self.rate_hz, self.metadata.get("unit", "")
            )

        starts = np.asarray(self.raw["events"][:])
        durations = np.asarray(self.raw["durations"][:])
        if np.any(durations < 0):
            raise FormatError("Negative event duration")
        ends = starts + durations
        keep = overlaps(starts / 1e6, ends / 1e6, lower, upper)
        if upper == lower:
            keep[:] = False
        positions = np.flatnonzero(keep)
        result = {
            "start_us": starts[keep],
            "duration_us": durations[keep],
            "start_time_s": starts[keep] / 1e6,
            "end_time_s": ends[keep] / 1e6,
        }
        if "labels" in self.raw:
            indices = np.asarray(self.raw["labels"].oindex[positions])
            names = self.metadata["label_names"]
            if np.any(indices < 0) or np.any(indices >= len(names)):
                raise FormatError("Event label index out of range")
            result["label_index"] = indices
            result["label"] = [names[int(i)] for i in indices]
            if "label_codes" in self.provenance:
                result["code"] = [
                    self.provenance["label_codes"][int(i)] for i in indices
                ]
        if "values" in self.raw:
            result["value"] = np.asarray(self.raw["values"].oindex[positions])
        return pd.DataFrame(result)
