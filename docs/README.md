# Getting started with BDSP recordings

Use this guide after the [two-minute overview](../README.md).
The package reads standardized sleep/PSG H5 and split Penn TS-Zarr recordings;
it is not a reader for every dataset or file type on bdsp.io.
Dataset access, release descriptions, and citation instructions remain on
[bdsp.io](https://bdsp.io/).

## 1. Install the reader

For H5 and Zarr, use Python 3.11+ and Git in a project environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install "bdsp[zarr] @ git+https://github.com/bdsp-core/bdsp-python.git@main"
```

The current PyPI release, `bdsp==0.1.0`, predates Zarr support and does not provide
the `zarr` extra. Until a newer release is published, use the GitHub command above
for Zarr. Both the current source and that PyPI release report version `0.1.0`,
so the version number alone does not distinguish them. For reproducible work,
replace `@main` with the full commit SHA you have validated and record the
dataset release and artifact identities separately.

For H5-only use on Python 3.10+, the published package is sufficient:

```bash
python -m pip install bdsp
```

## 2. Identify the files for one recording

Follow your dataset's access and download instructions before opening files.
Installation does not grant data access or download recordings.

| Format | What to pass to the reader |
| --- | --- |
| Split Zarr | The associated primary `.zarr` directory, derivative `.zarr` directory, and derivative summary `.json` |
| H5 | One standardized `.h5` or `.hdf5` file |

Keep all files inside each Zarr directory. Do not mix artifacts from different
recordings or processing revisions. The primary store holds physiological
signals and named expert annotations; the derivative store holds model
annotations and temporal features; the JSON holds feature summaries and exported
vectors. See the [data-format guide](bids-zarr/README.md) for actual filename
patterns, directory trees, and encoding details.

## 3. Read Zarr signals, annotations, and features

Replace the example paths with your downloaded artifacts:

```python
from bdsp.bids_zarr import open_recording

with open_recording(
    primary="/data/primary.zarr",
    derivatives="/data/derivative.zarr",
    summary_json="/data/summary.json",
) as record:
    print("Signals:", list(record.signals))
    print("Scorers and tracks:", {
        scorer: list(tracks)
        for scorer, tracks in record.annotations.items()
    })

    signal = next(iter(record.signals.values()))
    window = signal.read(start=0, duration=30)
    values, times = window.values, window.times_s
    print("Sampling rate and unit:", window.rate_hz, window.unit)

    summaries = record.features.summary.to_dataframe()
    print("Temporal feature families:", list(record.features.time_series))
```

Opening reads metadata and summary JSON; physiological samples and temporal
feature arrays stay lazy. Each signal retains its own sampling rate, offset,
physical unit, and missing samples. Values are already calibrated; do not apply
H5 digital-to-physical calibration to them again.

To read annotations or a temporal family, choose names from the inventories
above and run these examples **inside the same `with` block**:

```python
# Example names: use those actually present in your recording.
stages = record.annotations["expert_1"]["stage"].read(start=0, duration=30)
family = record.features.time_series["eeg.multitaper_spectrogram"]
first_rows = family.read(rows=slice(0, 2))
```

Annotation reads return a DataFrame with timing, labels, and original codes
when supplied. Feature reads preserve row statuses and presence information;
an unavailable result must not be interpreted as a measured zero. Feature
families and expert/model scorers vary between recordings.

Finish lazy reads before leaving the `with` block. Loaded NumPy arrays and
DataFrames remain usable afterward. To explicitly load the supported recording
content into memory, call `record.materialize()` inside the block; this can use
substantial RAM.

By default, opening requires the derivative artifacts and the supported complete
export schema. For intentional primary-only or older-export inspection, pass
`require_complete=False` and check `record.availability` inside the block. This
does not repair an incomplete export or compute missing results. See the
[reader reference](bids-zarr/READER_DESIGN.md) for validation, array and structured
payload access, and remote Stores. Remote access requires a caller-configured,
authorized Zarr Store; raw `s3://` strings are not resolved automatically.

## 4. Read an H5 recording

For a complete in-memory load:

```python
import bdsp

record = bdsp.read_h5("/data/recording.h5")
print(list(record.signals))
print(record.channel_info)
annotations = record.annotations
features = record.features
```

Signals are NumPy arrays; annotations and features are nested dictionaries
reflecting the file's contents. Full loading can require substantial memory.
To read a bounded signal window instead:

```python
import bdsp

with bdsp.open_h5("/data/recording.h5") as reader:
    values, times = reader.read_signal("c3-m2", start=0, duration=30)
```

Choose a channel present in your file. If your analysis needs a synchronized
pandas DataFrame, use `record.to_dataframe(resample_fs=200)` with an explicit
target rate; this resamples rather than preserving every native signal clock.
The H5 `open_h5` reader also uses a 200 Hz compatibility grid; use `read_h5`
for native signal arrays or the Zarr reader for lazy native-clock windows.
`bdsp.read()` currently dispatches H5 files only; use
`bdsp.bids_zarr.open_recording()` explicitly for Zarr.
`bdsp.compat.sleep_yoda` retains the `load_standardized_h5` and `PSGReader`
compatibility aliases.

## 5. Scope and further reading

The package reads existing recordings and results. It does not perform EDF
ingestion, obtain cloud credentials, run models, compute features, or generate
reports. Conversion and feature computation have their own producer workflows:

- [BDSP data format and repository ownership](bids-zarr/README.md)
- [Zarr API and validation reference](bids-zarr/READER_DESIGN.md)
- [H5-to-Zarr converter](https://github.com/bdsp-core/bdsp-psg-conversion)
- [Feature producer](https://github.com/bdsp-core/bdsp-sleep-features)
- [Historical format migration](bids-zarr/MIGRATION_HISTORY.md)

## Development

From a checkout of this repository, using Python 3.11+:

```bash
python -m pip install -e ".[test,zarr]"
pytest
```

H5 reading supports Python 3.10+ without Zarr installed. CI tests H5 on Python
3.10–3.13 and the optional Zarr reader on Python 3.11–3.13.
