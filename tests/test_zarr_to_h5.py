"""Split Zarr -> H5 round trips on synthetic recordings; no converter or cloud access."""

from __future__ import annotations

import json

import h5py
import numpy as np
import pytest

zarr = pytest.importorskip("zarr", minversion="3.1")

import bdsp
from bdsp.h5_compare import compare_h5
from bdsp.zarr_to_h5 import main, write_h5

SCHEMA = "bdsp.sleep-feature-payload.v1"
SUBJECT, SESSION = "sub-T0001123", "ses-1"
ROOT = "/features/bdsp_sleep_features"
#: name -> (fs, unit, phys_min, phys_max, dig_min, dig_max, digital samples)
RNG = np.random.default_rng(7)
SIGNALS = {
    "c3-m2": (4.0, "uV", -65536.0, 65534.0, -32768.0, 32767.0, RNG.integers(-300, 300, 80)),
    "abd": (4.0, "uV", -3200.0, 3200.0, -32768.0, 32767.0, RNG.integers(-32768, 32768, 80)),
    "spo2": (1.0, "%", 0.0, 100.0, -32768.0, 32767.0,
             np.round((RNG.integers(88, 100, 20) - 50.00076) / (100 / 65535))),
    "ecg": (4.0, "mV", -5.0, 5.0, -32768.0, 32767.0, RNG.integers(-9000, 9000, 80)),
    "etco2": (1.0, "mmHg", -328.0, 328.0, -32768.0, 32767.0, np.array([0, 5] * 10)),
    "cpap_on": (1.0, "bool", 0.0, 1.0, 0.0, 1.0, np.array([0, 1] * 10)),
}  # fmt: skip
TO_UV = {"mV": 1e3}


def physical(phys_min, phys_max, dig_min, dig_max, digital, unit):
    """The converter's calibration (bdsp-psg-conversion common/h5_reader.py)."""
    scale = (phys_max - phys_min) / (dig_max - dig_min)
    offset = phys_min - dig_min * scale
    scale, offset = scale * TO_UV.get(unit, 1.0), offset * TO_UV.get(unit, 1.0)
    return (digital.astype(np.float64) * scale + offset).astype(np.float32)


def write_source_h5(path):
    with h5py.File(path, "w") as h5:
        h5.attrs.update(subject_id=SUBJECT, session_id=SESSION, sex="M", age=np.float64(93.0),
                        duration_sec=np.float32(20.0), meas_date="2001-02-03 22:15:00",
                        metadata_source="synthetic.csv")  # fmt: skip
        for name, (fs, unit, pmin, pmax, dmin, dmax, digital) in SIGNALS.items():
            data = h5.create_dataset(f"signals/{name}", data=digital.astype(np.int16))
            data.attrs.update(fs=fs, unit=unit, phys_min=pmin, phys_max=pmax, dig_min=dmin,
                              dig_max=dmax, prefilter="HP:0.3Hz", type="raw")  # fmt: skip
        for annotator, codes in (("expert_1", [5, 2, 2]), ("caisr", [5, 5, 2])):
            table = h5.create_group(f"annotations/{annotator}/stage")
            starts = np.array([0.0, 7.3333335, 13.1], dtype=np.float32)
            durations = np.array([7.3333335, 5.7666664, 0.0], dtype=np.float32)
            table["starts"], table["durations"] = starts, durations
            table["ends"] = starts + durations
            table["codes"] = np.array(codes, dtype=np.int16)
            table.attrs["event_map"] = json.dumps({"5": "Wake", "2": "N2", "0": "Unscored"})
            table.attrs.update(format="event_table", rule="synthetic")
        features = h5.create_group(ROOT)
        features.attrs.update(namespace=ROOT, package_version="0.0.0", recording_id="x",
                              schema_version="sleep_feature_h5_v1")  # fmt: skip
        family = features.create_group("time_series/eeg.spectrum")
        family.attrs.update(feature_id="eeg.spectrum", row_count=np.int64(3), unit="dB",
                            temporal_semantics="regular", value_json_storage="numeric_array")  # fmt: skip
        family["start_time_s"] = np.array([0.0, 4.0, 8.0])
        family["end_time_s"] = np.array([4.0, 8.0, 12.0])
        family["value_array"] = np.arange(12, dtype=np.float32).reshape(3, 4) / 3
        family["value_array_present"] = np.array([1, 0, 1], dtype=np.uint8)
        family["status_code"] = np.array([0, 1, 0], dtype=np.int32)
        family.create_dataset("status_labels", data=["ok", "qc_rejected"],
                              dtype=h5py.string_dtype())  # fmt: skip
        axis = family.create_group("information/field_0000")
        axis.attrs.update(name="frequency_hz", storage="constant_json")
        axis.create_dataset("value_json", data="[1, 2, 3, 4]", dtype=h5py.string_dtype())
        features["time_series"].attrs.update(feature_count=np.int64(1), row_count=np.int64(3))
        summary = features.create_group("recording_summary")
        rows = [
            ("sleep.tst", "number", "ok", 401.5, ""),
            ("sleep.missing", "number", "missing_input", np.nan, ""),
            ("brain_health.embedding", "array", "ok", np.nan, "arrays/brain_health.embedding"),
        ]
        text = h5py.string_dtype()
        for column in ("short_name", "display_name", "display_scope", "unit", "source",
                       "result_kind", "value_shape", "message", "value_text"):  # fmt: skip
            summary.create_dataset(column, data=[column] * len(rows), dtype=text)
        summary.create_dataset("feature_id", data=[r[0] for r in rows], dtype=text)
        summary.create_dataset("storage_type", data=[r[1] for r in rows], dtype=text)
        summary.create_dataset("status", data=[r[2] for r in rows], dtype=text)
        summary.create_dataset("array_path", data=[r[4] for r in rows], dtype=text)
        summary["value_number"] = np.array([r[3] for r in rows])
        summary["value_integer"] = np.zeros(len(rows), dtype=np.int64)
        summary["value_boolean"] = np.zeros(len(rows), dtype=np.uint8)
        summary["arrays/brain_health.embedding"] = np.array([0.25, -0.5, 0.1], dtype=np.float32)
        summary["arrays/brain_health.embedding"].attrs["feature_id"] = "brain_health.embedding"
        h5.create_group("features/research/spindles")


def recording_json(path, extra=None):
    meta = {"subject": {"id": SUBJECT, "sex": "Male", "age_years": 90.0},
            "session": {"id": SESSION, "duration_s": 20.0},
            "source": {"file": "source.h5", "converter": "mimic", "skipped": {}, **(extra or {})}}
    (path / "recording.json").write_text(json.dumps(meta))  # fmt: skip


def channel(root, key, attrs):
    return root.create_group(str(key), attributes={"tsz": attrs})


def copy_group(source, target):
    target.attrs.update({k: v.item() if isinstance(v, np.generic) else v
                         for k, v in source.attrs.items()})  # fmt: skip
    for name, node in source.items():
        if isinstance(node, h5py.Group):
            copy_group(node, target.create_group(name))
        elif h5py.check_string_dtype(node.dtype):
            data = np.asarray(node.asstr()[()], dtype=object)
            target.create_array(name, shape=data.shape, dtype="str")[...] = data
        else:
            target.create_array(name, data=node[()])


def convert(source, tmp_path):
    """A minimal mimic of the converter's H5 -> split Zarr mapping."""
    paths = {"primary": tmp_path / "primary.zarr", "derivatives": tmp_path / "derivative.zarr",
             "summary_json": tmp_path / "summary.json"}  # fmt: skip
    primary = zarr.open_group(str(paths["primary"]), mode="w", zarr_format=3,
                              attributes={"tsz": {"version": "1.0"}})  # fmt: skip
    derivative = zarr.open_group(str(paths["derivatives"]), mode="w", zarr_format=3,
                                 attributes={"tsz": {"version": "1.0"}})  # fmt: skip
    recording_json(paths["primary"])
    recording_json(paths["derivatives"], {"feature_payload_schema": SCHEMA})
    keys = {"primary": 0, "derivatives": 0}
    with h5py.File(source) as h5:
        for name, node in h5["signals"].items():
            unit = node.attrs["unit"]
            attrs = [float(node.attrs[k]) for k in ("phys_min", "phys_max", "dig_min", "dig_max")]
            provenance = {"origin": "signal", "source_path": node.name}
            if unit in TO_UV:
                provenance["unit_converted_from"] = unit
            group = channel(primary, keys["primary"],
                            {"kind": "continuous", "name": name, "id": node.name,
                             "unit": "uV" if unit in TO_UV else unit,
                             "rate_hz": float(node.attrs["fs"]), "offset_us": 0,
                             "provenance": provenance})  # fmt: skip
            group.create_array("raw", data=physical(*attrs, node[()], unit))
            keys["primary"] += 1
        for annotator, tables in h5["annotations"].items():
            role = "primary" if annotator.startswith("expert") else "derivatives"
            root = primary if role == "primary" else derivative
            for kind, table in tables.items():
                event_map = {int(k): v for k, v in json.loads(table.attrs["event_map"]).items()}
                codes = sorted(event_map)
                group = channel(root, keys[role], {
                    "kind": "event", "name": f"{annotator}/{kind}", "id": table.name,
                    "label_names": [event_map[c] for c in codes],
                    "provenance": {"origin": "annotation", "annotator": annotator,
                                   "label_codes": codes}})  # fmt: skip
                us = lambda s: np.round(s[()].astype(np.float64) * 1e6).astype(np.int64)  # noqa: E731
                group.create_array("events", data=us(table["starts"]))
                group.create_array("durations", data=us(table["durations"]))
                labels = [codes.index(int(c)) for c in table["codes"][()]]
                group.create_array("labels", data=np.asarray(labels, dtype=np.uint16))
                keys[role] += 1
        for name, family in h5[f"{ROOT}/time_series"].items():
            key = keys["derivatives"]
            group = channel(derivative, key, {
                "kind": "event", "name": name, "id": name,
                "provenance": {"origin": "feature", "feature_id": name,
                               "feature_payload": {"schema": SCHEMA,
                                                   "path": f"{key}/feature_payload"}}})  # fmt: skip
            group.create_array("events", data=np.zeros(0, dtype=np.int64))
            group.create_array("durations", data=np.zeros(0, dtype=np.int64))
            payload = group.create_group("feature_payload", attributes={
                "schema": SCHEMA, "source_h5_path": family.name})  # fmt: skip
            copy_group(family, payload.create_group("source"))
            keys["derivatives"] += 1
        summary = h5[f"{ROOT}/recording_summary"]
        columns = {name: summary[name] for name in summary if name != "arrays"}
        features = {}
        for i, feature_id in enumerate(columns["feature_id"].asstr()[()]):
            row = {}
            for name, data in columns.items():
                if name == "feature_id":
                    continue
                value = data.asstr()[i] if h5py.check_string_dtype(data.dtype) else data[i].item()
                row[name] = None if isinstance(value, float) and np.isnan(value) else value
            row["value_boolean"] = bool(row["value_boolean"])
            features[feature_id] = row
        embedding = summary["arrays/brain_health.embedding"][()].astype(float).tolist()
    paths["summary_json"].write_text(json.dumps({
        "schema_version": "psg_features_sidecar_v0.5", "recording_id": f"{SUBJECT}_{SESSION}",
        "subject": {"label": SUBJECT, "session": SESSION}, "recording": {"duration_s": 20.0},
        "provenance": {"meas_date": "2001-02-03 22:15:00"},
        "counts": {"summary_features": len(features)}, "features": features,
        "vectors": {"brain_health.embedding": {"values": embedding, "length": 3}}}))  # fmt: skip
    return paths


@pytest.fixture
def split(tmp_path):
    source = tmp_path / "source.h5"
    write_source_h5(source)
    return source, convert(source, tmp_path)


def test_round_trip_reproduces_the_source_h5(split, tmp_path):
    source, paths = split
    out = tmp_path / "rebuilt.h5"
    report = write_h5(out, **paths)
    assert (report.signals_int16, report.signals_float32) == (len(SIGNALS), 0)
    assert (report.annotation_tracks, report.feature_families, report.summary_rows) == (2, 1, 3)
    result = compare_h5(source, out)
    assert result["verdict"] == "pass", result["failures"]
    signals = result["signals"]
    assert signals["digital_equal"] == signals["calibration_equal"] == len(SIGNALS)
    assert signals["unit_equal"] == len(SIGNALS)
    assert signals["attrs_not_exported"] == ["prefilter", "type"]
    assert result["annotations"]["equal"] == 2
    assert result["annotations"]["attrs_not_exported"] == ["rule"]
    assert result["features"]["time_series"]["equal"] == 1
    assert result["features"]["recording_summary"]["arrays_equal"] == 1
    assert result["not_exported"]["root_attrs"] == ["metadata_source"]
    assert "/features/research" in result["not_exported"]["nodes"]
    with h5py.File(out) as h5:
        assert h5["signals/ecg"].attrs["unit"] == "mV"  # restored from unit_converted_from
        assert float(h5.attrs["age"]) == 90.0  # published cap, not the archived 93
        export = json.loads(h5.attrs["bdsp_h5_export"])
        assert export["inputs"]["primary"] == "primary.zarr"


def test_rebuilt_h5_reads_with_the_h5_reader(split, tmp_path):
    source, paths = split
    out = tmp_path / "rebuilt.h5"
    write_h5(out, **paths)
    original, rebuilt = bdsp.read_h5(source), bdsp.read_h5(out)
    assert set(original.signals) == set(rebuilt.signals)
    for name, values in original.signals.items():
        np.testing.assert_array_equal(values, rebuilt.signals[name])
    stage = rebuilt.annotations["expert_1"]["stage"]
    np.testing.assert_array_equal(stage["codes"], [5, 2, 2])
    assert stage["event_map"] == {0: "Unscored", 2: "N2", 5: "Wake"}


def test_compare_detects_changed_samples_and_events(split, tmp_path):
    source, paths = split
    out = tmp_path / "rebuilt.h5"
    write_h5(out, **paths)
    with h5py.File(out, "r+") as h5:
        h5["signals/abd"][3] += 1
        h5["annotations/caisr/stage/codes"][0] = 2
        h5[f"{ROOT}/time_series/eeg.spectrum/value_array"][0, 0] = 99
    failures = compare_h5(source, out)["failures"]
    assert "signal samples differ: abd" in failures
    assert "annotation table differs: caisr/stage" in failures
    assert "feature family differs: eeg.spectrum" in failures


def test_unencodable_samples_stay_float32_and_partial_inputs(split, tmp_path):
    _, paths = split
    raw = zarr.open_group(str(paths["primary"]), mode="r+")["0/raw"]
    values = raw[...]
    values[2] = np.nan  # an implicit-fill gap has no int16 representation
    raw[...] = values
    out = tmp_path / "primary_only.h5"
    report = write_h5(out, paths["primary"])
    assert report.signals_float32 == 1 and report.feature_families == report.summary_rows == 0
    with h5py.File(out) as h5:
        name = next(n for n in h5["signals"] if h5["signals"][n].dtype == np.float32)
        np.testing.assert_array_equal(h5["signals"][name][()], values)
        assert "features" not in h5 and "annotations/caisr" not in h5
        assert "meas_date" not in h5.attrs


def test_refuses_mismatched_artifacts_and_existing_output(split, tmp_path):
    _, paths = split
    other = json.loads(paths["summary_json"].read_text())
    other["subject"]["label"] = "sub-T0001999"
    wrong = tmp_path / "other.json"
    wrong.write_text(json.dumps(other))
    with pytest.raises(ValueError, match="different recording"):
        write_h5(tmp_path / "x.h5", paths["primary"], paths["derivatives"], wrong)
    out = tmp_path / "rebuilt.h5"
    write_h5(out, **paths)
    with pytest.raises(FileExistsError):
        write_h5(out, **paths)
    write_h5(out, **paths, overwrite=True)
    assert not list(tmp_path.glob(".*.tmp"))


def test_command_line(split, tmp_path, capsys):
    _, paths = split
    out = tmp_path / "cli.h5"
    assert main(["--primary", str(paths["primary"]), "--derivatives", str(paths["derivatives"]),
                 "--summary-json", str(paths["summary_json"]), "-o", str(out)]) == 0  # fmt: skip
    assert out.exists() and "6 signals (6 int16, 0 float32)" in capsys.readouterr().out
