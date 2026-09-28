import datetime
import enum
import inspect
import itertools
import json
import math
import pathlib
import types
import uuid

from observe.redactor import is_redacted_key, normalize_redaction_keys


_MISSING = object()


def _type_name(value):
    value_type = type(value)
    try:
        module = type.__getattribute__(value_type, "__module__")
        qualname = type.__getattribute__(value_type, "__qualname__")
    except (AttributeError, TypeError):
        return "<unknown>"
    if type(module) is not str or type(qualname) is not str:
        return "<unknown>"
    return f"{module}.{qualname}"


def _descriptor_value(descriptor, value, value_type):
    try:
        return descriptor.__get__(value, value_type)
    except (AttributeError, TypeError):
        return _MISSING


def _instance_dictionary(value):
    value_type = type(value)
    try:
        mro = type.__getattribute__(value_type, "__mro__")
    except (AttributeError, TypeError):
        return None
    for parent_type in mro:
        try:
            class_dictionary = type.__getattribute__(parent_type, "__dict__")
        except (AttributeError, TypeError):
            continue
        descriptor = class_dictionary.get("__dict__")
        if type(descriptor) is types.GetSetDescriptorType:
            instance_dictionary = _descriptor_value(descriptor, value, value_type)
            if type(instance_dictionary) is dict:
                return instance_dictionary
    return None


def _dataclass_fields(value_type):
    try:
        field_map = type.__getattribute__(value_type, "__dataclass_fields__")
    except (AttributeError, TypeError):
        return None
    if type(field_map) is not dict:
        return None
    return tuple(name for name in field_map if type(name) is str)


def _dataclass_field_value(value, field_name, instance_dictionary):
    if instance_dictionary is not None and field_name in instance_dictionary:
        return instance_dictionary[field_name]
    value_type = type(value)
    try:
        mro = type.__getattribute__(value_type, "__mro__")
    except (AttributeError, TypeError):
        return _MISSING
    for parent_type in mro:
        try:
            descriptor = type.__getattribute__(parent_type, "__dict__").get(field_name)
        except (AttributeError, TypeError):
            continue
        if type(descriptor) is types.MemberDescriptorType:
            return _descriptor_value(descriptor, value, value_type)
        if descriptor is not None:
            return _MISSING
    return _MISSING


class _Snapshotter:
    def __init__(
        self,
        *,
        serializers,
        redact,
        max_depth,
        max_items,
        max_string_length,
        max_nodes,
    ):
        self.serializers = tuple((value_type, serializer) for value_type, serializer in (serializers or {}).items())
        self.redact = normalize_redaction_keys(redact or ())
        self.max_depth = max_depth
        self.max_items = max_items
        self.max_string_length = max_string_length
        self.max_nodes = max_nodes
        self.nodes = 0
        self.active = set()

    def _string(self, value):
        if len(value) <= self.max_string_length:
            return value
        return {
            "__type__": "builtins.str",
            "value": value[: self.max_string_length] + "…",
            "__total_length__": len(value),
            "__truncated__": True,
        }

    def _redacted(self, key):
        return is_redacted_key(key, self.redact)

    def _key(self, key):
        key_type = type(key)
        if key_type is str:
            return key
        if key is None:
            return "None"
        if key_type in (bool, int, float):
            if key_type is float and not math.isfinite(key):
                return "nan" if math.isnan(key) else ("inf" if key > 0 else "-inf")
            try:
                return str(key)
            except ValueError:
                return f"<{_type_name(key)} bits={key.bit_length()}>"
        return f"<{_type_name(key)}>"

    def _container_result(self, type_name, values, total, truncated):
        if not truncated:
            return values
        return {
            "__type__": type_name,
            "__items__": values,
            "__total_items__": total,
            "__truncated__": True,
        }

    def _timezone(self, value, value_type):
        descriptor = (
            datetime.datetime.tzinfo
            if value_type is datetime.datetime
            else datetime.time.tzinfo
        )
        tzinfo = descriptor.__get__(value, value_type)
        if tzinfo is None:
            return None
        result = {"__type__": _type_name(tzinfo)}
        if type(tzinfo) is datetime.timezone:
            offset = datetime.timezone.utcoffset(tzinfo, value)
            total_microseconds = (
                (offset.days * 86_400 + offset.seconds) * 1_000_000
                + offset.microseconds
            )
            sign = "-" if total_microseconds < 0 else "+"
            hours, remainder = divmod(abs(total_microseconds), 3_600_000_000)
            minutes, remainder = divmod(remainder, 60_000_000)
            seconds, microseconds = divmod(remainder, 1_000_000)
            text = f"{sign}{hours:02d}:{minutes:02d}"
            if seconds or microseconds:
                text += f":{seconds:02d}"
                if microseconds:
                    text += f".{microseconds:06d}"
            result["offset"] = text
        return result

    def _mapping(self, value, depth, type_name):
        total = dict.__len__(value)
        result = {}
        truncated = total > self.max_items
        for key, item in itertools.islice(dict.items(value), self.max_items):
            output_key = self._key(key)
            bounded_key = output_key[: self.max_string_length]
            if self._redacted(output_key):
                result[bounded_key] = "<redacted>"
            else:
                result[bounded_key] = self.snapshot(item, depth + 1)
        return self._container_result(type_name, result, total, truncated)

    def _sequence(self, value, depth, type_name, value_type):
        total = value_type.__len__(value)
        count = min(total, self.max_items)
        get_item = value_type.__getitem__
        result = [self.snapshot(get_item(value, index), depth + 1) for index in range(count)]
        return self._container_result(type_name, result, total, total > count)

    def _set(self, value, depth, type_name, value_type):
        total = value_type.__len__(value)
        iterator = value_type.__iter__(value)
        result = []
        for index, item in enumerate(iterator):
            if index >= self.max_items:
                break
            result.append(self.snapshot(item, depth + 1))
        return self._container_result(type_name, result, total, total > len(result))

    def _dataclass(self, value, depth, type_name, field_names):
        instance_dictionary = _instance_dictionary(value)
        fields = {}
        for field_name in field_names[: self.max_items]:
            output_key = field_name[: self.max_string_length]
            if self._redacted(field_name):
                fields[output_key] = "<redacted>"
                continue
            field_value = _dataclass_field_value(value, field_name, instance_dictionary)
            if field_value is not _MISSING:
                fields[output_key] = self.snapshot(field_value, depth + 1)
        result = {"__type__": type_name, **fields}
        if len(field_names) > self.max_items:
            result["__total_items__"] = len(field_names)
            result["__truncated__"] = True
        return result

    def _special(self, value, value_type, type_name, depth):
        if value_type is bytes:
            preview = bytes.hex(bytes.__getitem__(value, slice(0, 24)))
            return {
                "__type__": type_name,
                "length": bytes.__len__(value),
                "preview_hex": preview,
                "__truncated__": bytes.__len__(value) > 24,
            }
        if value_type in (
            pathlib.PosixPath,
            pathlib.WindowsPath,
            pathlib.PurePosixPath,
            pathlib.PureWindowsPath,
        ):
            path_value = pathlib.PurePath.__str__(value)
            return {"__type__": type_name, "value": self._string(path_value)}
        if value_type is datetime.datetime:
            year = datetime.datetime.year.__get__(value, value_type)
            month = datetime.datetime.month.__get__(value, value_type)
            day = datetime.datetime.day.__get__(value, value_type)
            hour = datetime.datetime.hour.__get__(value, value_type)
            minute = datetime.datetime.minute.__get__(value, value_type)
            second = datetime.datetime.second.__get__(value, value_type)
            microsecond = datetime.datetime.microsecond.__get__(value, value_type)
            text = f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}"
            if microsecond:
                text += f".{microsecond:06d}"
            result = {"__type__": type_name, "value": text}
            timezone = self._timezone(value, value_type)
            if timezone is not None:
                result["timezone"] = timezone
            return result
        if value_type is datetime.date:
            year = datetime.date.year.__get__(value, value_type)
            month = datetime.date.month.__get__(value, value_type)
            day = datetime.date.day.__get__(value, value_type)
            return {"__type__": type_name, "value": f"{year:04d}-{month:02d}-{day:02d}"}
        if value_type is datetime.time:
            hour = datetime.time.hour.__get__(value, value_type)
            minute = datetime.time.minute.__get__(value, value_type)
            second = datetime.time.second.__get__(value, value_type)
            microsecond = datetime.time.microsecond.__get__(value, value_type)
            text = f"{hour:02d}:{minute:02d}:{second:02d}"
            if microsecond:
                text += f".{microsecond:06d}"
            result = {"__type__": type_name, "value": text}
            timezone = self._timezone(value, value_type)
            if timezone is not None:
                result["timezone"] = timezone
            return result
        if value_type is uuid.UUID:
            hexadecimal = uuid.UUID.hex.__get__(value, value_type)
            text = f"{hexadecimal[:8]}-{hexadecimal[8:12]}-{hexadecimal[12:16]}-{hexadecimal[16:20]}-{hexadecimal[20:]}"
            return {"__type__": type_name, "value": text}
        try:
            is_enum = issubclass(value_type, enum.Enum)
        except TypeError:
            is_enum = False
        if is_enum:
            name = object.__getattribute__(value, "_name_")
            member_value = object.__getattribute__(value, "_value_")
            return {
                "__type__": type_name,
                "name": self.snapshot(name, depth + 1),
                "value": self.snapshot(member_value, depth + 1),
            }
        return _MISSING

    def snapshot(self, value, depth=0):
        self.nodes += 1
        value_type = type(value)
        type_name = _type_name(value)
        if self.nodes > self.max_nodes:
            return {"__type__": type_name, "__truncated__": True}
        if depth > self.max_depth:
            return {"__type__": type_name, "__truncated__": "max_depth"}
        if value is None or value_type in (bool, int):
            if value_type is int and value.bit_length() > 14_000:
                return {"__type__": type_name, "bits": value.bit_length(), "__truncated__": True}
            return value
        if value_type is float:
            if math.isfinite(value):
                return value
            return {"__type__": type_name, "value": "nan" if math.isnan(value) else ("inf" if value > 0 else "-inf")}
        if value_type is str:
            return self._string(value)

        value_id = id(value)
        if value_id in self.active:
            return {"__type__": type_name, "__cycle__": True}
        self.active.add(value_id)
        try:
            serializer = next(
                (
                    registered_serializer
                    for registered_type, registered_serializer in self.serializers
                    if value_type is registered_type
                ),
                None,
            )
            if serializer is not None:
                try:
                    return self.snapshot(serializer(value), depth + 1)
                except Exception as error:
                    return {
                        "__type__": type_name,
                        "__serialization_error__": _type_name(error),
                    }
            special = self._special(value, value_type, type_name, depth)
            if special is not _MISSING:
                return special
            if value_type is dict:
                return self._mapping(value, depth, type_name)
            if value_type in (list, tuple):
                return self._sequence(value, depth, type_name, value_type)
            if value_type in (set, frozenset):
                return self._set(value, depth, type_name, value_type)
            field_names = _dataclass_fields(value_type)
            if field_names is not None:
                return self._dataclass(value, depth, type_name, field_names)
            return {"__type__": type_name}
        finally:
            self.active.remove(value_id)


def _validate_limits(max_depth, max_items, max_string_length, max_nodes, max_object_bytes):
    limits = {
        "max_depth": max_depth,
        "max_items": max_items,
        "max_string_length": max_string_length,
        "max_nodes": max_nodes,
        "max_object_bytes": max_object_bytes,
    }
    for name, value in limits.items():
        if type(value) is not int or value < (0 if name in {"max_depth", "max_items", "max_string_length"} else 1):
            raise ValueError(f"{name} must be a valid non-negative integer limit")


def validate_serialization_options(options):
    _validate_limits(
        options["max_depth"],
        options["max_items"],
        options["max_string_length"],
        options["max_nodes"],
        options["max_object_bytes"],
    )
    serializers = options["serializers"]
    if serializers is not None and type(serializers) is not dict:
        raise ValueError("serializers must be a dictionary of types to callables")
    if serializers is not None and any(
        not isinstance(value_type, type) or not callable(serializer)
        for value_type, serializer in serializers.items()
    ):
        raise ValueError("serializers must map types to callables")
    normalize_redaction_keys(options["redact"])
    minimum_summary = {"__truncated__": "max_object_bytes"}
    if _encoded_size(minimum_summary) > options["max_object_bytes"]:
        raise ValueError("max_object_bytes is too small for a JSON truncation summary")


def _encoded_size(value):
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return len(encoded.encode("utf-8"))


def snapshot_value(
    value,
    *,
    serializers=None,
    redact=(),
    max_depth=6,
    max_items=100,
    max_string_length=4096,
    max_nodes=1000,
    max_object_bytes=64_000,
):
    validate_serialization_options(
        {
            "serializers": serializers,
            "redact": redact,
            "max_depth": max_depth,
            "max_items": max_items,
            "max_string_length": max_string_length,
            "max_nodes": max_nodes,
            "max_object_bytes": max_object_bytes,
        }
    )
    snapshotter = _Snapshotter(
        serializers=serializers,
        redact=redact,
        max_depth=max_depth,
        max_items=max_items,
        max_string_length=max_string_length,
        max_nodes=max_nodes,
    )
    result = snapshotter.snapshot(value)
    try:
        size = _encoded_size(result)
    except (TypeError, ValueError, OverflowError):
        result = {"__type__": _type_name(value), "__truncated__": "serialization_error"}
        size = _encoded_size(result)
    if size <= max_object_bytes:
        return result
    summary = {
        "__type__": _type_name(value),
        "__total_bytes__": size,
        "__truncated__": "max_object_bytes",
    }
    if _encoded_size(summary) <= max_object_bytes:
        return summary
    minimal = {"__truncated__": "max_object_bytes"}
    if _encoded_size(minimal) <= max_object_bytes:
        return minimal
    raise ValueError("max_object_bytes is too small for a JSON truncation summary")


def argument_snapshot(frame, **options):
    code = frame.f_code
    argument_count = code.co_argcount + code.co_kwonlyargcount
    argument_count += bool(code.co_flags & inspect.CO_VARARGS)
    argument_count += bool(code.co_flags & inspect.CO_VARKEYWORDS)
    local_values = frame.f_locals
    redaction_keys = normalize_redaction_keys(options.get("redact", ()))
    result = {}
    for name in code.co_varnames[:argument_count]:
        if name not in local_values:
            continue
        if is_redacted_key(name, redaction_keys):
            result[name] = "<redacted>"
        else:
            result[name] = snapshot_value(local_values[name], **options)
    return result
