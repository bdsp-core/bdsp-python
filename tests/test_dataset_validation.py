"""The acceptance audit must detect real damage without cloud credentials."""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from test_bids_zarr import artifacts  # Reuse independent synthetic export fixtures.

spec = importlib.util.spec_from_file_location(
    "dataset_audit", Path(__file__).parents[1] / "validation" / "run_audit.py"
)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


@pytest.fixture
def dataset(artifacts, tmp_path):
    root = tmp_path / "S0001"
    paths = audit.recording_paths("sub-synthetic", "ses-1")
    for role in ["primary", "derivatives", "summary_json"]:
        target = root / paths[role]
        target.parent.mkdir(parents=True, exist_ok=True)
        if role == "summary_json":
            shutil.copyfile(artifacts[role], target)
        else:
            shutil.copytree(artifacts[role], target)
    for role in ["sidecar", "sessions_json", "scans_json"]:
        (root / paths[role]).write_text("{}")
    (root / paths["sessions"]).write_text("session_id\nses-1\n")
    (root / paths["scans"]).write_text(
        "filename\npsg/sub-synthetic_ses-1_task-psg_eeg.zarr\n"
    )
    (root / paths["channels"]).write_text(
        "name\tsampling_frequency\tunits\neeg\t4\tµV\nspo2\t1\t%\n"
    )
    (root / paths["source_h5"]).parent.mkdir(parents=True)
    (root / paths["source_h5"]).touch()  # Only existence is tested, never H5 parity.
    (root / "dataset_description.json").write_text(
        json.dumps({"Name": "Synthetic", "BIDSVersion": "1.11.1"})
    )
    (root / "participants.tsv").write_text("participant_id\nsub-synthetic\n")
    (root / "participants.json").write_text("{}")
    (root / "README").write_text("Synthetic dataset")
    (root / ".bidsignore").write_text("psg/\n")
    derivative = root / "derivatives/bdsp_sleep_features"
    (derivative / "dataset_description.json").write_text("{}")
    (derivative / "S0001.parquet").touch()  # No claim of validating Parquet contents.
    return audit.Dataset(root), paths


def run(dataset):
    return audit.audit_recording(dataset, {"subject": "sub-synthetic", "session": "ses-1"})


def test_sample_is_reproducible_without_replacement_or_listing_order_bias():
    population = [f"sub-{i}" for i in range(1000)]
    sample = audit.sample_subjects(population, 200, 42)
    assert sample == audit.sample_subjects(reversed(population), 200, 42)
    assert len(set(sample)) == 200
    assert sample != audit.sample_subjects(population, 200, 43)
    assert audit.select_session(["ses-1", "ses-2"], "sub-x", 42) == audit.select_session(
        ["ses-2", "ses-1"], "sub-x", 42
    )


def test_audit_reads_real_payloads_and_detects_metadata_gaps(dataset):
    source, paths = dataset
    result = run(source)
    assert result["passed"], result
    assert result["counts"]["signal_windows"] == 6
    assert result["counts"]["annotation_reads"] == 3
    assert result["counts"]["temporal_families"] == 3
    assert result["bytes_read"] > 0
    global_result, subjects = audit.global_checks(source)
    assert not global_result["issues"]
    assert subjects == ["sub-synthetic"]
    (source.path / "participants.tsv").write_text("participant_id\nsub-other\n")
    global_result, _ = audit.global_checks(source)
    assert global_result["primary_subjects_missing_from_participants"] == 1
    (source.path / paths["scans"]).write_text("filename\npsg/obsolete.edf\n")
    result = run(source)
    assert not result["passed"]
    assert "primary_zarr_not_in_scans_table" in result["layout_issues"]


def test_strict_failure_is_not_hidden_by_successful_diagnostic_open(dataset):
    source, paths = dataset
    (source.path / paths["summary_json"]).unlink()
    result = run(source)
    assert not result["strict_open"] and not result["passed"]
    assert result["diagnostic_open"]
    assert result["strict_error"]["type"] == "IncompleteRecordingError"


def test_missing_chunk_is_not_mistaken_for_successful_fill_value_read(dataset):
    source, paths = dataset
    (source.path / paths["primary"] / "0/raw/c/0").unlink()
    result = run(source)
    assert result["strict_open"]
    assert not result["passed"]
    assert result["missing_sampled_chunks"]["primary"] == ["0/raw/c/0"]


def test_range_reads_and_read_only_store(dataset):
    from zarr.abc.store import RangeByteRequest, SuffixByteRequest
    from zarr.core.buffer import default_buffer_prototype
    from zarr.core.sync import sync

    source, paths = dataset
    key = paths["primary"] + "/recording.json"
    full = source.read(key)[0]
    assert source.read(key, byte_range=RangeByteRequest(2, 9))[0] == full[2:9]
    assert source.read(key, byte_range=SuffixByteRequest(7))[0] == full[-7:]
    store = audit.ReadStore(source, paths["primary"], 1024 * 1024)
    assert sync(store.get("recording.json", byte_range=SuffixByteRequest(7))).to_bytes() == full[-7:]
    before = store.bytes_read
    sync(store.get("recording.json", byte_range=SuffixByteRequest(7)))
    assert store.bytes_read == before == 7
    with pytest.raises(ValueError, match="read-only"):
        sync(store.set("recording.json", default_buffer_prototype().buffer.from_bytes(b"bad")))
    assert source.read(key)[0] == full


def test_budget_exhaustion_is_reported_as_failure(dataset):
    source, _ = dataset
    result = audit.audit_recording(source, {"subject": "sub-synthetic", "session": "ses-1"}, max_mib=0)
    assert not result["passed"]
    assert result["read_errors"]


def test_cli_checkpoints_resumes_and_rejects_changed_options(dataset, tmp_path):
    source, _ = dataset
    output = tmp_path / "private-results"
    args = [source.root, "--output", str(output), "--subjects", "1", "--workers", "1"]
    assert audit.main(args) == 0
    saved = (output / "recordings.jsonl").read_bytes()
    assert len(saved.splitlines()) == 1
    assert audit.main(args) == 0
    assert (output / "recordings.jsonl").read_bytes() == saved
    with pytest.raises(SystemExit, match="2"):
        audit.main(args + ["--seed", "7"])


def test_parallel_cli_spawns_independent_readers(dataset, tmp_path):
    source, _ = dataset
    output = tmp_path / "parallel-results"
    completed = subprocess.run(
        [sys.executable, str(Path(audit.__file__)), source.root, "--output", str(output),
         "--subjects", "1", "--workers", "2"],
        capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads((output / "summary.json").read_text())["accepted"]
