"""QQ 机器人开放平台（官方机器人 · Websocket）OpenAPI 客户端。

只覆盖本插件需要的三个能力：
  * 群成员批量移除  POST /v2/groups/{group_openid}/batch_remove_members
  * 群成员禁言      POST /v2/groups/{group_openid}/restrict_chat_setting
  * 群消息撤回      DELETE /v2/groups/{group_openid}/messages/{message_id}

注意：批量移除成员在官方平台属于「内邀 / 白名单」能力，普通机器人调用会返回
错误码 11253（应用无接口访问权限）。此时 APIResult.permission_denied 为 True，
调用方应降级为禁言。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiohttp

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
BEIJING = timezone(timedelta(hours=8))

# 官方文档中与「无权限 / 未开通」相关的错误码
PERMISSION_CODES = {11253, 11244, 11252, 11255, 10001}

# 「目标成员不可被管理（机器人 / 群主 / 管理员）」——遇到这些就别再对它动手了
UNMANAGEABLE_CODES = {40103004, 11254, 11255}


@dataclass
class APIResult:
    ok: bool
    http_status: int = 0
    code: int = 0
    message: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def permission_denied(self) -> bool:
        return self.http_status in (401, 403) or self.code in PERMISSION_CODES

    @property
    def unmanageable(self) -> bool:
        """该成员是机器人 / 群主 / 管理员，平台不允许禁言或移出。"""
        if self.code in UNMANAGEABLE_CODES:
            return True
        text = self.message or ""
        return ("机器人" in text and ("不允许" in text or "不可" in text)) or (
            "管理员" in text and "不允许" in text
        )

    def brief(self) -> str:
        if self.ok:
            return "成功"
        detail = self.message or "平台未返回原因"
        return f"HTTP {self.http_status} / 错误码 {self.code}：{detail}"


def rfc3339_after(seconds: int, now: Optional[float] = None) -> str:
    """生成禁言到期时间（RFC3339，带 +08:00）。"""
    base = datetime.fromtimestamp(now if now is not None else time.time(), BEIJING)
    return (base + timedelta(seconds=max(60, int(seconds)))).strftime("%Y-%m-%dT%H:%M:%S+08:00")


class QQOfficialAPI:
    def __init__(self, appid: str = "", secret: str = "", timeout: float = 10.0):
        self.appid = str(appid or "").strip()
        self.secret = str(secret or "").strip()
        self.timeout = float(timeout)
        self._token = ""
        self._expire_at = 0.0
        self._lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def configured(self) -> bool:
        return bool(self.appid and self.secret)

    # ---------- 基础 ----------
    async def _session_or_new(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def access_token(self) -> str:
        if self._token and time.time() < self._expire_at - 60:
            return self._token
        async with self._lock:
            if self._token and time.time() < self._expire_at - 60:
                return self._token
            session = await self._session_or_new()
            async with session.post(
                TOKEN_URL, json={"appId": self.appid, "clientSecret": self.secret}
            ) as resp:
                status = resp.status
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = {"raw": (await resp.text())[:200]}
            token = data.get("access_token") if isinstance(data, dict) else None
            if status != 200 or not token:
                raise RuntimeError(f"获取 access_token 失败：HTTP {status} {data}")
            self._token = str(token)
            try:
                self._expire_at = time.time() + float(data.get("expires_in", 7200) or 7200)
            except (TypeError, ValueError):
                self._expire_at = time.time() + 7200
            return self._token

    async def request(
        self, method: str, path: str, body: Optional[dict] = None
    ) -> APIResult:
        if not self.configured:
            return APIResult(False, message="未配置 AppID / AppSecret")
        try:
            headers = {"Content-Type": "application/json"}
            headers["Authorization"] = f"QQBot {await self.access_token()}"
            session = await self._session_or_new()
            async with session.request(
                method.upper(), API_BASE + path, json=body, headers=headers
            ) as resp:
                status = resp.status
                text = await resp.text()
        except Exception as exc:  # 网络 / 凭证异常都不应打断事件处理
            return APIResult(False, message=f"请求异常 {type(exc).__name__}: {exc}")

        data: Any = {}
        if text:
            try:
                data = json.loads(text)
            except Exception:
                data = {"raw": text[:200]}
        if not isinstance(data, dict):
            data = {"raw": data}

        try:
            code = int(data.get("code") or data.get("err_code") or 0)
        except (TypeError, ValueError):
            code = 0
        message = str(
            data.get("message") or data.get("msg") or data.get("err_msg") or data.get("raw") or ""
        )[:200]
        ok = 200 <= status < 300 and code == 0
        return APIResult(ok, status, code, message, data)

    # ---------- 业务 ----------
    async def kick_members(
        self, group_openid: str, openids: List[str], blacklist: bool = False
    ) -> APIResult:
        body = {
            "member_openids": [str(x) for x in openids][:20],
            "add_to_member_blacklist": bool(blacklist),
        }
        return await self.request(
            "POST", f"/v2/groups/{group_openid}/batch_remove_members", body
        )

    async def mute_member(self, group_openid: str, openid: str, seconds: int) -> APIResult:
        body = {
            "members": [
                {
                    "op": "add",
                    "member_openid": str(openid),
                    "mute_expire_at": rfc3339_after(seconds),
                }
            ]
        }
        return await self.request(
            "POST", f"/v2/groups/{group_openid}/restrict_chat_setting", body
        )

    async def unmute_member(self, group_openid: str, openid: str) -> APIResult:
        body = {"members": [{"op": "del", "member_openid": str(openid), "mute_expire_at": ""}]}
        return await self.request(
            "POST", f"/v2/groups/{group_openid}/restrict_chat_setting", body
        )

    async def recall_message(self, group_openid: str, message_id: str) -> APIResult:
        return await self.request(
            "DELETE", f"/v2/groups/{group_openid}/messages/{message_id}"
        )
