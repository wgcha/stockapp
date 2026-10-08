"""Safe loading of application environment files.

The loader deliberately treats an environment file as data.  It never passes
the file through a shell and does not expand references such as ``${TOKEN}``.
"""
from __future__ import annotations

import os
import stat
from io import StringIO
from os import PathLike
from typing import MutableMapping

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from .data_lock import checked_path


class EnvFileError(ValueError):
    """Raised when an environment file cannot be safely loaded."""


def load_environment_file(
    path: str | PathLike[str],
    environment: MutableMapping[str, str] | None = None,
) -> MutableMapping[str, str]:
    """Validate and load *path* into ``environment``.

    Existing keys always win, including keys whose existing value is an empty
    string.  The supplied mapping is changed only after the complete file has
    been read and validated.  The mapping is returned for convenient use by
    callers; when omitted, this is ``os.environ``.
    """
    target = os.environ if environment is None else environment

    try:
        checked = checked_path(path)
        if checked.is_symlink() or not stat.S_ISREG(checked.stat().st_mode):
            raise EnvFileError("Environment file must be a regular file")
        text = checked.read_text(encoding="utf-8-sig")
    except EnvFileError:
        raise
    except (OSError, UnicodeError, TypeError, ValueError):
        raise EnvFileError("Unable to read environment file") from None

    try:
        bindings = tuple(parse_stream(StringIO(text)))
        for binding in bindings:
            # Comments and blank lines are represented as key/value-less
            # bindings by python-dotenv and are valid.
            if binding.error:
                raise EnvFileError("Invalid environment file")
            if binding.key is None:
                continue
            if (
                not binding.key
                or "=" in binding.key
                or "\x00" in binding.key
                or binding.value is None
                or "\x00" in binding.value
            ):
                raise EnvFileError("Invalid environment file")

        # Use the package's public parser for the resulting values while
        # explicitly disabling interpolation.  ``parse_stream`` above is the
        # validation pass because dotenv_values intentionally skips bad lines.
        values = dotenv_values(stream=StringIO(text), interpolate=False, encoding="utf-8")
        if any(value is None for value in values.values()):
            raise EnvFileError("Invalid environment file")
        updates: dict[str, str] = {}
        for key, value in values.items():
            # The validation pass rejects None and NUL values. Keep this
            # guard for parser/API drift before touching the target mapping.
            if value is None or not key or "=" in key or "\x00" in key or "\x00" in value:
                raise EnvFileError("Invalid environment file")
            if key not in target:
                updates[key] = value
    except EnvFileError:
        raise
    except Exception:
        # Do not expose parser details: malformed files can contain secrets.
        raise EnvFileError("Invalid environment file") from None

    try:
        target.update(updates)
    except Exception:
        # A mutable mapping may reject writes.  Do not leak its exception text.
        raise EnvFileError("Unable to update environment") from None
    return target
