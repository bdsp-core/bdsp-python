# Split BIDS/Zarr reader design

This documents the implemented `bdsp.bids_zarr` API and its boundaries.
Read the [storage and migration guide](README.md) for the artifact schemas.

## Ownership and organization

The reusable code lives in `src/bdsp/bids_zarr/`, namespace `bdsp.bids_zarr`.
It keeps artifact association, Zarr reads, feature decoding and validation
together, preserving the existing H5 modules and public APIs.
Sleep YODA desktop and web should consume this reader
through thin adapters, without duplicating conversion or scientific pipelines.

Install with `pip install bdsp` on Python 3.11+. Starting with version 0.3.0,
Zarr is a default dependency. The earlier `bdsp[zarr]` spelling remains a
compatibility alias. H5 and namespace imports still load Zarr lazily.

## Inputs and the Python object

Accept explicit primary Zarr, derivative Zarr and derivative summary JSON
references, locally or through an authorized read-only store. A private manifest
may resolve these references before opening. Do not list a whole bucket or
guess the latest derivative. An explicit artifact set avoids requiring the
Morpheus master just to open one recording.

Public entry point:

```python
from bdsp.bids_zarr import open_recording

with open_recording(
    primary=primary_store,
    derivatives=derivative_store,
    summary_json=summary_reference,
) as recording:
    # Lazy signals with native rates, units and origins.
    recording.signals
    recording.annotations
    recording.features.summary
    recording.features.time_series
    recording.features.arrays
    recording.features.structured
    recording.features.vectors
    recording.features.projections
    recording.metadata
    recording.availability
```

This is one logical recording over separate artifacts. Use a storage-independent
record model rather than manufacturing an in-memory H5. Explicit eager
materialization is available for scientific callers; bounded lazy reads are the default for
viewing. The existing `BDSPRecord.to_dataframe()` reopens an H5 path, so merely
filling that dataclass with Zarr data is not a complete integration.

`arrays` and `structured` are required capabilities of the complete reader.
The local converter repair defines `bdsp.sleep-feature-payload.v1`, retaining
original temporal groups under `<owner-channel>/feature_payload/source`.
Decode that version explicitly; do not advertise support based only on a
legacy scalar/text TS projection. See the storage guide for discovery and scope.

## Reading and materialization

`open_recording` accepts local `str`/`Path` store roots or configured Zarr v3
Stores containing `recording.json`. Remote URL strings are deliberately not
resolved: configure a read-only Store in trusted caller code, then pass it in.
The reader does not close caller-owned Stores. It closes stores opened from
local paths when its context exits. `summary_json` accepts a local path or a
decoded JSON dictionary; no remote listing or filename guessing occurs.

| Access | Result |
| --- | --- |
| `record.signals[name].read(start=60, duration=30)` | `SignalWindow(values, times_s, rate_hz, unit)` at the channel's native rate |
| `record.annotations[scorer][kind].read(start=60, duration=30)` | DataFrame with original `start_us`, `duration_us`, seconds, label index/name and original code |
| `record.features.summary[feature_id]` | Original summary metadata plus status-qualified `value` and `available` |
| `record.features.summary.raw` | Unmodified summary row dictionaries, including inactive value columns |
| `record.features.summary.to_dataframe()` | All summary rows, including unavailable results |
| `record.features.vectors[feature_id]` | Exported one-dimensional numeric array; associated metadata remains in the summary |
| `record.features.time_series[family].read(rows=slice(0, 2))` | Original rows with global `row_index`, clocks, statuses, numeric/text/JSON values and per-row `information` dictionaries |
| `record.features.arrays[family]` | Numeric-body temporal family; `.values` and `.present` expose lazy original arrays and masks |
| `record.features.structured[family]` | Structured-JSON temporal family; `.read()` decodes bodies |
| `record.features.projections[feature_id].read(...)` | Converter's convenient TS projection, potentially faceted, rounded or status-filtered |
| `record.materialize()` | Detached `MemoryRecording`, usable after closing the stores |

Omit time arguments to read a whole channel/family. Windows are half-open:
`[start, start + duration)`. Samples use their recorded times; events/segments
intersecting a window retain their complete original boundaries. Point events
at the start are included; events beginning at the end are excluded. If duration
is supplied, start must also be supplied. Negative duration raises an error.

Temporal `rows` selects family-local positions before an optional time-window
intersection. It does not renumber the producer's global `row_index` or discard
withheld rows. Time selection may scan the family's start/end columns; large
value bodies are loaded only for selected rows. Numeric JSON bodies remain
NumPy arrays with original dtype and dimensions. `information` dictionaries
retain coordinates, facets and presence without risking column-name collisions.
Use `include_values=False` or `include_information=False` for timing-only reads.
`.raw` exposes the preserved Zarr group, including dataset attributes and masks.

Eager `MemoryRecording.features` is a dictionary with `summary`, `vectors`,
`time_series`, `projections` and per-family `metadata`. Its tables use None for
absent decoded values. `metadata["channels"]` preserves TS channel provenance;
feature metadata includes original producer attributes, export provenance and
information storage types. Full materialization can consume substantial RAM;
bounded reads are the default. Lazy objects must be used while the recording
is open; explicitly returned NumPy arrays/DataFrames remain usable afterward.

By default `require_complete=True` requires both derivatives, the summary JSON,
the supported payload schema, valid payload references, no declared skips and
exported values for available summary rows. A malformed/unknown schema, mixed
subject/session/duration or contradictory declared source filename raises
`FormatError`, including in permissive mode. Known missing representations raise
`IncompleteRecordingError`. Explicit `require_complete=False` permits legacy or
primary-only inspection while `availability["complete"]` remains False and
`availability["issues"]` explains the gaps. Legitimately unavailable feature
rows retain their statuses and do not make an otherwise valid export incomplete.

The completeness flag covers declared signals, annotations, temporal payloads,
summary scalars and exported vectors. It does not certify arbitrary H5 groups,
reports, producer provenance not exported by the converter, summary matrices,
or an unlisted family silently omitted by an upstream process. Opening validates
metadata/array shapes; data-dependent categorical indices and interval validity
are checked as their rows are read. Use source-to-export parity receipts for
stronger validation. `identity_binding="metadata_only"` explicitly records that
the current format cannot reject every stale derivative revision sharing the
same metadata. A future content identity manifest is needed for that guarantee.

## Interpretation rules

1. Validate the declared TS-Zarr/JSON schemas and recording identity across all
   artifacts, including session and duration. Channel numbers are store-local;
   index channels by declared identity/origin, not number or guessed ordering.
2. Read physiological samples as already calibrated physical values. Respect
   each channel's rate, origin, extent, unit and gaps. Resampling is an explicit
   caller operation, never a default 200 Hz assumption.
3. Expose annotations as named scorer/event-family tables. Convert microseconds
   to seconds at the API boundary while retaining original times when needed.
   Decode label indices through both labels and original code metadata.
4. Keep the source used to compute saved features distinct from the scorer
   selected for display. Opening a recording or switching scorers does not
   recompute features or relabel their source.
5. Decode summary values by storage type and preserve status, message, units,
   full feature ID, display metadata and raw/normative distinctions. Expose all
   summary rows as well as a convenience view of status-qualified values.
6. Preserve independent feature clocks, event/segment semantics, dimensions,
   coordinates, structured payloads, presence masks and information fields.
   Preserve facet/family identities without treating every feature as a signal.
7. Surface missing artifacts, skipped exports and unsupported representations
   distinctly from valid empty results. Do not infer lost per-row statuses from
   NaN values or absence of exported events.
8. Bind caches to the exact versions/content identities of the primary,
   derivative Zarr and JSON together. Matching IDs or `source.file` names alone
   is not cryptographic proof of a common source or completed derivative run.
   The inspected metadata does not provide a complete source/model identity
   manifest; preserve available provenance and record missing bindings honestly.

## Completeness and upstream work

All outputs of the selected `bdsp-sleep-features` execution scope must survive
conversion, including non-scalar outputs. Scope may be a full panel or an
explicit supplemental module; absent families outside a declared module are
different from outputs accidentally lost in conversion. Legitimate unavailable
results retain their statuses rather than being required to contain values.

Use the converter's versioned temporal payload representation for numeric
arrays, structured bodies, row metadata and information fields. Additional
producer provenance, summary matrices and fixed assets still need explicit
preservation/discovery contracts. Zarr supports these data; legacy exports
omitted some of them. Do not silently invent an undocumented extension in the
reader or fall back to H5 and claim that Zarr preservation passed.

Primary-only mode is available through explicit `require_complete=False`.
A request for the complete supported export fails when required representations
are known to be absent. Opening stores successfully is not a producer-wide
completeness gate.

## Application integration

The shared reader performs storage reads and decoding. Model execution,
feature computation and fixed report generation remain offline producers.
The web adapter projects saved data into the existing descriptor/window/asset
API and retains the established interactive filter contract. Existing report,
spectrogram and other viewer assets need an explicit mapping to the new export.

Keep credentials, artifact locations, source subject/session identifiers,
dates and private provenance on the trusted Python side of the web service.
The browser receives opaque recording IDs and bounded application data.
Local Python analysis and browser metadata have different disclosure boundaries.

## First implementation acceptance

- Synthetic split artifacts exercise mixed-rate signals, time offsets, expert
  and CAISR tracks, label/code mapping, statuses and typed JSON summaries.
- Array and structured payload round trips exercise the agreed derivative
  extension, including dimensions, coordinates, missing rows and provenance.
- Mismatched declared artifact identities fail visibly; known incomplete exports
  cannot be mistaken for complete supported exports. Revision binding requires
  a future content manifest.
- One owner-selected recording then proves bounded reads and producer-to-export
  parity in the authorized environment. Preserve private evidence outside Git.
- Run the existing H5 tests to protect compatibility, followed by focused web
  adapter acceptance when that integration is in scope.

Reader acceptance uses synthetic tests and the local repaired recording, with
private parity receipts outside Git. Converter tests alone do not establish
reader completeness or scientific parity. Application adapters and their
acceptance checks remain separate work.

Acceptance on 2026-10-20: all 397,397 temporal rows across 36 original families
matched the producer H5 decoder, including bodies, information, statuses and
row metadata. All 5,283 summary rows and the exported vector matched the source
H5 contract. All 10 annotation tracks (2,719 events) matched original times,
labels and codes; 69 windows across 23 physiological channels matched calibrated
H5 values. Synthetic coverage also exercises eager data after closing, metadata-only
opening, presence masks, typed zero/False and legacy/mixed-artifact failures.

The full real-case `.materialize()` path also passed: 23 physiological signals
(84,754,462 samples), all temporal rows and 224 TS feature projections remained
usable after the stores closed. Local validation used Python 3.12 and Zarr
3.3.0: 34 tests passed, with the opt-in AWS download test skipped. The wheel
built and passed isolated installation/import and synthetic signal-read smoke
checks, including imports with Zarr blocked. Other Python versions are covered
by the configured CI matrix; that matrix was not executed in this local session.
