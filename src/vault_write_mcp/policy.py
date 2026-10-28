"""Conservative segment-prefix protections for explicit service write APIs."""

import unicodedata


def canonical_path(value):
    return unicodedata.normalize("NFC", value).casefold()


def normalized_rule(value):
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError("write_policy paths must be nonempty relative POSIX paths of at most 1024 characters.")
    name = value.removesuffix("/")
    if (not name or "\\" in name or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or any(c in name for c in "*?[]") or any(not part or part.startswith(".") for part in name.split("/"))):
        raise ValueError("write_policy paths must be visible relative POSIX paths without dot segments or glob syntax.")
    try:
        name.encode("utf-8")
    except UnicodeError:
        raise ValueError("write_policy paths must be valid UTF-8.") from None
    return canonical_path(name)


def permits(config, path):
    name = canonical_path(path)
    def matches(rule):
        return name == rule or name.startswith(rule + "/")
    return not any(matches(rule) for rule in config.deny_paths) and (
        not config.allow_paths or any(matches(rule) for rule in config.allow_paths))
