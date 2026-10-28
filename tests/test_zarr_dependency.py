"""Namespace imports stay lazy and a missing required dependency is explained."""

import subprocess
import sys


def test_lazy_import_and_error_when_required_zarr_is_missing():
    code = """
import sys
sys.modules['zarr'] = None
import bdsp
from bdsp.bids_zarr import open_recording
assert bdsp.read_h5
try:
    open_recording(primary='/unused.zarr')
except ImportError as error:
    assert 'pip install --upgrade bdsp' in str(error)
else:
    raise AssertionError('missing required dependency did not fail clearly')
"""
    subprocess.run([sys.executable, "-c", code], check=True)
