import datetime
import enum
import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from observe.serializer import snapshot_value


class Shade(enum.Enum):
    BLUE = "blue"


@dataclass
class Sample:
    label: str
    values: list

    @property
    def dangerous(self):
        raise AssertionError("property must not be evaluated")


def test_supported_scalar_and_standard_library_types_are_json_safe(tmp_path):
    sample_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    value = {
        "none": None,
        "flag": True,
        "integer": 9,
        "float": 1.25,
        "not_finite": float("nan"),
        "text": "繁體中文",
        "bytes": b"abc",
        "tuple": ("left", "right"),
        "set": {"red", "blue"},
        "sample": Sample("title", [1, 2]),
        "shade": Shade.BLUE,
        "path": Path(tmp_path / "file.txt"),
        "date": datetime.date(2025, 1, 2),
        "time": datetime.time(3, 4, 5),
        "datetime": datetime.datetime(2025, 1, 2, 3, 4, 5),
        "aware_datetime": datetime.datetime(
            2025,
            1,
            2,
            3,
            4,
            5,
            tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30)),
        ),
        "uuid": sample_id,
    }

    snapshot = snapshot_value(value)
    json.dumps(snapshot, ensure_ascii=False, allow_nan=False)

    assert snapshot["text"] == "繁體中文"
    assert snapshot["not_finite"]["value"] == "nan"
    assert snapshot["tuple"] == ["left", "right"]
    assert set(snapshot["set"]) == {"red", "blue"}
    assert snapshot["sample"]["label"] == "title"
    assert snapshot["sample"]["values"] == [1, 2]
    assert snapshot["shade"]["name"] == "BLUE"
    assert snapshot["date"]["value"] == "2025-01-02"
    assert snapshot["aware_datetime"]["timezone"]["offset"] == "+05:30"
    assert snapshot["path"]["value"].endswith("file.txt")
    assert snapshot["uuid"]["value"] == str(sample_id)


def test_cycles_and_unknown_objects_never_call_repr_or_properties():
    side_effects = []

    class Dangerous:
        @property
        def value(self):
            side_effects.append("property")
            raise AssertionError("property must not be evaluated")

        def __repr__(self):
            side_effects.append("repr")
            raise AssertionError("repr must not be evaluated")

    values = []
    values.append(values)
    values.append(Dangerous())

    snapshot = snapshot_value(values)

    assert snapshot[0]["__cycle__"] is True
    assert "Dangerous" in snapshot[1]["__type__"]
    assert side_effects == []


def test_custom_serializer_and_recursive_redaction():
    class Token:
        def __init__(self, value):
            self.value = value

    snapshot = snapshot_value(
        {
            "auth": {
                "access_token": Token("secret"),
                "credential": Token("visible"),
                "visible": "ok",
            },
            "nested": [{"Password": "hidden"}],
        },
        serializers={Token: lambda item: {"value": item.value}},
        redact=["password", "token"],
    )

    assert snapshot["auth"]["access_token"] == "<redacted>"
    assert snapshot["auth"]["credential"]["value"] == "visible"
    assert snapshot["auth"]["visible"] == "ok"
    assert snapshot["nested"][0]["Password"] == "<redacted>"


def test_custom_serializer_accepts_classes_with_custom_metaclasses():
    class ModelMeta(type):
        pass

    class Model(metaclass=ModelMeta):
        def __init__(self, value):
            self.value = value

    snapshot = snapshot_value(
        Model("captured"),
        serializers={Model: lambda item: {"value": item.value}},
    )

    assert snapshot == {"value": "captured"}


def test_trace_applies_redaction_to_inputs_and_outputs(tmp_path):
    import json

    from observe import trace

    output = tmp_path / "redacted.jsonl"

    @trace(
        output=output,
        project_root=Path(__file__).resolve().parents[2],
        redact=["password", "token"],
    )
    def scenario(password, token):
        return {"access_token": token, "visible": password}

    assert scenario("input secret", "output secret") == {
        "access_token": "output secret",
        "visible": "input secret",
    }
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    call = next(record for record in records if record["event"] == "call")
    returned = next(record for record in records if record["event"] == "return")
    assert call["input"] == {"password": "<redacted>", "token": "<redacted>"}
    assert returned["output"] == {"access_token": "<redacted>", "visible": "input secret"}


def test_serializer_failure_is_summarized_without_escaping():
    class Item:
        pass

    snapshot = snapshot_value(
        Item(),
        serializers={Item: lambda item: (_ for _ in ()).throw(RuntimeError("failure"))},
    )

    assert snapshot["__serialization_error__"] == "builtins.RuntimeError"


def test_depth_item_string_node_and_object_budgets_are_bounded():
    nested = {"a": {"b": {"c": "value"}}}
    limited_depth = snapshot_value(nested, max_depth=1)
    limited_items = snapshot_value([0, 1, 2, 3], max_items=2)
    limited_string = snapshot_value("abcdefgh", max_string_length=3)
    limited_nodes = snapshot_value([1, 2, 3], max_nodes=2)
    limited_object = snapshot_value(["x" * 100 for _ in range(8)], max_object_bytes=100)

    assert limited_depth["a"]["b"]["__truncated__"] == "max_depth"
    assert limited_items["__total_items__"] == 4
    assert limited_items["__truncated__"] is True
    assert limited_string["value"] == "abc…"
    assert limited_string["__truncated__"] is True
    assert limited_nodes[1]["__truncated__"] is True
    assert limited_object["__truncated__"] == "max_object_bytes"
    assert len(json.dumps(limited_object, separators=(",", ":")).encode("utf-8")) <= 100
