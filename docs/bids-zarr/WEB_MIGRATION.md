# Sleep YODA web migration — 2026-10-20

`sleep-yoda-web` now has a local `bids-zarr` backend built on
`bdsp.bids_zarr.open_recording`. Its implementation and commands are documented in
[`sleep-yoda-web/docs/BIDS_ZARR.md`](../../../sleep-yoda-web/docs/BIDS_ZARR.md).
This is a local prototype, not deployment or publication of new S3 stores.

## Association with older web bundles

The previous showcase uses a custom wide Zarr layout rather than Penn TS-Zarr.
Its reviewed physiological/expert source H5 and native BDSP feature H5 are often
separate files. Local conversion pins both hashes, associates them in a temporary
H5 view, adds already saved model scoring and calls the repaired
`bdsp-psg-conversion` public conversion API. No model/feature execution occurs.
The converter places expert scoring with primary signals, and model scoring
with derivative feature payloads. The shared reader composes both stores and
the summary JSON without rebuilding a monolithic source file.

Some Sleep YODA reports, images and presentation metadata are outside the new
BDSP artifacts. The initial web adapter retains an explicitly pinned old bundle
as a companion for those saved assets. It reads physiological signals and
scoring through the shared reader, with no legacy signal or H5 fallback at
runtime. The web engine continues to own bounded interactive filtering and its
existing authorization/protocol/privacy contracts.

Physical TS-Zarr samples must not receive H5 affine calibration again. The
adapter retains accepted display units, reversing only an explicitly documented
voltage-unit normalization when required. The scientific reader exposes stored
units. Old web event tables were rounded to milliseconds and retained input
order; new event tables use sorted microseconds. Association checks compare the
event sets at the old half-millisecond precision, require identical code/label
maps and restore saved display order using the precise new times.

## Verified local scope

All 62 retained showcase sources were checked. Exact online target checks found
primary/derivative/summary objects for 19; none of those derivatives declared
the repaired temporal-payload marker. The other 43 lacked those targets. This
does not establish equivalence of the online feature execution to the saved
showcase inputs, so local conversion used the pinned inputs for all cases.

The batch produced 57 complete conversions. Converter checks compare signals,
scoring, original complete temporal payloads and summary outputs with the H5
view; strict shared-reader opening then checks the declared export contract.
The web audit passed all 57, comparing 4,971 native windows, 57 filtered windows
and 1,148 saved assets, and sampling decoded rows from 1,983 temporal-family
instances. This is migration parity evidence, not new clinical validation.

Five cases remain blocked by original expert timing problems. Scoped original
CSV/JSON and EDF-header investigation found one H5 with whole-day onset errors,
three source scoring exports extending beyond physical EDF coverage, and one
unsupported trailing H5 stage row. The old ingestion writer can retain an
out-of-recording start while clipping its end, creating negative durations.
The owner chose to keep the five blocked. No annotation correction, row removal,
signal change or dependent feature/report regeneration was performed.

The full batch remains incomplete. An explicitly declared 57-case local preview
is separate from the original 62-case catalog. A scoring correction requires
review/regeneration of dependent analyses before later acceptance; reader
permissiveness or a changed clock must not silently make that scientific decision.
Case identities, source locations, hashes, source versions, diagnoses, datasets
and detailed receipts remain private outside Git.

A separate follow-up downloaded three owner-selected I0002 merged source H5s
from their exact archived S3 keys. All three passed full converter source parity
and strict shared-reader validation, retaining each of the five formerly skipped
feature families. The original H5s, primary/derivative stores, summary JSON
and private receipts are stored locally with verified destination hashes. No
models were run and no showcase selection or published artifacts were changed.
