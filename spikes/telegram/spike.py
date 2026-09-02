"""Telegram Bot API spike for P0-13 / V-P0-19.

Runs against two forum-enabled supergroups using the bot token from ``.env``. Every request and
response is written, redacted, to ``evidence/phase-0/spikes/telegram/calls.jsonl`` and a
``summary.json`` records the observations the Bridge contract must not contradict.

Redaction: the bot token, the bot id (token prefix), chat ids, chat titles/usernames and any human
user data or message text from ``getUpdates`` never reach the evidence files.

Run: ``uv run python -m spikes.telegram.spike``
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "evidence" / "phase-0" / "spikes" / "telegram"
API = "https://api.telegram.org"


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class Redactor:
    def __init__(self, token: str, chats: dict[str, str]) -> None:
        self.token = token
        self.bot_id = token.split(":", 1)[0]
        self.chats = chats  # raw id -> label
        self.titles: set[str] = set()

    def text(self, s: str) -> str:
        s = s.replace(self.token, "<redacted-token>")
        for raw, label in self.chats.items():
            s = s.replace(raw, label)
            # supergroup ids appear as -100xxxxxxxxxx and as bare xxxxxxxxxx in some fields
            bare = raw.removeprefix("-100")
            if bare and bare != raw:
                s = re.sub(rf"(?<!\d){re.escape(bare)}(?!\d)", label, s)
        s = re.sub(rf"(?<!\d){re.escape(self.bot_id)}(?!\d)", "<bot-id>", s)
        # any other supergroup id (e.g. migrate_to_chat_id of unrelated chats in getUpdates)
        s = re.sub(r"-100\d{9,}", "<chat-id>", s)
        for t in self.titles:
            if t:
                s = s.replace(t, "<chat-title>")
        return s

    def value(self, v: Any, key: str = "") -> Any:
        if isinstance(v, dict):
            out: dict[str, Any] = {}
            for k, x in v.items():
                if k in {"title", "username", "first_name", "last_name", "invite_link"}:
                    if isinstance(x, str) and k == "title":
                        self.titles.add(x)
                    out[k] = "<redacted>" if x else x
                elif k == "from" and isinstance(x, dict):
                    is_bot_id = str(x.get("id")) == self.bot_id
                    out[k] = {
                        "is_bot": x.get("is_bot"),
                        "id": "<bot-id>" if is_bot_id else "<user-id>",
                    }
                elif k in {"migrate_from_chat_id", "migrate_to_chat_id"}:
                    out[k] = "<chat-id>"
                elif k in {"text", "caption"} and isinstance(x, str) and key == "update":
                    out[k] = f"<redacted:len={len(x)}>"
                elif k == "id" and isinstance(x, int) and str(x) in self.chats:
                    out[k] = self.chats[str(x)]
                elif k == "id" and isinstance(x, int) and str(x) == self.bot_id:
                    out[k] = "<bot-id>"
                else:
                    out[k] = self.value(x, key)
            return out
        if isinstance(v, list):
            return [self.value(x, key) for x in v]
        if isinstance(v, str):
            return self.text(v)
        return v


class Spike:
    def __init__(self) -> None:
        env = load_env()
        self.token = env["TELEGRAM_BOT_TOKEN"]
        self.chat_a = env["TELEGRAM_TEST_CHAT_A"]
        self.chat_b = env["TELEGRAM_TEST_CHAT_B"]
        self.red = Redactor(self.token, {self.chat_a: "chat-A", self.chat_b: "chat-B"})
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        self.calls = (EVIDENCE / "calls.jsonl").open("w", encoding="utf-8")
        self.summary: dict[str, Any] = {"started_at": _now(), "steps": {}, "observations": {}}
        self.client = httpx.AsyncClient(timeout=30)
        self.created: list[tuple[str, int]] = []  # (chat, message_id) to delete
        self.topics: list[tuple[str, int]] = []  # (chat, thread_id) to delete
        self.step = "init"

    async def call(self, method: str, **params: Any) -> tuple[int, dict[str, Any], float]:
        t0 = time.time()
        try:
            r = await self.client.post(f"{API}/bot{self.token}/{method}", json=params)
            status, body = r.status_code, r.json()
        except (httpx.HTTPError, ValueError) as exc:
            status, body = 0, {"ok": False, "transport_error": type(exc).__name__}
        elapsed = round(time.time() - t0, 3)
        rec = {
            "t": _now(),
            "step": self.step,
            "method": method,
            "params": self.red.value(params),
            "status": status,
            "elapsed_s": elapsed,
            "response": self.red.value(body, "update" if method == "getUpdates" else ""),
        }
        self.calls.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.calls.flush()
        return status, body, elapsed

    def label(self, chat: str) -> str:
        return self.red.chats[chat]

    async def run(self) -> None:
        obs = self.summary["observations"]
        steps = self.summary["steps"]
        # (a) identity, chats, rights
        self.step = "a-identity"
        st, me, _ = await self.call("getMe")
        steps["getMe"] = {
            "status": st,
            "ok": me.get("ok"),
            "is_bot": me.get("result", {}).get("is_bot"),
        }
        obs["bot_can_join_groups"] = me.get("result", {}).get("can_join_groups")
        obs["bot_can_read_all_group_messages"] = me.get("result", {}).get(
            "can_read_all_group_messages"
        )
        for chat in (self.chat_a, self.chat_b):
            st, c, _ = await self.call("getChat", chat_id=chat)
            res = c.get("result", {})
            steps[f"getChat:{self.label(chat)}"] = {
                "status": st,
                "type": res.get("type"),
                "is_forum": res.get("is_forum"),
            }
            st, m, _ = await self.call("getChatMember", chat_id=chat, user_id=int(self.red.bot_id))
            mres = m.get("result", {})
            rights = {k: v for k, v in mres.items() if k.startswith("can_")}
            steps[f"getChatMember:{self.label(chat)}"] = {
                "status": st,
                "member_status": mres.get("status"),
                "rights": rights,
            }
            obs[f"rights:{self.label(chat)}"] = {"status": mres.get("status"), **rights}
        # (b) topic in chat A, messages, reply, edit
        self.step = "b-topic-A"
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        st, t, _ = await self.call(
            "createForumTopic", chat_id=self.chat_a, name=f"agent-colab-spike-{stamp}"
        )
        topic_a = t.get("result", {}).get("message_thread_id")
        steps["createForumTopic:chat-A"] = {
            "status": st,
            "ok": t.get("ok"),
            "message_thread_id": topic_a,
            "error": t.get("description"),
        }
        if topic_a:
            self.topics.append((self.chat_a, topic_a))
            st, m1, _ = await self.call(
                "sendMessage",
                chat_id=self.chat_a,
                message_thread_id=topic_a,
                text="spike root message",
            )
            r1 = m1.get("result", {})
            steps["sendMessage:topic-A"] = {
                "status": st,
                "message_id": r1.get("message_id"),
                "message_thread_id": r1.get("message_thread_id"),
                "is_topic_message": r1.get("is_topic_message"),
            }
            obs["topic_message_fields"] = {
                k: r1.get(k) for k in ("message_thread_id", "is_topic_message")
            }
            obs["topic_thread_id_equals_first_message_id"] = None
            if r1.get("message_id"):
                self.created.append((self.chat_a, r1["message_id"]))
                st, m2, _ = await self.call(
                    "sendMessage",
                    chat_id=self.chat_a,
                    message_thread_id=topic_a,
                    text="spike reply",
                    reply_parameters={"message_id": r1["message_id"]},
                )
                r2 = m2.get("result", {})
                rt = r2.get("reply_to_message", {})
                steps["sendMessage:reply-in-topic-A"] = {
                    "status": st,
                    "message_id": r2.get("message_id"),
                    "message_thread_id": r2.get("message_thread_id"),
                    "reply_to_message_id": rt.get("message_id"),
                    "reply_to_is_topic_message": rt.get("is_topic_message"),
                }
                obs["reply_in_topic_keeps_thread_id"] = r2.get("message_thread_id") == topic_a
                obs["reply_to_message_present"] = bool(rt)
                if r2.get("message_id"):
                    self.created.append((self.chat_a, r2["message_id"]))
                st, e1, _ = await self.call(
                    "editMessageText",
                    chat_id=self.chat_a,
                    message_id=r1["message_id"],
                    text="spike root message (edited)",
                )
                er = e1.get("result", {})
                steps["editMessageText:own-A"] = {
                    "status": st,
                    "ok": e1.get("ok"),
                    "edit_date_present": "edit_date" in er,
                    "message_thread_id": er.get("message_thread_id"),
                }
                obs["edit_own_message_possible"] = bool(e1.get("ok"))
                # unchanged text edit -> documented 400 "message is not modified"
                st, e2, _ = await self.call(
                    "editMessageText",
                    chat_id=self.chat_a,
                    message_id=r1["message_id"],
                    text="spike root message (edited)",
                )
                steps["editMessageText:unchanged-A"] = {
                    "status": st,
                    "ok": e2.get("ok"),
                    "description": e2.get("description"),
                }

                # topic thread id == id of the forum_topic_created service message
            st, s1, _ = await self.call(
                "sendMessage",
                chat_id=self.chat_a,
                message_thread_id=topic_a,
                text="probe reply to topic service message",
                reply_parameters={"message_id": topic_a},
            )
            sr = s1.get("result", {})
            rt = sr.get("reply_to_message", {})
            steps["sendMessage:reply-to-topic-service-message-A"] = {
                "status": st,
                "ok": s1.get("ok"),
                "reply_to_message_id": rt.get("message_id"),
                "reply_to_forum_topic_created": rt.get("forum_topic_created"),
                "description": s1.get("description"),
            }
            obs["topic_thread_id_equals_first_message_id"] = (
                bool(rt) and rt.get("message_id") == topic_a
            )
            obs["topic_creating_message_is_forum_topic_created"] = bool(
                rt.get("forum_topic_created")
            )
            if sr.get("message_id"):
                self.created.append((self.chat_a, sr["message_id"]))

                # edit a message not owned by the bot: the topic service message
            st, e3, _ = await self.call(
                "editMessageText", chat_id=self.chat_a, message_id=topic_a, text="x"
            )
            steps["editMessageText:not-own-A"] = {
                "status": st,
                "ok": e3.get("ok"),
                "description": e3.get("description"),
            }
            obs["edit_foreign_message_rejected"] = not e3.get("ok")
        # (c) chat B: General topic and a second topic
        self.step = "c-chat-B"
        st, g1, _ = await self.call(
            "sendMessage", chat_id=self.chat_b, text="spike general-topic message"
        )
        gr = g1.get("result", {})
        steps["sendMessage:general-B(omitted-thread)"] = {
            "status": st,
            "message_id": gr.get("message_id"),
            "message_thread_id": gr.get("message_thread_id"),
            "is_topic_message": gr.get("is_topic_message"),
        }
        obs["general_topic_message_has_thread_id"] = "message_thread_id" in gr
        obs["general_topic_message_thread_id"] = gr.get("message_thread_id")
        if gr.get("message_id"):
            self.created.append((self.chat_b, gr["message_id"]))
        st, g2, _ = await self.call(
            "sendMessage",
            chat_id=self.chat_b,
            message_thread_id=1,
            text="spike general-topic message (thread id 1)",
        )
        gr2 = g2.get("result", {})
        steps["sendMessage:general-B(thread-1)"] = {
            "status": st,
            "ok": g2.get("ok"),
            "message_id": gr2.get("message_id"),
            "message_thread_id": gr2.get("message_thread_id"),
            "description": g2.get("description"),
        }
        obs["general_topic_accepts_thread_id_1"] = bool(g2.get("ok"))
        if gr2.get("message_id"):
            self.created.append((self.chat_b, gr2["message_id"]))
        st, t2, _ = await self.call(
            "createForumTopic", chat_id=self.chat_b, name=f"agent-colab-spike-{stamp}-b"
        )
        topic_b = t2.get("result", {}).get("message_thread_id")
        steps["createForumTopic:chat-B"] = {
            "status": st,
            "ok": t2.get("ok"),
            "message_thread_id": topic_b,
            "error": t2.get("description"),
        }
        if topic_b:
            self.topics.append((self.chat_b, topic_b))
            st, b1, _ = await self.call(
                "sendMessage",
                chat_id=self.chat_b,
                message_thread_id=topic_b,
                text="spike topic-B message",
            )
            br = b1.get("result", {})
            steps["sendMessage:topic-B"] = {
                "status": st,
                "message_id": br.get("message_id"),
                "message_thread_id": br.get("message_thread_id"),
            }
            if br.get("message_id"):
                self.created.append((self.chat_b, br["message_id"]))
            # cross-topic reply: reply_parameters to a message in another topic
            if gr.get("message_id"):
                st, x1, _ = await self.call(
                    "sendMessage",
                    chat_id=self.chat_b,
                    message_thread_id=topic_b,
                    text="cross-topic reply probe",
                    reply_parameters={"message_id": gr["message_id"]},
                )
                xr = x1.get("result", {})
                steps["sendMessage:cross-topic-reply-B"] = {
                    "status": st,
                    "ok": x1.get("ok"),
                    "message_thread_id": xr.get("message_thread_id"),
                    "reply_to_present": "reply_to_message" in xr,
                    "description": x1.get("description"),
                }
                obs["cross_topic_reply"] = {
                    "ok": bool(x1.get("ok")),
                    "lands_in_thread": xr.get("message_thread_id"),
                }
                if xr.get("message_id"):
                    self.created.append((self.chat_b, xr["message_id"]))
        # (d) update shape
        self.step = "d-updates"
        st, u, _ = await self.call(
            "getUpdates", timeout=0, allowed_updates=["message", "edited_message"]
        )
        ups = u.get("result", []) if isinstance(u.get("result"), list) else []
        shapes = []
        for up in ups[-10:]:
            msg = up.get("message") or up.get("edited_message") or {}
            shapes.append(
                {
                    "update_keys": sorted(up.keys()),
                    "message_thread_id": msg.get("message_thread_id"),
                    "is_topic_message": msg.get("is_topic_message"),
                    "has_reply_to_message": "reply_to_message" in msg,
                    "forum_topic_created": bool(msg.get("forum_topic_created")),
                    "chat_label": self.red.chats.get(str(msg.get("chat", {}).get("id")), "other"),
                }
            )
        steps["getUpdates"] = {"status": st, "ok": u.get("ok"), "count": len(ups), "shapes": shapes}
        obs["bot_own_messages_in_getUpdates"] = any(
            str((up.get("message") or {}).get("from", {}).get("id")) == self.red.bot_id
            for up in ups
        )
        obs["updates_available"] = len(ups)
        # (e) rate limit burst into topic A (or chat B general if no topic)
        self.step = "e-rate-limit"
        target_chat, target_thread = (self.chat_a, topic_a) if topic_a else (self.chat_b, None)
        results: list[dict[str, Any]] = []
        sem = asyncio.Semaphore(5)
        t_start = time.time()

        async def send(i: int) -> None:
            async with sem:
                params: dict[str, Any] = {"chat_id": target_chat, "text": f"burst {i:02d}"}
                if target_thread:
                    params["message_thread_id"] = target_thread
                st, b, el = await self.call("sendMessage", **params)
                ra = (
                    b.get("parameters", {}).get("retry_after")
                    if isinstance(b.get("parameters"), dict)
                    else None
                )
                results.append(
                    {
                        "i": i,
                        "status": st,
                        "retry_after": ra,
                        "t": round(time.time() - t_start, 3),
                        "elapsed_s": el,
                    }
                )
                if b.get("ok") and b["result"].get("message_id"):
                    self.created.append((target_chat, b["result"]["message_id"]))

        await asyncio.gather(*(send(i) for i in range(40)))
        results.sort(key=lambda r: r["i"])
        burst_s = round(time.time() - t_start, 3)
        n429 = [r for r in results if r["status"] == 429]
        ok = [r for r in results if r["status"] == 200]
        steps["burst40"] = {
            "total_s": burst_s,
            "sent_ok": len(ok),
            "http_429": len(n429),
            "retry_after_values": sorted(
                {r["retry_after"] for r in n429 if r["retry_after"] is not None}
            ),
            "results": results,
        }
        obs["burst_40_messages"] = {
            "ok": len(ok),
            "http_429": len(n429),
            "total_s": burst_s,
            "throughput_msg_per_s": round(len(ok) / burst_s, 2) if burst_s else None,
            "max_retry_after_s": max((r["retry_after"] or 0) for r in n429) if n429 else 0,
        }
        if n429:
            wait = max((r["retry_after"] or 1) for r in n429)
            await asyncio.sleep(min(wait, 60))
            st, after, _ = await self.call(
                "sendMessage",
                chat_id=target_chat,
                text="after retry_after",
                **({"message_thread_id": target_thread} if target_thread else {}),
            )
            steps["send-after-retry_after"] = {
                "status": st,
                "ok": after.get("ok"),
                "waited_s": min(wait, 60),
            }
            if after.get("ok"):
                self.created.append((target_chat, after["result"]["message_id"]))
        # (f) cleanup
        self.step = "f-cleanup"
        deleted, failed = 0, 0
        for chat, mid in self.created:
            st, d, _ = await self.call("deleteMessage", chat_id=chat, message_id=mid)
            if d.get("ok"):
                deleted += 1
            else:
                failed += 1
                if st == 429:
                    await asyncio.sleep(d.get("parameters", {}).get("retry_after", 1))
        steps["deleteMessage"] = {"deleted": deleted, "failed": failed}
        obs["delete_own_messages_possible"] = deleted > 0 and failed == 0
        topic_results = []
        for chat, tid in self.topics:
            st, c, _ = await self.call("closeForumTopic", chat_id=chat, message_thread_id=tid)
            _st2, d2, _ = await self.call("deleteForumTopic", chat_id=chat, message_thread_id=tid)
            topic_results.append(
                {
                    "chat": self.label(chat),
                    "thread_id": tid,
                    "close_ok": c.get("ok"),
                    "delete_ok": d2.get("ok"),
                    "delete_error": d2.get("description"),
                }
            )
        steps["topics-cleanup"] = topic_results
        obs["delete_topic_possible"] = (
            all(r["delete_ok"] for r in topic_results) if topic_results else None
        )
        self.summary["finished_at"] = _now()
        (EVIDENCE / "summary.json").write_text(
            json.dumps(self.red.value(self.summary), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        self.calls.close()
        await self.client.aclose()


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds")


def main() -> int:
    asyncio.run(Spike().run())
    summary = json.loads((EVIDENCE / "summary.json").read_text(encoding="utf-8"))
    print(json.dumps(summary["observations"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
