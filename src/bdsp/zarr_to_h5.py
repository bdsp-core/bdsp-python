"""Write a local Sleep YODA-style H5 from a split BDSP Zarr recording.

The split export keeps a recording in a primary Zarr (signals, expert scoring),
a derivative Zarr (model scoring, temporal feature payloads) and a summary JSON
(recording-level features). :func:`write_h5` assembles those artifacts into one
H5 file in the layout the converter read them from::

    /                       subject_id, session_id, duration_sec, age, sex, meas_date
    /signals/<name>         int16 digital samples + fs, unit, phys/dig min/max
    /annotations/<scorer>/<kind>/   starts, ends, durations (s), codes, event_map
    /features/<root>/time_series/<family>/   the original producer groups
    /features/<root>/recording_summary/      every summary row, arrays/

Only content held by the split export can be written. Signals are stored as
int16 when a calibration reproduces every Zarr sample bit-exactly through the
converter's digital-to-physical formula; otherwise they stay float32 with an
identity calibration. Annotation times round-trip through integer microseconds.
Metadata the split export never stored (signal filter strings, annotation rule
descriptions, producer provenance JSON, research groups) is listed in the root
``bdsp_h5_export`` attribute instead of being invented.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import h5py
import numpy as np

__all__ = ["H5ExportReport", "main", "physical_values", "write_h5"]

PAYLOAD_SCHEMA = "bdsp.sleep-feature-payload.v1"
SUMMARY_SCHEMA = "psg_features_sidecar_v0.5"
FEATURE_H5_SCHEMA = "sleep_feature_h5_v1"
#: Where features go when the payloads do not name a session-H5 location.
DEFAULT_FEATURE_ROOT = "/features/sleep_features"
UNDECLARED_UNIT = "(undeclared)"
#: Columns of ``recording_summary``, in the producer's order.
SUMMARY_COLUMNS = (
    "feature_id", "short_name", "display_name", "display_scope", "unit", "source",
    "result_kind", "storage_type", "value_shape", "status", "message",
    "value_number", "value_integer", "value_boolean", "value_text", "array_path",
)  # fmt: skip
#: Declared voltage units the converter rescaled to microvolts, with their factor.
TO_MICROVOLTS = {
    "v": 1e6, "volt": 1e6, "volts": 1e6,
    "mv": 1e3, "millivolt": 1e3, "millivolts": 1e3,
    "uv": 1.0, "uvolt": 1.0, "uvolts": 1.0,
}  # fmt: skip
#: Digital ranges tried, most common first, when recovering a calibration.
_DIGITAL_RANGES = ((-32768.0, 32767.0), (-32767.0, 32767.0), (-2048.0, 2047.0))
#: Significant digits of a physical limit (an EDF header field has 8 characters).
_MAX_DIGITS = 7
_INT16 = np.iinfo(np.int16)
_COMPRESSION = {"compression": "gzip", "compression_opts": 4}
_STRING = h5py.string_dtype("utf-8")
_NOT_IN_SPLIT_EXPORT = (
    "signal prefilter/type attributes",
    "annotation attributes other than event_map (e.g. rule, rule_description)",
    "root attributes other than identity, duration, age, sex and dates",
    "feature-producer package attributes, recording/identity_json and provenance/json",
    "groups outside signals, annotations and exported features (e.g. features/research)",
)


@dataclass(frozen=True)
class H5ExportReport:
    """What :func:`write_h5` wrote. Notes name channels or features, never values."""

    path: Path
    signals_int16: int
    signals_float32: int
    annotation_tracks: int
    feature_families: int
    summary_rows: int
    summary_arrays: int
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def signals(self) -> int:
        return self.signals_int16 + self.signals_float32


# ---------------------------------------------------------------------------
# Signal calibration


@dataclass(frozen=True)
class _Calibration:
    phys_min: float
    phys_max: float
    dig_min: float
    dig_max: float

    def scale_offset(self, factor: float) -> tuple[float, float]:
        """The converter's formula (``h5_reader._calibrated``), with its unit factor."""
        scale = (self.phys_max - self.phys_min) / (self.dig_max - self.dig_min)
        offset = self.phys_min - self.dig_min * scale
        return scale * factor, offset * factor


_IDENTITY = _Calibration(0.0, 1.0, 0.0, 1.0)


def physical_values(dataset: h5py.Dataset, *, to_microvolts: bool = True) -> np.ndarray:
    """A ``/signals`` dataset's physical float32 samples, as the Zarr converter computes them.

    ``physical = digital * scale + offset`` in float64, from the dataset's
    ``phys_min/phys_max/dig_min/dig_max``, rescaled to microvolts for declared
    voltage units, then rounded to float32. This is the value a converted
    primary Zarr stores, so it is the right basis for H5/Zarr parity checks.
    """
    attrs = {k: float(dataset.attrs[k]) for k in ("phys_min", "phys_max", "dig_min", "dig_max")}
    factor = _unit_factor(_text(dataset.attrs.get("unit", ""))) if to_microvolts else 1.0
    scale, offset = _Calibration(**attrs).scale_offset(factor)
    return (dataset[()].astype(np.float64) * scale + offset).astype(np.float32)


def _unit_factor(unit: str) -> float:
    key = unit.strip().lower().replace("µ", "u").replace("μ", "u").replace("micro", "u")
    return TO_MICROVOLTS.get(key, 1.0)


def _digits(value: float) -> int:
    """Significant decimal digits of ``value`` (0 for zero)."""
    if value == 0:
        return 0
    return len(f"{abs(value):.12e}".split("e")[0].replace(".", "").rstrip("0"))


def _short(value: float, tolerance: float) -> list[float]:
    """Roundings of ``value`` within ``tolerance`` with few significant digits, coarsest first."""
    out: list[float] = []
    for decimals in range(-9, 7):
        rounded = float(round(value, decimals))
        if abs(rounded - value) <= tolerance and _digits(rounded) <= _MAX_DIGITS:
            if rounded not in out:
                out.append(rounded)
    return out


def _decimals(value: float) -> int:
    return next(d for d in range(0, 13) if round(value, d) == value)


def _fit(values: np.ndarray, step: float, origin: float) -> tuple[float, float, np.ndarray]:
    steps = np.round((values - origin) / step)
    if steps[-1] == steps[0]:
        return step, origin, steps
    step, origin = (float(v) for v in np.polyfit(steps, values, 1))
    return step, origin, np.round((values - origin) / step)


def _lattice(unique: np.ndarray) -> tuple[float, float, int] | None:
    """``(step, origin, steps)`` with every sorted value ``origin + n * step``, ``0 <= n <= steps``.

    Values were rounded to float32 after an exact float64 calibration, so each
    lies within half a float32 spacing of its lattice point. The step is seeded
    from the smallest gap near zero (or a divisor of it) and refined by least
    squares over a value window centred there. Each window is only as wide as
    the step fitted so far allows: an error under a quarter step at its edge.
    """
    levels = _INT16.max - _INT16.min
    count = len(unique)
    if count < 2 or count > levels + 1:
        return None
    tolerance = float(np.spacing(np.float32(np.abs(unique).max())))
    center = int(np.argmin(np.abs(unique)))
    seed = unique[max(0, center - 32) : center + 33]
    smallest = float(np.diff(seed).min()) if len(seed) > 1 else float(np.diff(unique).min())
    for divisor in range(1, 1025):
        step, origin = smallest / divisor, float(unique[center])
        if step <= 16 * tolerance or (unique[-1] - unique[0]) / step > levels + 0.5:
            return None  # finer steps fit more loosely and need more levels
        width, radius = smallest, 0.0
        while True:
            radius = max(2 * radius, 0.25 * width * step / tolerance)
            low = np.searchsorted(unique, unique[center] - radius, "left")
            high = np.searchsorted(unique, unique[center] + radius, "right")
            part = unique[low:high]
            step, origin, steps = _fit(part, step, origin)
            if np.abs(part - (origin + steps * step)).max() > tolerance:
                break
            if low == 0 and high == count:
                origin += steps[0] * step
                span = int(steps[-1] - steps[0])
                return (step, origin, span) if span <= levels else None
            width = max(width, float(part[-1] - part[0]))
    return None


def _candidates(unique: np.ndarray, factor: float) -> Iterator[_Calibration]:
    """Calibrations to try, most plausible first; each must still be verified exactly.

    Source calibrations come from EDF headers: short decimal physical limits
    over a digital range, mostly the full int16 one. With the lattice known, the
    span is snapped to a short decimal and every digital alignment of the
    lowest value is tried for a short-decimal ``phys_min``. A recording may only
    reach every k-th level, so sub-multiples of the observed step are tried too.
    """
    integers = bool(np.all(unique == np.round(unique)))
    if factor == 1.0 and integers and unique[0] >= 0 and unique[-1] <= 1:
        yield _Calibration(0.0, 1.0, 0.0, 1.0)  # boolean channels such as cpap_on
    if len(unique) == 1:
        yield _Calibration(float(unique[0]) / factor, float(unique[0]) / factor + 1.0, 0.0, 1.0)
        return
    tolerance = 2 * float(np.spacing(np.float32(np.abs(unique).max()))) / factor
    lattice = _lattice(unique)
    if lattice is not None:
        step, origin, steps = lattice[0] / factor, lattice[1] / factor, lattice[2]
        for dig_min, dig_max in _DIGITAL_RANGES:
            levels = dig_max - dig_min
            ranked = []
            for divisor in range(1, 17):
                if steps * divisor > levels:
                    break
                raw_span = step / divisor * levels
                for span in _short(raw_span, abs(raw_span) * 5e-8)[:2]:
                    for key, calibration in _decimal_ranges(
                        origin, span, steps * divisor, dig_min, dig_max, tolerance
                    ):
                        ranked.append(((key[0], divisor, *key[1:]), calibration))
            ranked.sort(key=lambda item: item[0])
            yield from (calibration for _, calibration in ranked)
    yield from _sparse_candidates(unique, factor, tolerance)
    if lattice is not None:
        for dig_min, dig_max in _DIGITAL_RANGES:  # any exact calibration, however long
            if steps <= dig_max - dig_min:
                raw_min = origin - round((origin + step * (dig_max - dig_min) / 2) / step) * step
                yield _Calibration(raw_min, raw_min + step * (dig_max - dig_min), dig_min, dig_max)
    if factor == 1.0 and integers and unique[0] >= _INT16.min and unique[-1] <= _INT16.max:
        low, high = float(unique[0]), float(unique[-1])
        yield _Calibration(low, high, low, high)


def _digit_counts(values: np.ndarray, decimals: int) -> np.ndarray:
    """Significant digits of each value written with at most ``decimals`` decimals."""
    mantissa = np.round(np.abs(values) * 10.0**decimals).astype(np.int64)
    counts = np.zeros(len(mantissa), dtype=np.int64)
    nonzero = mantissa > 0
    counts[nonzero] = np.floor(np.log10(mantissa[nonzero])).astype(np.int64) + 1
    trailing = nonzero.copy()
    for power in range(1, 16):
        trailing &= mantissa % 10**power == 0
        if not trailing.any():
            break
        counts -= trailing
    return counts


def _decimal_ranges(
    origin: float, span: float, used: int, dig_min: float, dig_max: float, tolerance: float,
    limit: int = 64,
) -> list[tuple[tuple, _Calibration]]:
    """Short-decimal ranges of ``span`` containing ``origin`` (the lowest value) on their lattice.

    ``origin`` may sit at any digital level that keeps the ``used`` levels above
    it in range. Alignments whose ``phys_min`` is a short decimal are ranked by
    convention (zero offset, symmetric, zero-based, other), then significant
    digits of both limits, then asymmetry about zero.
    """
    levels = dig_max - dig_min
    unit = span / levels
    lows = origin - np.arange(0, levels - used + 1) * unit
    if not len(lows):
        return []
    span_decimals = _decimals(span)
    found = []
    for decimals in range(0, 7):
        quantum = 10.0**-decimals
        if quantum < 8 * tolerance:
            break  # finer decimals would match almost every alignment
        rounded = np.round(lows / quantum) * quantum
        close = np.abs(rounded - lows) <= tolerance
        if decimals:  # values with fewer decimals were found at a coarser step already
            close &= np.round(np.abs(rounded) / quantum) % 10 != 0
        if close.any():
            low = np.round(rounded[close], decimals)
            high = np.round(low + span, max(decimals, span_decimals))
            digits = _digit_counts(low, decimals) + _digit_counts(high, max(decimals, span_decimals))
            convention = np.full(len(low), 3)
            convention[low == 0] = 2
            convention[np.abs(low + high) <= tolerance] = 1
            convention[np.abs(low - dig_min * unit) <= tolerance] = 0
            found.append((convention, digits, np.abs(low + high) / span, low, high))
    if not found:
        return []
    convention, digits, asymmetry, low, high = (np.concatenate(parts) for parts in zip(*found))
    keep = digits <= 2 * _MAX_DIGITS
    convention, digits, asymmetry, low, high = (a[keep] for a in (convention, digits,
                                                                   asymmetry, low, high))
    order = np.lexsort((asymmetry, digits, convention))
    out, seen = [], set()
    for i in order:
        if low[i] in seen:
            continue
        seen.add(low[i])
        key = (int(convention[i]), int(digits[i]), float(asymmetry[i]))
        out.append((key, _Calibration(float(low[i]), float(high[i]), dig_min, dig_max)))
        if len(out) >= limit:
            break
    return out


def _sparse_candidates(
    unique: np.ndarray, factor: float, tolerance: float, limit: int = 64
) -> Iterator[_Calibration]:
    """Short-span calibrations solved from the extreme values, for sparsely used ranges.

    With few distinct values several lattices fit, and the coarsest need not be
    the producer's. The extremes differ by an integer number of digital levels,
    so each level count implies one span; spans of at most four significant
    digits are tried, with every alignment of the lowest value.
    """
    low, high = float(unique[0]) / factor, float(unique[-1]) / factor
    width = high - low
    relative = 2 * tolerance / width
    found = []
    for index, (dig_min, dig_max) in enumerate(_DIGITAL_RANGES):
        levels = dig_max - dig_min
        gaps = np.arange(1, levels + 1)
        spans = levels * width / gaps
        magnitude = np.floor(np.log10(spans))
        for digits in range(1, 5):
            quantum = 10.0 ** (magnitude - digits + 1)
            rounded = np.round(spans / quantum) * quantum
            for i in np.flatnonzero(np.abs(rounded - spans) <= relative * spans):
                span = float(f"{rounded[i]:.{digits}g}")
                found.append(((digits, index), span, int(gaps[i]), dig_min, dig_max))
    found.sort(key=lambda item: item[0])
    seen = set()
    for _, span, gap, dig_min, dig_max in found:
        if (span, dig_min) in seen:
            continue
        seen.add((span, dig_min))
        if len(seen) > limit:
            return
        for _, calibration in _decimal_ranges(low, span, gap, dig_min, dig_max, tolerance, 8):
            yield calibration


def _encode_signal(raw: np.ndarray, factor: float) -> tuple[np.ndarray, _Calibration]:
    """int16 digital samples and a calibration reproducing ``raw`` exactly, else float32."""
    raw = np.asarray(raw, dtype=np.float32)
    if raw.size == 0 or not np.isfinite(raw).all():
        return raw, _IDENTITY
    unique = np.unique(raw)
    values = unique.astype(np.float64)
    tried = set()
    for calibration in _candidates(values, factor):
        if calibration in tried:
            continue
        tried.add(calibration)
        scale, offset = calibration.scale_offset(factor)
        digital = np.round((values - offset) / scale)
        if digital[0] < max(calibration.dig_min, _INT16.min):
            continue
        if digital[-1] > min(calibration.dig_max, _INT16.max):
            continue
        if np.array_equal((digital * scale + offset).astype(np.float32), unique):
            samples = np.round((raw.astype(np.float64) - offset) / scale).astype(np.int16)
            if np.array_equal((samples * scale + offset).astype(np.float32), raw):
                return samples, calibration
    return raw, _IDENTITY


# ---------------------------------------------------------------------------
# Zarr and JSON inputs


def _text(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


class _Store:
    """One TS-Zarr store opened read-only, with its ``recording.json``."""

    def __init__(self, path: Path | str):
        import zarr

        self.path = Path(path)
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=r"Object at recording\.json")
            self.root = zarr.open_group(str(self.path), mode="r")
            keys = [key for key in self.root.group_keys() if key.isdecimal()]
        if self.root.attrs.get("tsz", {}).get("version") != "1.0":
            raise ValueError(f"{self.path.name} is not a TS-Zarr 1.0 store")
        self.metadata = json.loads((self.path / "recording.json").read_text(encoding="utf-8"))
        self.channels = [self.root[key] for key in sorted(keys, key=int)]

    @property
    def identity(self) -> tuple[str, str, float]:
        subject = self.metadata.get("subject") or {}
        session = self.metadata.get("session") or {}
        return str(subject.get("id")), str(session.get("id")), float(session["duration_s"])

    @property
    def source(self) -> dict[str, Any]:
        source = self.metadata.get("source")
        return source if isinstance(source, dict) else {}


def _check_association(primary: _Store, derivative: _Store | None, summary: dict | None) -> None:
    if derivative is not None:
        if derivative.identity[:2] != primary.identity[:2] or not math.isclose(
            derivative.identity[2], primary.identity[2], abs_tol=1e-6
        ):
            raise ValueError("primary and derivative describe different recordings")
        files = {primary.source.get("file"), derivative.source.get("file")} - {None}
        if len(files) > 1:
            raise ValueError("primary and derivative declare different source files")
    if summary is not None:
        if summary.get("schema_version") != SUMMARY_SCHEMA:
            raise ValueError(f"summary JSON is not {SUMMARY_SCHEMA}")
        subject = summary.get("subject") or {}
        if (subject.get("label"), subject.get("session")) != primary.identity[:2]:
            raise ValueError("summary JSON describes a different recording")


# ---------------------------------------------------------------------------
# H5 writing


def _attr(value: Any) -> Any:
    """A JSON attribute value (as the payload extension stores it) for h5py."""
    if isinstance(value, np.generic):
        return value
    if isinstance(value, bool):
        return np.bool_(value)
    if isinstance(value, int):
        return np.int64(value)
    if isinstance(value, float):
        return np.float64(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if value and all(isinstance(item, str) for item in value):
            return np.array(value, dtype=_STRING)
        return np.asarray(value)
    return json.dumps(value)


def _set_attrs(node: h5py.HLObject, attrs: dict[str, Any]) -> None:
    for name, value in attrs.items():
        if value is not None:
            node.attrs[name] = _attr(value)


def _dataset(group: h5py.Group, name: str, data: np.ndarray, *, shuffle: bool = False, **kw):
    data = np.asarray(data)
    if data.dtype.kind in "OTU":
        data, kw["dtype"] = data.astype(object), _STRING
    if data.ndim and data.size:
        kw = {**_COMPRESSION, "chunks": True, "shuffle": shuffle, **kw}
    return group.create_dataset(name, data=data, **kw)


def _float(values: list[float]) -> np.ndarray:
    """float32 when every value is exactly a float32 (as the producer stores), else float64."""
    array = np.asarray(values, dtype=np.float64)
    single = array.astype(np.float32)
    return single if np.array_equal(single.astype(np.float64), array, equal_nan=True) else array


def _write_signals(h5: h5py.File, primary: _Store, include_appended: bool) -> tuple[int, int, list]:
    group = h5.create_group("signals")
    counts, notes = [0, 0], []
    for channel in primary.channels:
        tsz = channel.attrs["tsz"]
        provenance = tsz.get("provenance", {})
        if provenance.get("origin") != "signal" or tsz.get("kind") != "continuous":
            continue
        if "source_file" in provenance and not include_appended:
            continue
        name = str(tsz["name"])
        unit = str(tsz.get("unit", ""))
        unit = "" if unit == UNDECLARED_UNIT else unit
        converted_from = provenance.get("unit_converted_from")
        factor = _unit_factor(converted_from) if converted_from else 1.0
        digital, calibration = _encode_signal(channel["raw"][...], factor)
        attrs: dict[str, Any] = {"fs": float(tsz["rate_hz"])}
        if digital.dtype == np.int16:
            attrs["unit"] = converted_from or unit
            counts[0] += 1
        else:
            attrs["unit"] = unit
            if converted_from:
                attrs["unit_converted_from"] = converted_from
            counts[1] += 1
            notes.append(f"signal {name} kept as float32 (no exact int16 calibration)")
        attrs.update(
            phys_min=calibration.phys_min,
            phys_max=calibration.phys_max,
            dig_min=calibration.dig_min,
            dig_max=calibration.dig_max,
        )
        if offset := int(tsz.get("offset_us", 0)):
            attrs["offset_s"] = offset / 1e6
        for key in ("source_file", "declared_unit"):
            if key in provenance:
                attrs[key] = str(provenance[key])
        dataset = _dataset(group, name, digital)
        _set_attrs(dataset, attrs)
    return counts[0], counts[1], notes


def _write_annotations(h5: h5py.File, stores: list[_Store]) -> int:
    root = h5.create_group("annotations")
    tracks = 0
    for store in stores:
        for channel in store.channels:
            tsz = channel.attrs["tsz"]
            provenance = tsz.get("provenance", {})
            if provenance.get("origin") != "annotation" or tsz.get("kind") != "event":
                continue
            annotator, kind = str(tsz["name"]).split("/", 1)
            codes = np.asarray(provenance["label_codes"], dtype=np.int64)
            names = list(tsz["label_names"])
            starts = channel["events"][...].astype(np.float64) / 1e6
            durations = channel["durations"][...].astype(np.float64) / 1e6
            labels = channel["labels"][...].astype(np.int64)
            table = root.require_group(annotator).create_group(kind)
            _dataset(table, "starts", starts.astype(np.float32))
            _dataset(table, "ends", (starts + durations).astype(np.float32))
            _dataset(table, "durations", durations.astype(np.float32))
            _dataset(table, "codes", codes[labels].astype(np.int16))
            event_map = {str(int(code)): str(label) for code, label in zip(codes, names)}
            table.attrs["event_map"] = json.dumps(event_map)
            table.attrs["format"] = "event_table"
            tracks += 1
    return tracks


def _copy_zarr_group(source, target: h5py.Group) -> None:
    _set_attrs(target, dict(source.attrs))
    for name, array in source.arrays():
        dataset = _dataset(target, name, array[...], shuffle=True)
        _set_attrs(dataset, dict(array.attrs))
    for name, group in source.groups():
        _copy_zarr_group(group, target.create_group(name))


def _payloads(derivative: _Store | None) -> tuple[dict[str, Any], list[str]]:
    """Original family path -> payload source group, and families left without a payload."""
    if derivative is None:
        return {}, []
    payloads, unpreserved = {}, []
    for channel in derivative.channels:
        tsz = channel.attrs["tsz"]
        provenance = tsz.get("provenance", {})
        if provenance.get("origin") != "feature":
            continue
        reference = provenance.get("feature_payload") or {}
        if reference.get("schema") != PAYLOAD_SCHEMA:
            unpreserved.append(str(tsz.get("name")))
            continue
        payload = derivative.root[reference["path"]]
        payloads.setdefault(str(payload.attrs["source_h5_path"]), payload["source"])
    return payloads, unpreserved


def _feature_root(paths: list[str]) -> str:
    """The session-H5 feature root the payloads came from, or the default one."""
    roots = {path.rsplit("/time_series/", 1)[0] for path in paths if "/time_series/" in path}
    if len(roots) == 1 and (root := roots.pop()).startswith("/features/"):
        return root
    return DEFAULT_FEATURE_ROOT


def _write_summary(group: h5py.Group, summary: dict) -> tuple[int, int, list[str]]:
    rows = summary.get("features") or {}
    columns: dict[str, list] = {name: [] for name in SUMMARY_COLUMNS}
    for feature_id, row in rows.items():
        columns["feature_id"].append(feature_id)
        for name in SUMMARY_COLUMNS[1:]:
            value = row.get(name)
            if name == "value_number":
                value = math.nan if value is None else float(value)
            elif name == "value_integer":
                value = int(value or 0)
            elif name == "value_boolean":
                value = bool(value)
            else:
                value = "" if value is None else str(value)
            columns[name].append(value)
    dtypes = {"value_number": np.float64, "value_integer": np.int64, "value_boolean": np.uint8}
    for name, values in columns.items():
        data = np.asarray(values, dtype=dtypes.get(name, object))
        _dataset(group, name, data, shuffle=True)
    arrays = group.create_group("arrays")
    written, notes = 0, []
    vectors = summary.get("vectors") or {}
    for feature_id, row in rows.items():
        if row.get("storage_type") != "array":
            continue
        path = str(row.get("array_path") or "")
        if feature_id not in vectors:
            if path:
                notes.append(f"summary array {feature_id} is not in the summary JSON vectors")
            continue
        path = path or f"arrays/{feature_id}"
        if not path.startswith("arrays/"):
            path = f"arrays/{path.rsplit('/', 1)[-1]}"
        dataset = _dataset(arrays, path.split("/", 1)[1], _float(vectors[feature_id]["values"]),
                           shuffle=True)  # fmt: skip
        dataset.attrs["feature_id"] = feature_id
        written += 1
    return len(rows), written, notes


def _write_features(
    h5: h5py.File, derivative: _Store | None, summary: dict | None, identity: tuple
) -> tuple[int, int, int, list[str]]:
    payloads, unpreserved = _payloads(derivative)
    notes = [f"feature channel {name} has no payload; its family was not written"
             for name in unpreserved]  # fmt: skip
    has_summary = bool(summary and summary.get("features"))
    if not payloads and not has_summary:
        return 0, 0, 0, notes
    root_path = _feature_root(list(payloads))
    root = h5.require_group(root_path)
    _set_attrs(root, {"namespace": root_path, "recording_id": f"{identity[0]}_{identity[1]}",
                      "schema_version": FEATURE_H5_SCHEMA})  # fmt: skip
    families = 0
    if payloads:
        series = root.create_group("time_series")
        rows = 0
        for path, source in payloads.items():
            target = series.create_group(path.rsplit("/", 1)[-1])
            _copy_zarr_group(source, target)
            rows += int(source.attrs.get("row_count", 0))
            families += 1
        series.attrs["feature_count"] = np.int64(families)
        series.attrs["row_count"] = np.int64(rows)
    rows_written = arrays = 0
    if has_summary:
        rows_written, arrays, summary_notes = _write_summary(
            root.create_group("recording_summary"), summary
        )
        notes += summary_notes
    return families, rows_written, arrays, notes


def _root_attrs(primary: _Store, summary: dict | None) -> dict[str, Any]:
    subject, session, duration = primary.identity
    demographics = primary.metadata.get("subject") or {}
    attrs: dict[str, Any] = {
        "subject_id": subject,
        "session_id": session,
        "duration_sec": _float([duration])[0],
    }
    if demographics.get("age_years") is not None:
        attrs["age"] = np.float64(demographics["age_years"])
    if demographics.get("sex") is not None:
        attrs["sex"] = str(demographics["sex"])
    provenance = (summary or {}).get("provenance") or {}
    if provenance.get("meas_date") is not None:
        attrs["meas_date"] = str(provenance["meas_date"])
    if provenance.get("h5_meas_date") is not None:
        attrs["h5_meas_date"] = str(provenance["h5_meas_date"])
    return attrs


def write_h5(
    output: Path | str,
    primary: Path | str,
    derivatives: Path | str | None = None,
    summary_json: Path | str | None = None,
    *,
    include_appended_signals: bool = True,
    overwrite: bool = False,
) -> H5ExportReport:
    """Assemble one split Zarr recording into a single local H5 file.

    Args:
        output: H5 path to write. It appears only once the file is complete.
        primary: The recording's primary ``*_task-psg_eeg.zarr`` directory.
        derivatives: Its ``*_deriv-bdspsleepfeatures.zarr`` directory, if any.
            Without it the H5 has no model annotations or feature time series.
        summary_json: Its ``*_deriv-bdspsleepfeatures.json``, if any. Without it
            the H5 has no ``recording_summary`` and no ``meas_date``.
        include_appended_signals: Keep channels appended to the primary store from
            another file after conversion (e.g. PAP channels from ``*_desc-pap.h5``).
            Each keeps a ``source_file`` attribute naming that file.
        overwrite: Replace an existing ``output``.

    Raises:
        FileExistsError: If ``output`` exists and ``overwrite`` is false.
        ValueError: If the artifacts do not describe the same recording.
    """
    output = Path(output)
    if output.exists() and not overwrite:
        raise FileExistsError(output)
    primary_store = _Store(primary)
    derivative_store = _Store(derivatives) if derivatives is not None else None
    summary = None
    if summary_json is not None:
        summary = json.loads(Path(summary_json).read_text(encoding="utf-8"))
    _check_association(primary_store, derivative_store, summary)

    from . import __version__

    stores = [primary_store] + ([derivative_store] if derivative_store else [])
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    try:
        with h5py.File(temporary, "w") as h5:
            _set_attrs(h5, _root_attrs(primary_store, summary))
            int16, float32, notes = _write_signals(h5, primary_store, include_appended_signals)
            tracks = _write_annotations(h5, stores)
            families, rows, arrays, feature_notes = _write_features(
                h5, derivative_store, summary, primary_store.identity
            )
            notes += feature_notes
            export = {
                "writer": f"bdsp {__version__} (bdsp.zarr_to_h5)",
                "inputs": {
                    "primary": primary_store.path.name,
                    "derivatives": derivative_store.path.name if derivative_store else None,
                    "summary_json": Path(summary_json).name if summary_json else None,
                },
                "source_file": primary_store.source.get("file"),
                "converter": primary_store.source.get("converter"),
                "skipped_at_conversion": {
                    store.path.name: store.source.get("skipped", {}) for store in stores
                },
                "not_in_split_export": list(_NOT_IN_SPLIT_EXPORT),
                "notes": notes,
            }
            h5.attrs["bdsp_h5_export"] = json.dumps(export)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return H5ExportReport(output, int16, float32, tracks, families, rows, arrays, tuple(notes))


# ---------------------------------------------------------------------------
# Command line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bdsp-zarr-to-h5",
        description="Write one local H5 from a split BDSP Zarr recording.",
    )
    parser.add_argument("--primary", required=True, help="primary *_task-psg_eeg.zarr directory")
    parser.add_argument("--derivatives", help="derivative *_deriv-bdspsleepfeatures.zarr directory")
    parser.add_argument("--summary-json", help="derivative *_deriv-bdspsleepfeatures.json file")
    parser.add_argument("--output", "-o", required=True, help="H5 file to write")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing output")
    parser.add_argument(
        "--no-appended-signals",
        action="store_true",
        help="leave out channels appended from other files (e.g. PAP sidecar channels)",
    )
    args = parser.parse_args(argv)
    if args.derivatives is None or args.summary_json is None:
        print("note: without --derivatives and --summary-json the H5 has no model "
              "annotations or features", file=sys.stderr)  # fmt: skip
    report = write_h5(
        args.output,
        args.primary,
        args.derivatives,
        args.summary_json,
        include_appended_signals=not args.no_appended_signals,
        overwrite=args.overwrite,
    )
    print(
        f"wrote {report.path}: {report.signals} signals ({report.signals_int16} int16, "
        f"{report.signals_float32} float32), {report.annotation_tracks} annotation tracks, "
        f"{report.feature_families} feature families, {report.summary_rows} summary rows"
    )
    for note in report.notes:
        print(f"note: {note}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
