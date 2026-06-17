from __future__ import annotations

import json
import os
import shutil
import subprocess

import h5py
import numpy as np
import pandas as pd
import pytest

import bdsp
from bdsp import load_feature_dataset, load_feature_table, load_standardized_h5
from bdsp.compat.sleep_yoda import PSGReader


def _write_native_v11_h5(path):
    with h5py.File(path, "w") as h5:
        h5.attrs["duration_sec"] = 4.0
        h5.attrs["subject_id"] = "sub-test"
        signals = h5.create_group("signals")

        eeg = signals.create_dataset(
            "c3-m2",
            data=np.array([-1000, -500, 0, 500, 1000] * 4, dtype=np.int16),
        )
        eeg.attrs["fs"] = 5.0
        eeg.attrs["unit"] = "uV"
        eeg.attrs["phys_min"] = -100.0
        eeg.attrs["phys_max"] = 100.0
        eeg.attrs["dig_min"] = -1000.0
        eeg.attrs["dig_max"] = 1000.0
        eeg.attrs["type"] = "raw_reference"

        spo2 = signals.create_dataset(
            "spo2",
            data=np.array([96.0, 95.0, 94.0, 93.0], dtype=np.float32),
        )
        spo2.attrs["fs"] = 1.0
        spo2.attrs["unit"] = "%"

        annotations = h5.create_group("annotations")
        expert = annotations.create_group("expert_1")
        stage = expert.create_group("stage")
        stage.create_dataset("starts", data=np.array([0.0, 2.0], dtype=np.float32))
        stage.create_dataset("ends", data=np.array([2.0, 4.0], dtype=np.float32))
        stage.create_dataset("durations", data=np.array([2.0, 2.0], dtype=np.float32))
        stage.create_dataset("codes", data=np.array([5, 2], dtype=np.int16))
        stage.attrs["format"] = "event_table"
        stage.attrs["event_map"] = json.dumps({"5": "Wake", "2": "N2"})

        resp = expert.create_group("resp")
        resp.create_dataset("starts", data=np.array([1.0], dtype=np.float32))
        resp.create_dataset("durations", data=np.array([1.0], dtype=np.float32))
        resp.create_dataset("codes", data=np.array([4], dtype=np.int16))
        resp.attrs["format"] = "event_table"
        resp.attrs["event_map"] = json.dumps({"4": "Obstructive Hypopnea"})

        features = h5.create_group("features")
        spindles = features.create_group("spindles")
        spindles.attrs["detector"] = "synthetic"
        medium = spindles.create_group("medium")
        medium.create_dataset("starts", data=np.array([1.5], dtype=np.float32))
        medium.create_dataset("ends", data=np.array([2.0], dtype=np.float32))
        medium.create_dataset("durations", data=np.array([0.5], dtype=np.float32))
        medium.create_dataset("codes", data=np.array([1], dtype=np.int16))
        medium.attrs["event_map"] = json.dumps({"1": "spindle"})


def test_public_loader_reads_native_v11_event_tables_and_features(tmp_path):
    h5_path = tmp_path / "native.h5"
    _write_native_v11_h5(h5_path)

    signals, metadata, annotations, features, channel_info = load_standardized_h5(h5_path)

    assert metadata["subject_id"] == "sub-test"
    assert np.allclose(signals["c3-m2"][:5], [-100.0, -50.0, 0.0, 50.0, 100.0])
    assert np.allclose(signals["spo2"], [96.0, 95.0, 94.0, 93.0])

    stage = annotations["expert_1"]["stage"]
    assert stage["codes"].tolist() == [5, 2]
    assert stage["event_map"] == {5: "Wake", 2: "N2"}
    assert annotations["expert_1"]["resp"]["event_map"] == {4: "Obstructive Hypopnea"}

    assert features["spindles"]["medium"]["event_map"] == {1: "spindle"}
    assert features["spindles"]["__attrs__"] == {"detector": "synthetic"}
    assert set(channel_info["channel"]) == {"c3-m2", "spo2"}
    assert bool(channel_info.set_index("channel").loc["c3-m2", "like_edf"]) is True


def test_read_h5_returns_record_object(tmp_path):
    h5_path = tmp_path / "native.h5"
    _write_native_v11_h5(h5_path)

    record = bdsp.read_h5(h5_path)

    assert record.path == h5_path
    assert record.metadata["subject_id"] == "sub-test"
    assert np.allclose(record.signals["c3-m2"][:3], [-100.0, -50.0, 0.0])
    assert record.annotations["expert_1"]["stage"]["event_map"] == {5: "Wake", 2: "N2"}
    assert set(record.channel_info["channel"]) == {"c3-m2", "spo2"}


def test_read_dispatches_only_h5(tmp_path):
    h5_path = tmp_path / "native.h5"
    _write_native_v11_h5(h5_path)

    assert bdsp.read(h5_path).metadata["subject_id"] == "sub-test"

    with pytest.raises(ValueError, match="supports .h5/.hdf5 only"):
        bdsp.read(tmp_path / "native.edf")


def test_record_to_dataframe(tmp_path):
    h5_path = tmp_path / "native.h5"
    _write_native_v11_h5(h5_path)

    record = bdsp.read_h5(h5_path)
    xy = record.to_dataframe(resample_fs=2)

    assert xy.shape[0] == 8
    assert {"c3-m2", "spo2", "expert_1_stage", "expert_1_resp"}.issubset(xy.columns)
    assert xy["expert_1_stage"].tolist() == [5, 5, 5, 5, 2, 2, 2, 2]


def test_load_feature_dataset_returns_data_and_dataset_attrs(tmp_path):
    h5_path = tmp_path / "feature_dataset.h5"
    dtype = [
        ("onset_sample", "<i8"),
        ("duration_samples", "<i8"),
        ("amplitude_uv", "<f4"),
        ("channel", "S32"),
    ]
    with h5py.File(h5_path, "w") as h5:
        ds = h5.create_group("features").create_group("kcomplexes").create_dataset(
            "v07_kcomplex_e1_logistic_high_recall",
            data=np.zeros(1, dtype=dtype),
        )
        ds.attrs["schema_version"] = "sleep_yoda_event_features_v1"
        ds.attrs["fs_hz"] = 200.0

    out = load_feature_dataset(h5_path, "features/kcomplexes/v07_kcomplex_e1_logistic_high_recall")

    assert out["path"] == "/features/kcomplexes/v07_kcomplex_e1_logistic_high_recall"
    assert out["data"].dtype.names == ("onset_sample", "duration_samples", "amplitude_uv", "channel")
    assert out["__attrs__"]["schema_version"] == "sleep_yoda_event_features_v1"
    assert out["__attrs__"]["fs_hz"] == 200.0


def test_load_feature_table_returns_group_columns_and_attrs(tmp_path):
    h5_path = tmp_path / "feature_group.h5"
    with h5py.File(h5_path, "w") as h5:
        group = h5.create_group("features").create_group("k_complexes").create_group(
            "v07_kcomplex_e1_logistic_high_recall"
        )
        group.create_dataset("onset_sample", data=np.array([10], dtype=np.int64))
        group.create_dataset("start_sample", data=np.array([10], dtype=np.int64))
        group.create_dataset("end_sample", data=np.array([20], dtype=np.int64))
        group.create_dataset("onset", data=np.array([0.1], dtype=np.float64))
        group.create_dataset("duration", data=np.array([0.1], dtype=np.float64))
        group.create_dataset("starts", data=np.array([0.1], dtype=np.float64))
        group.create_dataset("ends", data=np.array([0.2], dtype=np.float64))
        group.create_dataset("channel", data=np.asarray([b"c4-m1"], dtype="S32"))
        group.attrs["schema_version"] = "sleep_yoda_event_features_v2"
        group.attrs["fs_hz"] = 100.0

    out = load_feature_table(h5_path, "features/k_complexes/v07_kcomplex_e1_logistic_high_recall")

    assert out["path"] == "/features/k_complexes/v07_kcomplex_e1_logistic_high_recall"
    assert out["columns"]["onset_sample"].tolist() == [10]
    assert out["columns"]["start_sample"].tolist() == [10]
    assert out["columns"]["end_sample"].tolist() == [20]
    assert out["__attrs__"]["schema_version"] == "sleep_yoda_event_features_v2"


def test_public_loader_like_mne_resamples_native_signals_and_event_tables(tmp_path):
    h5_path = tmp_path / "native.h5"
    _write_native_v11_h5(h5_path)

    xy, metadata = load_standardized_h5(h5_path, like_mne=True, resample_fs=2)

    assert metadata["resample_fs"] == 2
    assert metadata["n_samples"] == 8
    assert xy.shape[0] == 8
    assert {"c3-m2", "spo2", "expert_1_stage", "expert_1_resp"}.issubset(xy.columns)
    assert xy["expert_1_stage"].tolist() == [5, 5, 5, 5, 2, 2, 2, 2]
    assert xy["expert_1_resp"].tolist() == [0, 0, 4, 4, 0, 0, 0, 0]
    assert np.isclose(xy["spo2"].iloc[0], 96.0)
    assert np.isclose(xy["spo2"].iloc[-1], 93.0)


def test_public_loader_supports_legacy_dense_annotations(tmp_path):
    h5_path = tmp_path / "legacy.h5"
    with h5py.File(h5_path, "w") as h5:
        h5.attrs["duration_sec"] = 2.0
        h5.attrs["sampling_rate"] = 2.0
        signals = h5.create_group("signals")
        c3 = signals.create_dataset("c3-m2", data=np.array([1.0, 2.0, 3.0, 4.0]))
        c3.attrs["fs"] = 2.0
        c3.attrs["unit"] = "uV"
        annotations = h5.create_group("annotations")
        annotations.create_dataset("stage", data=np.array([0, 0, 2, 2], dtype=np.float32))
        annotations.create_dataset("resp", data=np.array([0, 4, 4, 0], dtype=np.float32))

    signals, _, annotations, _, _ = load_standardized_h5(h5_path)
    assert signals["c3-m2"].tolist() == [1.0, 2.0, 3.0, 4.0]
    assert annotations["legacy"]["stage"]["values"].tolist() == [0, 0, 2, 2]

    xy, _ = load_standardized_h5(h5_path, like_mne=True, resample_fs=2)
    assert xy["stage"].tolist() == [0, 0, 2, 2]
    assert xy["resp"].tolist() == [0, 4, 4, 0]


def test_sleep_yoda_compat_aliases_public_loader():
    from bdsp.compat.sleep_yoda import load_standardized_h5 as legacy_loader
    from bdsp.h5 import load_standardized_h5 as public_loader

    assert legacy_loader is public_loader


def test_psg_reader_keeps_gui_canonical_mappings_for_native_event_tables(tmp_path):
    h5_path = tmp_path / "reader_native.h5"
    with h5py.File(h5_path, "w") as h5:
        h5.attrs["duration_sec"] = 2.0
        signals = h5.create_group("signals")
        c3 = signals.create_dataset("c3-m2", data=np.zeros(2, dtype=np.float32))
        c3.attrs["fs"] = 1.0
        c3.attrs["unit"] = "uV"
        expert = h5.create_group("annotations").create_group("expert_1")

        stage = expert.create_group("stage")
        stage.create_dataset("starts", data=np.array([0.0, 0.5, 1.0, 1.5]))
        stage.create_dataset("ends", data=np.array([0.5, 1.0, 1.5, 2.0]))
        stage.create_dataset("durations", data=np.array([0.5, 0.5, 0.5, 0.5]))
        stage.create_dataset("codes", data=np.array([5, 4, 1, 0], dtype=np.int16))
        stage.attrs["format"] = "event_table"
        stage.attrs["event_map"] = json.dumps(
            {"5": "Wake", "4": "REM", "1": "N3", "0": "Unknown"}
        )

        resp = expert.create_group("resp")
        resp.create_dataset("starts", data=np.array([0.0]))
        resp.create_dataset("ends", data=np.array([1.0]))
        resp.create_dataset("durations", data=np.array([1.0]))
        resp.create_dataset("codes", data=np.array([5], dtype=np.int16))
        resp.attrs["format"] = "event_table"
        resp.attrs["event_map"] = json.dumps({"5": "Central Hypopnea"})

    with PSGReader(h5_path) as reader:
        assert reader.fs == 200.0
        stage = reader.get_annotation("stage", 0, reader.n_samples)
        resp = reader.get_annotation("resp", 0, reader.n_samples)

    assert stage[0] == 0
    assert stage[100] == 5
    assert stage[200] == 3
    assert stage[300] == 9
    assert np.all(resp[:200] == 6)
    assert np.all(resp[200:] == 0)


def test_open_h5_reads_signal_by_time(tmp_path):
    h5_path = tmp_path / "reader_native.h5"
    _write_native_v11_h5(h5_path)

    with bdsp.open_h5(h5_path) as reader:
        signal, times = reader.read_signal("c3-m2", start=1.0, duration=1.0)

    assert signal.shape == times.shape
    assert len(signal) == 200
    assert np.isclose(times[0], 1.0)
    assert np.isclose(times[-1], 1.0 + 199 / 200)


@pytest.mark.skipif(
    os.environ.get("SLEEP_YODA_RUN_AWS_H5_LOADER") != "1",
    reason="Set SLEEP_YODA_RUN_AWS_H5_LOADER=1 to download AWS H5 samples.",
)
def test_public_loader_reads_optional_aws_samples(tmp_path):
    if shutil.which("aws") is None:
        pytest.skip("aws CLI is not installed")

    master_csv = "/home/wolfgang/repos/morpheus-internal/masterfile-operations/master_morpheus.csv"
    if not os.path.exists(master_csv):
        pytest.skip("Morpheus master table is unavailable")

    cohorts = ["I0002", "I0003", "I0004", "I0006", "S0001"]
    sample_limit = int(os.environ.get("SLEEP_YODA_AWS_SAMPLE_LIMIT", "2"))
    df = pd.read_csv(master_csv, usecols=["cohort", "fileid", "s3_path", "s3_exists"])
    exists = df["s3_exists"].astype(str).str.lower().eq("true")
    df = df[df["cohort"].isin(cohorts) & exists]
    samples = df.groupby("cohort", sort=True).head(1).head(sample_limit)
    if samples.empty:
        pytest.skip("No AWS H5 samples found in master table")

    for row in samples.itertuples(index=False):
        target = tmp_path / f"{row.fileid}.h5"
        result = subprocess.run(
            ["aws", "s3", "cp", row.s3_path, str(target), "--only-show-errors"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip(f"Could not download AWS sample: {result.stderr.strip()}")

        signals, metadata, annotations, _, channel_info = load_standardized_h5(
            target,
            load_features=False,
        )
        assert signals
        assert metadata.get("duration_sec", 0) > 0
        assert not channel_info.empty
        assert isinstance(annotations, dict)
