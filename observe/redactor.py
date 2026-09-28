def normalize_redaction_keys(keys):
    if type(keys) not in (tuple, list, set, frozenset) or any(
        type(key) is not str for key in keys
    ):
        raise ValueError("redact must be a sequence of string keys")
    return tuple(key.casefold() for key in keys if key)


def is_redacted_key(key, patterns):
    lowered = key.casefold()
    return any(pattern in lowered for pattern in patterns)
