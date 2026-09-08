"""Runtime resource path discovery for source checkouts and installed images.

The development stack installs the Python package into a virtualenv but copies
repo-level resources such as ``schemas/``, ``policy/``, ``migrations/`` and
``i18n/`` under ``/app``.  Source checkouts keep those resources at the project
root.  This module is the single place that resolves that difference.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

_ENV_ROOT = "AGENT_COLAB_RESOURCE_ROOT"


@lru_cache(maxsize=1)
def project_root() -> Path:
    """Return the directory containing repo-level Agent-Colab resources.

    Resolution order:
    1. ``AGENT_COLAB_RESOURCE_ROOT`` when set.
    2. Ancestors of this file that contain both ``schemas`` and ``policy``.
    3. Container/runtime install locations used by Docker images.

    The final fallback deliberately raises a useful path error instead of
    silently returning the Python package root.
    """

    configured = os.environ.get(_ENV_ROOT)
    if configured:
        return Path(configured).expanduser().resolve()

    here = Path(__file__).resolve()
    for base in (here.parent, *here.parents):
        if (base / "schemas").is_dir() and (base / "policy").is_dir():
            return base

    for base in (Path("/app"), Path("/workspace"), Path.cwd()):
        if (base / "schemas").is_dir() and (base / "policy").is_dir():
            return base.resolve()

    raise RuntimeError(
        "Agent-Colab resource root not found; set AGENT_COLAB_RESOURCE_ROOT "
        "to a directory containing schemas/ and policy/"
    )


def resource_path(*parts: str) -> Path:
    return project_root().joinpath(*parts)


def schemas_path(*parts: str) -> Path:
    return resource_path("schemas", *parts)


def policy_path(*parts: str) -> Path:
    return resource_path("policy", *parts)


def migrations_path(*parts: str) -> Path:
    return resource_path("migrations", *parts)


def i18n_path(*parts: str) -> Path:
    return resource_path("i18n", *parts)


def web_admin_dist() -> Path:
    return resource_path("web-admin", "dist")
