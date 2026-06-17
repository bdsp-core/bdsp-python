"""BDSP Python reader package."""

from .api import BDSPRecord, open_h5, read, read_h5
from .h5 import load_feature_dataset, load_feature_table, load_standardized_h5
from .reader import BDSPH5Reader, PSGReader

__all__ = [
    "BDSPH5Reader",
    "BDSPRecord",
    "PSGReader",
    "load_feature_dataset",
    "load_feature_table",
    "load_standardized_h5",
    "open_h5",
    "read",
    "read_h5",
]

__version__ = "0.1.0"
