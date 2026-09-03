"""V-P7-01: a clean production-like install on empty volumes reaches LOCKED through the Wizard.

The install itself runs containers, so this test skips with a stated reason where Docker, a
released image or a reachable Mattermost is unavailable, and otherwise performs the real thing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tools import clean_install

ROOT = Path(__file__).resolve().parents[2]


def test_clean_install_reaches_locked() -> None:
    ok, detail = clean_install.available()
    if not ok:
        pytest.skip(f"clean install not runnable here: {detail}")
    report = clean_install.run_install(port=int(os.environ.get("COLAB_CLEAN_PORT", "8099")))
    payload = report.as_dict()
    out = ROOT / "evidence" / "phase-7" / "install" / "clean-install.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    assert report.state == "LOCKED", payload
    assert report.seconds < clean_install.DEADLINE_S, payload
    assert report.ok, payload
