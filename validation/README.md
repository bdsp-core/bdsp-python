# Dataset acceptance checks

These checks ask whether a published BDSP cohort has the expected organization
and can actually be read by this repository. Keep them here: compatibility with
real releases is useful evidence for reader users. `tests/` remains the fast,
synthetic regression suite; conversion and cloud repairs belong to the producer.
The root README stays a short user quickstart.

## Run an audit

Use Python 3.11+ in a project environment and existing authorized AWS credentials:

```bash
pip install -e ".[test,validation]"
pytest
python validation/run_audit.py \
  s3://bdsp-opendata-repository/PSG/bids/S0001 \
  --subjects 200 --seed 20261028 --workers 4 \
  --output validation/results/s0001-20261028
```

A local cohort directory also works in place of the S3 URL. The script only
lists and reads inputs. It neither converts data nor writes to S3. AWS profiles
can be selected with `--profile`. Live audits are explicit operations, never an
automatic part of pytest or CI. For a different cohort, date, or audit revision,
use a new output directory.

Start with `--subjects 1 --workers 1` and a separate pilot output directory
before scaling. Run long audits under a local supervisor, with stdout/stderr
saved in the private output directory. Repeat the exact command to resume a
saved manifest; completed recordings are skipped and options/code must match.
The manifest fixes the sample even if the bucket changes later. The source is a
live prefix, not an atomic snapshot; receipts retain object versions/ETags and
read timestamps, and S3 range reads avoid downloading whole signal shards.
Multiple workers use separate processes and AWS clients; small metadata/column
objects are prefetched in bounded parallel reads. One worker runs in-process.

## Methodology

1. Inventory all primary subject directories. Check cohort metadata, participant
   coverage, primary/derivative/source subject coverage, and leftover subject
   directories at the cohort root. Check existence of the documented derivative
   dataset description and cohort Parquet roll-up; Parquet contents are outside
   this audit's scope.
2. Sample **200 subjects without replacement**, using a fixed seed on the sorted
   population. Select **one random session per sampled subject**, with a stable
   subject-specific seed. This gives equal subject inclusion probabilities; it
   is not a uniform session sample. Save exact selections, population digest,
   reader revision/path, runtime versions, options, and audit code digest.
3. Check each session's primary/derivative filenames and locations, BIDS
   sidecars, sessions/scans references, archived source H5 presence, and channel
   inventory, units and sampling frequencies. Flag H5/EDF or CAISR CSV files in
   primary. Human annotation CSV names can vary by cohort. Compare paths with
   the identities inside the artifacts.
4. Call the public `bdsp.bids_zarr.open_recording()` with its strict defaults.
   This validates the supported split export schema and artifact association.
   An optional relaxed diagnostic open preserves the original failure; it
   cannot turn an incomplete recording into a pass.
5. Actually decode a one-second window near the beginning, middle and end of
   **every physiological signal**, keeping its native clock. Check sample
   counts, finite/increasing times, and infinite sample values. Count all-NaN
   windows separately: missing samples are allowed by the format.
6. Read **every annotation track** and every event feature projection; read a
   bounded window from continuous projections. Read the first, middle and last
   original row of **every temporal family**, including numeric arrays,
   structured JSON, presence masks and information fields. Decode the summary
   DataFrame and vectors through the reader.
7. Record errors independently, checkpoint each finished recording, and produce
   a machine-readable aggregate. Transfer is bounded to 256 MiB per Zarr store
   by default (`--max-mib`). Exhausting a limit is an incomplete test, never a
   pass. Absent sampled chunk objects are flagged because Zarr can silently
   substitute fill values. Such absence can be legal for implicit-fill storage;
   it requires review and is not by itself proof of corruption.

Exit status 0 means all implemented layout and sampled reading checks passed;
1 means there are findings. An interrupted run can resume. A sample of 200 with
zero failures would put the approximate 95% upper bound on failure prevalence
near 1.5% **for this subject/session sampling design and tested behavior**, not
prove that every file reads flawlessly.

## References and acceptance limits

- [Penn's ARPA-H REST BIDS sheet](https://docs.google.com/spreadsheets/d/1yKaBUB7aw16VVQkaFXzm20GJACVrrwFxjf-GsnPhaX8/edit),
  `BIDS Retrospective`, describes the primary layout. Its derivative proposal is
  older: it still places `deriv-caisrannotations` beside primary data.
- The later split contract is in the [format guide](../docs/bids-zarr/README.md)
  and the converter's `common/layout.py` / `common/file_rules.py`: model outputs
  live under `derivatives/bdsp_sleep_features/sub-…/`, without a session directory.
  This audit uses that split and does not claim the sheet has been updated.
- [Reader validation scope](../docs/bids-zarr/READER_DESIGN.md).

These checks do not establish full BIDS validator compliance, source-to-export
H5 numerical parity, all-chunk integrity, completeness of feature computation,
scientific validity, speed improvements over H5, or Wolfgang/Qichen sign-off.
Those are separate acceptance evidence. Source archives are never removed by
these checks, regardless of older spreadsheet cleanup suggestions.

## Evidence and privacy

`results/` is Git-ignored. It contains `manifest.json`, `global.json`, append-only
`recordings.jsonl`, and `summary.json`. Exact subject/session identifiers, object
receipts and exception text stay there. Do not commit raw recordings, clinical
values, credentials, row-level manifests, or unsanitized logs. Console output
contains aggregate progress only. Permissions should keep results private.

Publish reviewed, aggregate findings in `reports/` with a date, source contract,
sample design, actual outcomes, limitations and next actions. Do not copy
participant-level detail out of `results/`. The local ignored `AGENTS.md` is a
working guide; reproducible public methodology remains in this folder.

Completed review: [S0001 — 2026-10-28](reports/2026-10-28-s0001.md).
