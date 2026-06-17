# BDSP Python Reader

Reader-first Python package for BDSP/Sleep YODA standardized HDF5 data.

```python
import bdsp

record = bdsp.read_h5("/path/to/study.h5")

record.signals["c3-m2"]
record.metadata
record.annotations
record.features
record.channel_info

df = record.to_dataframe(resample_fs=200)

with bdsp.open_h5("/path/to/study.h5") as reader:
    x, t = reader.read_signal("c3-m2", start=60, duration=30)
```

## Scope

Version 0.1 reads BDSP/Sleep YODA H5 files. It intentionally does not include
EDF conversion, S3 downloading, GUI code, model inference, report generation, or
feature computation.

## API

- `bdsp.read(path, **kwargs)` dispatches to the H5 reader for `.h5` and `.hdf5`.
- `bdsp.read_h5(path, raw_digital=False, include_annotations=True, include_features=True)` returns a `BDSPRecord`.
- `BDSPRecord.to_dataframe(resample_fs=200)` returns synchronized signal and annotation columns.
- `bdsp.open_h5(path)` returns a lazy `BDSPH5Reader` for slice-based reads.
- `bdsp.compat.sleep_yoda` exposes `load_standardized_h5` and `PSGReader` compatibility aliases.

## Development

```bash
python -m pip install -e ".[test]"
pytest
```

The project metadata requires Python 3.10+. The reader source itself is kept
small and depends only on `h5py`, `numpy`, `pandas`, and `scipy`.

## License

This package is licensed under the Apache License, Version 2.0.
