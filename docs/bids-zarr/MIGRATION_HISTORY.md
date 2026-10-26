# BIDS/Zarr migration history and initial verification

This records the October 2026 format investigation and converter repair.
Statements about local-only changes and unpublished stores describe those
checkpoints, not the current status of every recording. Start with the
[current storage README](README.md) for the format and repository ownership,
and [enrichment completion](ENRICHMENT_COMPLETION.md) for the subsequent
Sleep YODA release. No cohort-wide availability is implied by these initial
single-recording checks.

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
