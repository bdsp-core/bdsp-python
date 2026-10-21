"""H5 and namespace imports must not require the optional Zarr dependency."""

import subprocess
import sys


def test_h5_import_without_zarr():
    code = """
import sys
sys.modules['zarr'] = None
import bdsp
from bdsp.bids_zarr import open_recording
assert bdsp.read_h5
try:
    open_recording(primary='/unused.zarr')
except ImportError as error:
    assert 'bdsp[zarr]' in str(error)
else:
    raise AssertionError('missing optional dependency did not fail clearly')
"""
    subprocess.run([sys.executable, "-c", code], check=True)
