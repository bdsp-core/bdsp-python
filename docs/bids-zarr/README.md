# BDSP PSG storage: BIDS organization and split Penn TS-Zarr

This is the entry point for humans and agents reading or migrating BDSP/Sleep
YODA recordings. It explains **where artifacts live**, **what they contain**,
**how values are encoded**, and **which repository produces or reads them**.
Implementation references were checked on 2026-10-26; a recording's own schema,
producer version and provenance determine how that particular recording is read.

## 1. One recording, three related data artifacts

The current storage model separates recorded/expert data from algorithm outputs:

| Artifact | Contents | Typical filename ending |
| --- | --- | --- |
| **Primary Zarr** | Physiological PSG signals and named expert annotations | `_task-psg_eeg.zarr/` |
| **Derivative Zarr** | CAISR/other model annotations and BDSP temporal features, including arrays and structured payloads | `_task-psg_deriv-bdspsleepfeatures.zarr/` |
| **Derivative summary JSON** | Recording-level feature summaries, statuses, units and exported vectors | `_task-psg_deriv-bdspsleepfeatures.json` |

The Python reader combines these into **one logical recording**. Reading does
not merge them into a new H5 or a single larger Zarr store. A `.zarr/` is a
directory locally and a collection of objects under a prefix on S3, rather than
one monolithic file.

**BIDS organization and Zarr encoding answer different questions.** The BIDS-based
layout names and associates subjects, sessions, recordings and derivatives.
Zarr describes how the arrays and metadata inside each store are encoded.
Original EDF/H5 inputs remain immutable archives under `sourcedata/`.

## 2. Repository responsibilities and data flow

| Repository | Responsibility |
| --- | --- |
| [`bdsp-sleep-features`](https://github.com/bdsp-core/bdsp-sleep-features) | Computes/reuses CAISR and the registered scientific feature panel; writes feature H5, scalar TSV, integrity JSON and, for full runs, a PDF report |
| [`bdsp-psg-conversion`](https://github.com/bdsp-core/bdsp-psg-conversion) | Converts the selected session H5 content into primary/derivative Penn TS-Zarr, summary JSON and BIDS metadata; also provides cohort scalar roll-ups. It does not compute missing features or rerun CAISR |
| [`bdsp-python`](https://github.com/bdsp-core/bdsp-python) — this repository | Shared readers: associates the three artifacts, decodes values and exposes lazy reads or explicit materialization. It does not run models, generate reports, discover cloud inputs or repair scoring |
| [`sleep-yoda-web`](https://github.com/bdsp-core/sleep-yoda-web) | Offline SY orchestration/assembly, additional retained analyses, reports and viewer assets; serves completed data through the shared reader |
| `arpa-h-psg-eeg-ingestion` | Upstream vendor/EDF ingestion and standardization, as identified by the converter's ingestion boundary; separate from H5-to-Zarr export |
| `sleep-yoda` desktop | Legacy implementations and compatibility consumers; some retained producers still use pinned desktop code |
| `morpheus-internal` | Research/master metadata used for cohort selection and joins; not required to open an explicitly identified recording |

```text
Recorded PSG + expert exports
    → upstream ingestion / standardized source H5
    → bdsp-sleep-features: CAISR + scientific feature bundle
    → offline association/assembly of the selected source and feature revision
    → bdsp-psg-conversion
        ├── primary Zarr
        ├── derivative Zarr
        └── derivative summary JSON
    → bdsp-python: one logical recording for analysis or an application adapter
```

The validated feature-computation input path currently uses standardized H5.
The producer's standalone feature H5 has a `/sleep_features` namespace; assembled
session H5s can embed it under `/features/sleep_features` or
`/features/bdsp_sleep_features`. The offline workflow must select and bind the
appropriate feature revision before export. The reader does not guess which
feature H5 belongs to a source recording.

The producer's `*_desc-sleepfeatures.json` is an **integrity/QC manifest**.
It is different from the converter's `*_deriv-bdspsleepfeatures.json`, which is
the **feature summary consumed by the split reader**.

### Which features come from which producer?

`bdsp-sleep-features` owns the registered feature families. Representative outputs
include sleep architecture and event indices; oxygen/desaturation measures;
breathing stability, self-similarity, ventilation and loop gain; ECG/HRV and CPC;
limb/EMG and coactivation; EEG spectra, bandpower and complexity; spindle,
slow-wave and coupling results; and model outputs such as sleep depth, brain age
and Brain Health embeddings. CAISR can be reused or computed according to the
selected producer's input/staging policy. SPIN code/model assets and other model
dependencies retain their own versions; the converter does not choose a new one.

Use the producer's [feature registry](https://github.com/bdsp-core/bdsp-sleep-features/blob/main/config/feature_registry.yaml),
[generated catalog](https://github.com/bdsp-core/bdsp-sleep-features/blob/main/docs/FEATURE_CATALOG.md)
and [output contract](https://github.com/bdsp-core/bdsp-sleep-features/blob/main/docs/OUTPUT_CONTRACT.md)
for exact IDs, definitions and required inputs. Feature counts and availability
depend on the recorded execution scope and producer version. A supplemental
SPIN-only run is not a full feature-panel run.

Sleep YODA also prepares content outside that native BDSP export: for example,
its retained RSWA reports, ECGFounder AFib screening, titration reports,
experimental EEG-quality output and viewer images. Those producers and assets
have separate contracts in the
[web architecture guide](https://github.com/bdsp-core/sleep-yoda-web/blob/main/docs/ENRICHMENT_ARCHITECTURE.md).
Do not assume an arbitrary legacy H5 cache or web report is automatically stored
in the BDSP derivative Zarr.

## 3. BIDS-based directory layout

These are **templates**, not paths to actual participants. `<S>` below abbreviates
`sub-<SUBJECT>_ses-<SESSION>`; `<COHORT>` identifies the site's/cohort's dataset.

```text
PSG/bids/<COHORT>/
├── dataset_description.json
├── README, .bidsignore
├── participants.tsv, participants.json
├── primary/
│   └── sub-<SUBJECT>/
│       ├── sub-<SUBJECT>_sessions.tsv, sub-<SUBJECT>_sessions.json
│       └── ses-<SESSION>/
│           ├── <S>_scans.tsv, <S>_scans.json
│           └── psg/
│               ├── <S>_task-psg_eeg.zarr/
│               ├── <S>_task-psg_eeg.json
│               ├── <S>_task-psg_channels.tsv
│               └── source expert-annotation CSVs, with cohort-specific names
├── derivatives/
│   └── bdsp_sleep_features/
│       ├── dataset_description.json
│       ├── <COHORT>.parquet
│       └── sub-<SUBJECT>/
│           ├── <S>_task-psg_deriv-bdspsleepfeatures.zarr/
│           └── <S>_task-psg_deriv-bdspsleepfeatures.json
└── sourcedata/
    └── sub-<SUBJECT>/ses-<SESSION>/
        └── original H5, EDF, model CSVs and other input sidecars
```

Important naming rules:

- `_eeg.zarr` contains **all exported PSG signals**, not only EEG. `psg` is the
  task/datatype spelling used here; filename case matters.
- Derivative files currently sit directly under the subject directory. There
  is no additional `ses-…/psg/` level there; session identity is in the filename.
- Expert CSV routing/naming varies by cohort. Consult the converter's
  `common/file_rules.py` instead of inferring another cohort's rules from one example.
- `primary/`, `psg/` and `deriv-` are project conventions with nonstandard names
  declared in `.bidsignore`. This is a BIDS-based project layout, not a claim of
  unmodified BIDS compliance or independent standards validation.

### Metadata files are not interchangeable

| File | Purpose |
| --- | --- |
| `dataset_description.json` | Dataset/derivative description, including its declared BIDS version and producer metadata |
| `participants.tsv` + `.json` | Participant table and column descriptions |
| `*_sessions.tsv` / `*_scans.tsv` + `.json` | Session and scan inventory with metadata dictionaries |
| `*_channels.tsv` | Channel inventory; signal data remain in primary Zarr |
| Adjacent `*_task-psg_eeg.json` | Primary acquisition/BIDS metadata; not the feature summary |
| `recording.json` **inside each Zarr store** | Store-level recording identity, duration and source/conversion metadata |
| `zarr.json` | Zarr group/array metadata, attributes, shape, dtype, codecs and chunking |
| `*_deriv-bdspsleepfeatures.json` | Per-recording feature summaries and exported vectors |
| `<COHORT>.parquet` | Cohort scalar roll-up for analysis; not a substitute for the recording's JSON or temporal arrays |

## 4. Inside the Zarr stores

The current converter writes **Zarr v3**, using the Pennsieve/Penn TS-Zarr draft
convention tagged **`1.0`**. These are different version numbers. Root
`zarr.json` contains Zarr metadata, with root attributes
`{"tsz": {"version": "1.0"}}`; it also contains consolidated metadata.
`recording.json` is an ordinary JSON object inside the store, not a Zarr group.
It is organized into `subject`, `session` and `source` objects: identity and
demographics, session duration, source filename, converter identity and declared
`source.skipped` items. It currently omits a recording start date; other sidecars
or provenance can still contain dates. Array/annotation clocks are relative to
recording zero rather than reconstructed from a wall-clock timestamp.

Each numbered child group represents a channel. Read its `tsz.name`, `tsz.id`,
`tsz.kind` and `tsz.provenance` attributes. **Numbers are local to a store and
are not stable signal IDs or a way to join primary and derivative channels.**

```text
<primary-or-derivative>.zarr/
├── zarr.json
├── recording.json
├── 0/
│   ├── zarr.json                 channel metadata under attributes.tsz
│   └── raw/                     for a continuous channel
│       ├── zarr.json             array shape/dtype/chunk/codec metadata
│       └── c/...                 encoded chunk/shard objects
├── 1/
│   ├── zarr.json
│   ├── events/                  for an event channel
│   ├── durations/
│   └── labels/                  and/or numeric values, as declared
└── ...
```

This tree illustrates channel kinds, not a promise that channel `0` is a
particular signal. `tsz.provenance.origin` distinguishes `signal`, `annotation`
and `feature`. The converter currently routes annotators whose names start
with `expert` into primary; other annotators, including CAISR, go into the
derivative. That naming rule is not proof that an arbitrary human annotator
name has been classified correctly.

### Physiological samples

- A signal's `raw` array is one-dimensional, **already calibrated physical
  `float32`**, rather than the original H5 digital counts.
- Channel metadata declares `rate_hz`, `offset_us`, `unit`, name and source
  dataset identity. The converter normalizes declared voltage units to `uV`
  and records `unit_converted_from` where applicable. Missing units become
  `(undeclared)`; a reader must not invent them.
- Sample `i` occurs at `offset_us / 1e6 + i / rate_hz` seconds from recording
  zero. Channels retain their own rates, offsets, lengths and NaN gaps.
  There is no recording-wide assumption of 200 Hz or implicit resampling.
- Do not apply digital-to-physical calibration a second time. Original digital
  samples/calibration remain available in the archived inputs; converted
  continuous values use physical float32 precision.

The current continuous-array writer uses Zstd level 5, inner chunks of at most
8,192 elements and shards targeting approximately 16 MiB, without a zoom
hierarchy. Lossless feature payload arrays have their own chunking and retained
dtypes. These are implementation settings, not a request to retune existing data.

### Expert and model annotations

Annotation channels contain `events` (onsets) and `durations` as integer
**microseconds from recording zero**, plus `labels`. A label is an index into
`tsz.label_names`; the original stage/event code is at the same index in
`tsz.provenance.label_codes`. Label index and original code are not interchangeable.

Preserve named scorers and event families, including different respiratory
definitions. Primary expert scoring is distinct from derivative model scoring.
Some explicitly revised recordings can be model-only, with unsupported original
expert exports preserved in an archive. Missing expert scoring is not permission
to rename model scoring as an expert assessment.

### Temporal features: convenient projections and lossless payloads

Features have independent clocks and shapes: a scalar track, sparse spindle
events, interval table, spectrum or structured result are different representations.
The converter creates TS convenience projections: continuous `raw` channels for
regular numeric series and event channels for segments, sparse events or other
temporal outputs. Facets such as band/region may have separate projected channels.

For complete source rows, use the **`bdsp.sleep-feature-payload.v1` extension**:

```text
<owner-channel>/feature_payload/
├── attributes: schema = "bdsp.sleep-feature-payload.v1", source_h5_path
└── source/                          original unsplit temporal family
    ├── attributes: feature_id, row_count, temporal semantics, units, source, ...
    ├── row_index/, start_time_s/, end_time_s/
    ├── status_code/, status_labels/                  when categorical
    ├── value_numeric/, value_numeric_present/        when numeric
    ├── value_array/, value_array_present/            when array-valued
    ├── value_json_code/, value_json_labels/          when structured JSON
    └── information/                                 axes, coordinates, other row metadata
```

Only the fields appropriate to that family occur. Numeric dtypes, dimensions,
row order, original timestamp precision, statuses, presence masks, logical text,
attributes and `information` fields are preserved. Matrix-valued spectral data
remain arrays; they are not reduced to a scalar or represented only by an image.

Discovery is explicit: projected channels declare
`tsz.provenance.feature_payload = {"schema": "bdsp.sleep-feature-payload.v1",
"path": "<owner-channel>/feature_payload"}`. Multiple facets can reference the
same family payload. Derivative `recording.json` also declares
`source.feature_payload_schema = "bdsp.sleep-feature-payload.v1"`.
This is a BDSP extension within TS-Zarr, not a new Penn core channel kind or a
claim of formal external validator acceptance.

Read the preserved payload when precision, rejected/missing rows or coordinates
matter. A TS projection can filter rows, reorder events or insert NaN gaps;
its event count and NaNs do not reconstruct the original row statuses.

## 5. Derivative summary JSON and availability

The current summary schema is **`psg_features_sidecar_v0.5`**. Its top-level
fields are `schema_version`, `recording_id`, `subject`, `demographics`,
`recording`, `provenance`, `counts`, `features` and `vectors`.

`features` maps each full feature ID to metadata and a recording-summary value:
`short_name`, `display_name`, `display_scope`, `unit`, `source`, `result_kind`,
`storage_type`, `value_shape`, `status`, `message`, typed value columns and
`array_path`. Scalar storage types select `value_number`, `value_integer`,
`value_boolean` or `value_text`. Available exported one-dimensional summary
arrays, such as an embedding, are in `vectors`, with values and metadata.
Full feature IDs encode context such as stage, event type, band or region;
`result_kind` distinguishes raw and normative results. Display names and short
names are descriptive metadata, not a reason to discard the canonical ID.

**Check status before reading a value.** `ok` and `valid_zero` are available;
unavailable states such as `missing_input`, `qc_rejected`, `not_applicable`,
`failed`, `not_run` or `withheld` are different conditions. Preserve their
messages, and handle unknown statuses explicitly. Inactive typed columns can
contain placeholder zero/False values. They are not observed feature values.
The shared reader exposes status-qualified `value` and `available`; unavailable
values are `None`, while a legitimate available zero or False is retained.

The intended split is recording summaries in JSON and complete temporal,
array-valued and structured feature content in derivative Zarr. Existing
exported summary vectors remain in JSON. An H5 `array_path` is not automatically
a usable path in Zarr. Arbitrary H5 groups, unexported summary matrices, complete
producer provenance and external reports are **not** guaranteed by the temporal
payload extension; preserve and bind those producer artifacts separately.

`recording.json` and summary metadata can contain subject/session identifiers,
demographics, source filenames or dates. Keep them on the trusted analysis/server
side; do not forward them wholesale to the browser. The source feature H5 and
integrity/provenance companions retain evidence not fully represented by the
converted JSON's compact `provenance` object.

## 6. Read the artifacts in Python

Use Python 3.11+ and install the current reader from GitHub in your project environment:

```bash
python -m pip install "bdsp[zarr] @ git+https://github.com/bdsp-core/bdsp-python.git@main"
```

The PyPI release `bdsp==0.1.0` predates Zarr support. See the
[installation guide](../README.md#1-install-the-reader) for H5-only installation
and reproducible source pins.

Pass the three associated artifacts explicitly. Generic local names below can
be replaced by the BIDS filenames above; the reader does not depend on renaming them.

```python
from bdsp.bids_zarr import open_recording

with open_recording(
    primary="/data/primary.zarr",
    derivatives="/data/derivative.zarr",
    summary_json="/data/summary.json",
) as recording:
    signal = next(iter(recording.signals.values()))
    window = signal.read(start=0, duration=30)
    # window.values, window.times_s, window.rate_hz, window.unit

    scorers_and_tracks = {
        scorer: list(tracks) for scorer, tracks in recording.annotations.items()
    }
    summaries = recording.features.summary.to_dataframe()
    temporal_families = list(recording.features.time_series)

    # Optional: copy everything into RAM before the stores close.
    # in_memory = recording.materialize()
```

For a family present in the recording, bounded reads preserve the original body:

```python
# Run inside the open_recording context above.
family = recording.features.time_series["eeg.multitaper_spectrogram"]
first_two_rows = family.read(rows=slice(0, 2))
# family.raw exposes original Zarr arrays, masks and information groups.
```

Signals and temporal arrays are lazy. Use them while the recording is open;
explicitly loaded arrays/DataFrames or `.materialize()` results remain usable
afterward. Full materialization can require substantial RAM. For remote access,
pass authorized, caller-configured Zarr Stores and a decoded summary dictionary;
raw `s3://` URL strings are not automatically resolved by this reader.
See [the full reader API](READER_DESIGN.md) for annotation windows, arrays,
structured results, ownership/lifetime and remote-store details.

## 7. Completeness, versions and older files

Default `require_complete=True` checks the supported export: primary, derivative,
summary, recognized payload schema, declared skips, metadata consistency and
available exported values. It does **not** establish that every scientific
module ran, that an unlisted family was never lost upstream, that results are
clinically validated, or that reports were preserved. A module-only supplement
and a full-panel run must remain distinguishable by their execution scope.

Older conversions omitted array/JSON-only bodies such as
`breathing.loop_gain_input`, `breathing.ventilation`, `coactivation.track`,
`cpc.window` and `eeg.multitaper_spectrogram`. Those were computed producer outputs
lost during export, not five feature families the producer never computed.
The repaired converter preserves them with the payload extension. Installing
new code does not update old stores: affected artifacts need offline reconversion
and separate publication. See [migration history](MIGRATION_HISTORY.md).

The reader offers explicit `require_complete=False` for primary-only or older
Penn-export inspection, with gaps in `recording.availability`. Format/schema
validation still applies. Sleep YODA **product serving**
requires strict split stores and rejects old enriched-H5/wide-Zarr serving
formats; historical readers are offline migration/parity tools.

Associate the **exact primary, derivative and JSON versions**. Matching subject,
session, duration or source filename alone is not cryptographic proof that they
belong to the same source/scoring/model revision. Preserve content hashes,
producer/model/configuration identities and private manifests where available;
invalidate affected derivatives when the selected source/scoring changes.
Opening or switching displayed scorers does not recompute saved features.

## 8. Sleep YODA presentation packages

The cohort tree in Section 3 is distinct from Sleep YODA's content-addressed web
distribution package. A web package can use the generic names `primary.zarr/`,
`derivative.zarr/` and `summary.json`, plus `presentation.json`, `assets/` and
`package-manifest.json`. The logical primary/derivative split is the same.

The extra files hold saved reports, figures and application projections.
The web's private manifest binds the delivered objects by exact version, size
and hash; this stronger binding is an application layer, not a property to infer
from native TS-Zarr metadata alone. All feature/model computation and fixed
report production occur offline. Serving reads saved data and may apply the
approved interactive calibration/filter/montage operations.

See [the completion workflow](ENRICHMENT_COMPLETION.md) for source/scoring
revisions, availability and report versions, and
[web migration notes](WEB_MIGRATION.md) for historical adapter work.

## 9. Quick translation from older repositories

| Older assumption | Current contract |
| --- | --- |
| Open one enriched H5 for signals, scorers and features | Open primary Zarr + derivative Zarr + summary JSON as one logical recording |
| Signal, expert and CAISR data belong in one store | Primary holds signals/experts; derivative holds model annotations/features |
| Every numeric array is a physiological signal at 200 Hz | Inspect origin, rate, offset, temporal semantics and shape |
| Decode `labels` directly as original stage codes | Resolve label index through `label_names` and `label_codes` |
| A zero in a JSON numeric field means the feature is zero | Check status/storage type, or use the reader's qualified `value` |
| A scalar catalog or plotted image preserves all computed output | Original temporal arrays, structured bodies, masks and information must be retained |
| A readable Zarr directory or `source.skipped` entry proves completeness | Validate the supported payload schema, source bindings and source-to-export parity |
| An H5 report/cache automatically moves into the derivative Zarr | Prepare/preserve separately unless its export is explicitly part of the contract |

## 10. Implementation references and history

| Topic | Authoritative implementation/reference |
| --- | --- |
| Directory/filename generation | Converter `src/bdsp_psg_conversion/common/layout.py` and `common/file_rules.py` |
| Native storage and payload extension | [Converter file structures](https://github.com/bdsp-core/bdsp-psg-conversion/blob/16c2d95f10dbeed8a1549da48dbd2ef51384fe57/docs/file-structures.md), `zarr_json/feature_payload.py` |
| Feature definitions and producer H5 | Producer registry, generated catalog and output contract linked in Section 2 |
| Reader schemas, decoding and validation | [`src/bdsp/bids_zarr/`](../../src/bdsp/bids_zarr/), [reader design](READER_DESIGN.md) |
| Initial export gap, fix and single-recording parity evidence | [Migration history](MIGRATION_HISTORY.md) |
| Storage completeness versus scientific completion | [Enrichment audit](ENRICHMENT_AUDIT.md), [completion workflow](ENRICHMENT_COMPLETION.md) |

The converter reference pins the payload repair at `16c2d95`; it does not imply
every cohort has been reconverted. This guide was checked against the local
reader and converter implementation. The feature producer has additional local
working-tree changes; those are not a clean published release pin. For a real
recording, its own producer identity and integrity evidence take precedence over
a general README or whichever checkout happens to be newest.

Only schema templates and implementation facts belong here. Recording-level
catalogs, clinical data, source locations, credentials and verification receipts
remain private outside Git.
