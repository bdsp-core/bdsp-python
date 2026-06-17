"""Public API for BDSP reader utilities."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping

import numpy as np
import pandas as pd

from .h5 import load_standardized_h5
from .reader import BDSPH5Reader


@dataclass(frozen=True)
class BDSPRecord:
    """Eagerly loaded BDSP/Sleep YODA H5 recording."""

    path: Path
    signals: Mapping[str, np.ndarray]
    metadata: Dict[str, Any]
    annotations: Dict[str, Dict[str, Any]]
    features: Dict[str, Any]
    channel_info: pd.DataFrame
    raw_digital: bool = False
    include_annotations: bool = True
    include_features: bool = True

    def to_dataframe(self, resample_fs: int = 200) -> pd.DataFrame:
        """Return synchronized signals and annotations as a pandas DataFrame."""

        frame, _ = load_standardized_h5(
            self.path,
            load_digital=self.raw_digital,
            load_annotations=self.include_annotations,
            load_features=False,
            like_mne=True,
            resample_fs=resample_fs,
        )
        return frame


def read_h5(
    path: str | Path,
    *,
    raw_digital: bool = False,
    include_annotations: bool = True,
    include_features: bool = True,
) -> BDSPRecord:
    """Read a BDSP/Sleep YODA H5 file into a :class:`BDSPRecord`."""

    h5_path = Path(path)
    signals, metadata, annotations, features, channel_info = load_standardized_h5(
        h5_path,
        load_digital=raw_digital,
        load_annotations=include_annotations,
        load_features=include_features,
    )
    return BDSPRecord(
        path=h5_path,
        signals=signals,
        metadata=metadata,
        annotations=annotations,
        features=features,
        channel_info=channel_info,
        raw_digital=raw_digital,
        include_annotations=include_annotations,
        include_features=include_features,
    )


def read(path: str | Path, **kwargs: Any) -> BDSPRecord:
    """Read a supported BDSP data file.

    v0.1 supports HDF5 only.
    """

    file_path = Path(path)
    if file_path.suffix.lower() in {".h5", ".hdf5"}:
        return read_h5(file_path, **kwargs)
    raise ValueError(
        f"Unsupported BDSP file type {file_path.suffix!r}; v0.1 supports .h5/.hdf5 only."
    )


def open_h5(path: str | Path, mode: str = "r") -> BDSPH5Reader:
    """Open a BDSP/Sleep YODA H5 file for lazy slice-based access."""

    return BDSPH5Reader(path, mode=mode)
