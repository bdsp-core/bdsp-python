# Writing a local H5 from a split Zarr recording

New BDSP conversions publish only the split representation: primary Zarr,
derivative Zarr and summary JSON. For local work it is still convenient to have
one H5 per recording in the layout the converter read. `bdsp.zarr_to_h5`
assembles the three artifacts into that file. It reads only local paths, does
not run models or compute features, and never changes its inputs.

```python
from bdsp.zarr_to_h5 import write_h5

report = write_h5(
    "/data/recording.h5",
    primary="/data/primary.zarr",
    derivatives="/data/derivative.zarr",
    summary_json="/data/summary.json",
)
print(report.signals_int16, report.signals_float32, report.notes)
```

The same from a shell, after `pip install bdsp`:

```bash
bdsp-zarr-to-h5 --primary primary.zarr --derivatives derivative.zarr \
    --summary-json summary.json --output recording.h5
```

`derivatives` and `summary_json` are optional. Without the derivative the H5 has
no model annotations or feature time series; without the summary JSON it has no
`recording_summary` and no `meas_date`. The output appears only once it is
complete, and an existing file is replaced only with `overwrite=True`
(`--overwrite`). The three artifacts must describe the same subject, session and
duration, and the two stores the same source file; otherwise the call fails.
Read the result with `bdsp.read_h5()` or `bdsp.open_h5()` as before.

## What goes where

| H5 | From | Encoding |
| --- | --- | --- |
| root `subject_id`, `session_id`, `duration_sec`, `age`, `sex` | primary `recording.json` | `age` is the published value (capped at 90); `sex` is `Male`/`Female`/`Unknown` |
| root `meas_date` (and `h5_meas_date`) | summary JSON `provenance` | the corrected date where the JSON has one; the archived H5 date is kept as `h5_meas_date` |
| `/signals/<name>` | primary continuous channels with origin `signal` | int16 digital samples with `fs`, `unit`, `phys_min`, `phys_max`, `dig_min`, `dig_max` (below) |
| `/annotations/<scorer>/<kind>/` | event channels with origin `annotation`, primary and derivative | `starts`, `ends`, `durations` (float32 seconds), `codes` (int16 original codes), `event_map`, `format="event_table"` |
| `<root>/time_series/<family>/` | `bdsp.sleep-feature-payload.v1` payload groups | copied as stored: attributes, dtypes, shapes, masks, statuses, `information` |
| `<root>/recording_summary/` | summary JSON `features` and `vectors` | every row and column in its original order; exported vectors under `arrays/` |
| root `bdsp_h5_export` | — | JSON naming the writer, inputs, conversion skips and what the split export does not carry |

`<root>` is the location the payloads name, normally
`/features/bdsp_sleep_features` (2026-10 conversions) or
`/features/sleep_features`. Features that came from a producer's separate
feature H5 (`/sleep_features/...`, I0007) are written under
`/features/sleep_features`, so every rebuilt H5 has its features under
`/features`. Channels appended to the primary store after conversion, such as
the PAP channels copied from `*_desc-pap.h5`, are included with a
`source_file` attribute naming that file; pass
`include_appended_signals=False` (`--no-appended-signals`) to leave them out.

### Signal samples and calibration

A primary store holds each signal as float32 physical values, computed by the
converter as `float32(digital * scale + offset)` in float64, with
`scale` and `offset` from the source calibration and voltages rescaled to
microvolts. The calibration itself is not stored. The writer recovers an int16
encoding instead of storing float32:

1. find the quantization lattice of the channel's distinct values;
2. try calibrations in producer conventions — the full int16 digital range
   first, then zero-offset, symmetric and zero-based physical ranges with the
   fewest significant digits (for a sparsely used range, also spans solved from
   its extreme values);
3. accept a calibration only if the converter's formula reproduces **every
   sample bit-exactly**, then write the int16 samples.

A channel declared in mV or V keeps that unit and its own calibration; the
microvolt rescaling is restored through `unit_converted_from`. A channel for
which no exact int16 calibration exists (for example one with missing samples,
`NaN`) is written as float32 with an identity calibration
(`phys_min = dig_min = 0`, `phys_max = dig_max = 1`), and the report notes it.
Either way, every consumer applying the usual digital-to-physical formula gets
the values the Zarr store holds. Different exact calibrations can encode the
same values, so a recovered `phys_min`/`dig_min` need not equal the archived
one; the parity check reports how often they do.

### Annotation times

Stores keep onsets and durations as integer microseconds rounded from the
source float32 seconds. The writer converts them back to float32 seconds, so
values round-trip to the source float32 except where that float32 resolves
about a microsecond or less (times below about 16 s), and then within 1 µs. `ends`
is written as `starts + durations`; a source table may have stored a slightly
different float32 `ends` (the converter accepted up to 10 ms). Events are in
start order, and `event_map` lists the codes in ascending order.

## What cannot be rebuilt

The split export never stored the following, so no writer can recover them
from Zarr and JSON. They are listed in `bdsp_h5_export.not_in_split_export`
rather than invented:

- signal `prefilter` and `type` attributes;
- annotation attributes other than `event_map`, such as `rule` and
  `rule_description` on `resp_3`/`resp_4`;
- root attributes other than identity, duration, age, sex and dates (for
  example `age_in_days`, `metadata_source`, `hypopnea_rule_source`), and an
  archived age above 90;
- the feature producer's `package_name`/`package_version` attributes and its
  `recording/identity_json` and `provenance/json` datasets;
- groups outside signals, annotations and exported features, such as
  `/features/research/spindles`;
- conversion-time skips: channels, tables or families the converter left out
  are listed in each store's `source.skipped`, repeated in `bdsp_h5_export`;
- summary arrays that are not exported vectors (withheld or multidimensional
  arrays); their rows keep `array_path`, and the report notes each one;
- temporal families exported without a payload by pre-2026-10-20 converters
  (none remain in the published cohorts); the report notes each one.

Original H5 and EDF files remain in `sourcedata/` and are the reference for
these items.

## Checking a rebuilt H5 against its source

`bdsp.h5_compare.compare_h5(source_h5, rebuilt_h5)` compares everything the
split export carries: identity and dates, each signal's rate, length and
physical samples (with the converter's formula, so a different exact
calibration still matches), annotation events and codes, every feature family
recursively, and the summary columns and arrays. It returns a JSON-safe report
with `verdict`, `failures` and `not_exported`; it names channels, tables,
features and attributes but never recorded values. Pass `features=` with the
producer's feature H5 for sessions whose features were never merged into the
session H5. The repeatable dataset check is
[`validation/h5_parity.py`](../../validation/README.md#h5-parity-zarr-to-h5),
and the [2026-11-27 review](../../validation/reports/2026-11-27-h5-parity.md)
records the results for all six cohorts.
