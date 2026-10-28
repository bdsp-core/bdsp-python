# BDSP Data Guide and Python Reader

Understand and load standardized sleep/PSG Zarr recordings from
[BDSP](https://bdsp.io/) with the `bdsp` Python package.
This repository contains the reader, data-format documentation, and examples.
Start with your dataset's page on bdsp.io to request access and download its files.

## Install

Use Python 3.11+ in a project environment:

```bash
pip install bdsp
```

Zarr support is installed automatically.

## Open a Zarr recording

One recording has three matching artifacts: **primary Zarr** (signals and expert
annotations), **derivative Zarr** (model annotations and time-varying features),
and **summary JSON** (feature summaries).
Pass those three paths to the reader:

```python
from bdsp.bids_zarr import open_recording

with open_recording(
    primary="/data/primary.zarr",
    derivatives="/data/derivative.zarr",
    summary_json="/data/summary.json",
) as record:
    print(list(record.signals))
    signal = next(iter(record.signals.values()))
    window = signal.read(start=0, duration=30)
    summaries = record.features.summary.to_dataframe()
```

Replace the example paths with your files; `.zarr` paths refer to directories.
`window` contains sample values, times, sampling rate, and physical units.
Signals keep their native sampling rates, and only the requested window is loaded.
Keep lazy reads inside the `with` block.
Available channels, scorers, and features depend on the recording.

## Further help

The [getting started guide](https://github.com/bdsp-core/bdsp-python/blob/main/docs/README.md) covers signals, annotations, features,
installation, and development.
The [data-format guide](https://github.com/bdsp-core/bdsp-python/blob/main/docs/bids-zarr/README.md) explains the directory layout,
units, and primary/derivative split; the
[reader reference](https://github.com/bdsp-core/bdsp-python/blob/main/docs/bids-zarr/READER_DESIGN.md) covers the full Zarr API.

Licensed under [Apache 2.0](https://github.com/bdsp-core/bdsp-python/blob/main/LICENSE).
