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

## Strict production cutover preparation (2026-10-21)

The owner authorized adding the three I0002 cases and moving the showcase to the
split reader exclusively. Older wide-Zarr and enriched-H5 catalogs must fail
explicitly; they are diagnostic migration inputs rather than product fallbacks.
The web contract is `sleep-yoda.bids-zarr-runtime.v1`, with a digest/version-pinned
manifest for each native primary store, derivative store, JSON summary and
separate precomputed viewer presentation. The reusable reader remains in this
package; the web artifact installs an exact wheel bound to a clean source commit.

The release inventory has 65 retained identities: 60 ready and five blocked by
the previously documented expert-source defects. Existing hidden and retired
identity policies are preserved. The three additions use new showcase aliases
and neutral selection labels. Missing summary values remain null, not numeric
zero; unavailable producer outputs retain their original status. These archived
inputs are not relabeled as passing a full clean-producer enrichment profile.

All 60 standalone packages passed local signal/asset checks without runtime H5
or old wide-Zarr reads. The real S3 pilot verified native windows, filtering and
all five repaired non-scalar families. Publication and live acceptance remain
separate gates; preparation is not evidence of completed deployment.

Some original CAISR event ends exceed the recording duration. Preserve those
positive intervals exactly while bounding visualization to the recording
clock. This observation is separate from the five blocked expert timing defects;
do not silently clip/delete source model outputs or claim they were repaired.
The private API audit records the affected intervals without adding them to Git.

Public projections must distinguish participant input age from sleep-stage
features. An unanchored `age` privacy match also matches `stage`; use explicit
input-field boundaries. The imported scalar catalogs were corrected and checked
against their typed summaries before release. Available arrays remain described
as stored in the derivative; unavailable source results retain their status.

## Completed web cutover (2026-10-21)

The new reader is deployed and the three additions are online. The 65-identity
catalog has 60 ready cases and five explicitly blocked cases, with 57 visible
rows and 54 active links. Full live acceptance passes 60 descriptors, 1,157 assets,
60 filtered windows, the five conversion errors and retired-identity exclusion.
The new cases pass browser rendering and subject-list checks through the
privileged operator route. Public TLS/authentication denial checks pass; fresh
human Cognito sign-in was not tested. Original H5s and producer outputs remain
preserved, and no models or source-scoring repairs ran during this migration.
