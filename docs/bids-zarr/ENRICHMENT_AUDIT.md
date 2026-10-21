# Sleep YODA enrichment audit — 2026-10-21

The current showcase is viewable in the split format, but it is **not uniformly
fully enriched for Sleep YODA**. Storage conversion, preservation of a selected
producer artifact, completion of scientific modules and preparation of viewer
assets are separate checks. A successful `open_recording(require_complete=True)`
does not require every producer module to have run. It preserves explicit
`not_run`, missing-input and other availability states from the archived export.

## Scope and evidence

The published catalog has 65 retained identities: 60 ready for storage/serving
and five blocked by previously reviewed expert-source timing defects. This audit
checked all 60 serving packages; it did not repair or re-enrich the five blocked
sources. Later local report/AFib experiments and previews were excluded.

The exact version-pinned catalog was read from S3. Every package manifest was
reconstructed from the immutable local package and the publication ledger's
full-readback object receipts; its canonical bytes, length and SHA-256 had to
match the published catalog pin. Previously fetched manifest copies also agreed.
All inspected local primary/derivative metadata, summaries, presentations and
**1,157 viewer assets** matched those published hashes. This binds the review to
the released packages without re-downloading every physiological shard.

Archived producer provenance and temporal schemas were inspected separately.
Every nonempty temporal family and its row count in the selected feature H5 is
present in the derivative: **25,386,816 original rows** across the 60 cases.
Those rows have only accepted completed/availability states. The non-scalar
export repair is intact. Loop-gain inputs, ventilation, CPC and multitaper bodies
are present in all 60. Coactivation tracks are present in 49; the other 11 have
no nonempty source-H5 track and declare missing coactivation inputs. Their absence
is not an additional conversion loss.

Recording-level inputs, locations, hashes, statuses, source provenance and
diagnostics remain private outside Git. No source data, model outputs, runtime
catalog or published objects were changed. No models ran during this audit.

## Current coverage

Counts below use the 60 serving cases, including hidden retained cases.
Presence of a report is not clinical validation of its interpretation.

| Saved capability | Cases |
| --- | ---: |
| Physiological signals, hypnogram, sleep statistics and BDSP catalog | 60 |
| Full Main report, viewer spectrograms and CPC image | 57 |
| Web EEG-quality projection and OSD navigation | 57 |
| Brain Health/Age Norms, breathing, loop-gain and coactivation report tabs | 57 |
| Summary report, metrics CSV and saved titration report | 57 |
| Qualified legacy oxygen/report and hypoxic-burden presentation | 56 |
| Saved AFib projection, RSWA report tab or corrected-signal display modes | 0 |

AFib, corrected RSWA and optional artifact correction are separate capabilities;
their absence is not automatically failure of the existing BDSP profile. New
age-norm development and TNT remain governed by the owner's deferred/research
decisions, not newly mandatory enrichment tasks.

The 57 richer viewer packages comprise 50 older cases and SY59–SY65. The three
new imports, SY66–SY68, have signals, expert/CAISR scoring, a BDSP-selected
hypnogram, summary statistics, scalar/vector results and native temporal payloads.
Their Main page is a basic availability notice rather than the full report.
They lack the established viewer spectrogram/CPC assets, EEG-quality projection,
OSD navigation, detector overlays and the richer report tabs. Available native
Brain Age/Brain Health, OSD, CPC and spectra must not be described as uncomputed
merely because these presentation layers are absent. Their public oxygen values
remain deliberately withheld pending source-qualified integration.

## Scientific and presentation reconciliation

**SPIN:** 53 native summary JSONs contain the same 15 `not_run` summaries:
five spindle metrics, seven slow-wave metrics and three spindle/slow-wave
coupling metrics. The bound source provenance declares `spin_v11=runtime_blocked`.
This is 795 not-run summaries, not a failure of the Zarr payload decoder.

Fifty older packages already carry a later, source-bound SPIN supplement in
their saved presentation/catalog. Across these, 708 supplement values are
available in the catalog while the native summary still says `not_run`; other
supplement entries retain unavailable states. Those supplements were computed
separately without rewriting the original feature H5. The migration converted
that original H5 and retained the later viewer assets, so the scientific and
presentation layers do not describe one consolidated enrichment revision.
Reuse the validated supplemental results and provenance when assembling a new
consistent derivative; do not start by rerunning all 50 recordings.

SY59–SY65 have completed SPIN provenance and no not-run summary states. Five
have all 15 usable SPIN summaries; two retain accepted unavailable/not-applicable
results. These seven also have a clean recorded producer identity. This is
stronger completion evidence, not an independent clinical validation or a new
certification of every Sleep YODA capability.

**Other source spindle output:** all three new archived H5s also contain
nonempty `/features/research/spindles` detections from the named v12 research
track. This namespace is outside the registered BDSP temporal export and is
absent from their current split stores and viewer overlays. Preserve and import
that named result with its clock, model identity and research scope. It is not
interchangeable with the missing SPIN v11 summary/slow-wave/coupling computation.
The current converter does not export arbitrary research namespaces.

**Oxygen:** 22 native feature runs declare missing oxygen inputs. Twenty-one
of those packages nevertheless have later available oxygen results in their
saved catalog/report, producing 1,092 availability disagreements. Reconcile the
source-qualified calculation/interpretation and its identity; do not copy values
between revisions or relax qualification solely to make a completeness count pass.
The remaining missing/QC/not-applicable results must stay distinct from zero.

**Brain Age and other models:** native Brain Age results are available in 55
cases; three declare insufficient predictors and two not-applicable status.
Brain Health execution is recorded in all 60. CAISR stage/arousal execution is
recorded in all 60; respiratory output has one missing-input case and limb output
has nine. These availability states need input-aware review, rather than a rule
that every possible feature must be a number. The seven later cases have no
not-run summaries; that alone does not certify the earlier or newly imported cases.

## Next useful work

1. Assemble a consistent scientific revision from the preserved feature H5s and
   verified supplements, resolving the 50 older SPIN and 21 oxygen discrepancies
   with their actual source/configuration identities. Preserve all historical
   files and report bytes as evidence.
2. Complete SY66–SY68's missing applicable scientific modules, import their
   existing named research spindle detections, compute the distinct web
   EEG-quality output, and prepare viewer assets/reports from results already
   present. Native BDSP QC is not the same algorithm as the web EEG-quality panel.
   Apply titration/report producers only where their inputs and study type permit.
3. Give the assembled output an explicit full Sleep YODA profile covering both
   BDSP results and retained viewer requirements. The existing archived-import
   profile establishes readability and preserved statuses, not full enrichment.
   Accepted missing-input, QC and not-applicable states remain explicit.
4. Validate scientific/presentation agreement, then create new immutable package
   identities. Publication and live activation remain separate authorized actions.
   Keep the five source-timing cases blocked until their separate review/repair.

The relevant contracts are `sleep-yoda-web/packer/bdsp_profiles.json`,
`docs/BDSP_ENRICHMENT.md`, `docs/ENRICHMENT_ARCHITECTURE.md`, and the producer
ownership decisions in `docs/DESKTOP_DEPENDENCY_DECISIONS.md`. The BDSP full
profile remains a provisional integration contract; the storage-ready catalog
flag and the archived-import profile must not be treated as its completion gate.
