"""Efficient H5 reader for legacy standardized and native event-table PSG files.

Legacy standardized files expose a single 200 Hz sample space with dense
annotations under ``/annotations``.

Native Sleep YODA files keep each signal at its own sampling rate under
``/signals`` and store annotations as event tables under
``/annotations/<annotator>/<type>``. This reader presents both formats through
the same 200 Hz compatibility API used by the rest of the application.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import h5py
import numpy as np
import pandas as pd

from .derivations import BIPOLAR_DERIVATIONS, available_derivations


_COMPAT_FS = 200.0
_SUPPORTED_ANN_TYPES = ('stage', 'arousal', 'resp', 'limb', 'position')
_MODEL_ANNOTATOR_NAMES = {'caisr', 'panda', 'tnt', 'model', 'models'}


# Sleep stage mapping
STAGE_MAP = {0: 'W', 1: 'N1', 2: 'N2', 3: 'N3', 5: 'REM', 9: 'Unknown'}
STAGE_COLORS = {
    'W': '#FFD700',     # Gold (255, 215, 0)
    'REM': '#800080',   # Purple (128, 0, 128)
    'N1': '#ADD8E6',    # Light blue (173, 216, 230)
    'N2': '#0000FF',    # Blue (0, 0, 255)
    'N3': '#00008B',    # Dark blue (0, 0, 139)
    'Unknown': '#808080',  # Gray
}

# Y-axis levels for hypnogram (W at top, N3 at bottom)
STAGE_Y_LEVELS = {'W': 4, 'REM': 3, 'N1': 2, 'N2': 1, 'N3': 0, 'Unknown': 4}

_STAGE_LABEL_TO_CANONICAL = {
    'w': 0,
    'wake': 0,
    'n1': 1,
    'stage n1': 1,
    'n2': 2,
    'stage n2': 2,
    'n3': 3,
    'stage n3': 3,
    'rem': 5,
    'r': 5,
    'unknown': 9,
    'unk': 9,
    'unscored': 9,
}
_NEW_STAGE_CODE_TO_CANONICAL = {0: 9, 1: 3, 2: 2, 3: 1, 4: 5, 5: 0}
_RESP_EVENT_TABLE_CODE_TO_CANONICAL = {
    1: 1,   # OA
    2: 2,   # CA
    3: 3,   # MA
    4: 4,   # obstructive hypopnea
    5: 6,   # central hypopnea
    6: 4,   # mixed hypopnea -> generic hypopnea bucket
    7: 5,   # RERA
    8: 1,   # unspecified apnea -> apnea bucket
    9: 4,   # unspecified hypopnea
}


def _decode_h5_scalar(value: Any) -> Any:
    if isinstance(value, (bytes, np.bytes_)):
        return value.decode('utf-8', errors='replace')
    if isinstance(value, np.ndarray) and value.ndim == 0:
        return value.item()
    return value


def _normalize_label(value: Any) -> str:
    value = _decode_h5_scalar(value)
    if value is None:
        return ''
    text = str(value).strip().lower().replace('_', ' ').replace('-', ' ')
    return ' '.join(text.split())


def _decode_event_map(raw: Any) -> Dict[int, Any]:
    raw = _decode_h5_scalar(raw)
    if raw in (None, ''):
        return {}
    if isinstance(raw, dict):
        items = raw.items()
    else:
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        if not isinstance(parsed, dict):
            return {}
        items = parsed.items()

    decoded: Dict[int, Any] = {}
    for key, value in items:
        try:
            decoded[int(key)] = value
        except Exception:
            continue
    return decoded


def _is_event_table_group(obj: Any) -> bool:
    return (
        isinstance(obj, h5py.Group)
        and 'starts' in obj
        and 'codes' in obj
        and ('ends' in obj or 'durations' in obj or obj.attrs.get('format') == 'event_table')
    )


class BDSPH5Reader:
    """Memory-efficient reader for BDSP/Sleep YODA H5 files."""

    def __init__(self, h5_path, mode: str = 'r'):
        self.h5_path = Path(h5_path)
        self.mode = mode
        self._file: Optional[h5py.File] = None
        self._metadata: Dict[str, Any] = {}
        self._signal_info: Dict[str, Dict[str, Any]] = {}
        self._annotation_cache: Dict[Tuple[str, str], np.ndarray] = {}
        self._human_annotation_group_name: Optional[str] = None
        self._annotation_source: str = 'auto'  # 'auto', 'caisr', 'human'
        self._stage_override: Optional[Dict[str, Any]] = None
        # In-memory spectrogram cache, populated by an on-the-fly worker when
        # the H5 has no `/spectrograms` group. Keyed by channel ch_key
        # (e.g. 'c3_m2') -> (spec_db, freqs, times).
        self._inmem_spectrograms: Optional[
            Dict[str, Tuple[np.ndarray, np.ndarray, np.ndarray]]
        ] = None
        self.open()

    def set_inmem_spectrograms(
        self,
        specs: Optional[Dict[str, Tuple[np.ndarray, np.ndarray, np.ndarray]]],
    ) -> None:
        """Install (or clear) the in-memory spectrogram cache."""
        self._inmem_spectrograms = specs or None

    def open(self):
        if self._file is not None:
            return
        if not self.h5_path.exists():
            raise FileNotFoundError(f"H5 file not found: {self.h5_path}")
        self._file = h5py.File(self.h5_path, self.mode)
        self._load_metadata()

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _load_metadata(self):
        if self._file is None:
            return

        self._signal_info = self._discover_signals()
        self._human_annotation_group_name = self._discover_human_annotation_group_name()
        uses_event_tables = self._detect_event_table_annotations()
        native_duration = self._infer_duration_seconds()

        compat_fs = self._infer_compat_fs(uses_event_tables)
        if native_duration > 0:
            compat_n_samples = int(round(native_duration * compat_fs))
        elif self._signal_info:
            first_info = next(iter(self._signal_info.values()))
            native_fs = float(first_info.get('fs', compat_fs) or compat_fs)
            compat_n_samples = int(round(first_info['n_samples'] * compat_fs / native_fs))
            native_duration = compat_n_samples / compat_fs if compat_fs > 0 else 0.0
        else:
            compat_n_samples = 0

        self._metadata['uses_event_tables'] = uses_event_tables
        self._metadata['native_duration_seconds'] = native_duration
        self._metadata['fs'] = compat_fs
        self._metadata['n_samples'] = compat_n_samples
        self._metadata['channel_names'] = sorted(self._signal_info.keys())
        self._metadata['annotation_types'] = self._discover_annotation_types()

        meas_date = _decode_h5_scalar(self._file.attrs.get('meas_date'))
        if meas_date is not None:
            try:
                self._metadata['meas_date'] = datetime.fromisoformat(str(meas_date))
            except ValueError:
                self._metadata['meas_date'] = None
        else:
            self._metadata['meas_date'] = None

        unit_voltage = _decode_h5_scalar(self._file.attrs.get('unit_voltage'))
        if not unit_voltage:
            unit_voltage = self._infer_unit_voltage()
        self._metadata['unit_voltage'] = unit_voltage or 'V'

    def _discover_signals(self) -> Dict[str, Dict[str, Any]]:
        info: Dict[str, Dict[str, Any]] = {}
        if self._file is None or 'signals' not in self._file:
            return info

        def visit(name: str, obj: Any):
            if not isinstance(obj, h5py.Dataset):
                return
            channel_name = name.split('/')[-1].lower()
            attrs = {key: _decode_h5_scalar(val) for key, val in obj.attrs.items()}
            fs = attrs.get('fs')
            try:
                fs = float(fs) if fs is not None else None
            except Exception:
                fs = None
            if fs is None or fs <= 0:
                root_fs = self._file.attrs.get('sampling_rate', self._file.attrs.get('fs'))
                fs = float(root_fs) if root_fs is not None else _COMPAT_FS
            info[channel_name] = {
                'path': f"signals/{name}",
                'fs': fs,
                'shape': obj.shape,
                'n_samples': int(obj.shape[0]) if obj.shape else 0,
                'unit': attrs.get('unit', ''),
                'type': attrs.get('type', ''),
                'phys_min': attrs.get('phys_min'),
                'phys_max': attrs.get('phys_max'),
                'dig_min': attrs.get('dig_min'),
                'dig_max': attrs.get('dig_max'),
            }

        self._file['signals'].visititems(visit)
        return info

    def _discover_human_annotation_group_name(self) -> Optional[str]:
        if self._file is None or 'annotations' not in self._file:
            return None

        annotations = self._file['annotations']
        if any(name in annotations and isinstance(annotations[name], h5py.Dataset)
               for name in _SUPPORTED_ANN_TYPES):
            return None

        preferred = ('expert_1', 'human', 'expert1', 'scorer', 'expert_2')
        for name in preferred:
            if name in annotations and isinstance(annotations[name], h5py.Group):
                return name

        for name, obj in annotations.items():
            if not isinstance(obj, h5py.Group):
                continue
            norm = _normalize_label(name)
            if norm in _MODEL_ANNOTATOR_NAMES:
                continue
            if any(key in obj for key in _SUPPORTED_ANN_TYPES):
                return name
        return None

    def _detect_event_table_annotations(self) -> bool:
        if self._file is None or 'annotations' not in self._file:
            return False

        annotations = self._file['annotations']
        for _, obj in annotations.items():
            if _is_event_table_group(obj):
                return True
            if isinstance(obj, h5py.Group):
                for child in obj.values():
                    if _is_event_table_group(child):
                        return True
        return False

    def _infer_duration_seconds(self) -> float:
        if self._file is None:
            return 0.0

        duration = self._file.attrs.get('duration_sec')
        if duration is not None:
            try:
                return float(duration)
            except Exception:
                pass

        max_duration = 0.0
        for info in self._signal_info.values():
            fs = float(info.get('fs', 0.0) or 0.0)
            if fs > 0:
                max_duration = max(max_duration, info['n_samples'] / fs)
        return max_duration

    def _infer_compat_fs(self, uses_event_tables: bool) -> float:
        if uses_event_tables:
            return _COMPAT_FS

        if self._file is not None:
            for attr_name in ('sampling_rate', 'fs'):
                if attr_name in self._file.attrs:
                    try:
                        return float(self._file.attrs[attr_name])
                    except Exception:
                        pass

        if self._signal_info:
            return float(next(iter(self._signal_info.values())).get('fs', _COMPAT_FS))
        return _COMPAT_FS

    def _infer_unit_voltage(self) -> str:
        for info in self._signal_info.values():
            unit = _normalize_label(info.get('unit'))
            if unit in {'uv', '\u00b5v'}:
                return 'uV'
            if unit == 'v':
                return 'V'
            if unit == 'mv':
                return 'mV'
        return 'uV' if self.uses_event_tables else 'V'

    def _discover_annotation_types(self) -> List[str]:
        found: List[str] = []
        if self._file is None:
            return found

        human_group = self._annotation_group_for_source('human')
        if human_group is not None:
            found.extend([name for name in human_group.keys() if name not in found])

        caisr_group = self._annotation_group_for_source('caisr')
        if caisr_group is not None:
            for name in caisr_group.keys():
                if name not in found:
                    found.append(name)
        return found

    @property
    def fs(self) -> float:
        return self._metadata.get('fs', _COMPAT_FS)

    @property
    def n_samples(self) -> int:
        return self._metadata.get('n_samples', 0)

    @property
    def duration_seconds(self) -> float:
        return self._metadata.get('native_duration_seconds', 0.0)

    @property
    def channel_names(self) -> List[str]:
        raw = self._metadata.get('channel_names', [])
        derived = available_derivations(raw)
        if not derived:
            return raw
        return sorted(set(raw) | set(derived))

    @property
    def raw_channel_names(self) -> List[str]:
        """Channels physically stored in the H5 (excludes runtime derivations)."""
        return list(self._metadata.get('channel_names', []))

    @property
    def meas_date(self) -> Optional[datetime]:
        return self._metadata.get('meas_date', None)

    @property
    def unit_voltage(self) -> str:
        return self._metadata.get('unit_voltage', 'V')

    @property
    def annotation_types(self) -> List[str]:
        return self._metadata.get('annotation_types', [])

    @property
    def uses_event_tables(self) -> bool:
        return bool(self._metadata.get('uses_event_tables', False))

    @property
    def is_standardized(self) -> bool:
        """Whether the file is already in a viewer-ready H5 format."""
        if self._file is None:
            return False
        return bool(self._file.attrs.get('standardized', False) or self.uses_event_tables)

    # --- Annotation source switching ---

    @property
    def annotation_source(self) -> str:
        return self._annotation_source

    @annotation_source.setter
    def annotation_source(self, value: str):
        if value not in ('auto', 'caisr', 'human'):
            raise ValueError(f"Invalid annotation source: {value!r}")
        self._annotation_source = value

    @property
    def has_caisr_annotations(self) -> bool:
        if self._annotation_group_for_source('caisr') is not None:
            return True
        try:
            return self._find_companion_file('caisr_annotations') is not None
        except Exception:
            return False

    @property
    def has_human_annotations(self) -> bool:
        group = self._annotation_group_for_source('human')
        return bool(group is not None and 'stage' in group)

    @property
    def active_annotation_source(self) -> str:
        if self._annotation_source == 'caisr':
            return 'caisr'
        if self._annotation_source == 'human':
            return 'human'
        return 'caisr' if self.has_caisr_annotations else 'human'

    def available_annotation_sources(self) -> List[str]:
        """Sources eligible for the annotation-source dropdown.

        'auto' is always offered. 'human' / 'caisr' appear only when the
        underlying annotations exist in the H5 / companion file. The caller
        can still force 'caisr' (to trigger on-the-fly CAISR inference)
        even when it isn't in this list.
        """
        sources: List[str] = ['auto']
        if self.has_human_annotations:
            sources.append('human')
        if self.has_caisr_annotations:
            sources.append('caisr')
        return sources

    def _annotation_group_for_source(self, source: str) -> Optional[h5py.Group]:
        if self._file is None:
            return None

        if source == 'caisr':
            if ('annotations_source' in self._file
                    and 'caisr' in self._file['annotations_source']
                    and isinstance(self._file['annotations_source']['caisr'], h5py.Group)):
                return self._file['annotations_source']['caisr']
            if ('annotations' in self._file
                    and 'caisr' in self._file['annotations']
                    and isinstance(self._file['annotations']['caisr'], h5py.Group)):
                return self._file['annotations']['caisr']
            return None

        if 'annotations' not in self._file:
            return None

        annotations = self._file['annotations']
        if any(name in annotations and isinstance(annotations[name], h5py.Dataset)
               for name in _SUPPORTED_ANN_TYPES):
            return annotations
        if self._human_annotation_group_name and self._human_annotation_group_name in annotations:
            grp = annotations[self._human_annotation_group_name]
            if isinstance(grp, h5py.Group):
                return grp
        return None

    def _annotation_group(self) -> str:
        if self.active_annotation_source == 'caisr':
            if ('annotations_source' in self._file
                    and 'caisr' in self._file['annotations_source']):
                return 'annotations_source/caisr'
            return 'annotations/caisr'
        if self._human_annotation_group_name:
            return f"annotations/{self._human_annotation_group_name}"
        return 'annotations'

    def _caisr_h5_group_present(self) -> bool:
        return self._annotation_group_for_source('caisr') is not None

    def _get_caisr_annotation_values(self, ann_type: str) -> Optional[np.ndarray]:
        try:
            caisr = self.get_caisr_csv()
        except Exception:
            caisr = None
        if caisr is None:
            return None
        col_map = {
            'stage': 'stage_caisr',
            'arousal': 'arousal_caisr',
            'resp': 'resp_caisr',
            'limb': 'limb_caisr',
        }
        col = col_map.get(ann_type)
        if col is None or col not in caisr.columns:
            return None
        vals = caisr[col].to_numpy(dtype=np.float32).flatten()
        if ann_type == 'stage':
            remapped = np.full_like(vals, 9.0, dtype=np.float32)
            remapped[np.isnan(vals)] = np.nan
            # Companion CAISR CSVs use CAISR coding:
            # 1=N3, 2=N2, 3=N1, 4=REM, 5=Wake.
            for caisr_val, yoda_val in {1: 3, 2: 2, 3: 1, 4: 5, 5: 0, 9: 9}.items():
                remapped[vals == caisr_val] = yoda_val
            vals = remapped
        fill_value = 9.0 if ann_type == 'stage' else np.nan
        if len(vals) < self.n_samples:
            pad = np.full(self.n_samples - len(vals), fill_value, dtype=np.float32)
            vals = np.concatenate([vals, pad])
        else:
            vals = vals[:self.n_samples]
        return vals

    # --- Stage-source overrides (model-inferred / external hypnogram) ---

    def set_annotation_source(self, source: str) -> None:
        self.annotation_source = source

    def annotation_source_name(self) -> str:
        return self._annotation_source

    @property
    def has_stage_override(self) -> bool:
        return self._stage_override is not None

    @property
    def stage_source_name(self) -> str:
        if self._stage_override is not None:
            return str(self._stage_override.get('source_name', 'model'))
        return self.active_annotation_source

    @property
    def stage_epoch_seconds(self) -> Optional[float]:
        if self._stage_override is None:
            return None
        return float(self._stage_override['epoch_seconds'])

    def has_stage_annotations(self) -> bool:
        group = self._annotation_group_for_source('human')
        return bool(group is not None and 'stage' in group)

    def has_stage_data(self) -> bool:
        return (
            self.has_stage_override
            or self.has_stage_annotations()
            or self.has_caisr_annotations
        )

    def set_stage_override(
        self,
        stage_codes: np.ndarray,
        epoch_seconds: float = 30.0,
        source_name: str = 'model',
        stage_probs: Optional[np.ndarray] = None,
    ) -> None:
        self._stage_override = {
            'stage_codes': np.asarray(stage_codes, dtype=np.float32),
            'epoch_seconds': float(epoch_seconds),
            'source_name': source_name,
            'stage_probs': stage_probs,
        }

    def clear_stage_override(self) -> None:
        self._stage_override = None

    def get_annotation_for_source(
        self,
        source: str,
        ann_type: str,
        start_sample: int,
        n_samples: int,
    ) -> np.ndarray:
        """Return an annotation from a specific source, ignoring stage overrides.

        Valid sources are `human`, `caisr`, and `active`. The `active` source
        preserves the current reader behavior, including any stage override.
        """

        if source == 'active':
            return self.get_annotation(ann_type, start_sample, n_samples)
        if source not in ('human', 'caisr'):
            raise ValueError(f"Invalid annotation source: {source!r}")

        old_source = self._annotation_source
        old_override = self._stage_override
        try:
            self._stage_override = None
            self._annotation_source = source
            return self.get_annotation(ann_type, start_sample, n_samples)
        finally:
            self._annotation_source = old_source
            self._stage_override = old_override

    def _stage_override_samples(self) -> np.ndarray:
        if self._stage_override is None:
            return np.array([], dtype=np.float32)
        codes = np.asarray(self._stage_override.get('stage_codes', []), dtype=np.float32).flatten()
        if codes.size == 0:
            return np.array([], dtype=np.float32)
        epoch_samples = max(1, int(round(float(self._stage_override['epoch_seconds']) * self.fs)))
        samples = np.repeat(codes, epoch_samples).astype(np.float32)
        if samples.size < self.n_samples:
            pad = np.full(self.n_samples - samples.size, 9.0, dtype=np.float32)
            samples = np.concatenate([samples, pad])
        else:
            samples = samples[:self.n_samples]
        return samples

    def get_stage_probabilities(
        self, epoch_seconds: float = 30.0,
    ) -> Tuple[np.ndarray, np.ndarray]:
        if (self._stage_override is not None
                and self._stage_override.get('stage_probs') is not None):
            probs = np.asarray(self._stage_override['stage_probs'], dtype=np.float32)
            ep = float(self._stage_override['epoch_seconds'])
            times = np.arange(probs.shape[0], dtype=np.float64) * ep
            return probs, times

        try:
            caisr = self.get_caisr_csv()
        except Exception:
            caisr = None
        if caisr is not None:
            needed = [
                'caisr_prob_n3', 'caisr_prob_n2', 'caisr_prob_n1',
                'caisr_prob_r', 'caisr_prob_w',
            ]
            if all(c in caisr.columns for c in needed):
                raw = caisr[needed].to_numpy(dtype=np.float32)
                epoch_size = max(1, int(epoch_seconds * self.fs))
                probs = raw[::epoch_size]
                times = np.arange(probs.shape[0], dtype=np.float64) * epoch_seconds
                return probs, times

        if self._file is not None:
            for grp in filter(None, (
                self._annotation_group_for_source('caisr'),
                self._annotation_group_for_source('human'),
                self._file['annotations'] if 'annotations' in self._file else None,
            )):
                # CAISR in-process writes the per-epoch probability matrix as
                # `stage_probs` under /annotations_source/caisr/. Older files
                # (or external CAISR-App output) may use `stage_probabilities`.
                # Accept either.
                ds_name = next(
                    (n for n in ('stage_probs', 'stage_probabilities') if n in grp),
                    None,
                )
                if ds_name is None:
                    continue
                try:
                    data = grp[ds_name][:]
                    n_epochs = data.shape[0]
                    duration = self.duration_seconds
                    native_epoch = duration / n_epochs if n_epochs > 0 else epoch_seconds
                    times = np.arange(n_epochs, dtype=np.float64) * native_epoch
                    return data.astype(np.float32), times
                except Exception:
                    pass

        return np.empty((0, 5), dtype=np.float32), np.array([], dtype=np.float64)

    def _resolve_channel_name(self, name: str) -> str:
        key = name.lower()
        if key not in self._signal_info:
            raise KeyError(f"Channel '{name}' not found in H5 file")
        return key

    def _dataset_to_1d(self, dataset: h5py.Dataset, start: int, end: int) -> np.ndarray:
        data = dataset[start:end]
        if isinstance(data, np.ndarray) and data.ndim > 1:
            data = data[:, 0]
        return np.asarray(data)

    def _signal_needs_physical_conversion(self, info: Dict[str, Any]) -> bool:
        return all(info.get(key) is not None for key in ('phys_min', 'phys_max', 'dig_min', 'dig_max'))

    def _read_signal_native_slice(self, info: Dict[str, Any], start: int, end: int) -> np.ndarray:
        if self._file is None:
            return np.zeros(0, dtype=np.float32)
        start = max(0, start)
        end = min(end, int(info['n_samples']))
        if end <= start:
            return np.zeros(0, dtype=np.float32)

        dataset = self._file[info['path']]
        data = self._dataset_to_1d(dataset, start, end)
        if not self._signal_needs_physical_conversion(info):
            return data.astype(np.float32, copy=False)

        dig_min = float(info['dig_min'])
        dig_max = float(info['dig_max'])
        phys_min = float(info['phys_min'])
        phys_max = float(info['phys_max'])
        if dig_max == dig_min:
            return data.astype(np.float32, copy=False)

        scale = (phys_max - phys_min) / (dig_max - dig_min)
        offset = phys_min - dig_min * scale
        return data.astype(np.float32) * scale + offset

    def _signal_is_discrete(self, name: str, info: Dict[str, Any]) -> bool:
        unit = _normalize_label(info.get('unit'))
        typ = _normalize_label(info.get('type'))
        return (
            unit == 'bool'
            or 'bool' in unit
            or 'bool' in typ
            or name == 'cpap_on'
        )

    def get_channel_slice(self, name: str, start_sample: int, n_samples: int) -> np.ndarray:
        if self._file is None:
            raise RuntimeError("H5 file is not open")

        # Bipolar derivations: read both members and subtract. Members go
        # through the standard signal path (digital to physical conversion,
        # native-fs to compat-fs resampling) before we subtract, so the result
        # is in physical units and aligned to the same time base.
        key = name.lower()
        if key in BIPOLAR_DERIVATIONS and key not in self._signal_info:
            pos, neg = BIPOLAR_DERIVATIONS[key]
            if pos not in self._signal_info or neg not in self._signal_info:
                missing = [c for c in (pos, neg) if c not in self._signal_info]
                raise KeyError(
                    f"Channel '{name}' is a bipolar derivation but the H5 "
                    f"is missing required member(s): {missing}"
                )
            sig_pos = self.get_channel_slice(pos, start_sample, n_samples)
            sig_neg = self.get_channel_slice(neg, start_sample, n_samples)
            return sig_pos - sig_neg

        key = self._resolve_channel_name(name)
        info = self._signal_info[key]

        start_sample = max(0, int(start_sample))
        end_sample = min(start_sample + int(n_samples), self.n_samples)
        actual_n = end_sample - start_sample
        if actual_n <= 0:
            return np.zeros(0, dtype=np.float32)

        native_fs = float(info.get('fs', self.fs) or self.fs)
        if abs(native_fs - self.fs) < 1e-6 and not self.uses_event_tables:
            return self._read_signal_native_slice(info, start_sample, end_sample)

        target_times = (start_sample + np.arange(actual_n, dtype=np.float64)) / self.fs
        native_start = max(0, int(np.floor(target_times[0] * native_fs)) - 1)
        native_end = min(
            int(info['n_samples']),
            int(np.ceil((target_times[-1] + (1.0 / self.fs)) * native_fs)) + 1,
        )
        native = self._read_signal_native_slice(info, native_start, native_end)
        if native.size == 0:
            return np.zeros(0, dtype=np.float32)
        if native.size == 1:
            return np.full(actual_n, native[0], dtype=np.float32)

        if self._signal_is_discrete(key, info):
            src_idx = np.clip(
                np.round(target_times * native_fs).astype(np.int64),
                native_start,
                native_end - 1,
            ) - native_start
            return native[src_idx].astype(np.float32, copy=False)

        native_times = (native_start + np.arange(native.size, dtype=np.float64)) / native_fs
        resampled = np.interp(
            target_times,
            native_times,
            native.astype(np.float64, copy=False),
            left=float(native[0]),
            right=float(native[-1]),
        )
        return resampled.astype(np.float32)

    def get_channels_slice(
        self, names: List[str], start_time: float, duration: float,
    ) -> Tuple[np.ndarray, np.ndarray]:
        start_sample = int(start_time * self.fs)
        n_samples = int(duration * self.fs)

        data = np.zeros((len(names), n_samples), dtype=np.float32)
        for i, name in enumerate(names):
            try:
                ch_data = self.get_channel_slice(name, start_sample, n_samples)
            except KeyError:
                continue
            data[i, :len(ch_data)] = ch_data

        time_vector = np.arange(data.shape[1], dtype=np.float64) / self.fs + start_time
        return data, time_vector

    def read_signal(self, name: str, start: float = 0.0, duration: Optional[float] = None) -> Tuple[np.ndarray, np.ndarray]:
        """Read one signal by time range and return ``(values, times)``.

        Args:
            name: Channel name, including runtime derivations such as ``c3-m2``.
            start: Start time in seconds from recording start.
            duration: Duration in seconds. If omitted, reads to the end.
        """

        start_time = max(0.0, float(start))
        if duration is None:
            duration = max(0.0, self.duration_seconds - start_time)
        data, times = self.get_channels_slice([name], start_time, float(duration))
        return data[0], times

    def _annotation_code_to_canonical(
        self,
        ann_type: str,
        code: Any,
        event_map: Dict[int, Any],
    ) -> int:
        try:
            code_int = int(code)
        except Exception:
            return 9 if ann_type == 'stage' else 0

        label = _normalize_label(event_map.get(code_int))

        if ann_type == 'stage':
            if label in _STAGE_LABEL_TO_CANONICAL:
                return _STAGE_LABEL_TO_CANONICAL[label]
            if self.uses_event_tables:
                return _NEW_STAGE_CODE_TO_CANONICAL.get(code_int, 9)
            return code_int if code_int in STAGE_MAP else 9

        if ann_type == 'arousal':
            if code_int == 0:
                return 0
            if 'resp' in label:
                return 2
            if 'plm' in label or 'limb' in label or 'leg' in label:
                return 3
            if code_int in (1, 2, 3):
                return code_int
            return 1

        if ann_type == 'resp':
            if label:
                if 'rera' in label:
                    return 5
                if 'central hyp' in label:
                    return 6
                if 'mixed hyp' in label or 'obstructive hyp' in label or 'hypop' in label:
                    return 4
                if 'central apnea' in label or label == 'ca':
                    return 2
                if 'mixed apnea' in label or label == 'ma':
                    return 3
                if 'obstructive apnea' in label or label == 'oa':
                    return 1
                if 'apnea' in label:
                    return 1
            if self.uses_event_tables:
                return _RESP_EVENT_TABLE_CODE_TO_CANONICAL.get(code_int, 0)
            return code_int

        if ann_type == 'limb':
            if code_int == 0:
                return 0
            if label:
                if 'plm' in label or 'periodic' in label:
                    return 3 if 'arousal' in label else 2
                if 'arousal' in label and ('movement' in label or 'limb' in label or 'leg' in label):
                    return 3
                if 'movement' in label or 'limb' in label or 'leg' in label or label == 'lm':
                    return 1
            if code_int in (1, 2, 3, 4):
                return code_int
            return 1

        if ann_type == 'position':
            return code_int

        return code_int

    def _load_event_table_dense(self, ann_type: str, group: h5py.Group) -> np.ndarray:
        cache_key = (group.name, ann_type)
        cached = self._annotation_cache.get(cache_key)
        if cached is not None:
            return cached

        fill_value = 9 if ann_type == 'stage' else 0
        dense = np.full(self.n_samples, fill_value, dtype=np.int16)
        event_map = _decode_event_map(group.attrs.get('event_map'))

        starts = np.asarray(group['starts'][:], dtype=np.float64)
        if 'ends' in group:
            ends = np.asarray(group['ends'][:], dtype=np.float64)
        elif 'durations' in group:
            ends = starts + np.asarray(group['durations'][:], dtype=np.float64)
        else:
            ends = starts
        codes = np.asarray(group['codes'][:], dtype=np.int64)

        starts_idx = np.floor(starts * self.fs).astype(np.int64)
        ends_idx = np.ceil(ends * self.fs).astype(np.int64)

        for start_idx, end_idx, raw_code in zip(starts_idx, ends_idx, codes):
            start_idx = max(0, int(start_idx))
            end_idx = min(self.n_samples, int(end_idx))
            if end_idx <= start_idx:
                continue
            dense[start_idx:end_idx] = self._annotation_code_to_canonical(
                ann_type, raw_code, event_map,
            )

        self._annotation_cache[cache_key] = dense
        return dense

    def _annotation_object(self, source: str, ann_type: str) -> Optional[Any]:
        group = self._annotation_group_for_source(source)
        if group is None or ann_type not in group:
            return None
        return group[ann_type]

    def get_annotation(self, ann_type: str, start_sample: int, n_samples: int) -> np.ndarray:
        if self._file is None:
            raise RuntimeError("H5 file is not open")

        start_sample = max(0, int(start_sample))
        end_sample = min(start_sample + int(n_samples), self.n_samples)
        actual_n = end_sample - start_sample
        if actual_n <= 0:
            return np.zeros(0, dtype=np.float32)

        if ann_type == 'stage' and self._stage_override is not None:
            samples = self._stage_override_samples()
            return samples[start_sample:end_sample].copy()

        if self.active_annotation_source == 'caisr' and not self._caisr_h5_group_present():
            vals = self._get_caisr_annotation_values(ann_type)
            if vals is not None:
                return vals[start_sample:end_sample].copy()

        obj = self._annotation_object(self.active_annotation_source, ann_type)
        if obj is None and self.active_annotation_source != 'human':
            obj = self._annotation_object('human', ann_type)
        if obj is None:
            return np.full(actual_n, np.nan, dtype=np.float32)

        if isinstance(obj, h5py.Dataset):
            data = self._dataset_to_1d(obj, start_sample, end_sample)
            return data.astype(np.float32, copy=False)

        if _is_event_table_group(obj):
            dense = self._load_event_table_dense(ann_type, obj)
            return dense[start_sample:end_sample].astype(np.float32, copy=True)

        return np.full(actual_n, np.nan, dtype=np.float32)

    def get_annotation_time_slice(self, ann_type: str, start_time: float, duration: float) -> np.ndarray:
        start_sample = int(start_time * self.fs)
        n_samples = int(duration * self.fs)
        return self.get_annotation(ann_type, start_sample, n_samples)

    def get_stage_hypnogram(self, epoch_seconds: float = 30.0) -> Tuple[np.ndarray, np.ndarray]:
        if self._stage_override is not None:
            codes = np.asarray(self._stage_override['stage_codes'], dtype=np.float32)
            ep = float(self._stage_override['epoch_seconds'])
            times = np.arange(len(codes), dtype=np.float64) * ep
            if abs(ep - epoch_seconds) < 1e-6:
                return codes, times

        stage_data = self.get_annotation('stage', 0, self.n_samples)
        if stage_data.size == 0 or np.all(np.isnan(stage_data)):
            return np.array([]), np.array([])

        epoch_size = max(1, int(round(epoch_seconds * self.fs)))
        n_epochs = len(stage_data) // epoch_size
        stages = np.zeros(n_epochs, dtype=np.float32)
        times = np.zeros(n_epochs, dtype=np.float64)

        for i in range(n_epochs):
            start = i * epoch_size
            end = start + epoch_size
            epoch = stage_data[start:end]
            vals, counts = np.unique(epoch[~np.isnan(epoch)], return_counts=True)
            stages[i] = vals[np.argmax(counts)] if len(vals) > 0 else 9
            times[i] = start / self.fs

        return stages, times

    def get_spo2_trend(self, epoch_seconds: float = 30.0) -> Tuple[np.ndarray, np.ndarray]:
        if 'spo2' not in self.channel_names:
            return np.array([]), np.array([])

        spo2_data = self.get_channel_slice('spo2', 0, self.n_samples)
        if spo2_data.size == 0:
            return np.array([]), np.array([])

        epoch_size = max(1, int(round(epoch_seconds * self.fs)))
        n_epochs = len(spo2_data) // epoch_size

        spo2_trend = np.zeros(n_epochs, dtype=np.float32)
        times = np.zeros(n_epochs, dtype=np.float64)

        for i in range(n_epochs):
            start = i * epoch_size
            end = start + epoch_size
            epoch = spo2_data[start:end]
            valid = epoch[(epoch > 0) & (epoch <= 100)]
            spo2_trend[i] = np.median(valid) if len(valid) > 0 else np.nan
            times[i] = start / self.fs

        return spo2_trend, times

    # --- Companion file access ---

    def _find_companion_file(self, suffix: str) -> Optional[Path]:
        parent = self.h5_path.parent
        stem = self.h5_path.stem
        for f in parent.iterdir():
            if f.name.startswith(stem.replace('.h5', '')) and suffix in f.name and f.suffix == '.csv':
                return f
        for f in parent.iterdir():
            if suffix in f.name and f.suffix == '.csv':
                return f
        return None

    def get_sleep_staging_csv(self) -> Optional[pd.DataFrame]:
        path = self._find_companion_file('sleep_annotations')
        if path is not None:
            return pd.read_csv(path)
        return None

    def get_events_csv(self) -> Optional[pd.DataFrame]:
        path = self._find_companion_file('events_annotations')
        if path is not None:
            return pd.read_csv(path)
        return None

    def get_caisr_csv(self) -> Optional[pd.DataFrame]:
        path = self._find_companion_file('caisr_annotations')
        if path is not None:
            return pd.read_csv(path)
        return None

    def get_events(self, event_type: str) -> np.ndarray:
        if self._file is None:
            return np.array([])
        if 'events' in self._file and event_type in self._file['events']:
            return self._file['events'][event_type][()]

        obj = self._annotation_object(self.active_annotation_source, event_type)
        if obj is None and self.active_annotation_source != 'human':
            obj = self._annotation_object('human', event_type)
        if obj is None or not _is_event_table_group(obj):
            return np.array([])

        starts = np.asarray(obj['starts'][:], dtype=np.float64)
        if 'durations' in obj:
            durations = np.asarray(obj['durations'][:], dtype=np.float64)
        elif 'ends' in obj:
            durations = np.asarray(obj['ends'][:], dtype=np.float64) - starts
        else:
            durations = np.zeros_like(starts)
        codes = np.asarray(obj['codes'][:], dtype=np.int64)
        event_map = _decode_event_map(obj.attrs.get('event_map'))

        dtype = np.dtype([
            ('onset_sample', np.int64),
            ('duration_samples', np.int64),
            ('subtype', np.uint16),
        ])
        out = np.zeros(len(starts), dtype=dtype)
        out['onset_sample'] = np.floor(starts * self.fs).astype(np.int64)
        out['duration_samples'] = np.maximum(1, np.round(durations * self.fs).astype(np.int64))
        out['subtype'] = np.asarray([
            self._annotation_code_to_canonical(event_type, code, event_map)
            for code in codes
        ], dtype=np.uint16)
        return out

    # --- Spectrogram access ---

    def _spectrogram_group(self) -> Optional[h5py.Group]:
        if self._file is None:
            return None
        if 'spectrograms' in self._file and isinstance(self._file['spectrograms'], h5py.Group):
            return self._file['spectrograms']
        if ('features' in self._file
                and 'spectrograms' in self._file['features']
                and isinstance(self._file['features']['spectrograms'], h5py.Group)):
            return self._file['features']['spectrograms']
        return None

    @property
    def has_spectrograms(self) -> bool:
        if self._inmem_spectrograms:
            return True
        return self._spectrogram_group() is not None

    def get_spectrogram(self, channel: str) -> Optional[Tuple[np.ndarray, np.ndarray, np.ndarray]]:
        ch_key = channel.replace('-', '_').lower()

        # In-memory cache wins over any on-disk group so an on-the-fly
        # compute can immediately show up after it finishes, even if a
        # stale (or partial) cache exists on disk.
        if self._inmem_spectrograms and ch_key in self._inmem_spectrograms:
            spec_db, freqs, times = self._inmem_spectrograms[ch_key]
            return spec_db, np.asarray(freqs), np.asarray(times)

        group = self._spectrogram_group()
        if group is None:
            return None
        if ch_key not in group:
            return None

        spec_db = group[ch_key][()]
        freqs = group.attrs.get('freqs')
        times = group.attrs.get('times')
        if freqs is None and 'freqs' in group:
            freqs = group['freqs'][()]
        if times is None and 'times' in group:
            times = group['times'][()]
        if freqs is None or times is None:
            return None
        return spec_db, np.asarray(freqs), np.asarray(times)

    def __repr__(self):
        return (
            f"BDSPH5Reader(path={self.h5_path.name}, "
            f"channels={len(self.channel_names)}, "
            f"samples={self.n_samples}, "
            f"duration={self.duration_seconds:.1f}s, "
            f"fs={self.fs}Hz)"
        )


PSGReader = BDSPH5Reader
