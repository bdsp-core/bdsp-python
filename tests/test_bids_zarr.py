"""Independent synthetic contracts: no converter or private dataset dependency."""

from __future__ import annotations

import copy
import json

import numpy as np
import pytest

zarr = pytest.importorskip("zarr", minversion="3.1")

from bdsp.bids_zarr import FormatError, IncompleteRecordingError, open_primary, open_recording

SCHEMA = "bdsp.sleep-feature-payload.v1"


def array(group, name, data, dtype=None):
    values = np.asarray(data, dtype=dtype)
    return group.create_array(name, data=values)


def categorical(group, name, codes, labels):
    array(group, name + "_code", codes, "int32")
    group.create_array(name + "_labels", shape=(len(labels),), dtype="str")[:] = labels


def channel(root, key, name, kind, origin, **metadata):
    return root.create_group(
        key,
        attributes={
            "tsz": {
                "name": name,
                "id": name,
                "kind": kind,
                "provenance": {"origin": origin, **metadata.pop("provenance", {})},
                **metadata,
            }
        },
    )


def events(root, key, scorer, kind, starts, durations):
    group = channel(
        root,
        key,
        f"{scorer}/{kind}",
        "event",
        "annotation",
        label_names=["Wake", "N2"],
        provenance={"annotator": scorer, "label_codes": [5, 2]},
    )
    array(group, "events", starts, "int64")
    array(group, "durations", durations, "int64")
    array(group, "labels", [0, 1], "uint16")
    return group


def feature(root, key, family, storage, facet=None):
    group = channel(
        root,
        key,
        facet or family,
        "event",
        "feature",
        provenance={
            "feature_id": facet or family,
            "family": family,
            "feature_payload": {"schema": SCHEMA, "path": f"{key}/feature_payload"},
        },
    )
    array(group, "events", [0, 1000000, 2000000], "int64")
    array(group, "durations", [1000000] * 3, "int64")
    payload = group.create_group(
        "feature_payload",
        attributes={
            "schema": SCHEMA,
            "source_h5_path": f"/features/bdsp_sleep_features/time_series/{family}",
        },
    )
    source = payload.create_group(
        "source",
        attributes={
            "feature_id": family,
            "row_count": 3,
            "temporal_semantics": "regular",
            "unit": "power",
            "source": "synthetic-fixed-producer",
            "value_json_storage": storage,
        },
    )
    array(source, "row_index", [10, 12, 14], "int64")
    array(source, "start_time_s", [0, 1, 2], "float64")
    array(source, "end_time_s", [1, 2, 3], "float64")
    categorical(source, "status", [0, 1, 0], ["ok", "qc_rejected"])
    categorical(source, "value_text", [0, -1, 1], ["first", "last"])
    array(source, "value_numeric", [1.23456789012345, 0, 0], "float64")
    array(source, "value_numeric_present", [1, 0, 1], "uint8")
    info = source.create_group("information")
    axis = info.create_group(
        "field_0000", attributes={"name": "frequency_hz", "storage": "constant_json"}
    )
    axis.create_array("value_json", shape=(), dtype="str")[()] = "[0.25, 0.5]"
    region = info.create_group(
        "field_0001", attributes={"name": "channel", "storage": "categorical_json"}
    )
    categorical(region, "value_json", [0, -1, 1], ['"C3"', '"C4"'])
    numeric = info.create_group(
        "field_0002", attributes={"name": "codes", "storage": "numeric_array"}
    )
    array(numeric, "value", [[3, 4], [0, 0], [1, 2]], "int16")
    array(numeric, "present", [1, 0, 1], "uint8")
    for i, storage_kind in enumerate(["boolean", "integer", "number"], 3):
        node = info.create_group(
            f"field_{i:04d}", attributes={"name": storage_kind, "storage": storage_kind}
        )
        array(
            node,
            "value",
            [1, 0, 1],
            {"boolean": "bool", "integer": "int64", "number": "float64"}[storage_kind],
        )
        array(node, "present", [1, 0, 1], "uint8")
    if storage == "numeric_array":
        array(source, "value_array", np.arange(12).reshape(3, 2, 2) / 7, "float64")
        array(source, "value_array_present", [1, 0, 1], "uint8")
    elif storage == "categorical_json":
        categorical(
            source,
            "value_json",
            [0, -1, 1],
            ['{"track": [1, 2], "ok": true}', '[{"x": 3}]'],
        )
    return group


@pytest.fixture
def artifacts(tmp_path):
    paths = {
        "primary": tmp_path / "primary.zarr",
        "derivatives": tmp_path / "derivative.zarr",
        "summary_json": tmp_path / "summary.json",
    }
    roots = {}
    for role in ("primary", "derivatives"):
        root = zarr.open_group(str(paths[role]), mode="w", zarr_format=3)
        root.attrs["tsz"] = {"version": "1.0"}
        metadata = {
            "subject": {"id": "sub-synthetic"},
            "session": {"id": "ses-1", "duration_s": 4},
            "source": {"file": "synthetic.h5", "skipped": {}},
        }
        if role == "derivatives":
            metadata["source"]["feature_payload_schema"] = SCHEMA
        (paths[role] / "recording.json").write_text(json.dumps(metadata))
        roots[role] = root
    primary, derivative = roots["primary"], roots["derivatives"]
    eeg = channel(
        primary,
        "0",
        "eeg",
        "continuous",
        "signal",
        rate_hz=4,
        offset_us=500000,
        unit="uV",
    )
    # Values already physical; stray legacy calibration attributes must not be applied.
    eeg.attrs["phys_max"] = 1000
    array(eeg, "raw", np.arange(12) / 8, "float32")
    slow = channel(
        primary, "1", "spo2", "continuous", "signal", rate_hz=1, offset_us=0, unit="%"
    )
    array(slow, "raw", [97, 96, 95, 94], "float32")
    events(primary, "2", "expert_1", "stage", [0, 2000000], [2000000, 2000000])
    events(derivative, "0", "caisr", "resp_3", [1000000, 3000000], [2000000, 0])
    events(derivative, "1", "caisr", "resp_4", [1000000, 3000000], [1000000, 0])
    feature(
        derivative, "2", "eeg.spectrum", "numeric_array", "eeg.spectrum__channel-c3"
    )
    # A second facet shares one original family, rather than duplicating it.
    shared = channel(
        derivative,
        "3",
        "eeg.spectrum__channel-c4",
        "event",
        "feature",
        provenance={
            "feature_id": "eeg.spectrum__channel-c4",
            "family": "eeg.spectrum",
            "feature_payload": {"schema": SCHEMA, "path": "2/feature_payload"},
        },
    )
    array(shared, "events", [], "int64")
    array(shared, "durations", [], "int64")
    feature(derivative, "4", "breathing.track", "categorical_json")
    feature(derivative, "5", "empty.family", "absent")
    summary = {
        "schema_version": "psg_features_sidecar_v0.5",
        "recording_id": "sub-synthetic_ses-1",
        "subject": {"label": "sub-synthetic", "session": "ses-1"},
        "recording": {"duration_s": 4},
        "features": {},
        "vectors": {
            "embedding": {
                "values": [0.25, 0.75],
                "length": 2,
                "unit": "score",
                "status": "ok",
            }
        },
    }
    for key, storage, status, value in [
        ("zero", "number", "valid_zero", 0.0),
        ("no", "boolean", "valid_zero", False),
        ("count", "integer", "ok", 7),
        ("label", "string", "ok", "N2"),
        ("missing", "number", "missing_input", None),
        ("embedding", "array", "ok", None),
    ]:
        row = {
            "storage_type": storage,
            "status": status,
            "unit": "score",
            "message": "preserve message",
            "value_number": 0,
            "value_integer": 0,
            "value_boolean": False,
            "value_text": "",
        }
        if storage in {"number", "integer", "boolean", "string"}:
            row[
                {
                    "number": "value_number",
                    "integer": "value_integer",
                    "boolean": "value_boolean",
                    "string": "value_text",
                }[storage]
            ] = value
        summary["features"][key] = row
    summary["counts"] = {"summary_features": 6, "not_scalar": 0}
    paths["summary_json"].write_text(json.dumps(summary))
    return paths


def test_native_clocks_physical_values_and_half_open_windows(artifacts):
    with open_recording(**artifacts) as recording:
        eeg = recording.signals["eeg"].read(start=0.75, duration=0.75)
        np.testing.assert_array_equal(eeg.values, [0.125, 0.25, 0.375])
        np.testing.assert_array_equal(eeg.times_s, [0.75, 1, 1.25])
        assert eeg.unit == "uV" and eeg.rate_hz == 4
        assert eeg.values.dtype == np.float32
        np.testing.assert_array_equal(
            recording.signals["spo2"].read(start=0.75, duration=0.75).values, [96]
        )
        assert recording.signals["eeg"].read(start=20, duration=1).values.size == 0
        assert recording.signals["eeg"].read(start=1, duration=0).values.size == 0
        with pytest.raises(ValueError):
            recording.signals["eeg"].read(start=0, duration=-1)


def test_annotations_keep_scorers_codes_points_and_overlap(artifacts):
    with open_recording(**artifacts) as recording:
        assert set(recording.annotations) == {"expert_1", "caisr"}
        assert set(recording.annotations["caisr"]) == {"resp_3", "resp_4"}
        table = recording.annotations["expert_1"]["stage"].read(start=1, duration=1)
        assert table["code"].tolist() == [5]
        assert table["label"].tolist() == ["Wake"]
        assert table["start_us"].tolist() == [0]
        points = recording.annotations["caisr"]["resp_3"].read(start=3, duration=1)
        assert points["duration_us"].tolist() == [0]
        assert recording.annotations["caisr"]["resp_3"].read(start=3, duration=0).empty


def test_typed_summary_preserves_false_zero_missing_and_vectors(artifacts):
    with open_recording(**artifacts) as recording:
        summary = recording.features.summary
        assert summary["zero"]["value"] == 0 and summary["zero"]["available"]
        assert summary["no"]["value"] is False and summary["no"]["available"]
        assert summary["count"]["value"] == 7
        assert summary["label"]["value"] == "N2"
        assert (
            summary["missing"]["value"] is None and not summary["missing"]["available"]
        )
        assert summary["missing"]["message"] == "preserve message"
        np.testing.assert_array_equal(
            recording.features.vectors["embedding"], [0.25, 0.75]
        )
        assert summary.to_dataframe().loc["missing", "status"] == "missing_input"


def test_payload_preserves_precision_dimensions_masks_information_and_facets(artifacts):
    with open_recording(**artifacts) as recording:
        assert recording.availability["complete"]
        assert recording.availability["identity_binding"] == "metadata_only"
        assert len(recording.features.projections) == 4
        assert len(recording.features.time_series) == 3
        family = recording.features.arrays["eeg.spectrum"]
        table = family.read()
        assert table["row_index"].tolist() == [10, 12, 14]
        assert table["status"].tolist() == ["ok", "qc_rejected", "ok"]
        assert table.loc[0, "value_numeric"] == 1.23456789012345
        assert (
            table.loc[1, "value_numeric"] is None and table.loc[2, "value_numeric"] == 0
        )
        assert table.loc[1, "value_json"] is None
        assert table.loc[0, "value_json"].shape == (2, 2)
        assert table.loc[0, "value_json"].dtype == np.float64
        np.testing.assert_array_equal(family.present[:], [1, 0, 1])
        assert table.loc[0, "information"]["frequency_hz"] == [0.25, 0.5]
        assert table.loc[0, "information"]["channel"] == "C3"
        assert "channel" not in table.loc[1, "information"]
        assert table.loc[0, "information"]["codes"].dtype == np.int16
        assert table.loc[0, "information"]["boolean"] is True
        selected = family.read(rows=slice(1, 3), start=1.5, duration=0.5)
        assert selected["row_index"].tolist() == [12]
        assert "value_json" not in family.read(rows=slice(0, 1), include_values=False)
        assert family.read(start=1, duration=0).empty


def test_structured_and_absent_payloads(artifacts):
    with open_recording(**artifacts) as recording:
        rows = recording.features.structured["breathing.track"].read()
        assert rows.loc[0, "value_json"] == {"track": [1, 2], "ok": True}
        assert rows.loc[1, "value_json"] is None
        assert rows.loc[2, "value_json"] == [{"x": 3}]
        assert (
            recording.features.time_series["empty.family"].read()["value_json"].tolist()
            == [None] * 3
        )


def test_open_reads_metadata_only_and_eager_result_survives_close(
    artifacts, monkeypatch
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Opening a recording must not load Zarr array data")

    with monkeypatch.context() as patch:
        patch.setattr(zarr.Array, "__getitem__", forbidden)
        recording = open_recording(**artifacts)
    eager = recording.materialize()
    recording.close()
    assert eager.signals["eeg"].values.size == 12
    assert eager.metadata["channels"]["primary"]["eeg"]["rate_hz"] == 4
    assert eager.features["metadata"]["eeg.spectrum"]["source"]["row_count"] == 3
    assert eager.features["metadata"]["eeg.spectrum"]["export"]["schema"] == SCHEMA
    assert eager.features["time_series"]["breathing.track"].loc[0, "value_json"]["ok"]
    with pytest.raises(RuntimeError, match="closed"):
        recording.signals["eeg"].read()
    with pytest.raises(RuntimeError, match="closed"):
        recording.features.arrays["eeg.spectrum"].read()


@pytest.mark.parametrize("field", ["subject", "session", "duration", "source"])
def test_mixed_artifacts_fail_even_when_incomplete_allowed(artifacts, field):
    path = artifacts["derivatives"] / "recording.json"
    metadata = json.loads(path.read_text())
    if field == "subject":
        metadata["subject"]["id"] = "sub-other"
    elif field == "session":
        metadata["session"]["id"] = "ses-other"
    elif field == "duration":
        metadata["session"]["duration_s"] = 4.1
    else:
        metadata["source"]["file"] = "different-source.h5"
    path.write_text(json.dumps(metadata))
    with pytest.raises(FormatError, match="different"):
        open_recording(**artifacts, require_complete=False)


def test_summary_identity_mismatch(artifacts):
    summary = json.loads(artifacts["summary_json"].read_text())
    summary["subject"]["session"] = "ses-other"
    with pytest.raises(FormatError, match="different"):
        open_recording(**{**artifacts, "summary_json": summary})


def test_primary_only_is_explicit_and_reports_missing_enrichment(artifacts):
    with pytest.raises(IncompleteRecordingError):
        open_recording(primary=artifacts["primary"])
    with open_recording(
        primary=artifacts["primary"], require_complete=False
    ) as recording:
        assert not recording.availability["complete"]
        assert not recording.features.time_series and not recording.features.summary
        assert recording.signals["eeg"].read().values.size == 12


def test_open_primary_validates_only_primary_and_retains_native_clock(artifacts):
    with open_primary(artifacts["primary"]) as recording:
        assert recording.availability["complete"]
        assert recording.availability["completeness_scope"] == "declared_primary_signals_annotations"
        assert not recording.availability["derivatives"]
        assert not recording.availability["summary"]
        assert recording.signals["eeg"].read().times_s[0] == .5
    # Adding the new primary contract must not weaken the complete-export gate.
    with pytest.raises(IncompleteRecordingError):
        open_recording(primary=artifacts["primary"])


@pytest.mark.parametrize("inventory", [None, {"annotations": {"expert_1/stage": "bad timing"}}])
def test_open_primary_rejects_missing_or_skipped_export_inventory(artifacts, inventory):
    path = artifacts["primary"] / "recording.json"
    metadata = json.loads(path.read_text())
    if inventory is None:
        del metadata["source"]["skipped"]
    else:
        metadata["source"]["skipped"] = inventory
    path.write_text(json.dumps(metadata))
    with pytest.raises(IncompleteRecordingError):
        open_primary(artifacts["primary"])


@pytest.mark.parametrize(
    "case", ["legacy", "skipped", "missing_vector", "summary_matrix"]
)
def test_incomplete_export_is_never_silently_complete(artifacts, case):
    if case in {"legacy", "skipped"}:
        path = artifacts["derivatives"] / "recording.json"
        metadata = json.loads(path.read_text())
        if case == "legacy":
            del metadata["source"]["feature_payload_schema"]
            root = zarr.open_group(str(artifacts["derivatives"]), mode="r+")
            for key in ["2", "3", "4", "5"]:
                tsz = copy.deepcopy(root[key].attrs["tsz"])
                del tsz["provenance"]["feature_payload"]
                root[key].attrs["tsz"] = tsz
        else:
            metadata["source"]["skipped"] = {
                "features": {"eeg.spectrum": "unsupported"}
            }
        path.write_text(json.dumps(metadata))
    else:
        summary = json.loads(artifacts["summary_json"].read_text())
        if case == "missing_vector":
            summary["vectors"] = {}
        else:
            summary["counts"]["not_scalar"] = 1
        artifacts["summary_json"].write_text(json.dumps(summary))
    with pytest.raises(IncompleteRecordingError):
        open_recording(**artifacts)
    with open_recording(**artifacts, require_complete=False) as recording:
        assert not recording.availability["complete"]
        assert recording.availability["issues"]


@pytest.mark.parametrize(
    "case", ["version", "payload_schema", "path", "shape", "family"]
)
def test_unsupported_or_broken_schema_fails(artifacts, case):
    root = zarr.open_group(str(artifacts["derivatives"]), mode="r+")
    if case == "version":
        root.attrs["tsz"] = {"version": "future"}
    elif case == "shape":
        root["2/feature_payload/source"].attrs["row_count"] = 99
    elif case == "family":
        root["2/feature_payload/source"].attrs["feature_id"] = "wrong.family"
    else:
        tsz = copy.deepcopy(root["2"].attrs["tsz"])
        tsz["provenance"]["feature_payload"][
            "schema" if case == "payload_schema" else "path"
        ] = "../other"
        root["2"].attrs["tsz"] = tsz
    with pytest.raises(FormatError):
        open_recording(**artifacts, require_complete=False)


def test_caller_owned_memory_store(artifacts):
    from zarr.core.buffer import default_buffer_prototype
    from zarr.core.sync import sync

    store = zarr.storage.MemoryStore()
    # Copy only the primary test artifact into an explicitly configured Store.
    for path in artifacts["primary"].rglob("*"):
        if path.is_file():
            sync(
                store.set(
                    path.relative_to(artifacts["primary"]).as_posix(),
                    default_buffer_prototype().buffer.from_bytes(path.read_bytes()),
                )
            )
    with open_recording(primary=store, require_complete=False) as recording:
        np.testing.assert_array_equal(
            recording.signals["spo2"].read().values, [97, 96, 95, 94]
        )
    assert sync(store.get("recording.json")) is not None
