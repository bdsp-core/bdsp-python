"""Decode the exported BDSP summary and lossless temporal payload contracts."""

from __future__ import annotations

import json
from collections.abc import Mapping

import numpy as np
import pandas as pd

from ._core import (
    AVAILABLE_STATUSES,
    SUMMARY_SCHEMA,
    FormatError,
    overlaps,
    time_bounds,
)

_SCALAR_COLUMNS = {
    "number": "value_number",
    "integer": "value_integer",
    "boolean": "value_boolean",
    "string": "value_text",
}
_INFO_STORAGE = {
    "constant_json",
    "categorical_json",
    "numeric_array",
    "boolean",
    "integer",
    "number",
}


class SummaryFeatures(Mapping):
    """All summary rows, with status-qualified typed ``value`` and ``available``.

    ``raw`` retains the original JSON rows, including inactive value columns.
    Unavailable results have value=None; valid zero/False remain available.
    """

    def __init__(self, sidecar):
        self.sidecar = sidecar or {}
        self.raw = self.sidecar.get("features", {})
        self.vectors = {}
        if sidecar is None:
            return
        if sidecar.get("schema_version") != SUMMARY_SCHEMA:
            raise FormatError("Unsupported feature summary schema")
        if not isinstance(self.raw, dict) or not isinstance(
            sidecar.get("vectors"), dict
        ):
            raise FormatError("Summary requires features and vectors mappings")
        for key, row in self.raw.items():
            storage = row.get("storage_type")
            if storage not in {
                *_SCALAR_COLUMNS,
                "array",
                "null",
                "json",
            } or not isinstance(row.get("status"), str):
                raise FormatError("Unsupported summary storage type or missing status")
            if storage in _SCALAR_COLUMNS and row["status"] in AVAILABLE_STATUSES:
                value = row.get(_SCALAR_COLUMNS[storage])
                valid = {
                    "number": isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and np.isfinite(value),
                    "integer": type(value) is int,
                    "boolean": type(value) is bool,
                    "string": isinstance(value, str),
                }[storage]
                if not valid:
                    raise FormatError(
                        "Available summary scalar has an invalid typed value"
                    )
        for key, vector in sidecar["vectors"].items():
            row = self.raw.get(key, {})
            values = np.asarray(vector["values"], dtype=np.float64)
            if (
                values.ndim != 1
                or vector.get("length") != values.size
                or not np.isfinite(values).all()
                or row.get("storage_type") != "array"
                or vector.get("status") != row.get("status")
                or row.get("status") not in AVAILABLE_STATUSES
                or vector.get("unit") != row.get("unit")
            ):
                raise FormatError("Vector metadata, dimensions or summary row disagree")
            self.vectors[key] = values

    def __iter__(self):
        return iter(self.raw)

    def __len__(self):
        return len(self.raw)

    def __getitem__(self, key):
        row = self.raw[key]
        storage = row["storage_type"]
        available = row["status"] in AVAILABLE_STATUSES
        value = None
        if available and storage in _SCALAR_COLUMNS:
            value = row[_SCALAR_COLUMNS[storage]]
        elif available and key in self.vectors:
            value = self.vectors[key]
        else:
            available = False
        return {**row, "value": value, "available": available}

    def to_dataframe(self):
        return pd.DataFrame.from_dict(
            dict(self), orient="index", dtype=object
        ).rename_axis("feature_id")


def _take(array, positions):
    return np.asarray(array.oindex[positions])


def _categorical(group, field, positions):
    codes = _take(group[f"{field}_code"], positions)
    labels = np.asarray(group[f"{field}_labels"][:])
    if (
        codes.dtype.kind not in "iu"
        or np.any(codes < -1)
        or np.any(codes >= len(labels))
    ):
        raise FormatError("Categorical index out of range")
    return [None if i == -1 else str(labels[int(i)]) for i in codes]


class TemporalFeature:
    """One original feature family, without projection rounding or row removal.

    ``raw`` exposes preserved arrays/masks/attributes lazily. ``read`` selects
    original row positions or a time window before loading large value bodies.
    Numeric JSON bodies remain ndarrays, including their original dtype/shape.
    """

    def __init__(self, group, access):
        self.raw = group
        self._access = access
        self.metadata = dict(group.attrs)
        self.feature_id = self.metadata.get("feature_id")
        self.row_count = self.metadata.get("row_count")
        self.storage = self.metadata.get("value_json_storage")
        if (
            not self.feature_id
            or not isinstance(self.row_count, int)
            or self.row_count < 0
        ):
            raise FormatError("Payload requires feature_id and nonnegative row_count")
        if self.storage not in {"numeric_array", "categorical_json", "absent"}:
            raise FormatError("Unsupported temporal value_json storage")
        for field in ("row_index", "start_time_s", "end_time_s"):
            self._column(field)
        for field in ("temporal_semantics", "status", "unit", "source"):
            if f"{field}_code" in group:
                self._category(group, field)
            elif field not in self.metadata:
                raise FormatError("Missing temporal metadata column")
        for field in ("value_text", "value_json"):
            if f"{field}_code" in group:
                self._category(group, field)
        if "value_numeric" in group:
            self._column("value_numeric")
            self._column("value_numeric_present")
        if self.storage == "numeric_array":
            self._column("value_array", multidimensional=True)
            self._column("value_array_present")
        elif self.storage == "categorical_json":
            self._category(group, "value_json")
        self.information_schema = {}
        if "information" not in group:
            raise FormatError("Payload has no information group")
        self._information = {}
        for key in sorted(group["information"].group_keys()):
            node = group["information"][key]
            name, storage = node.attrs.get("name"), node.attrs.get("storage")
            if not name or name in self._information or storage not in _INFO_STORAGE:
                raise FormatError("Unsupported or duplicate information field")
            if storage == "constant_json":
                if "value_json" not in node or node["value_json"].shape != ():
                    raise FormatError("Constant information requires scalar JSON")
            elif storage == "categorical_json":
                self._category(node, "value_json")
            else:
                self._column(
                    "value", group=node, multidimensional=storage == "numeric_array"
                )
                self._column("present", group=node)
            self._information[name] = node
            self.information_schema[name] = storage

    def _column(self, name, group=None, multidimensional=False):
        node = self.raw if group is None else group
        if name not in node:
            raise FormatError("Missing temporal payload column")
        shape = node[name].shape
        if (
            not shape
            or shape[0] != self.row_count
            or (not multidimensional and len(shape) != 1)
        ):
            raise FormatError("Temporal payload column dimensions disagree")

    def _category(self, group, field):
        self._column(f"{field}_code", group=group)
        if f"{field}_labels" not in group or group[f"{field}_labels"].ndim != 1:
            raise FormatError("Categorical column requires label table")

    @property
    def values(self):
        """Lazy original numeric bodies; presence is a separate mask."""
        self._access.check()
        if self.storage != "numeric_array":
            raise TypeError("This family does not store numeric array bodies")
        return self.raw["value_array"]

    @property
    def present(self):
        self._access.check()
        if self.storage != "numeric_array":
            raise TypeError("Numeric array presence is only defined for array bodies")
        return self.raw["value_array_present"]

    def read(
        self,
        rows=None,
        *,
        start=None,
        duration=None,
        include_values=True,
        include_information=True,
    ):
        """Return original rows, including withheld ones, in stored row order.

        ``rows`` is a slice of family-local positions (not global row_index).
        Intersecting intervals and point events follow [start, start+duration).
        Information fields live in a dict column to avoid name collisions.
        """
        self._access.check()
        if rows is not None and not isinstance(rows, slice):
            raise TypeError("rows must be a slice of family-local row positions")
        first, stop, step = (rows if rows is not None else slice(None)).indices(
            self.row_count
        )
        positions = np.arange(first, stop, step, dtype=np.int64)
        starts = _take(self.raw["start_time_s"], positions)
        ends = _take(self.raw["end_time_s"], positions)
        if (
            np.any(~np.isfinite(starts))
            or np.any(~np.isfinite(ends))
            or np.any(ends < starts)
        ):
            raise FormatError("Invalid temporal interval")
        lower, upper = time_bounds(start, duration)
        keep = overlaps(starts, ends, lower, upper)
        if upper == lower:
            keep[:] = False
        positions, starts, ends = positions[keep], starts[keep], ends[keep]
        n = len(positions)
        columns = {
            "feature_id": [self.feature_id] * n,
            "row_index": _take(self.raw["row_index"], positions),
            "start_time_s": starts,
            "end_time_s": ends,
        }
        for field in ("temporal_semantics", "status", "unit", "source"):
            columns[field] = (
                _categorical(self.raw, field, positions)
                if f"{field}_code" in self.raw
                else [self.metadata[field]] * n
            )
        if include_values:
            columns.update(
                value_numeric=[None] * n, value_text=[None] * n, value_json=[None] * n
            )
            if "value_numeric" in self.raw:
                values = _take(self.raw["value_numeric"], positions)
                present = _take(self.raw["value_numeric_present"], positions).astype(
                    bool
                )
                columns["value_numeric"] = [
                    v if p else None for v, p in zip(values, present)
                ]
            if "value_text_code" in self.raw:
                columns["value_text"] = _categorical(self.raw, "value_text", positions)
            if self.storage == "numeric_array":
                values = _take(self.values, positions)
                present = _take(self.present, positions).astype(bool)
                columns["value_json"] = [
                    v if p else None for v, p in zip(values, present)
                ]
            elif self.storage == "categorical_json":
                columns["value_json"] = [
                    None if v is None else json.loads(v)
                    for v in _categorical(self.raw, "value_json", positions)
                ]
        if include_information:
            information = [{} for _ in positions]
            for name, node in self._information.items():
                storage = self.information_schema[name]
                if storage == "constant_json":
                    value = json.loads(str(node["value_json"][()]))
                    for info in information:
                        info[name] = value
                elif storage == "categorical_json":
                    values = _categorical(node, "value_json", positions)
                    for info, value in zip(information, values):
                        if value is not None:
                            info[name] = json.loads(value)
                else:
                    values = _take(node["value"], positions)
                    present = _take(node["present"], positions).astype(bool)
                    for info, value, exists in zip(information, values, present):
                        if exists:
                            info[name] = (
                                value if storage == "numeric_array" else value.item()
                            )
            columns["information"] = information
        return pd.DataFrame(columns, dtype=object)


class Features:
    def __init__(self, summary_json):
        self.summary = SummaryFeatures(summary_json)
        self.vectors = self.summary.vectors
        self.time_series = {}
        self.arrays = {}
        self.structured = {}
        self.projections = {}
