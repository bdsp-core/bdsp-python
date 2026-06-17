"""Public loader for Sleep YODA standardized H5 files.

This module is intended for downstream projects that want a convenient,
eager-loading API. The GUI and analysis code should continue to use
``PSGReader`` when lazy slices are preferable.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import h5py
import numpy as np
import pandas as pd
from scipy import signal as sp_signal


_EVENT_TABLE_DATASETS = ("starts", "codes")
_DISCRETE_SIGNAL_NAMES = {"cpap_on", "position"}


def _decode_h5_value(value: Any) -> Any:
    if isinstance(value, (bytes, np.bytes_)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, np.ndarray) and value.ndim == 0:
        return _decode_h5_value(value.item())
    if isinstance(value, np.generic):
        return value.item()
    return value


def _decode_attrs(attrs: h5py.AttributeManager) -> Dict[str, Any]:
    decoded: Dict[str, Any] = {}
    for name, value in attrs.items():
        value = _decode_h5_value(value)
        if name.endswith("_map") and isinstance(value, str):
            try:
                parsed = json.loads(value)
            except Exception:
                decoded[name] = value
                continue
            if isinstance(parsed, dict):
                mapped: Dict[Any, Any] = {}
                for key, mapped_value in parsed.items():
                    try:
                        mapped[int(key)] = mapped_value
                    except (TypeError, ValueError):
                        mapped[key] = mapped_value
                decoded[name] = mapped
            else:
                decoded[name] = parsed
        else:
            decoded[name] = value
    return decoded


def _as_float(value: Any, default: float = 0.0) -> float:
    value = _decode_h5_value(value)
    try:
        return float(value)
    except Exception:
        return default


def _flatten_dataset_values(dataset: h5py.Dataset) -> np.ndarray:
    data = np.asarray(dataset[()])
    if data.ndim <= 1:
        return data
    if 1 in data.shape:
        return data.reshape(-1)
    return data[:, 0]


def _is_event_table_group(node: Any) -> bool:
    if not isinstance(node, h5py.Group):
        return False
    has_required = all(
        name in node and isinstance(node[name], h5py.Dataset)
        for name in _EVENT_TABLE_DATASETS
    )
    has_end_or_duration = (
        "ends" in node
        or "durations" in node
        or node.attrs.get("format") == "event_table"
    )
    return bool(has_required and has_end_or_duration)


def _read_annotation_group(group: h5py.Group) -> Dict[str, Any]:
    entry: Dict[str, Any] = {}
    for name, child in group.items():
        if isinstance(child, h5py.Dataset):
            entry[name] = np.asarray(child[()])
    entry.update(_decode_attrs(group.attrs))
    return entry


def _read_dense_annotation_dataset(dataset: h5py.Dataset, root_fs: float) -> Dict[str, Any]:
    entry = {"values": _flatten_dataset_values(dataset), "fs": root_fs}
    entry.update(_decode_attrs(dataset.attrs))
    return entry


def _read_annotations(h5: h5py.File, root_fs: float) -> Dict[str, Dict[str, Any]]:
    annotations: Dict[str, Dict[str, Any]] = {}
    if "annotations" not in h5:
        return annotations

    root = h5["annotations"]
    for name, child in root.items():
        if isinstance(child, h5py.Dataset):
            annotations.setdefault("legacy", {})[name] = _read_dense_annotation_dataset(
                child, root_fs,
            )
            continue

        if _is_event_table_group(child):
            annotations.setdefault("default", {})[name] = _read_annotation_group(child)
            continue

        if not isinstance(child, h5py.Group):
            continue

        annotator_entry: Dict[str, Any] = {}
        for ann_type, ann_node in child.items():
            if isinstance(ann_node, h5py.Dataset):
                annotator_entry[ann_type] = _read_dense_annotation_dataset(
                    ann_node, root_fs,
                )
            elif isinstance(ann_node, h5py.Group):
                annotator_entry[ann_type] = _read_annotation_group(ann_node)
        if annotator_entry:
            annotations[name] = annotator_entry

    return annotations


def _read_feature_node(node: Any) -> Any:
    if isinstance(node, h5py.Dataset):
        return np.asarray(node[()])

    if _is_event_table_group(node):
        return _read_annotation_group(node)

    if not isinstance(node, h5py.Group):
        return None

    out: Dict[str, Any] = {}
    for name, child in node.items():
        out[name] = _read_feature_node(child)
    out.update(_decode_attrs(node.attrs))
    return out


def _read_features(h5: h5py.File) -> Dict[str, Any]:
    features: Dict[str, Any] = {}
    if "features" not in h5:
        return features

    root = h5["features"]
    for name, child in root.items():
        if isinstance(child, h5py.Dataset):
            features[name] = {"data": np.asarray(child[()])}
            continue
        if not isinstance(child, h5py.Group):
            continue

        entry: Dict[str, Any] = {}
        for feature_name, feature_node in child.items():
            entry[feature_name] = _read_feature_node(feature_node)
        attrs = _decode_attrs(child.attrs)
        if attrs:
            entry["__attrs__"] = attrs
        features[name] = entry
    return features


def load_feature_dataset(
    file_path: str | Path,
    feature_path: str,
) -> Dict[str, Any]:
    """Load a single `/features/...` dataset with decoded dataset attrs.

    This is intentionally separate from ``load_standardized_h5`` so the public
    eager-loader return shape remains backward compatible for callers that
    expect feature leaves to be NumPy arrays.
    """

    normalized = str(feature_path).strip("/")
    if not normalized.startswith("features/"):
        normalized = f"features/{normalized}"
    with h5py.File(Path(file_path), "r") as h5:
        if normalized not in h5:
            raise KeyError(f"Feature dataset not found: /{normalized}")
        node = h5[normalized]
        if not isinstance(node, h5py.Dataset):
            raise TypeError(f"Feature path is not a dataset: /{normalized}")
        return {
            "path": f"/{normalized}",
            "data": np.asarray(node[()]),
            "__attrs__": _decode_attrs(node.attrs),
        }


def load_feature_table(
    file_path: str | Path,
    feature_path: str,
) -> Dict[str, Any]:
    """Load a single feature table, whether stored as a group or dataset."""

    normalized = str(feature_path).strip("/")
    if not normalized.startswith("features/"):
        normalized = f"features/{normalized}"
    with h5py.File(Path(file_path), "r") as h5:
        if normalized not in h5:
            raise KeyError(f"Feature table not found: /{normalized}")
        node = h5[normalized]
        if isinstance(node, h5py.Dataset):
            return {
                "path": f"/{normalized}",
                "data": np.asarray(node[()]),
                "__attrs__": _decode_attrs(node.attrs),
            }
        if not isinstance(node, h5py.Group):
            raise TypeError(f"Feature path is neither a group nor a dataset: /{normalized}")
        return {
            "path": f"/{normalized}",
            "columns": {
                name: np.asarray(child[()])
                for name, child in node.items()
                if isinstance(child, h5py.Dataset)
            },
            "__attrs__": _decode_attrs(node.attrs),
        }


def _has_physical_conversion(attrs: Dict[str, Any]) -> bool:
    return all(
        attrs.get(name) is not None
        for name in ("phys_min", "phys_max", "dig_min", "dig_max")
    )


def _read_signal_dataset(
    dataset: h5py.Dataset,
    channel_name: str,
    h5_path: str,
    load_digital: bool,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    attrs = _decode_attrs(dataset.attrs)
    fs = _as_float(attrs.get("fs"), 0.0)
    has_conversion = _has_physical_conversion(attrs)
    data = _flatten_dataset_values(dataset)

    if not load_digital and has_conversion:
        dig_min = _as_float(attrs.get("dig_min"))
        dig_max = _as_float(attrs.get("dig_max"))
        phys_min = _as_float(attrs.get("phys_min"))
        phys_max = _as_float(attrs.get("phys_max"))
        data = data.astype(np.float32, copy=False)
        if dig_max != dig_min:
            scale = (phys_max - phys_min) / (dig_max - dig_min)
            offset = phys_min - dig_min * scale
            data = data * scale + offset
    elif not load_digital:
        data = data.astype(np.float32, copy=False)

    group_tag = h5_path.rsplit("/", 1)[0] if "/" in h5_path else "root"
    info = {
        "group": group_tag,
        "channel": channel_name,
        "path": f"signals/{h5_path}",
        "fs": fs,
        "phys_min": attrs.get("phys_min"),
        "phys_max": attrs.get("phys_max"),
        "dig_min": attrs.get("dig_min"),
        "dig_max": attrs.get("dig_max"),
        "unit": attrs.get("unit", ""),
        "prefilter": attrs.get("prefilter", ""),
        "n_samples": int(len(data)),
        "type": attrs.get("type", ""),
        "channel_annotations": attrs.get("channel_annotations"),
        "like_edf": has_conversion,
    }
    return data, info


def _read_signals(
    h5: h5py.File,
    load_digital: bool,
) -> Tuple[Dict[str, np.ndarray], Dict[str, float], pd.DataFrame]:
    if "signals" not in h5:
        raise ValueError("No 'signals' group found in H5 file.")

    signals: Dict[str, np.ndarray] = {}
    fs_by_channel: Dict[str, float] = {}
    channel_info = []

    def visit(name: str, node: Any) -> None:
        if not isinstance(node, h5py.Dataset):
            return
        channel_name = name.split("/")[-1]
        data, info = _read_signal_dataset(node, channel_name, name, load_digital)
        signals[channel_name] = data
        fs_by_channel[channel_name] = _as_float(info.get("fs"), 0.0)
        channel_info.append(info)

    h5["signals"].visititems(visit)
    return signals, fs_by_channel, pd.DataFrame(channel_info)


def _metadata_duration_seconds(
    metadata: Dict[str, Any],
    signals: Dict[str, np.ndarray],
    fs_by_channel: Dict[str, float],
) -> float:
    duration = _as_float(metadata.get("duration_sec"), 0.0)
    if duration > 0:
        return duration

    max_duration = 0.0
    for name, data in signals.items():
        fs = fs_by_channel.get(name, 0.0)
        if fs > 0:
            max_duration = max(max_duration, len(data) / fs)
    return max_duration


def _is_discrete_signal(name: str, info: Optional[pd.Series]) -> bool:
    key = name.lower()
    if key in _DISCRETE_SIGNAL_NAMES:
        return True
    if info is None:
        return False
    unit = str(info.get("unit", "")).strip().lower()
    type_name = str(info.get("type", "")).strip().lower()
    return "bool" in unit or "bool" in type_name


def _resample_by_nearest(
    data: np.ndarray,
    source_fs: float,
    target_fs: float,
    target_length: int,
) -> np.ndarray:
    if len(data) == 0:
        return np.zeros(target_length, dtype=np.float32)
    if target_length == len(data):
        return data.copy()
    if source_fs <= 0:
        source_fs = target_fs
    target_times = np.arange(target_length, dtype=np.float64) / float(target_fs)
    indices = np.floor(target_times * source_fs).astype(np.int64)
    indices = np.clip(indices, 0, len(data) - 1)
    return data[indices]


def _resample_by_interp(
    data: np.ndarray,
    source_fs: float,
    target_fs: float,
    target_length: int,
) -> np.ndarray:
    if len(data) == 0:
        return np.zeros(target_length, dtype=np.float32)
    if len(data) == 1:
        return np.full(target_length, data[0], dtype=np.float32)
    if source_fs <= 0:
        source_fs = target_fs

    source_times = np.arange(len(data), dtype=np.float64) / float(source_fs)
    target_times = np.arange(target_length, dtype=np.float64) / float(target_fs)
    resampled = np.interp(
        target_times,
        source_times,
        data.astype(np.float64, copy=False),
        left=float(data[0]),
        right=float(data[-1]),
    )
    return resampled.astype(np.float32)


def _resample_continuous_signal(
    data: np.ndarray,
    source_fs: float,
    target_fs: float,
    target_length: int,
) -> np.ndarray:
    if len(data) == target_length:
        return data.copy()
    if len(data) == 0:
        return np.zeros(target_length, dtype=np.float32)
    if len(data) == 1 or source_fs <= 0:
        return _resample_by_interp(data, source_fs, target_fs, target_length)

    try:
        ratio = Fraction(float(target_fs) / float(source_fs)).limit_denominator(1000)
        if ratio.numerator <= 0 or ratio.denominator <= 0:
            raise ValueError("invalid resampling ratio")
        resampled = sp_signal.resample_poly(
            data.astype(np.float32, copy=False),
            ratio.numerator,
            ratio.denominator,
        )
    except Exception:
        return _resample_by_interp(data, source_fs, target_fs, target_length)

    if len(resampled) > target_length:
        resampled = resampled[:target_length]
    elif len(resampled) < target_length:
        pad_width = target_length - len(resampled)
        edge_value = resampled[-1] if len(resampled) else 0.0
        resampled = np.pad(resampled, (0, pad_width), constant_values=edge_value)
    return resampled.astype(np.float32, copy=False)


def _resample_signal(
    name: str,
    data: np.ndarray,
    source_fs: float,
    target_fs: float,
    target_length: int,
    channel_info: pd.DataFrame,
) -> np.ndarray:
    row = None
    if not channel_info.empty:
        matches = channel_info[channel_info["channel"] == name]
        if not matches.empty:
            row = matches.iloc[0]

    if _is_discrete_signal(name, row):
        return _resample_by_nearest(data, source_fs, target_fs, target_length)
    if name.lower() == "spo2":
        return _resample_by_interp(data, source_fs, target_fs, target_length)
    return _resample_continuous_signal(data, source_fs, target_fs, target_length)


def _annotation_column_name(annotator: str, annotation_type: str) -> str:
    if annotator in {"legacy", "default"}:
        return annotation_type
    return f"{annotator}_{annotation_type}"


def _event_table_to_dense(
    entry: Dict[str, Any],
    target_length: int,
    target_fs: float,
) -> np.ndarray:
    dense = np.zeros(target_length, dtype=np.int16)
    if "starts" not in entry or "codes" not in entry:
        return dense

    starts = np.asarray(entry["starts"], dtype=np.float64)
    if "ends" in entry:
        ends = np.asarray(entry["ends"], dtype=np.float64)
    elif "durations" in entry:
        ends = starts + np.asarray(entry["durations"], dtype=np.float64)
    else:
        return dense
    codes = np.asarray(entry["codes"], dtype=np.int16)

    start_idx = np.floor(starts * target_fs).astype(np.int64)
    end_idx = np.ceil(ends * target_fs).astype(np.int64)
    valid = (start_idx < target_length) & (end_idx > start_idx)
    for start, end, code in zip(start_idx[valid], end_idx[valid], codes[valid]):
        start = max(0, int(start))
        end = min(target_length, int(end))
        if end > start:
            dense[start:end] = code
    return dense


def _dense_annotation_to_target(
    entry: Dict[str, Any],
    target_length: int,
    target_fs: float,
    fallback_fs: float,
) -> np.ndarray:
    values = np.asarray(entry.get("values", []))
    source_fs = _as_float(entry.get("fs"), fallback_fs)
    return _resample_by_nearest(values, source_fs, target_fs, target_length)


def _annotations_to_frame_columns(
    annotations: Dict[str, Dict[str, Any]],
    target_length: int,
    target_fs: float,
    fallback_fs: float,
) -> Dict[str, np.ndarray]:
    columns: Dict[str, np.ndarray] = {}
    for annotator, ann_types in annotations.items():
        for ann_type, entry in ann_types.items():
            if not isinstance(entry, dict):
                continue
            column_name = _annotation_column_name(annotator, ann_type)
            if "values" in entry:
                columns[column_name] = _dense_annotation_to_target(
                    entry, target_length, target_fs, fallback_fs,
                )
            elif "starts" in entry and "codes" in entry:
                columns[column_name] = _event_table_to_dense(
                    entry, target_length, target_fs,
                )
    return columns


def load_standardized_h5(
    file_path: str | Path,
    load_digital: bool = False,
    load_annotations: bool = True,
    load_features: bool = True,
    like_mne: bool = False,
    resample_fs: int = 200,
):
    """Load a Sleep YODA H5 file.

    Dictionary mode returns ``signals, metadata, annotations, features,
    channel_info``. ``like_mne=True`` returns ``Xy_df, metadata`` with all
    signals and annotations synchronized to ``resample_fs``.
    """

    file_path = Path(file_path)
    signals: Dict[str, np.ndarray]
    fs_by_channel: Dict[str, float]
    annotations: Dict[str, Dict[str, Any]] = {}
    features: Dict[str, Any] = {}

    with h5py.File(file_path, "r") as h5:
        metadata = _decode_attrs(h5.attrs)
        root_fs = _as_float(
            metadata.get("sampling_rate", metadata.get("fs")),
            float(resample_fs),
        )
        signals, fs_by_channel, channel_info = _read_signals(h5, load_digital)
        if load_annotations:
            annotations = _read_annotations(h5, root_fs)
        if load_features:
            features = _read_features(h5)

    duration_sec = _metadata_duration_seconds(metadata, signals, fs_by_channel)
    if duration_sec > 0:
        metadata.setdefault("duration_sec", duration_sec)

    if not like_mne:
        return signals, metadata, annotations, features, channel_info

    if duration_sec <= 0:
        raise ValueError(
            "duration_sec is missing and could not be inferred from signal lengths."
        )

    target_fs = float(resample_fs)
    target_length = int(round(duration_sec * target_fs))
    xy: Dict[str, np.ndarray] = {}

    for name, data in signals.items():
        source_fs = fs_by_channel.get(name) or root_fs
        xy[name] = _resample_signal(
            name,
            data,
            source_fs,
            target_fs,
            target_length,
            channel_info,
        )

    if load_annotations:
        xy.update(
            _annotations_to_frame_columns(
                annotations,
                target_length,
                target_fs,
                fallback_fs=root_fs,
            )
        )

    metadata = dict(metadata)
    metadata["resample_fs"] = resample_fs
    metadata["duration_sec"] = duration_sec
    metadata["n_samples"] = target_length
    return pd.DataFrame(xy), metadata
