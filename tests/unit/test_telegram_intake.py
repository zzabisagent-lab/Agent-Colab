"""P2-04: update normalization, polling offsets, webhook spoof/replay/stale (V-P2-09 unit part),
and the attachment policy matrix (V-P2-11)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from server.artifacts.storage import ArtifactStorage, ArtifactStorageError
from server.channels.telegram.attachments import (
    ATTACHMENT_SCAN_FAILED,
    ATTACHMENT_TOO_LARGE,
    AttachmentPolicy,
    evaluate_attachment,
    fetch_to_artifact,
)
from server.channels.telegram.client import FakeTelegramClient
from server.channels.telegram.intake import (
    InboundAttachment,
    IntakeError,
    MemoryOffsetStore,
    normalize_update,
    poll_updates,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telegram"
UPDATES: dict[str, dict[str, Any]] = json.loads((FIXTURES / "updates-samples.json").read_text())
CASES = yaml.safe_load((FIXTURES / "attachments-cases.yaml").read_text())["cases"]
PI = "tg:424242"


def test_normalize_topic_general_reply_and_service_messages() -> None:
    topic = normalize_update(PI, UPDATES["topic_message"])
    assert topic is not None and topic.message_thread_id == 3 and topic.is_topic_message
    assert topic.from_display_name == "Ada L" and topic.chat_id == "-1001234567890"
    general = normalize_update(PI, UPDATES["general_message"])
    assert (
        general is not None and general.message_thread_id is None and not general.is_topic_message
    )
    reply = normalize_update(PI, UPDATES["reply_in_topic"])
    assert (
        reply is not None and reply.reply_to_message_id == 41 and reply.from_display_name == "bob"
    )
    svc = normalize_update(PI, UPDATES["forum_topic_created"])
    assert svc is not None and svc.forum_topic_created == "agent-colab-topic" and svc.from_is_bot
    assert svc.message_id == svc.message_thread_id == 3  # spike: topic id == service message id
    edited = normalize_update(PI, UPDATES["edited"])
    assert edited is not None and edited.edited and edited.raw_kind == "edited_message"
    assert normalize_update(PI, UPDATES["membership"]) is None


def test_normalize_attachments_and_invalid_update() -> None:
    doc = normalize_update(PI, UPDATES["document"])
    assert doc is not None and doc.text == "the report"
    assert doc.attachments == (
        InboundAttachment("document", "BQACAgIAAxkBAAIBc", "report.pdf", "application/pdf", 12345),
    )
    photo = normalize_update(PI, UPDATES["photo"])
    assert photo is not None and photo.attachments[0].file_id == "large"  # largest size wins
    with pytest.raises(IntakeError) as exc:
        normalize_update(PI, UPDATES["invalid_missing_chat"])
    assert exc.value.code == "TELEGRAM_UPDATE_INVALID"


def test_poll_updates_persists_offsets_and_skips_unsupported() -> None:
    fake = FakeTelegramClient()
    fake.updates = [UPDATES["topic_message"], UPDATES["membership"], UPDATES["general_message"]]
    seen: list[int] = []
    store = MemoryOffsetStore()
    handled = list(poll_updates(fake, PI, lambda m: seen.append(m.message_id), store, max_rounds=1))
    assert handled == [900001, 900002, 900008] and seen == [41, 42]
    assert store.load(PI) == 900009
    # a second round starts from the saved offset and re-handles nothing
    assert (
        list(poll_updates(fake, PI, lambda m: seen.append(m.message_id), store, max_rounds=1)) == []
    )


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_attachment_policy_matrix(case: dict[str, Any]) -> None:
    meta = InboundAttachment(
        case["kind"], "file-x", case["file_name"], case["mime_type"], case["file_size"]
    )
    policy = (
        AttachmentPolicy.from_dict(case["policy"]) if case.get("policy") else AttachmentPolicy()
    )
    decision = evaluate_attachment(meta, policy)
    assert decision.allowed is case["expect_allowed"], decision
    if case.get("expect_code"):
        assert decision.reason_code == case["expect_code"]


def test_policy_schema_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError):
        AttachmentPolicy.from_dict({"max_bytes": 10, "surprise": True})


def test_fetch_to_artifact_stores_allowed_and_blocks_denied(tmp_path: Path) -> None:
    storage = ArtifactStorage(root=tmp_path)
    fake = FakeTelegramClient()
    ws = "0f1e2d3c-4b5a-4a6b-8c7d-9e8f7a6b5c4d"
    fetched = fetch_to_artifact(
        fake, storage, ws, InboundAttachment("document", "f1", "report.txt", "text/plain", 4)
    )
    assert (
        fetched.blob.sha256 == hashlib.sha256(b"data").hexdigest() and fetched.mime == "text/plain"
    )
    assert storage.read(fetched.blob.storage_uri, fetched.blob.sha256) == b"data"
    with pytest.raises(ArtifactStorageError) as exc:
        fetch_to_artifact(
            fake,
            storage,
            ws,
            InboundAttachment("document", "f2", "tool.exe", "application/x-msdownload", 4),
        )
    assert exc.value.code == "ATTACHMENT_MIME_DENIED"
    assert [c for c in fake.calls if c[0] == "getFile"] == [
        ("getFile", {"file_id": "f1"})
    ]  # denied never downloaded
    with pytest.raises(ArtifactStorageError) as exc2:
        fetch_to_artifact(
            fake,
            storage,
            ws,
            InboundAttachment("document", "f3", "ok.txt", "text/plain", 4),
            AttachmentPolicy(max_bytes=2),
        )
    assert exc2.value.code == ATTACHMENT_TOO_LARGE

    class DirtyScanner:
        def scan(self, path: Path) -> Any:
            from server.artifacts.storage import ScanResult

            return ScanResult(False, "EICAR")

    with pytest.raises(ArtifactStorageError) as exc3:
        fetch_to_artifact(
            fake,
            storage,
            ws,
            InboundAttachment("document", "f4", "ok2.txt", "text/plain", 4),
            scanner=DirtyScanner(),
        )
    assert exc3.value.code == ATTACHMENT_SCAN_FAILED
