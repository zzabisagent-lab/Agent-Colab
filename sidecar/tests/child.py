"""Child process used by the tests: reads the injected value (env or fd), prints an HMAC of it
(never the value), then optionally holds until terminated."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys
import time

KEY = b"sidecar-test-key"


def read_value() -> bytes | None:
    fd_env = os.environ.get("AGENT_COLAB_SECRET_FD")
    if fd_env:
        fd = int(fd_env)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
        os.close(fd)
        return b"".join(chunks)
    name = os.environ.get("SECRET_ENV_NAME", "AGENT_COLAB_SECRET")
    value = os.environ.get(name)
    if value is None:
        return None
    if os.environ.get(f"{name}_ENCODING") == "base64":
        return base64.b64decode(value)
    return value.encode("utf-8")


def main() -> int:
    value = read_value()
    if value is None:
        print("no-secret", flush=True)
    else:
        print("hmac=" + hmac.new(KEY, value, hashlib.sha256).hexdigest(), flush=True)
    if "--hold" in sys.argv:
        while True:
            time.sleep(0.05)
    return 0


if __name__ == "__main__":
    sys.exit(main())
