"""Compare an H5 written by :mod:`bdsp.zarr_to_h5` with the source H5 it came from.

The comparison follows what the split Zarr export is meant to preserve:

* identity, duration, age (capped at 90 as published), sex and recording date;
* every ``/signals`` channel's sampling rate, length and physical float32
  samples, computed with the converter's calibration formula (digital samples
  and calibration attributes are reported separately: other exact encodings exist);
* every annotation table's events, codes and ``event_map``;
* every feature time-series group, recursively (attributes, dtypes, values);
* the ``recording_summary`` columns and arrays.

Anything else in the source H5 is listed under ``not_exported`` rather than
counted as a failure: the split export does not carry it. Results name
channels, tables, features and attributes, never recorded values.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from .zarr_to_h5 import SUMMARY_COLUMNS, _unit_factor, physical_values

__all__ = ["FEATURE_ROOTS", "compare_h5"]

#: Feature roots in session H5s (current and 2026-10 names) and in a producer's own H5.
FEATURE_ROOTS = ("features/sleep_features", "features/bdsp_sleep_features", "sleep_features")
_AGE_CAP = 90.0
_SEX = {"m": "Male", "male": "Male", "f": "Female", "female": "Female",
        "u": "Unknown", "unknown": "Unknown"}  # fmt: skip
_LOADABLE = {"ok", "valid_zero"}
#: An ``ends`` value may differ from starts + durations by this much (converter tolerance).
_END_TOLERANCE_S = 0.011


def _plain(value: Any) -> Any:
    """An H5 attribute or value as a JSON-comparable Python value."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, np.ndarray):
        return [_plain(item) for item in value.tolist()] if value.ndim else _plain(value.item())
    if isinstance(value, np.generic):
        return _plain(value.item())
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _attrs(node) -> dict[str, Any]:
    return {name: _plain(value) for name, value in node.attrs.items()}


def _text(value: Any) -> str:
    value = _plain(value)
    return "" if value is None else str(value)


def _values(dataset: h5py.Dataset) -> np.ndarray:
    if h5py.check_string_dtype(dataset.dtype) is not None:
        return np.asarray(dataset.asstr()[()], dtype=object)
    return np.asarray(dataset[()])


def _equal(a: np.ndarray, b: np.ndarray) -> bool:
    if a.shape != b.shape:
        return False
    if a.dtype.kind in "fc" or b.dtype.kind in "fc":
        try:
            return bool(np.array_equal(a.astype(np.float64), b.astype(np.float64), equal_nan=True))
        except (TypeError, ValueError):
            return False
    return bool(np.array_equal(a, b))


def _same_tree(a: h5py.Group, b: h5py.Group) -> list[str]:
    """Paths (relative to ``a``) whose attributes, members, dtype or values differ."""
    problems = []
    if _attrs(a) != _attrs(b):
        problems.append(f"{a.name}@attrs")
    if set(a) != set(b):
        problems.append(f"{a.name}:members")
    for name in set(a) & set(b):
        x, y = a[name], b[name]
        if isinstance(x, h5py.Group) or isinstance(y, h5py.Group):
            if isinstance(x, h5py.Group) and isinstance(y, h5py.Group):
                problems += _same_tree(x, y)
            else:
                problems.append(f"{x.name}:kind")
            continue
        text = h5py.check_string_dtype(x.dtype) is not None
        if text != (h5py.check_string_dtype(y.dtype) is not None) or (
            not text and x.dtype != y.dtype
        ):
            problems.append(f"{x.name}:dtype")
        elif _attrs(x) != _attrs(y):
            problems.append(f"{x.name}@attrs")
        elif not _equal(_values(x), _values(y)):
            problems.append(f"{x.name}:values")
    return problems


def _feature_root(h5: h5py.File) -> h5py.Group | None:
    for root in FEATURE_ROOTS:
        if isinstance(h5.get(root), h5py.Group):
            return h5[root]
    return None


def _export_info(h5: h5py.File) -> dict[str, Any]:
    try:
        return json.loads(_text(h5.attrs.get("bdsp_h5_export", "{}")))
    except ValueError:
        return {}


def _skipped(info: dict[str, Any], kind: str) -> set[str]:
    out: set[str] = set()
    for skipped in (info.get("skipped_at_conversion") or {}).values():
        out |= set((skipped or {}).get(kind, {}))
    return out


def _identity(original: h5py.File, exported: h5py.File, failures: list[str]) -> dict:
    report: dict[str, str] = {}

    def check(name: str, expected: Any, actual: Any, required: bool = False) -> None:
        if expected is None:
            report[name] = "absent_in_original"
        elif actual is None:
            report[name] = "missing_in_export"
            if required:
                failures.append(f"identity: {name} missing")
        elif expected == actual:
            report[name] = "equal"
        else:
            report[name] = "differs"
            failures.append(f"identity: {name} differs")

    def number(h5, name):
        value = h5.attrs.get(name)
        if value is None:
            return None
        value = float(_plain(value))
        return None if math.isnan(value) else value

    def sex(h5):
        value = h5.attrs.get("sex")
        return None if value is None else _SEX.get(_text(value).strip().lower(), _text(value))

    for name in ("subject_id", "session_id"):
        check(name, _text(original.attrs.get(name)) or None,
              _text(exported.attrs.get(name)) or None, required=True)  # fmt: skip
    check("duration_sec", number(original, "duration_sec"), number(exported, "duration_sec"),
          required=True)  # fmt: skip
    age = number(original, "age")
    check("age", None if age is None else min(age, _AGE_CAP), number(exported, "age"))
    check("sex", sex(original), sex(exported))
    date = exported.attrs.get("h5_meas_date", exported.attrs.get("meas_date"))
    check("meas_date", _text(original.attrs.get("meas_date")) or None,
          _text(date) or None)  # fmt: skip
    return report


def _signals(original: h5py.File, exported: h5py.File, info: dict, failures: list[str]) -> dict:
    a, b = original.get("signals", {}), exported.get("signals", {})
    names_a, names_b = set(a), set(b)
    skipped = _skipped(info, "signals")
    missing = sorted(names_a - names_b)
    appended = sorted(n for n in names_b - names_a if "source_file" in b[n].attrs)
    extra = sorted(names_b - names_a - set(appended))
    for name in missing:
        if name not in skipped:
            failures.append(f"signal missing: {name}")
    for name in extra:
        failures.append(f"signal not in original: {name}")
    counts = {"physical_equal": 0, "digital_equal": 0, "calibration_equal": 0, "unit_equal": 0,
              "int16": 0}  # fmt: skip
    attrs_not_exported: set[str] = set()
    calibration = ("phys_min", "phys_max", "dig_min", "dig_max")
    for name in sorted(names_a & names_b):
        x, y = a[name], b[name]
        attrs_not_exported |= set(x.attrs) - set(y.attrs)
        if float(x.attrs["fs"]) != float(y.attrs["fs"]) or x.shape != y.shape:
            failures.append(f"signal rate or length differs: {name}")
            continue
        if np.array_equal(physical_values(x), physical_values(y)):
            counts["physical_equal"] += 1
        else:
            failures.append(f"signal samples differ: {name}")
        counts["int16"] += y.dtype == np.int16
        counts["digital_equal"] += bool(y.dtype == x.dtype and np.array_equal(x[()], y[()]))
        counts["calibration_equal"] += all(
            float(x.attrs[k]) == float(y.attrs[k]) for k in calibration
        )
        unit_a, unit_b = _text(x.attrs.get("unit", "")), _text(y.attrs.get("unit", ""))
        counts["unit_equal"] += unit_a == unit_b
        if unit_a != unit_b and _unit_factor(unit_a) == _unit_factor(unit_b) == 1.0:
            failures.append(f"signal unit differs: {name}")
    return {
        "original": len(names_a),
        "exported": len(names_b),
        "compared": len(names_a & names_b),
        "missing_in_export": missing,
        "skipped_at_conversion": sorted(skipped & set(missing)),
        "appended_from_other_files": appended,
        "extra_in_export": extra,
        **counts,
        "attrs_not_exported": sorted(attrs_not_exported),
    }


def _tables(h5: h5py.File) -> dict[str, h5py.Group]:
    root = h5.get("annotations")
    out = {}
    if isinstance(root, h5py.Group):
        for annotator, group in root.items():
            if isinstance(group, h5py.Group):
                for kind, table in group.items():
                    if isinstance(table, h5py.Group) and "starts" in table:
                        out[f"{annotator}/{kind}"] = table
    return out


def _same_events(x: h5py.Group, y: h5py.Group) -> tuple[bool, dict[str, float]]:
    starts = x["starts"][()].astype(np.float64)
    order = np.argsort(starts, kind="stable")
    starts = starts[order]
    columns = {name: x[name][()][order] for name in ("codes", "durations", "ends") if name in x}
    exported = {name: y[name][()] for name in ("starts", "codes", "durations", "ends")}
    if len(starts) != len(exported["starts"]):
        return False, {}
    deviation = {}
    ok = np.array_equal(columns["codes"].astype(np.int64), exported["codes"].astype(np.int64))
    for name, values in (("starts", starts), ("durations", columns.get("durations"))):
        if values is None:
            continue
        values = values.astype(np.float64)
        delta = np.abs(exported[name].astype(np.float64) - values)
        limit = 1e-6 + 2 * np.spacing(np.abs(values).astype(np.float32)).astype(np.float64)
        deviation[name] = float(delta.max(initial=0))
        ok &= bool((delta <= limit).all())
    if "ends" in columns:
        delta = np.abs(exported["ends"].astype(np.float64) - columns["ends"].astype(np.float64))
        deviation["ends"] = float(delta.max(initial=0))
        ok &= bool((delta <= _END_TOLERANCE_S).all())
    try:
        maps = [
            {int(k): str(v) for k, v in json.loads(_text(t.attrs["event_map"])).items()}
            for t in (x, y)
        ]
        ok &= maps[0] == maps[1]
    except (KeyError, ValueError, TypeError):
        ok = False
    return bool(ok), deviation


def _annotations(original: h5py.File, exported: h5py.File, info: dict, failures: list) -> dict:
    a, b = _tables(original), _tables(exported)
    skipped = _skipped(info, "annotations")
    missing = sorted(set(a) - set(b))
    extra = sorted(set(b) - set(a))
    for name in missing:
        if name not in skipped:
            failures.append(f"annotation table missing: {name}")
    for name in extra:
        failures.append(f"annotation table not in original: {name}")
    equal, deviation = 0, {"starts": 0.0, "durations": 0.0, "ends": 0.0}
    attrs_not_exported: set[str] = set()
    for name in sorted(set(a) & set(b)):
        same, worst = _same_events(a[name], b[name])
        attrs_not_exported |= set(a[name].attrs) - set(b[name].attrs)
        if same:
            equal += 1
        else:
            failures.append(f"annotation table differs: {name}")
        for key, value in worst.items():
            deviation[key] = max(deviation[key], value)
    return {
        "original": len(a),
        "exported": len(b),
        "equal": equal,
        "missing_in_export": missing,
        "skipped_at_conversion": sorted(skipped & set(missing)),
        "extra_in_export": extra,
        "max_abs_deviation_s": deviation,
        "attrs_not_exported": sorted(attrs_not_exported),
    }


def _summary(a: h5py.Group, b: h5py.Group | None, failures: list[str]) -> dict:
    if b is None:
        failures.append("recording_summary missing")
        return {"rows": int(a["feature_id"].shape[0]), "exported": False}
    differ = []
    for column in SUMMARY_COLUMNS:
        if column not in a:
            continue
        if column not in b:
            differ.append(column)
            continue
        x, y = _values(a[column]), _values(b[column])
        if column == "value_boolean":
            x, y = x.astype(bool), y.astype(bool)
        if not _equal(x, y):
            differ.append(column)
    for column in differ:
        failures.append(f"recording_summary column differs: {column}")
    arrays_a = a.get("arrays", {})
    arrays_b = b.get("arrays", {})
    rows = {fid: i for i, fid in enumerate(_values(a["feature_id"]).tolist())}
    status = _values(a["status"]).tolist()
    missing, not_exported, equal, dtype_differs = [], [], 0, []
    for name in arrays_a:
        node = arrays_a[name]
        feature = _text(node.attrs.get("feature_id", name))
        if name not in arrays_b:
            expected = status[rows[feature]] in _LOADABLE if feature in rows else True
            (missing if expected and node.ndim == 1 else not_exported).append(feature)
            continue
        if _equal(_values(node), _values(arrays_b[name])):
            equal += 1
        else:
            failures.append(f"recording_summary array differs: {feature}")
        if node.dtype != arrays_b[name].dtype:
            dtype_differs.append(feature)
    for feature in missing:
        failures.append(f"recording_summary array missing: {feature}")
    return {
        "rows": int(a["feature_id"].shape[0]),
        "exported_rows": int(b["feature_id"].shape[0]) if "feature_id" in b else 0,
        "columns_differ": differ,
        "arrays": len(arrays_a),
        "arrays_equal": equal,
        "arrays_missing": missing,
        "arrays_not_in_split_export": not_exported,
        "arrays_dtype_differs": dtype_differs,
    }


def _features(original_root, exported_root, info: dict, failures: list[str]) -> dict:
    if original_root is None:
        return {"original": False, "exported": exported_root is not None}
    if exported_root is None:
        failures.append("features missing")
        return {"original": True, "exported": False}
    report: dict[str, Any] = {"original_root": original_root.name,
                              "exported_root": exported_root.name}  # fmt: skip
    a, b = original_root.get("time_series"), exported_root.get("time_series")
    if isinstance(a, h5py.Group):
        b = b if isinstance(b, h5py.Group) else {}
        skipped = _skipped(info, "features")
        family_ids = {name: _text(a[name].attrs.get("feature_id", name)) for name in a}
        missing = sorted(set(a) - set(b))
        differ = []
        for name in missing:
            if family_ids[name] not in skipped:
                failures.append(f"feature family missing: {family_ids[name]}")
        for name in sorted(set(a) & set(b)):
            if _same_tree(a[name], b[name]):
                differ.append(family_ids[name])
                failures.append(f"feature family differs: {family_ids[name]}")
        extra = sorted(set(b) - set(a))
        for name in extra:
            failures.append(f"feature family not in original: {name}")
        group_attrs = {k for k in _attrs(a) if _attrs(a)[k] != _attrs(b).get(k)} if b else set()
        report["time_series"] = {
            "original": len(a),
            "exported": len(b),
            "equal": len(set(a) & set(b)) - len(differ),
            "missing_in_export": [family_ids[n] for n in missing],
            "skipped_at_conversion": sorted(skipped & {family_ids[n] for n in missing}),
            "extra_in_export": extra,
            "group_attrs_differ": sorted(group_attrs),
        }
        for name in report["time_series"]["group_attrs_differ"]:
            failures.append(f"time_series attribute differs: {name}")
    summary = original_root.get("recording_summary")
    if isinstance(summary, h5py.Group):
        exported_summary = exported_root.get("recording_summary")
        report["recording_summary"] = _summary(
            summary, exported_summary if isinstance(exported_summary, h5py.Group) else None,
            failures,
        )
    root_attrs = _attrs(original_root)
    report["root_attrs_not_exported"] = sorted(set(root_attrs) - set(exported_root.attrs))
    return report


def _missing_nodes(original: h5py.File, exported: h5py.File, roots: tuple) -> list[str]:
    """Top-most original paths absent from the export, outside signals and annotations."""
    missing: list[str] = []

    def visit(name: str, _node) -> None:
        path = "/" + name
        if path.startswith(("/signals", "/annotations")):
            return
        if any(path == m or path.startswith(m + "/") for m in missing):
            return
        mapped = path
        for source, target in roots:
            if source and (path == source or path.startswith(source + "/")):
                mapped = target + path[len(source):]
        if mapped not in exported:
            missing.append(path)

    original.visititems(visit)
    return missing


def compare_h5(
    original: Path | str,
    exported: Path | str,
    *,
    features: Path | str | None = None,
) -> dict[str, Any]:
    """Compare ``exported`` (from :func:`bdsp.zarr_to_h5.write_h5`) with its source H5.

    Args:
        original: The session H5 the split Zarr export was converted from.
        exported: The H5 rebuilt from that split export.
        features: The producer's separate feature H5, for sessions whose features
            were never merged into the session H5 (I0007); features are then
            compared with it instead.

    Returns:
        A JSON-serializable report. ``verdict`` is ``"pass"`` when every item the
        split export carries matches; ``failures`` lists what does not; and
        ``not_exported`` lists source content the split export does not carry.
    """
    failures: list[str] = []
    with h5py.File(original, "r") as a, h5py.File(exported, "r") as b:
        info = _export_info(b)
        report: dict[str, Any] = {
            "identity": _identity(a, b, failures),
            "signals": _signals(a, b, info, failures),
            "annotations": _annotations(a, b, info, failures),
        }
        if features is not None:
            with h5py.File(features, "r") as sidecar:
                source_root = _feature_root(sidecar)
                report["features"] = _features(source_root, _feature_root(b), info, failures)
                report["features"]["compared_with"] = "separate feature H5"
        else:
            source_root = _feature_root(a)
            report["features"] = _features(source_root, _feature_root(b), info, failures)
            if source_root is None and _feature_root(b) is not None:
                report["features"]["note"] = "export has features the session H5 lacks"
        exported_root = _feature_root(b)
        mapping = ()
        if features is None and source_root is not None and exported_root is not None:
            mapping = ((source_root.name, exported_root.name),)
        report["not_exported"] = {
            "root_attrs": sorted(set(a.attrs) - set(b.attrs)),
            "nodes": _missing_nodes(a, b, mapping),
        }
    report["failures"] = failures
    report["verdict"] = "pass" if not failures else "fail"
    return report
