# BDSP Python Reader

Python readers for BDSP/Sleep YODA HDF5 and split Penn TS-Zarr/BIDS recordings.
The split reader combines primary signals and expert annotations, derivative
model annotations and feature payloads, and a derivative summary JSON. See the
[BIDS/Zarr storage and migration guide](docs/bids-zarr/README.md) and
[reader API and design](docs/bids-zarr/READER_DESIGN.md) for the storage contract
and differences from older H5 and Sleep YODA web bundles.

## Split BIDS/TS-Zarr

Install the optional reader on Python 3.11+:

```bash
python -m pip install 'bdsp[zarr]'
```

```python
from bdsp.bids_zarr import open_recording

with open_recording(
    primary="/data/primary.zarr",
    derivatives="/data/derivative.zarr",
    summary_json="/data/derivative.json",
) as record:
    window = record.signals["c3-m2"].read(start=60, duration=30)
    window.values, window.times_s, window.unit, window.rate_hz
    stages = record.annotations["expert_1"]["stage"].read(start=60, duration=30)
    summary = record.features.summary.to_dataframe()
    spectrum = record.features.arrays["eeg.multitaper_spectrogram"].read(
        rows=slice(0, 2)
    )
    # Optional: load everything into memory before closing the stores.
    # memory_record = record.materialize()
```

Opening loads metadata and the summary JSON; signal and temporal payload arrays
remain lazy. Signals use their native clocks and already calibrated physical
values. Feature tables retain original rows, statuses, precision, presence and
information fields. Default opening requires the repaired temporal payload
schema; use `require_complete=False` for explicit primary-only or legacy
inspection, and inspect `record.availability` for omissions. Completeness refers
to the supported export contract; reports and other unexported producer assets
are outside that contract. Artifact identities are checked through metadata,
which is not a cryptographic source/run binding.

## HDF5

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

The readers intentionally do not include
EDF conversion, S3 downloading, GUI code, model inference, report generation, or
feature computation. Remote Zarr access accepts an explicitly configured,
caller-owned Zarr Store; this package does not discover buckets or obtain AWS
credentials. Remote summaries can be supplied as decoded JSON dictionaries.

## API

- `bdsp.read(path, **kwargs)` dispatches to the H5 reader for `.h5` and `.hdf5`.
- `bdsp.read_h5(path, raw_digital=False, include_annotations=True, include_features=True)` returns a `BDSPRecord`.
- `BDSPRecord.to_dataframe(resample_fs=200)` returns synchronized signal and annotation columns.
- `bdsp.open_h5(path)` returns a lazy `BDSPH5Reader` for slice-based reads.
- `bdsp.compat.sleep_yoda` exposes `load_standardized_h5` and `PSGReader` compatibility aliases.
- `bdsp.bids_zarr.open_recording(...)` opens the split recording lazily; `.materialize()` returns detached in-memory data.

## Development

```bash
python -m pip install -e ".[test,zarr]"
pytest
```

H5 reading requires Python 3.10+ with `h5py`, `numpy`, `pandas`, and `scipy`.
Zarr reading requires Python 3.11+ and the `zarr` extra. H5 and namespace imports
work without Zarr installed. CI exercises H5 on Python 3.10–3.13 and the Zarr
extra on Python 3.11–3.13.

## License

This package is licensed under the Apache License, Version 2.0.
