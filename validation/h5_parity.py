"""Rebuild sampled source H5s from published split Zarr; see validation/README.md.

For each sampled recording this downloads the primary Zarr, derivative Zarr,
summary JSON and archived source H5 (plus a separate feature H5 where the
session H5 never carried features), writes an H5 with ``bdsp.zarr_to_h5``,
compares it with the source H5 using ``bdsp.h5_compare`` and, when the
converter source is supplied, also asks the converter's own source-to-export
verifier whether the rebuilt H5 is a valid source for the published stores.

It only lists and reads S3. Downloads and rebuilt H5s live under the private
output directory and are deleted after each recording unless --keep-files.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import random
import shutil
import statistics
import sys
import time
import traceback
from urllib.parse import urlparse

DEFAULT_ROOT = "s3://bdsp-opendata-repository/PSG/bids"
COHORTS = ("S0001", "I0002", "I0003", "I0004", "I0006", "I0007")
DERIV = "derivatives/bdsp_sleep_features"


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def client(profile=None):
    import boto3
    from botocore.config import Config

    config = Config(connect_timeout=10, read_timeout=60, retries={"max_attempts": 5},
                    max_pool_connections=32)  # fmt: skip
    return boto3.Session(profile_name=profile).client("s3", config=config)


def split(url):
    parsed = urlparse(url)
    return parsed.netloc, parsed.path.strip("/")


def children(s3, bucket, prefix):
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=prefix.rstrip("/") + "/", Delimiter="/"
    ):
        out += [p["Prefix"].rstrip("/").rsplit("/", 1)[-1] for p in page.get("CommonPrefixes", [])]
    return sorted(out)


def exists(s3, bucket, key):
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except s3.exceptions.ClientError:
        return False


def read_json_object(s3, bucket, key):
    return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())


def paths(cohort_prefix, subject, session, source_file):
    stem = f"{subject}_{session}"
    deriv = f"{cohort_prefix}/{DERIV}/{subject}/{stem}_task-psg_deriv-bdspsleepfeatures"
    return {
        "primary": f"{cohort_prefix}/primary/{subject}/{session}/psg/{stem}_task-psg_eeg.zarr",
        "derivatives": deriv + ".zarr",
        "summary_json": deriv + ".json",
        "source_h5": f"{cohort_prefix}/sourcedata/{subject}/{session}/{source_file}",
    }


def sample(args, s3):
    """Seeded subjects per cohort, one seeded session each, with their artifact keys."""
    bucket, root = split(args.root)
    selections, design = [], {}
    for cohort in args.cohorts:
        prefix = f"{root}/{cohort}"
        subjects = children(s3, bucket, f"{prefix}/primary")
        subjects = [s for s in subjects if s.startswith("sub-")]
        order = list(subjects)
        random.Random(f"{args.seed}:{cohort}").shuffle(order)
        examined, excluded, chosen = 0, Counter(), []
        for subject in order:
            if len(chosen) == args.per_cohort:
                break
            examined += 1
            sessions = [s for s in children(s3, bucket, f"{prefix}/primary/{subject}")
                        if s.startswith("ses-")]  # fmt: skip
            if not sessions:
                excluded["no primary session"] += 1
                continue
            session = random.Random(f"{args.seed}:{cohort}:{subject}").choice(sessions)
            keys = paths(prefix, subject, session, "")
            try:
                meta = read_json_object(s3, bucket, keys["primary"] + "/recording.json")
            except s3.exceptions.ClientError:
                excluded["no primary recording.json"] += 1
                continue
            source_file = (meta.get("source") or {}).get("file")
            keys = paths(prefix, subject, session, source_file or "")
            if not source_file or not exists(s3, bucket, keys["source_h5"]):
                excluded["no archived source H5"] += 1
                continue
            entry = {"cohort": cohort, "subject": subject, "session": session,
                     "keys": keys, "bucket": bucket}  # fmt: skip
            entry["has_derivatives"] = exists(s3, bucket, keys["derivatives"] + "/recording.json")
            entry["has_summary_json"] = exists(s3, bucket, keys["summary_json"])
            feature_file = None
            if entry["has_derivatives"]:
                deriv = read_json_object(s3, bucket, keys["derivatives"] + "/recording.json")
                feature_file = (deriv.get("source") or {}).get("feature_file")
                entry["derivative_feature_file"] = bool(feature_file)
            if cohort in args.feature_h5:  # the producer's name when no derivative names it
                feature_file = feature_file or f"{subject}_{session}_desc-sleepfeatures.h5"
                fbucket, fprefix = split(args.feature_h5[cohort])
                entry["feature_h5"] = {"bucket": fbucket,
                                       "key": f"{fprefix}/{subject}/{session}/{feature_file}"}
            entry["appended_signals"] = list((meta.get("source") or {}).get("pap_channels", []))
            entry["id"] = f"{cohort}-{len(chosen) + 1}"
            chosen.append(entry)
        selections += chosen
        design[cohort] = {"population_subjects": len(subjects), "examined": examined,
                          "selected": len(chosen), "excluded": dict(excluded)}  # fmt: skip
        print(f"{cohort}: {len(chosen)} selected from {examined} examined "
              f"({len(subjects)} subjects)", flush=True)  # fmt: skip
    return selections, design


def download_prefix(s3, bucket, prefix, target):
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix + "/"):
        keys += [(o["Key"], o["Size"]) for o in page.get("Contents", [])]
    if not keys:
        raise FileNotFoundError("empty prefix")

    def fetch(key):
        path = target / key[len(prefix) + 1 :]
        path.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(bucket, key, str(path))

    with ThreadPoolExecutor(16) as pool:
        for future in as_completed([pool.submit(fetch, key) for key, _ in keys]):
            future.result()
    return sum(size for _, size in keys)


def converter_checks(converter_src, files, entry, exported, exported_plain):
    """The converter's own verifier, with the rebuilt H5 standing in as the source H5."""
    if str(converter_src) not in sys.path:
        sys.path.insert(0, str(converter_src))
    import warnings

    import h5py
    from bdsp_psg_conversion.common.h5_reader import read_identity, read_summary, summary_group
    from bdsp_psg_conversion.zarr_json.recording_zarr import DERIVATIVE
    from bdsp_psg_conversion.zarr_json.verify import verify_features_json, verify_recording_zarr

    out = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        out["primary"] = verify_recording_zarr(files["primary"], exported_plain)
        if "derivatives" in files:
            if entry.get("derivative_feature_file"):
                out["derivatives"] = "not run: payload paths name a separate feature H5"
            else:
                out["derivatives"] = verify_recording_zarr(
                    files["derivatives"], exported_plain, DERIVATIVE
                )
        if "summary_json" in files:
            with h5py.File(exported, "r") as h5:
                identity = read_identity(h5)
                summary = read_summary(h5) if summary_group(h5) else []
            out["summary_json"] = verify_features_json(files["summary_json"], identity, summary)
    return out


def process(entry, options):
    """Download, rebuild, compare and clean up one recording; returns a result row."""
    from bdsp.h5_compare import compare_h5
    from bdsp.zarr_to_h5 import write_h5

    s3 = client(options["profile"])
    bucket = entry["bucket"]
    work = Path(options["output"]) / "work" / entry["id"]
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    row = {"id": entry["id"], "cohort": entry["cohort"], "subject": entry["subject"],
           "session": entry["session"], "started": utcnow()}  # fmt: skip
    timing, sizes, files = {}, {}, {}
    try:
        start = time.monotonic()
        for role in ("primary", "derivatives"):
            if role == "primary" or entry["has_derivatives"]:
                files[role] = work / entry["keys"][role].rsplit("/", 1)[-1]
                sizes[role] = download_prefix(s3, bucket, entry["keys"][role], files[role])
        singles = [("source_h5", bucket, entry["keys"]["source_h5"])]
        if entry["has_summary_json"]:
            singles.append(("summary_json", bucket, entry["keys"]["summary_json"]))
        if entry.get("feature_h5"):
            singles.append(("feature_h5", entry["feature_h5"]["bucket"], entry["feature_h5"]["key"]))
        for role, source_bucket, key in singles:
            files[role] = work / key.rsplit("/", 1)[-1]
            try:
                s3.download_file(source_bucket, key, str(files[role]))
            except Exception as err:  # a missing optional feature H5 is reported, not fatal
                if role != "feature_h5":
                    raise
                row["feature_h5_unavailable"] = type(err).__name__
                files.pop(role)
            else:
                sizes[role] = files[role].stat().st_size
        timing["download_s"] = time.monotonic() - start

        start = time.monotonic()
        exported = work / "rebuilt.h5"
        report = write_h5(exported, files["primary"], files.get("derivatives"),
                          files.get("summary_json"))  # fmt: skip
        timing["export_s"] = time.monotonic() - start
        sizes["rebuilt_h5"] = exported.stat().st_size
        row["export"] = {"signals_int16": report.signals_int16,
                         "signals_float32": report.signals_float32,
                         "annotation_tracks": report.annotation_tracks,
                         "feature_families": report.feature_families,
                         "summary_rows": report.summary_rows,
                         "summary_arrays": report.summary_arrays,
                         "notes": list(report.notes)}  # fmt: skip

        start = time.monotonic()
        row["comparison"] = compare_h5(files["source_h5"], exported,
                                       features=files.get("feature_h5"))  # fmt: skip
        timing["compare_s"] = time.monotonic() - start

        if options["converter_src"]:
            start = time.monotonic()
            plain = exported
            if entry["appended_signals"]:  # the converter checks appended channels elsewhere
                plain = work / "rebuilt_without_appended.h5"
                write_h5(plain, files["primary"], files.get("derivatives"),
                         files.get("summary_json"), include_appended_signals=False)  # fmt: skip
            row["converter_verify"] = converter_checks(
                options["converter_src"], files, entry, exported, plain
            )
            timing["converter_s"] = time.monotonic() - start
    except Exception as err:
        row["error"] = f"{type(err).__name__}: {err}"
        row["traceback"] = traceback.format_exc(limit=8)
    finally:
        if not options["keep_files"]:
            shutil.rmtree(work, ignore_errors=True)
    row.update(timing=timing, sizes=sizes, finished=utcnow())
    return row


def converter_passed(row):
    checks = row.get("converter_verify")
    if not checks:
        return None
    return all(value == [] for value in checks.values() if isinstance(value, list))


def summarize(rows, design, manifest):
    by_cohort = defaultdict(list)
    for row in rows:
        by_cohort[row["cohort"]].append(row)
    selected = {entry["id"]: entry for entry in manifest["selections"]}

    def aggregate(items):
        done = [r for r in items if "comparison" in r]
        out = {"recordings": len(items), "errors": sum("error" in r for r in items),
               "pass": sum(r["comparison"]["verdict"] == "pass" for r in done),
               "without_derivative_zarr": sum(not selected[r["id"]]["has_derivatives"]
                                              for r in items),
               "without_summary_json": sum(not selected[r["id"]]["has_summary_json"]
                                           for r in items)}  # fmt: skip
        failures, not_exported = Counter(), Counter()
        totals = Counter()
        for r in done:
            c = r["comparison"]
            failures.update({f.split(":")[0] for f in c["failures"]})
            s, a = c["signals"], c["annotations"]
            totals.update(signals=s["compared"], physical_equal=s["physical_equal"],
                          digital_equal=s["digital_equal"], int16=s["int16"],
                          calibration_equal=s["calibration_equal"],
                          appended_signals=len(s["appended_from_other_files"]),
                          annotation_tables=a["original"], annotation_equal=a["equal"])  # fmt: skip
            f = c["features"]
            if "time_series" in f:
                totals.update(families=f["time_series"]["original"],
                              families_equal=f["time_series"]["equal"])  # fmt: skip
            if "recording_summary" in f:
                rs = f["recording_summary"]
                totals.update(summary_rows=rs["rows"], summary_arrays=rs.get("arrays", 0),
                              summary_arrays_equal=rs.get("arrays_equal", 0))  # fmt: skip
            items_seen = {f"signal attr {n}" for n in s["attrs_not_exported"]}
            items_seen |= {f"annotation attr {n}" for n in a["attrs_not_exported"]}
            items_seen |= {f"root attr {n}" for n in c["not_exported"]["root_attrs"]}
            items_seen |= {f"feature root attr {n}" for n in f.get("root_attrs_not_exported", [])}
            items_seen |= {"node " + "/".join(p.split("/")[:4]).replace(
                f.get("original_root", "\0"), "<feature root>")
                for p in c["not_exported"]["nodes"]}  # fmt: skip
            not_exported.update(items_seen)
        out["failure_kinds"] = dict(sorted(failures.items()))
        out["totals"] = dict(totals)
        out["not_exported_in_recordings"] = dict(sorted(not_exported.items()))
        verdicts = [converter_passed(r) for r in done]
        out["converter_verify_pass"] = sum(v is True for v in verdicts)
        out["converter_verify_run"] = sum(v is not None for v in verdicts)
        for key in ("download_s", "export_s", "compare_s"):
            values = [r["timing"][key] for r in done if key in r.get("timing", {})]
            if values:
                out[f"median_{key}"] = round(statistics.median(values), 1)
        ratios = [r["sizes"]["rebuilt_h5"] / r["sizes"]["source_h5"]
                  for r in done if "rebuilt_h5" in r.get("sizes", {})]  # fmt: skip
        if ratios:
            out["median_size_ratio_rebuilt_to_source"] = round(statistics.median(ratios), 3)
        return out

    return {
        "created": utcnow(),
        "options": manifest["options"],
        "runtime": manifest["runtime"],
        "design": design,
        "overall": aggregate(rows),
        "cohorts": {cohort: aggregate(items) for cohort, items in sorted(by_cohort.items())},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=DEFAULT_ROOT, help="S3 prefix holding cohorts")
    parser.add_argument("--cohorts", nargs="+", default=list(COHORTS))
    parser.add_argument("--per-cohort", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20261127)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--profile")
    parser.add_argument("--converter-src", type=Path,
                        help="bdsp-psg-conversion src/ directory for its own verifier")  # fmt: skip
    parser.add_argument("--feature-h5", action="append", default=[], metavar="COHORT=S3_PREFIX",
                        help="separate feature-H5 run prefix for a cohort (e.g. I0007)")  # fmt: skip
    parser.add_argument("--keep-files", action="store_true")
    args = parser.parse_args(argv)
    args.feature_h5 = dict(item.split("=", 1) for item in args.feature_h5)
    args.output.mkdir(parents=True, exist_ok=True)
    args.output.chmod(0o700)

    options = {"root": args.root, "cohorts": args.cohorts, "per_cohort": args.per_cohort,
               "seed": args.seed, "feature_h5": args.feature_h5,
               "converter_src": str(args.converter_src.resolve()) if args.converter_src else None}  # fmt: skip
    manifest_path = args.output / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if manifest["options"] != options:
            sys.exit("options differ from the saved manifest; use a new --output")
    else:
        import bdsp

        selections, design = sample(args, client(args.profile))
        manifest = {"created": utcnow(), "options": options, "design": design,
                    "selections": selections,
                    "runtime": {"python": sys.version.split()[0], "bdsp": bdsp.__version__,
                                "bdsp_path": str(Path(bdsp.__file__).parent),
                                **{p: importlib.metadata.version(p)
                                   for p in ("zarr", "h5py", "numpy")}}}  # fmt: skip
        write_json(manifest_path, manifest)

    results_path = args.output / "recordings.jsonl"
    rows = [json.loads(line) for line in results_path.open()] if results_path.exists() else []
    done = {row["id"] for row in rows if "error" not in row}
    rows = [row for row in rows if row["id"] in done]
    pending = [e for e in manifest["selections"] if e["id"] not in done]
    run = {"output": str(args.output), "profile": args.profile,
           "converter_src": options["converter_src"], "keep_files": args.keep_files}  # fmt: skip
    print(f"{len(done)} done, {len(pending)} pending", flush=True)
    with ProcessPoolExecutor(max(1, args.workers)) as pool, results_path.open("a") as log:
        futures = {pool.submit(process, entry, run): entry["id"] for entry in pending}
        for future in as_completed(futures):
            row = future.result()
            log.write(json.dumps(row, allow_nan=False) + "\n")
            log.flush()
            rows.append(row)
            verdict = "error" if "error" in row else row["comparison"]["verdict"]
            converter = converter_passed(row)
            print(f"[{len(rows)}/{len(manifest['selections'])}] {row['id']}: {verdict}"
                  + ("" if converter is None else f", converter verify {'pass' if converter else 'fail'}"),
                  flush=True)  # fmt: skip
    summary = summarize(rows, manifest["design"], manifest)
    write_json(args.output / "summary.json", summary)
    overall = summary["overall"]
    print(json.dumps({k: overall[k] for k in ("recordings", "errors", "pass",
                      "converter_verify_pass", "converter_verify_run")}))  # fmt: skip
    return 0 if overall["pass"] == overall["recordings"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
