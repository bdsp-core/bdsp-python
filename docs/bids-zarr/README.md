# BDSP BIDS and Penn TS-Zarr storage

This is the migration entry point for applications that previously opened one
enriched Sleep YODA H5. It records the owner-directed storage model and the
converter behavior inspected on 2026-10-20. Keep the reusable reader in
`bdsp-python`, with its implementation under `src/bdsp/bids_zarr/`; application
adapters belong in their respective repositories. See [reader design](READER_DESIGN.md).

The [Sleep YODA web migration notes](WEB_MIGRATION.md) record the adapter,
association with older bundles, local conversion evidence and blocked-source
policy, so applications migrating later can reuse these findings.
The [enrichment audit](ENRICHMENT_AUDIT.md) distinguishes complete storage exports
from completed scientific modules and viewer assets; it records the remaining
showcase gaps and the reconciliation needed for a full Sleep YODA profile.

**Implementation status:** `bdsp.bids_zarr.open_recording` reads explicit primary,
derivative and summary artifacts. It supports the repaired temporal payload
extension, lazy native-clock windows and explicit eager materialization. See the
[API and validation details](READER_DESIGN.md). Older derivative exports require
reconversion to recover omitted feature bodies; permissive inspection reports
these gaps instead of declaring the recording complete.

## Recording organization

A recording is a logical collection of related artifacts. Its physiological
signals and expert annotations are primary data. CAISR/model annotations and
the outputs of `bdsp-sleep-features` are derivatives. The reader combines these
into one Python object without physically merging them into another H5 or Zarr.

The following paths are templates, not locations of actual recordings. `<S>`
means `sub-<SUBJECT>_ses-<SESSION>`; task and suffix spelling is case-sensitive.

```text
<COHORT>/
├── dataset_description.json, README, .bidsignore
├── participants.tsv, participants.json
├── primary/sub-<SUBJECT>/
│   ├── sub-<SUBJECT>_sessions.tsv, .json
│   └── ses-<SESSION>/
│       ├── <S>_scans.tsv, .json
│       └── psg/
│           ├── <S>_task-psg_eeg.zarr/
│           ├── <S>_task-psg_eeg.json
│           ├── <S>_task-psg_channels.tsv
│           └── source expert-annotation CSVs, with cohort-specific names
├── derivatives/bdsp_sleep_features/
│   ├── dataset_description.json
│   ├── <COHORT>.parquet
│   └── sub-<SUBJECT>/
│       ├── <S>_task-psg_deriv-bdspsleepfeatures.zarr/
│       └── <S>_task-psg_deriv-bdspsleepfeatures.json
└── sourcedata/sub-<SUBJECT>/ses-<SESSION>/
    └── archived H5, EDF, model CSVs and other input sidecars
```

The primary suffix `_eeg.zarr` includes all exported PSG signals, not only EEG.
Derivative files currently have no intermediate session or datatype directory;
the session is in the filename. Annotation CSV routing varies by cohort; use
the converter's file rules rather than extrapolating from one site. The layout
uses BIDS conventions and metadata, with project-specific names such as `psg/`
and `deriv-` declared in `.bidsignore`. Do not claim that this is an unmodified
standard BIDS layout or that its compliance was independently validated here.

The cohort Parquet is a scalar roll-up for cohort analysis, not the per-recording
reader's substitute for the JSON or Zarr. Ordinary reading does not require the
Morpheus master or the archived EDF/H5. An explicit artifact set is sufficient.

## Current Zarr representation

The converter writes Zarr v3, tagged Pennsieve TS-Zarr draft version `1.0`.
The Zarr root attributes are `{"tsz": {"version": "1.0"}}`. Consolidated
metadata is stored in root `zarr.json`; `recording.json` is a separate JSON
object inside the store, not a Zarr group.

`recording.json` contains `subject`, `session` and `source` objects. Their fields
include subject/session IDs, demographics, recording duration, source filename,
converter identity, and `source.skipped` with reasons for excluded items. It
omits a recording start date. Private identifiers and source metadata must not
be forwarded wholesale to a browser.

Each numbered child group is a channel. Numbering restarts in each store and
is not a stable cross-store identifier. Read `tsz.name`, `tsz.id`, `tsz.kind`
and `tsz.provenance` to interpret it.

| Store | Current channel origins |
| --- | --- |
| Primary | Physiological signals, then annotations from annotators whose names start with `expert` |
| Derivative | Other annotators, including CAISR, then supported feature temporal outputs |

`tsz.provenance.origin` is `signal`, `annotation` or `feature`. This is the
converter's current classification rule, not proof that every possible human
annotator name will be routed correctly.

### Physiological signals

Each signal has a one-dimensional `raw` array of calibrated physical `float32`
samples. Channel metadata includes `rate_hz`, `offset_us`, `unit`, name and source
dataset identity. The converter normalizes declared voltage units to `uV` and
records `unit_converted_from` when applicable; absent units become `(undeclared)`.
The reader must not apply the old digital-to-physical calibration a second time,
invent an undeclared unit, or assume one sample rate for the recording.

Time for sample `i` is `offset_us / 1e6 + i / rate_hz`, in seconds from recording
start. Float arrays use NaN fill. Current writer settings are Zstd level 5,
inner chunks of at most 8,192 elements and shards targeting approximately
16 MiB. It writes no zoom hierarchy. These are observed writer settings, not
new tuning decisions for this reader.

### Expert and model annotations

Annotation channels use `events` and `durations`, both integer microseconds from
recording start, plus `labels`. A label is an index into `tsz.label_names`, not
an original stage/event code. `tsz.provenance.label_codes` supplies the original
code at that same index. Preserve scorer names and event families, including
distinct respiratory definitions, rather than merging or guessing stage codes.

### Feature temporal outputs

The current converter maps regular numeric series onto continuous channels,
with their own rate and offset. Gaps or rejected values can appear as NaN.
Segments, events and sparse series become event channels with labels and/or
numeric values. Multivariate families may be split into faceted channel names;
provenance retains the original family and temporal semantics.

The TS projection alone loses some source row detail: NaN does not recover the
original per-row status, and event export filters out rows that did not pass the
status gate. The local converter repair described below retains the original
temporal groups beside those projections. A complete reader should use those
payloads when original rows, precision, statuses or coordinates are needed.
Legacy stores without the extension cannot recover that information.

## Derivative summary JSON

The current schema is `psg_features_sidecar_v0.5`. Top-level fields are
`schema_version`, `recording_id`, `subject`, `demographics`, `recording`,
`provenance`, `counts`, `features` and `vectors`.

`features` maps each full feature ID to its recording-summary row, including
`short_name`, `display_name`, `display_scope`, `unit`, `source`, `result_kind`,
`value_shape`, `storage_type`, `status`, `message`, typed value columns and
`array_path`. Select the value column using `storage_type`; placeholder zeros
in other columns are not feature values. Retain unavailable rows and distinguish
`valid_zero`, `missing_input`, `not_applicable`, `qc_rejected`, `failed`,
`not_run` and `withheld` when present. Unknown statuses require explicit handling.

The converter exposes `ok` and `valid_zero` scalars as loadable values. It also
exports passed one-dimensional summary arrays in `vectors`, including an
embedding when produced. Thus the JSON is more than a scalar-only convenience
file. Its current `provenance` builder includes `meas_date`; do not assume it
retains the producer's complete model/configuration/source provenance.

The desired division is recording-level summaries in JSON and complete
non-scalar/temporal feature content in derivative Zarr. Preserve existing vector
exports while defining that complete representation; do not move or discard them
silently. A JSON `array_path` inherited from H5 is not automatically a usable
path into the new Zarr.

## Conversion gap and local repair: array and structured payloads

`bdsp-sleep-features` computes array and structured temporal results. Its H5
writer stores numeric arrays as `value_array` with `value_array_present`, or
structured JSON through `value_json_code` / `value_json_labels`. It also stores
timestamps, row indices, statuses, units, sources and feature-specific
`information` fields. These are part of the producer output to preserve.

At the initial converter baseline (`b9abaa5`), `common/h5_time_series.py` reads only
`value_numeric` and `value_text`. A feature with neither raises:

```text
no numeric or text values (array or JSON payload)
```

The converter catches that exception and lists the feature and reason under
**`recording.json` → `source.skipped.features`** in the derivative store.
Relevant producer families include `breathing.loop_gain_input`,
`breathing.ventilation`, `coactivation.track`, `cpc.window` and
`eeg.multitaper_spectrogram`. Their computation in H5 and their omission during
conversion are separate facts. Re-running feature computation is not the fix
for an unsupported export representation.

Zarr supports multidimensional arrays. The existing one-dimensional TS channel
projection does not establish a representation for every producer payload.
The converter now has a local repair implementing
`bdsp.sleep-feature-payload.v1`. Each exported temporal family is copied once to
`<owner-channel>/feature_payload/source` as real Zarr groups and arrays. Numeric
dtypes, shapes, all original rows, clocks, masks, JSON encodings and information
fields are retained. Faceted channels share one original family payload; scalar
families also retain their original groups beside the TS convenience projection.

Discovery is through
`tsz.provenance.feature_payload = {"schema": "bdsp.sleep-feature-payload.v1",
"path": "<owner-channel>/feature_payload"}`. The derivative's `recording.json`
also declares `source.feature_payload_schema`. This is a BDSP extension to
TS-Zarr, not an additional Penn core channel kind. Its detailed contract and
producer-to-Zarr comparison are in `bdsp-psg-conversion/docs/file-structures.md`
and `zarr_json/feature_payload.py`. Formal external TS validator acceptance has
not been established. Whole-file/producer provenance, arbitrary H5 groups and
summary matrices are outside this temporal extension's preservation scope.

These changes do not update existing published stores. A missing schema marker
indicates a legacy export; required bodies can still be absent even when the
store opens successfully. Reconversion and publication are separate actions.
Do not certify a derivative as complete merely because it opens or because all
omissions have entries in `source.skipped`.

Reports and other fixed presentation assets also need explicit preservation and
discovery rules: the current split converter is not evidence that older web
report assets are present in the new recording collection.

## Short history for older consumers

| Previous assumption | Current direction |
| --- | --- |
| One enriched H5 contains signals, experts, models and computed features | Primary Zarr plus derivative Zarr/JSON form a logical recording |
| Standardized H5 appears under `sub-…/ses-…/eeg/` | Archived inputs are under `sourcedata/`; usable data are under `primary/` and `derivatives/` |
| Raw H5 signals are digital counts with calibration attributes | Converted primary `raw` arrays are already physical float32 samples |
| Feature-H5 has `/sleep_features` | Merged H5 may use `/features/bdsp_sleep_features` or newer `/features/sleep_features` before conversion |
| Hosted Sleep YODA Zarr is a drop-in example of the new format | Existing web bundles use their own manifest, rate-grouped arrays, identity binding and embedded assets; they need a new adapter |
| Earlier Penn benchmark adapters establish the current reader contract | Those experiments include different profiles and shared companion assets; use the current split converter contract instead |
| Desktop H5 helpers are implemented inside Sleep YODA | They now delegate to `bdsp-python`; use this package for the shared reader |

Older Sleep YODA web documents still describe enriched H5 as canonical and Zarr
as a serving derivative. That describes the prior workflow. The owner-directed
new reader consumes the split primary/derivative artifacts; it must not rebuild
an enriched H5 as a prerequisite. Historical artifacts remain preserved.

## Sources and verification boundaries

Repository paths below identify the implementations to consult; they are not
runtime dependencies on neighboring checkouts.

- `bdsp-psg-conversion`: `README.md`, `docs/file-structures.md`,
  `common/layout.py`, `common/h5_time_series.py`, and the writers in
  `zarr_json/` under `src/bdsp_psg_conversion/`. Inspected at commit `b9abaa5`
  with an initially clean working tree. The subsequent payload-v1 repair is
  committed as `16c2d95f10dbeed8a1549da48dbd2ef51384fe57` in
  [converter PR #13](https://github.com/bdsp-core/bdsp-psg-conversion/pull/13).
  The local validation used that commit; this is not a published release pin.
- `bdsp-sleep-features`: `docs/OUTPUT_CONTRACT.md`,
  `src/bdsp_sleep_features/h5_output.py` and the feature producers. Inspected
  working tree based on `7c00668`; relevant uncommitted changes exist, so this
  is not a production release pin.
- `sleep-yoda-web`: `server/zarr_reader.py`, `server/zarr_source.py`,
  `server/zarr_engine.py` and `data-type-exploration/penn-varr-comparison-task/`.
  Inspected working tree based on `6fadbc5`; unrelated local changes exist.
- `sleep-yoda`: compatibility wrappers in `psg_viewer/core/h5_loader.py` and
  `psg_viewer/core/psg_h5_reader.py`, inspected at `eedb115`.

Initial scoped read-only inspection checked the owner's supplied artifact
metadata and the archived H5 payload structure. A subsequent owner-authorized
inspection downloaded both complete Zarr stores and the derivative JSON outside
Git, independently rehashed every downloaded file, inspected individual physical
Zarr nodes without relying on consolidated metadata, fully decoded the derivative
arrays, and sampled the primary arrays. This confirmed the reported payload
omissions in that artifact set. It does not establish cohort-wide conversion
completeness, numerical H5-to-Zarr sample parity, or clinical validation. Private
download inventories, version/checksum receipts, inspection reports and all
recording-level evidence remain outside Git. This guide contains only schema
templates and implementation facts.

The local payload-v1 repair was subsequently exercised by synthetic round-trip
and corruption checks and a separate local rebuild from the version-pinned
archived H5. The supported array/structured families were retained with no
payload comparison differences, and the source H5 checksum remained unchanged.
This validates that repair for the supplied artifact set; it does not upgrade
published stores or establish preservation of unrelated producer assets.

The shared Python reader was then validated against the local repaired artifact
set: 69 native signal windows, all 10 expert/CAISR annotation tracks, all 397,397
temporal rows across 36 original families and all 5,283 summary rows plus the
exported vector matched the archived H5/reference decoder. The executable
validation script, input references, source hashes and detailed receipt remain
in the owner's private data directory. This is single-recording reader
acceptance, not cohort-wide or clinical validation. No AWS artifacts were changed.
