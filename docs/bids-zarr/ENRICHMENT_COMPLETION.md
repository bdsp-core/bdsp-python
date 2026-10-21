# Completing an enriched split recording

Storage completeness and enrichment completeness are separate. The shared reader
checks the supported export schema; it preserves missing-input, QC and not-run
states and does not run scientific producers or create viewer reports.

The 2026-10-21 Sleep YODA completion task assembles a new scientific revision
before converting it. It keeps the original H5/EDF, selected feature exports,
model versions and prior derivatives as immutable evidence. The web application
owns offline orchestration and saved presentation assets; this repository owns
reading primary signals/expert annotations, derivative models/features and the
summary JSON. See the web repository's
[composed-enrichment contract](https://github.com/bdsp-core/sleep-yoda-web/blob/8ff6aa2d6cbec3b45fb44607b516d606cc114806/docs/ENRICHMENT_COMPLETION.md).

## Reuse and source binding

An archived feature package can be internally complete and still belong to the
wrong recording or scoring revision. Pin the opaque recording identity, original
source hash, selected scoring version, producer/model identity and every file in
the scientific integrity receipt. A corrected upstream invalidates dependent
canonical, conversion and presentation identities.

Reuse the supplied SPIN output only with its exact source and CAISR stage-sidecar
hashes. The final task selection uses 48 such reuses and 17 local runs, including
five scoring revisions. Reconcile qualified oxygen into the native scientific
body using its original method and verified source interpretation; a later
presentation catalog cannot serve as the scientific source.

Imported CAISR can differ from the version selected by a feature run. Preserve
both names and select explicitly. Dense annotation adapters may declare their
own clock; that does not resample physiological signals. A verified original CSV
can support a clock repair on a new version. Otherwise an owner-approved,
explicitly labelled CAISR version can archive unsupported expert exports without
representing model scoring as a repaired expert assessment.

## Availability and report versions

Completion means applicable modules have run, with supported input/QC limits
explained. Unavailable results need null values and reasons; valid zero remains
an available zero. Native JSON can retain placeholders in inactive typed columns
such as `value_number`; those columns alone do not establish availability. Use
the reader's status-qualified `features.summary[key]["value"]`, which returns
`None` for unavailable results. Native 64 Hz ECG cannot support the retained ECGFounder
preprocessing. Blank EMG units in both original H5 and EDF do not justify an
invented voltage conversion. Insufficient REM is a separate RSWA limitation.

One case with no PTAF cannot run the selected CAISR respiratory producer. Its
valid named expert respiratory scoring can still support a separately labelled
RSWA run. The retained reader requires a generic `resp` name, so a disposable
input view explicitly aliases the original expert `resp_3` table; original
objects are preserved and the expert source, 3% rule and actual computation-input
hash are recorded. This does not fill missing CAISR output with expert data.
The RSWA output separately records CAISR as the PLM-mask source; its label names
both expert scoring and CAISR, and validation checks both output attributes.

Fixed reports also have scoring and producer versions. Archived Sleep YODA
reports must identify their retained version when the default viewer uses a
different BDSP-selected CAISR timeline. The current BDSP catalog and Metrics CSV
use the selected scientific revision. Reading another scorer interactively does
not regenerate a fixed report or model output.

## Final validation boundary

Before promotion, verify every package inventory hash and current input/model
dependencies. Compare the complete lossless temporal arrays and scalar/vector
summary with the selected H5 body. Check source clocks, calibrated signal
windows, exact missing/zero masks and filtered-window parity. Penn continuous
samples use physical float32 precision; original digital samples/calibration
remain preserved in the immutable source archive.

Saved viewer reports, spectrograms, CPC, EEG quality and screening projections
need separate reader/browser checks and privacy validation. Neither successful
conversion nor a healthy page establishes these gates or clinical validation.

The 2026-10-21 release completed and activated all 65 retained split packages.
AFib is computed for 60 with five native-rate limits; RSWA is computed for 49
with sixteen verified unit/REM limits. Eight revised/new Main/Summary reports
were refreshed; the other 57 retain explicit archived report/scoring labels.
All 35,974 native temporal arrays and every package byte passed validation,
followed by all-case local/live API checks (1,438 assets) and eight browser cases.
The subject list has 57 active links; eight hidden cases and three retired
exclusions remain preserved. The earlier audit describes the superseded release.
Recording-level evidence remains private outside Git. See the web
[handoff](https://github.com/bdsp-core/sleep-yoda-web/blob/8ff6aa2d6cbec3b45fb44607b516d606cc114806/docs/SHARED_AWS_PILOT_HANDOFF.md)
for release, availability and cleanup details.
