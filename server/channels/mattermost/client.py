"""Mattermost REST v4 client (development plan §7A.1).

``MattermostClient`` is the protocol the gateway codes against; ``HttpMattermostClient`` talks to
a real server with a bot or admin token; ``FakeMattermostClient`` records calls for unit tests.
Tokens are held in memory only and never logged.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


class MattermostError(RuntimeError):
    def __init__(self, code: str, status: int, detail: str = "") -> None:
        super().__init__(f"{code}: {status} {detail}")
        self.code = code
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Post:
    id: str
    channel_id: str
    user_id: str
    message: str
    root_id: str = ""
    props: dict[str, Any] = field(default_factory=dict)
    create_at: int = 0
    update_at: int = 0


class MattermostClient(Protocol):
    def me(self) -> dict[str, Any]: ...

    def get_user(self, user_id: str) -> dict[str, Any]: ...

    def get_user_by_username(self, username: str) -> dict[str, Any]: ...

    def get_channel(self, channel_id: str) -> dict[str, Any]: ...

    def list_team_channels(self, team_id: str) -> list[dict[str, Any]]: ...

    def get_team_by_name(self, name: str) -> dict[str, Any]: ...

    def create_post(
        self,
        channel_id: str,
        message: str,
        root_id: str | None = None,
        props: dict[str, Any] | None = None,
    ) -> Post: ...

    def patch_post(
        self, post_id: str, message: str, props: dict[str, Any] | None = None
    ) -> Post: ...

    def get_post(self, post_id: str) -> Post: ...

    def list_posts(self, channel_id: str, per_page: int = 60) -> list[Post]: ...

    def ephemeral(self, user_id: str, channel_id: str, message: str) -> None: ...

    def direct_message(self, user_id: str, message: str) -> Post: ...

    def create_command(
        self, team_id: str, trigger: str, url: str, description: str = ""
    ) -> dict[str, Any]: ...

    def regen_command_token(self, command_id: str) -> dict[str, Any]: ...

    def update_command(self, command: dict[str, Any]) -> dict[str, Any]: ...

    def list_commands(self, team_id: str) -> list[dict[str, Any]]: ...

    def get_config(self) -> dict[str, Any]: ...


def _post_from(data: dict[str, Any]) -> Post:
    return Post(
        id=str(data.get("id", "")),
        channel_id=str(data.get("channel_id", "")),
        user_id=str(data.get("user_id", "")),
        message=str(data.get("message", "")),
        root_id=str(data.get("root_id", "") or ""),
        props=dict(data.get("props") or {}),
        create_at=int(data.get("create_at", 0) or 0),
        update_at=int(data.get("update_at", 0) or 0),
    )


class HttpMattermostClient:
    def __init__(self, base_url: str, token: str, http: httpx.Client | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=15.0)
        self._headers = {"Authorization": f"Bearer {token}"}

    def __repr__(self) -> str:
        return f"HttpMattermostClient({self._base!r}, token=<redacted>)"

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self._http.request(
            method, f"{self._base}/api/v4{path}", headers=self._headers, **kwargs
        )
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            raise MattermostError(
                str(body.get("id", "MATTERMOST_ERROR")),
                resp.status_code,
                str(body.get("message", "")),
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def me(self) -> dict[str, Any]:
        return dict(self._request("GET", "/users/me"))

    def get_user(self, user_id: str) -> dict[str, Any]:
        return dict(self._request("GET", f"/users/{user_id}"))

    def get_user_by_username(self, username: str) -> dict[str, Any]:
        return dict(self._request("GET", f"/users/username/{username.lstrip('@')}"))

    def get_channel(self, channel_id: str) -> dict[str, Any]:
        return dict(self._request("GET", f"/channels/{channel_id}"))

    def list_team_channels(self, team_id: str) -> list[dict[str, Any]]:
        return list(self._request("GET", f"/teams/{team_id}/channels", params={"per_page": 200}))

    def get_team_by_name(self, name: str) -> dict[str, Any]:
        return dict(self._request("GET", f"/teams/name/{name}"))

    def create_post(
        self,
        channel_id: str,
        message: str,
        root_id: str | None = None,
        props: dict[str, Any] | None = None,
    ) -> Post:
        body: dict[str, Any] = {"channel_id": channel_id, "message": message}
        if root_id:
            body["root_id"] = root_id
        if props:
            body["props"] = props
        return _post_from(self._request("POST", "/posts", json=body))

    def patch_post(self, post_id: str, message: str, props: dict[str, Any] | None = None) -> Post:
        body: dict[str, Any] = {"message": message}
        if props is not None:
            body["props"] = props
        return _post_from(self._request("PUT", f"/posts/{post_id}/patch", json=body))

    def get_post(self, post_id: str) -> Post:
        return _post_from(self._request("GET", f"/posts/{post_id}"))

    def list_posts(self, channel_id: str, per_page: int = 60) -> list[Post]:
        data = self._request("GET", f"/channels/{channel_id}/posts", params={"per_page": per_page})
        posts = data.get("posts", {})
        ordered = [posts[pid] for pid in data.get("order", []) if pid in posts]
        return [_post_from(p) for p in ordered]

    def ephemeral(self, user_id: str, channel_id: str, message: str) -> None:
        self._request(
            "POST",
            "/posts/ephemeral",
            json={"user_id": user_id, "post": {"channel_id": channel_id, "message": message}},
        )

    def direct_message(self, user_id: str, message: str) -> Post:
        me = self.me()["id"]
        channel = self._request("POST", "/channels/direct", json=[me, user_id])
        return self.create_post(str(channel["id"]), message)

    def create_command(
        self, team_id: str, trigger: str, url: str, description: str = ""
    ) -> dict[str, Any]:
        return dict(
            self._request(
                "POST",
                "/commands",
                json={
                    "team_id": team_id,
                    "trigger": trigger,
                    "method": "P",
                    "url": url,
                    "display_name": "Agent-Colab",
                    "description": description or "Agent-Colab collaboration commands",
                    "auto_complete": True,
                    "auto_complete_desc": "Agent-Colab: task, approve, verify, doc, link, help",
                    "auto_complete_hint": "<resource> <verb> [args]",
                },
            )
        )

    def regen_command_token(self, command_id: str) -> dict[str, Any]:
        return dict(self._request("PUT", f"/commands/{command_id}/regen_token"))

    def update_command(self, command: dict[str, Any]) -> dict[str, Any]:
        return dict(self._request("PUT", f"/commands/{command['id']}", json=command))

    def list_commands(self, team_id: str) -> list[dict[str, Any]]:
        return list(
            self._request("GET", "/commands", params={"team_id": team_id, "custom_only": "true"})
        )

    def get_config(self) -> dict[str, Any]:
        return dict(self._request("GET", "/config"))


class FakeMattermostClient:
    """In-memory stand-in recording every call; posts get ids ``post-<n>``."""

    def __init__(
        self,
        bot_user_id: str = "bot-user",
        users: dict[str, dict[str, Any]] | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.bot_user_id = bot_user_id
        self.users: dict[str, dict[str, Any]] = users or {}
        self.posts: dict[str, Post] = {}
        self.ephemerals: list[tuple[str, str, str]] = []
        self.dms: list[tuple[str, str]] = []
        self.commands: list[dict[str, Any]] = []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.config = config or {
            "ServiceSettings": {"EnablePostUsernameOverride": True, "EnablePostIconOverride": True}
        }
        self._ids = itertools.count(1)

    def me(self) -> dict[str, Any]:
        return {"id": self.bot_user_id, "username": "agent-colab", "is_bot": True}

    def get_user(self, user_id: str) -> dict[str, Any]:
        if user_id not in self.users:
            raise MattermostError("store.sql_user.missing_account.const", 404, user_id)
        return dict(self.users[user_id])

    def get_user_by_username(self, username: str) -> dict[str, Any]:
        for uid, u in self.users.items():
            if u.get("username") == username.lstrip("@"):
                return {**u, "id": uid}
        raise MattermostError("store.sql_user.get_by_username.app_error", 404, username)

    def get_channel(self, channel_id: str) -> dict[str, Any]:
        return {"id": channel_id, "name": channel_id, "display_name": channel_id, "type": "O"}

    def list_team_channels(self, team_id: str) -> list[dict[str, Any]]:
        return [{"id": "chan-ext-a", "name": "work-a", "team_id": team_id, "type": "O"}]

    def get_team_by_name(self, name: str) -> dict[str, Any]:
        return {"id": f"team-{name}", "name": name}

    def create_post(
        self,
        channel_id: str,
        message: str,
        root_id: str | None = None,
        props: dict[str, Any] | None = None,
    ) -> Post:
        pid = f"post-{next(self._ids)}"
        post = Post(pid, channel_id, self.bot_user_id, message, root_id or "", dict(props or {}))
        self.posts[pid] = post
        self.calls.append(("create_post", (channel_id, message, root_id)))
        return post

    def patch_post(self, post_id: str, message: str, props: dict[str, Any] | None = None) -> Post:
        old = self.posts[post_id]
        new = Post(
            old.id, old.channel_id, old.user_id, message, old.root_id, dict(props or old.props)
        )
        self.posts[post_id] = new
        self.calls.append(("patch_post", (post_id, message)))
        return new

    def get_post(self, post_id: str) -> Post:
        return self.posts[post_id]

    def list_posts(self, channel_id: str, per_page: int = 60) -> list[Post]:
        return [p for p in self.posts.values() if p.channel_id == channel_id][-per_page:]

    def ephemeral(self, user_id: str, channel_id: str, message: str) -> None:
        self.ephemerals.append((user_id, channel_id, message))

    def direct_message(self, user_id: str, message: str) -> Post:
        self.dms.append((user_id, message))
        return self.create_post(f"dm-{user_id}", message)

    def create_command(
        self, team_id: str, trigger: str, url: str, description: str = ""
    ) -> dict[str, Any]:
        cmd = {
            "id": f"cmd-{next(self._ids)}",
            "team_id": team_id,
            "trigger": trigger,
            "url": url,
            "token": f"fake-token-{trigger}",
        }
        self.commands.append(cmd)
        return dict(cmd)

    def regen_command_token(self, command_id: str) -> dict[str, Any]:
        for cmd in self.commands:
            if cmd["id"] == command_id:
                cmd["token"] = cmd["token"] + "-rotated"
                return dict(cmd)
        raise MattermostError("command_missing", 404, command_id)

    def update_command(self, command: dict[str, Any]) -> dict[str, Any]:
        for cmd in self.commands:
            if cmd["id"] == command["id"]:
                cmd.update(command)
                return dict(cmd)
        raise MattermostError("command_missing", 404, str(command.get("id")))

    def list_commands(self, team_id: str) -> list[dict[str, Any]]:
        return [dict(c) for c in self.commands if c["team_id"] == team_id]

    def get_config(self) -> dict[str, Any]:
        return dict(self.config)
