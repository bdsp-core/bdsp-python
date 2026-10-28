"""Read-only, resumable dataset acceptance checks; see validation/README.md."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
import csv
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import io
import json
import math
import multiprocessing
from pathlib import Path
import random
import subprocess
import sys
import time
from urllib.parse import urlparse

import numpy as np
from zarr.core.buffer import default_buffer_prototype
from zarr.abc.store import OffsetByteRequest, RangeByteRequest, SuffixByteRequest
from zarr.storage import MemoryStore

from bdsp.bids_zarr import open_recording


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


class Dataset:
    """Local directory or authorized S3 prefix; offers no remote mutations."""

    def __init__(self, root, profile=None):
        self.root = str(root).rstrip("/")
        self.profile = profile
        self.client = None
        if self.root.startswith("s3://"):
            import boto3
            from botocore.config import Config

            url = urlparse(self.root)
            self.bucket, self.prefix = url.netloc, url.path.strip("/") + "/"
            self.client = boto3.Session(profile_name=profile).client(
                "s3", config=Config(connect_timeout=10, read_timeout=30,
                                    retries={"max_attempts": 2}, max_pool_connections=32)
            )
        else:
            self.path = Path(root).resolve(strict=True)
            self.root = str(self.path)

    def __getstate__(self):
        # Each spawned worker owns its client and Zarr event loop. Shared
        # threads contend on Zarr's synchronous bridge during large audits.
        return self.root, self.profile

    def __setstate__(self, state):
        self.__init__(*state)

    def children(self, prefix=""):
        prefix = prefix.strip("/")
        if self.client is None:
            path = self.path / prefix
            return sorted(p.name for p in path.iterdir()) if path.is_dir() else []
        full = self.prefix + (prefix + "/" if prefix else "")
        result = set()
        for page in self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=full, Delimiter="/"
        ):
            result.update(x["Prefix"][len(full):].rstrip("/")
                          for x in page.get("CommonPrefixes", []))
            result.update(x["Key"][len(full):] for x in page.get("Contents", [])
                          if x["Key"] != full)
        return sorted(result)

    def exists(self, key):
        if self.client is None:
            return (self.path / key).is_file()
        try:
            self.client.head_object(Bucket=self.bucket, Key=self.prefix + key)
            return True
        except self.client.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] in {"NoSuchKey", "404", "NotFound"}:
                return False
            raise

    def objects(self, prefix):
        """List one store's object names, without downloading signal bodies."""
        prefix = prefix.rstrip("/") + "/"
        if self.client is None:
            base = self.path / prefix
            return {str(p.relative_to(base)): p.stat().st_size
                    for p in base.rglob("*") if p.is_file()}
        full = self.prefix + prefix
        return {item["Key"][len(full):]: item["Size"]
                for page in self.client.get_paginator("list_objects_v2").paginate(
                    Bucket=self.bucket, Prefix=full)
                for item in page.get("Contents", []) if item["Key"] != full}

    def read(self, key, max_bytes=None, byte_range=None):
        """Return bytes and an object identity receipt, or None only if absent."""
        if self.client is None:
            path = self.path / key
            try:
                size = path.stat().st_size
                start, end = 0, size
                if isinstance(byte_range, RangeByteRequest):
                    start, end = byte_range.start, min(size, byte_range.end)
                elif isinstance(byte_range, OffsetByteRequest):
                    start = byte_range.offset
                elif isinstance(byte_range, SuffixByteRequest):
                    start = max(0, size - byte_range.suffix)
                if max_bytes is not None and end - start > max_bytes:
                    raise RuntimeError("Object exceeds remaining transfer budget")
                with path.open("rb") as stream:
                    stream.seek(start)
                    data = stream.read(end - start)
            except (FileNotFoundError, NotADirectoryError):
                return None
            return data, {"sha256": hashlib.sha256(data).hexdigest(),
                          "bytes": len(data), "mtime_ns": path.stat().st_mtime_ns}
        request = {"Bucket": self.bucket, "Key": self.prefix + key}
        if isinstance(byte_range, RangeByteRequest):
            request["Range"] = f"bytes={byte_range.start}-{byte_range.end - 1}"
        elif isinstance(byte_range, OffsetByteRequest):
            request["Range"] = f"bytes={byte_range.offset}-"
        elif isinstance(byte_range, SuffixByteRequest):
            request["Range"] = f"bytes=-{byte_range.suffix}"
        try:
            response = self.client.get_object(**request)
        except self.client.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] in {"NoSuchKey", "404", "NotFound"}:
                return None
            raise
        with response["Body"] as stream:
            if max_bytes is not None and response["ContentLength"] > max_bytes:
                raise RuntimeError("Object exceeds remaining transfer budget")
            data = stream.read()
        return data, {"etag": response.get("ETag"),
                      "version_id": response.get("VersionId"), "bytes": len(data),
                      "last_modified": response["LastModified"].isoformat(),
                      "content_range": response.get("ContentRange")}

    def json(self, key):
        result = self.read(key)
        if result is None:
            raise FileNotFoundError(key)
        return json.loads(result[0]), result[1]

    def table(self, key):
        result = self.read(key)
        if result is None:
            raise FileNotFoundError(key)
        return list(csv.DictReader(io.StringIO(result[0].decode("utf-8-sig")),
                                   delimiter="\t"))


class ReadStore(MemoryStore):
    """Read-only Zarr Store with per-recording caching and read receipts.

    The cache is populated only by Dataset.read(), never by writes to the input.
    Missing sampled chunks are recorded: Zarr can otherwise return fill values.
    """

    def __init__(self, dataset, prefix, budget):
        self.cache = {}
        super().__init__(self.cache, read_only=True)
        self.dataset, self.prefix, self.budget = dataset, prefix.rstrip("/") + "/", budget
        self.receipts, self.absent, self.directories = {}, set(), {}
        self.ranges = {}
        self.inventory = None
        self.bytes_read = 0

    def warm_metadata(self):
        # A flat store listing also supplies directory names. Parallel metadata
        # GETs avoid thousands of serial round trips during strict validation.
        sizes = self.dataset.objects(self.prefix)
        self.inventory = set(sizes)
        directories = {}
        for key in self.inventory:
            parts = key.split("/")
            for index, name in enumerate(parts):
                directories.setdefault("/".join(parts[:index]), set()).add(name)
        self.directories = {key: sorted(names) for key, names in directories.items()}
        metadata = sorted(k for k in self.inventory if k.endswith("zarr.json") or k == "recording.json")
        # Small columns are cheap to fetch together. Large shards still use
        # range requests only; bounded prefetch is counted in transfer totals.
        small = sorted(k for k in self.inventory - set(metadata) if sizes[k] <= 65536)
        prefetch = []
        prefetched_bytes = 0
        for key in small:
            if prefetched_bytes + sizes[key] <= min(8 * 1024**2, self.budget // 2):
                prefetch.append(key)
                prefetched_bytes += sizes[key]
        metadata += prefetch
        with ThreadPoolExecutor(max_workers=12) as pool:
            for key, result in zip(metadata, pool.map(lambda k: self.dataset.read(self.prefix + k), metadata)):
                if result is None:
                    self.absent.add(key)
                    continue
                data, receipt = result
                self.bytes_read += len(data)
                if self.bytes_read > self.budget:
                    raise RuntimeError("Metadata exceeds per-store transfer budget")
                self.cache[key] = default_buffer_prototype().buffer.from_bytes(data)
                self.receipts[key] = receipt

    async def get(self, key, prototype=None, byte_range=None):
        prototype = prototype or default_buffer_prototype()
        if self.inventory is not None and key not in self.inventory:
            self.absent.add(key)
        if key in self.cache or key in self.absent:
            return await super().get(key, prototype, byte_range)
        if byte_range is not None:
            cache_key = (key, byte_range)
            if cache_key not in self.ranges:
                result = await asyncio.to_thread(self.dataset.read, self.prefix + key,
                                                 self.budget - self.bytes_read, byte_range)
                if result is None:
                    self.absent.add(key)
                    return None
                data, receipt = result
                self.bytes_read += len(data)
                if self.bytes_read > self.budget:
                    raise RuntimeError("Per-store transfer budget exceeded; result is incomplete")
                self.receipts[key + "@" + repr(byte_range)] = receipt
                self.ranges[cache_key] = prototype.buffer.from_bytes(data)
            return prototype.buffer.from_buffer(self.ranges[cache_key])
        if key not in self.cache and key not in self.absent:
            result = await asyncio.to_thread(self.dataset.read, self.prefix + key,
                                             self.budget - self.bytes_read)
            if result is None:
                self.absent.add(key)
            else:
                data, receipt = result
                self.bytes_read += len(data)
                if self.bytes_read > self.budget:
                    raise RuntimeError("Per-store transfer budget exceeded; result is incomplete")
                self.receipts[key] = receipt
                self.cache[key] = prototype.buffer.from_bytes(data)
        return await super().get(key, prototype, byte_range)

    async def exists(self, key):
        return await self.get(key) is not None

    async def list_dir(self, prefix):
        if prefix not in self.directories:
            if self.inventory is not None:
                return
            self.directories[prefix] = await asyncio.to_thread(
                self.dataset.children, self.prefix + prefix
            )
        for name in self.directories[prefix]:
            yield name


def sample_subjects(subjects, count, seed):
    subjects = sorted(set(subjects))
    if count < 1 or count > len(subjects):
        raise ValueError(f"Requested {count} subjects from a population of {len(subjects)}")
    return sorted(random.Random(seed).sample(subjects, count))


def select_session(sessions, subject, seed):
    sessions = sorted(set(sessions))
    if not sessions:
        raise ValueError("Selected subject has no session directories")
    digest = hashlib.sha256(f"{seed}:{subject}".encode()).digest()
    return random.Random(int.from_bytes(digest, "big")).choice(sessions)


def global_checks(dataset):
    root = dataset.children()
    subjects = {x for x in dataset.children("primary") if x.startswith("sub-")}
    derivative_subjects = {x for x in dataset.children("derivatives/bdsp_sleep_features")
                           if x.startswith("sub-")}
    source_subjects = {x for x in dataset.children("sourcedata") if x.startswith("sub-")}
    required = ["dataset_description.json", "README", ".bidsignore",
                "participants.tsv", "participants.json"]
    missing = [x for x in required if x not in root]
    cohort = dataset.root.rsplit("/", 1)[-1]
    for name in ["dataset_description.json", f"{cohort}.parquet"]:
        key = "derivatives/bdsp_sleep_features/" + name
        if not dataset.exists(key):
            missing.append(key)
    issues = ["missing_dataset_files"] if missing else []
    participant_ids = set()
    participant_rows = 0
    try:
        rows = dataset.table("participants.tsv")
        participant_rows = len(rows)
        participant_ids = {r["participant_id"] for r in rows}
        if not subjects.issubset(participant_ids):
            issues.append("participants_missing_primary_subjects")
        if len(participant_ids) != len(rows):
            issues.append("duplicate_participants")
    except (FileNotFoundError, KeyError, UnicodeError, csv.Error):
        issues.append("invalid_participants_table")
    try:
        description, _ = dataset.json("dataset_description.json")
        if not str(description.get("Name", "")).strip():
            issues.append("blank_dataset_name")
        if not description.get("BIDSVersion"):
            issues.append("missing_bids_version")
    except (FileNotFoundError, ValueError):
        issues.append("invalid_dataset_description")
    legacy = [x for x in root if x.startswith("sub-")]
    if legacy:
        issues.append("legacy_subject_directories_at_root")
    if subjects - derivative_subjects:
        issues.append("primary_subjects_without_derivative_directory")
    if subjects - source_subjects:
        issues.append("primary_subjects_without_source_directory")
    return {
        "checked_at": utcnow(), "primary_subjects": len(subjects),
        "derivative_subjects": len(derivative_subjects),
        "source_subjects": len(source_subjects), "legacy_root_subjects": len(legacy),
        "participant_rows": participant_rows,
        "primary_subjects_missing_from_participants": len(subjects - participant_ids),
        "participants_without_primary_subject": len(participant_ids - subjects),
        "primary_subjects_without_derivative_directory": len(subjects - derivative_subjects),
        "primary_subjects_without_source_directory": len(subjects - source_subjects),
        "missing_files": missing, "issues": issues,
    }, sorted(subjects)


def recording_paths(subject, session):
    stem = f"{subject}_{session}_task-psg"
    primary = f"primary/{subject}/{session}/psg/{stem}"
    derivative = f"derivatives/bdsp_sleep_features/{subject}/{stem}_deriv-bdspsleepfeatures"
    return {"primary": primary + "_eeg.zarr", "derivatives": derivative + ".zarr",
            "summary_json": derivative + ".json", "channels": primary + "_channels.tsv",
            "sidecar": primary + "_eeg.json",
            "sessions": f"primary/{subject}/{subject}_sessions.tsv",
            "sessions_json": f"primary/{subject}/{subject}_sessions.json",
            "scans": f"primary/{subject}/{session}/{subject}_{session}_scans.tsv",
            "scans_json": f"primary/{subject}/{session}/{subject}_{session}_scans.json",
            "source_h5": f"sourcedata/{subject}/{session}/{subject}_{session}.h5"}


def audit_recording(dataset, item, window_s=1.0, max_mib=256):
    started = time.monotonic()
    paths = recording_paths(item["subject"], item["session"])
    result = {**item, "checked_at": utcnow(), "layout_issues": [], "read_errors": [],
              "strict_open": False, "diagnostic_open": False, "counts": {}, "receipts": {}}
    stores = {}

    def issue(code):
        result["layout_issues"].append(code)

    def checked(label, function):
        try:
            return function()
        except Exception as exc:
            result["read_errors"].append({"check": label, "type": type(exc).__name__,
                                          "message": str(exc)})
            return None

    def layout_table(role):
        try:
            return dataset.table(paths[role])
        except (FileNotFoundError, KeyError, UnicodeError, csv.Error):
            issue("missing_or_invalid_" + role + "_table")
            return None

    def run():
        psg_files = dataset.children(f"primary/{item['subject']}/{item['session']}/psg")
        for role in ["primary", "sidecar", "channels"]:
            if paths[role].rsplit("/", 1)[-1] not in psg_files:
                issue("missing_" + role)
        if any(x.lower().endswith((".h5", ".edf")) for x in psg_files):
            issue("archival_input_in_primary")
        if any("caisr" in x.lower() for x in psg_files):
            issue("model_annotation_csv_in_primary")
        for role in ["sidecar", "sessions_json", "scans_json"]:
            if not dataset.exists(paths[role]):
                issue("missing_" + role)
        sessions = layout_table("sessions")
        if sessions is not None and item["session"] not in {r.get("session_id") for r in sessions}:
            issue("session_not_in_sessions_table")
        scans = layout_table("scans")
        expected_scan = "psg/" + paths["primary"].rsplit("/", 1)[-1]
        if scans is not None and expected_scan not in {r.get("filename", "").rstrip("/") for r in scans}:
            issue("primary_zarr_not_in_scans_table")
        sources = dataset.children(f"sourcedata/{item['subject']}/{item['session']}")
        if paths["source_h5"].rsplit("/", 1)[-1] not in sources:
            issue("missing_archived_source_h5")
        summary = None
        if paths["summary_json"].rsplit("/", 1)[-1] in dataset.children(
            f"derivatives/bdsp_sleep_features/{item['subject']}"
        ):
            summary, receipt = dataset.json(paths["summary_json"])
            result["receipts"]["summary_json"] = receipt
        else:
            issue("missing_summary_json")
        for role in ["primary", "derivatives"]:
            stores[role] = ReadStore(dataset, paths[role], max_mib * 1024**2)
            stores[role].warm_metadata()
        kwargs = {**stores, "summary_json": summary}
        record = None
        try:
            record = open_recording(**kwargs)
            result["strict_open"] = True
        except Exception as exc:
            result["strict_error"] = {"type": type(exc).__name__, "message": str(exc)}
            # Diagnostic reads never turn a strict failure into a success.
            diagnostic = dict(kwargs, require_complete=False)
            if awaitable_missing(stores["derivatives"], "zarr.json"):
                diagnostic["derivatives"] = None
                issue("missing_derivatives")
            record = checked("diagnostic_open", lambda: open_recording(**diagnostic))
            result["diagnostic_open"] = record is not None
        if record is None:
            return
        with record:
            metadata = record.metadata["primary"]
            if (metadata["subject"]["id"], metadata["session"]["id"]) != (item["subject"], item["session"]):
                issue("path_metadata_identity_mismatch")
            result["availability"] = record.availability
            counts = result["counts"]
            counts.update(signals=len(record.signals),
                          annotation_tracks=sum(map(len, record.annotations.values())),
                          feature_projections=len(record.features.projections),
                          temporal_families=len(record.features.time_series),
                          summary_features=len(record.features.summary),
                          vectors=len(record.features.vectors))
            counts.update(signal_windows=0, annotation_reads=0, projection_reads=0,
                          temporal_reads=0, all_nan_signal_windows=0)
            table = layout_table("channels")
            if table is not None:
                by_name = {r.get("name"): r for r in table}
                if set(by_name) != set(record.signals) or len(by_name) != len(table):
                    issue("channels_table_signal_inventory_mismatch")
                for name, channel in record.signals.items():
                    if name in by_name:
                        try:
                            if not math.isclose(float(by_name[name]["sampling_frequency"]),
                                                channel.rate_hz, rel_tol=1e-6):
                                issue("channels_table_sampling_rate_mismatch")
                            units = {"uv": "µV", "percent": "%", "": "n/a"}
                            unit = channel.metadata.get("unit", "")
                            if by_name[name].get("units") != units.get(unit.lower(), unit):
                                issue("channels_table_unit_mismatch")
                        except (ValueError, KeyError):
                            issue("channels_table_missing_sampling_frequency")
            for name, channel in record.signals.items():
                def signals():
                    n = channel.samples.shape[0]
                    if not n:
                        raise ValueError("Physiological signal has no samples")
                    width = min(n, max(1, int(window_s * channel.rate_hz)))
                    for first in sorted({0, max(0, (n - width) // 2), n - width}):
                        start = channel.offset_s + first / channel.rate_hz
                        window = channel.read(start=start, duration=width / channel.rate_hz)
                        # Floating-point clock arithmetic can shift a boundary by one sample.
                        if abs(len(window.values) - width) > 1 or not len(window.values):
                            raise ValueError("Signal window has unexpected sample count")
                        if not np.isfinite(window.times_s).all() or np.any(np.diff(window.times_s) <= 0):
                            raise ValueError("Signal clock is invalid")
                        if np.isinf(window.values).any():
                            raise ValueError("Infinite physiological samples")
                        if np.isnan(window.values).all():
                            counts["all_nan_signal_windows"] += 1
                        counts["signal_windows"] += 1
                checked("signal:" + name, signals)
            for scorer, tracks in record.annotations.items():
                for name, channel in tracks.items():
                    if checked(f"annotation:{scorer}/{name}", channel.read) is not None:
                        counts["annotation_reads"] += 1
            for name, channel in record.features.projections.items():
                read = (channel.read if channel.kind == "event" else
                        lambda c=channel: c.read(start=c.offset_s, duration=window_s))
                if checked("projection:" + name, read) is not None:
                    counts["projection_reads"] += 1
            for name, family in record.features.time_series.items():
                for position in sorted({0, max(0, family.row_count // 2), max(0, family.row_count - 1)}):
                    if checked("temporal:" + name, lambda p=position: family.read(rows=slice(p, p + 1))) is not None:
                        counts["temporal_reads"] += 1
            checked("summary_dataframe", record.features.summary.to_dataframe)

    try:
        checked("recording", run)
    finally:
        result["missing_sampled_chunks"] = {}
        result["bytes_read"] = sum(store.bytes_read for store in stores.values())
        for role, store in stores.items():
            result["receipts"][role] = store.receipts
            # Zarr v3's default chunk key encoding: metadata probes are excluded.
            missing = sorted(k for k in store.absent if "/c/" in k or k.endswith("/c"))
            if missing:
                result["missing_sampled_chunks"][role] = missing
            store.close()
        result["layout_issues"] = sorted(set(result["layout_issues"]))
        result["passed"] = (result["strict_open"] and not result["layout_issues"]
                            and not result["read_errors"] and not result["missing_sampled_chunks"])
        result["elapsed_s"] = round(time.monotonic() - started, 3)
    return result


def awaitable_missing(store, key):
    from zarr.core.sync import sync
    return sync(store.get(key)) is None


def summarize(manifest, global_result, results):
    counts = Counter()
    issues = Counter()
    strict_errors = Counter()
    for result in results:
        counts.update(result["counts"])
        issues.update(result["layout_issues"])
        if "strict_error" in result:
            strict_errors[result["strict_error"]["type"]] += 1
    finished = len(results) == len(manifest["sample"])
    return {"updated_at": utcnow(), "sample_size": len(manifest["sample"]),
            "completed": len(results), "finished": finished,
            "passed": sum(r["passed"] for r in results),
            "strict_open_passed": sum(r["strict_open"] for r in results),
            "records_with_read_errors": sum(bool(r["read_errors"]) for r in results),
            "records_with_missing_sampled_chunks": sum(bool(r["missing_sampled_chunks"]) for r in results),
            "strict_error_types": dict(strict_errors), "layout_issue_counts": dict(issues),
            "read_counts": dict(counts), "bytes_read": sum(r["bytes_read"] for r in results),
            "global": global_result,
            "accepted": finished and not global_result["issues"] and all(r["passed"] for r in results)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", help="Local cohort directory or s3://bucket/PSG/bids/COHORT")
    parser.add_argument("--output", type=Path, required=True, help="Private, Git-ignored output directory")
    parser.add_argument("--subjects", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20261028)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--window-s", type=float, default=1.0)
    parser.add_argument("--max-mib", type=int, default=256, help="Transfer ceiling per Zarr store")
    parser.add_argument("--profile")
    args = parser.parse_args(argv)
    if args.workers < 1 or args.max_mib < 1 or not math.isfinite(args.window_s) or args.window_s <= 0:
        parser.error("workers, max-mib, and window-s must be positive")
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    dataset = Dataset(args.root, args.profile)
    manifest_path = args.output / "manifest.json"
    options = {"root": dataset.root, "subjects": args.subjects, "seed": args.seed,
               "window_s": args.window_s, "max_mib": args.max_mib,
               "audit_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "reader_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in sorted(Path(open_recording.__code__.co_filename).parent.glob("*.py"))}}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if manifest["options"] != options:
            parser.error("Resume options or audit code differ from the saved manifest; use a new output")
        global_result = json.loads((args.output / "global.json").read_text())
    else:
        global_result, subjects = global_checks(dataset)
        selected = sample_subjects(subjects, args.subjects, args.seed)
        sample = []
        for subject in selected:
            sessions = [x for x in dataset.children("primary/" + subject) if x.startswith("ses-")]
            sample.append({"subject": subject, "session": select_session(sessions, subject, args.seed)})
        revision = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
        manifest = {"created_at": utcnow(), "options": options, "population": len(subjects),
                    "population_sha256": hashlib.sha256("\n".join(subjects).encode()).hexdigest(),
                    "selection": "uniform subjects without replacement; one uniform session per subject",
                    "execution_workers": args.workers,
                    "execution_mode": "processes" if args.workers > 1 else "single_worker",
                    "reader_git_revision": revision.stdout.strip(), "reader_source": str(Path(open_recording.__code__.co_filename).resolve()),
                    "runtime": {"python": sys.version, **{p: importlib.metadata.version(p)
                                 for p in ["bdsp", "zarr", "numpy", "pandas"]}}, "sample": sample}
        write_json(args.output / "global.json", global_result)
        write_json(manifest_path, manifest)
    results = []
    results_path = args.output / "recordings.jsonl"
    if results_path.exists():
        # A crash may leave only the last line partial; preserve it separately.
        lines = results_path.read_text().splitlines(keepends=True)
        good = []
        for index, line in enumerate(lines):
            try:
                result = json.loads(line)
            except ValueError:
                if index != len(lines) - 1:
                    raise
                (args.output / "interrupted-tail.txt").write_text(line)
                break
            results.append(result)
            good.append(line.rstrip("\n") + "\n")
        results_path.write_text("".join(good))
    done = {(r["subject"], r["session"]) for r in results}
    remaining = [x for x in manifest["sample"] if (x["subject"], x["session"]) not in done]
    print(json.dumps({"selected": len(manifest["sample"]), "previously_completed": len(done),
                      "global_issues": global_result["issues"]}), flush=True)
    pool = (ProcessPoolExecutor(max_workers=args.workers,
                                mp_context=multiprocessing.get_context("spawn"))
            if args.workers > 1 else ThreadPoolExecutor(max_workers=1))
    with results_path.open("a") as stream, pool:
        futures = [pool.submit(audit_recording, dataset, item, args.window_s, args.max_mib) for item in remaining]
        for future in as_completed(futures):
            result = future.result()
            stream.write(json.dumps(result, allow_nan=False) + "\n")
            stream.flush()
            results.append(result)
            summary = summarize(manifest, global_result, results)
            write_json(args.output / "summary.json", summary)
            print(json.dumps({k: summary[k] for k in ["completed", "sample_size", "strict_open_passed", "records_with_read_errors"]}), flush=True)
    summary = summarize(manifest, global_result, results)
    write_json(args.output / "summary.json", summary)
    return 0 if summary["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
