"""Sleep YODA compatibility aliases.

New code should prefer ``bdsp.read_h5`` and ``bdsp.open_h5``.
"""

from bdsp.h5 import load_feature_dataset, load_feature_table, load_standardized_h5
from bdsp.reader import BDSPH5Reader, PSGReader

__all__ = [
    "BDSPH5Reader",
    "PSGReader",
    "load_feature_dataset",
    "load_feature_table",
    "load_standardized_h5",
]
