"""RFC 8785 (JSON Canonicalization Scheme) serialization.

Used for every stored Event hash, snapshot hash, and idempotency body comparison. The output is
bytes so it can be hashed directly. Only JSON-compatible values are accepted; ``float`` values
must be finite and are serialized with ECMAScript ``Number::toString`` semantics as RFC 8785
requires. Callers are encouraged to keep monetary or precise values as integers or strings.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

_ESCAPES = {
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
    '"': '\\"',
    "\\": "\\\\",
}


class CanonicalizationError(ValueError):
    """Raised when a value cannot be represented in canonical JSON."""


def _escape_string(value: str) -> str:
    out: list[str] = ['"']
    for ch in value:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _es6_number(value: float) -> str:
    """ECMAScript Number::toString for finite doubles (RFC 8785 §3.2.2.3)."""
    if math.isnan(value) or math.isinf(value):
        raise CanonicalizationError("non-finite numbers are not allowed")
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    value = abs(value)
    r = repr(value)  # shortest round-trip digits, Python format
    m = re.fullmatch(r"(\d+)(?:\.(\d+))?(?:e([+-]\d+))?", r)
    if not m:  # pragma: no cover - repr always matches
        raise CanonicalizationError(f"unexpected float repr {r}")
    int_part, frac_part, exp_part = m.group(1), m.group(2) or "", m.group(3)
    exp = int(exp_part) if exp_part else 0
    if int_part == "0":
        stripped = frac_part.lstrip("0")
        n = -(len(frac_part) - len(stripped)) + exp
        digits = stripped
    else:
        digits = (int_part + frac_part).lstrip("0")
        n = len(int_part) + exp
    digits = digits.rstrip("0") or "0"
    k = len(digits)
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + digits
    e = n - 1
    exp_text = f"e{'+' if e >= 0 else '-'}{abs(e)}"
    if k == 1:
        return sign + digits + exp_text
    return sign + digits[0] + "." + digits[1:] + exp_text


def _serialize(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        out.append(str(value))
    elif isinstance(value, float):
        out.append(_es6_number(value))
    elif isinstance(value, str):
        out.append(_escape_string(value))
    elif isinstance(value, Mapping):
        keys = list(value.keys())
        if any(not isinstance(k, str) for k in keys):
            raise CanonicalizationError("object keys must be strings")
        keys.sort(key=lambda k: k.encode("utf-16-be"))
        out.append("{")
        for i, k in enumerate(keys):
            if i:
                out.append(",")
            out.append(_escape_string(k))
            out.append(":")
            _serialize(value[k], out)
        out.append("}")
    elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _serialize(item, out)
        out.append("]")
    else:
        raise CanonicalizationError(f"unsupported type {type(value).__name__}")


def canonical_json(value: Any) -> bytes:
    """Serialize ``value`` as RFC 8785 canonical JSON (UTF-8 bytes)."""
    out: list[str] = []
    _serialize(value, out)
    return "".join(out).encode("utf-8")
