"""纯逻辑层：规则判定 + 状态存储。

只依赖标准库，不 import astrbot，方便离线单测（tests/test_core.py）。
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 链接 / 广告话术特征
LINK_RE = re.compile(
    r"(https?://|www\.|t\.me/|t\.cn/|\.com/|\.cn/|\.top/|\.xyz/|\.vip/|\.icu/"
    r"|加群|进群|扫码|二维码|私聊我|加微信|加QQ)",
    re.IGNORECASE,
)

# QQ 官方平台的 member_openid / group_openid 形如 32 位十六进制
OPENID_RE = re.compile(r"[0-9A-Fa-f]{32}")

ACTIONS = ("kick", "mute", "off")


def looks_like_openid(value: str) -> bool:
    return bool(OPENID_RE.fullmatch((value or "").strip()))


def extract_openids(text: str) -> List[str]:
    return OPENID_RE.findall(text or "")


def parse_answer(raw: str) -> str:
    """从一条消息文本解析作答数字。

    只有**含数字**的内容才算作答；图片/表情/闲聊返回空串，
    调用方据此判断「这不是答案」，避免把闲聊当成答错。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if text.lstrip("-").isdigit():
        return text
    m = re.search(r"-?\d+", text)
    return m.group() if m else ""


def _action(value) -> str:
    value = str(value or "").strip().lower()
    return value if value in ACTIONS else "off"


@dataclass
class Decision:
    action: str = ""
    reason: str = ""
    mute_seconds: int = 0

    @property
    def hit(self) -> bool:
        return self.action in ("kick", "mute")

    def describe(self) -> str:
        if self.action == "kick":
            return f"移出群聊（{self.reason}）"
        if self.action == "mute":
            return f"禁言 {max(1, self.mute_seconds // 60)} 分钟（{self.reason}）"
        return "不处理"


class RuleEngine:
    """滑动窗口刷屏 / 重复内容 / 关键词 / 链接 / 新人广告 检测。"""

    def __init__(self, config: Optional[dict] = None, clock=time.time):
        config = dict(config or {})
        self.clock = clock
        self.msg_window = max(1, int(config.get("msg_window_sec", 10)))
        self.msg_threshold = max(2, int(config.get("msg_threshold", 6)))
        self.repeat_threshold = max(2, int(config.get("repeat_threshold", 3)))
        self.newbie_window = max(0, int(config.get("newbie_window_sec", 600)))
        self.mute_seconds = max(60, int(config.get("default_mute_seconds", 2592000)))
        self.cooldown = max(0, int(config.get("action_cooldown_sec", 30)))
        self.keywords = [
            str(k).strip().lower() for k in (config.get("banned_keywords") or []) if str(k).strip()
        ]
        self.keyword_action = _action(config.get("keyword_action", "kick"))
        self.link_action = _action(config.get("link_action", "mute"))
        self.newbie_link_action = _action(config.get("newbie_link_action", "mute"))
        self.flood_action = _action(config.get("flood_action", "mute"))
        self.repeat_action = _action(config.get("repeat_action", "mute"))
        self.ban_link = bool(config.get("ban_link", True))

        self._history: Dict[str, List[Tuple[float, str]]] = {}
        self._first_seen: Dict[str, float] = {}
        self._acted: Dict[str, float] = {}

    # ---------- 状态 ----------
    @staticmethod
    def _key(group: str, user: str) -> str:
        return f"{group}\u0000{user}"

    def touch(self, group: str, user: str) -> bool:
        """记录首次观测时间，返回是否首次观测到该成员。"""
        key = self._key(group, user)
        if key in self._first_seen:
            return False
        self._first_seen[key] = self.clock()
        return True

    def forget(self, group: str, user: str) -> None:
        key = self._key(group, user)
        self._history.pop(key, None)
        self._first_seen.pop(key, None)
        self._acted.pop(key, None)

    def is_newcomer(self, group: str, user: str) -> bool:
        first = self._first_seen.get(self._key(group, user))
        return first is not None and (self.clock() - first) <= self.newbie_window

    # ---------- 判定 ----------
    def inspect(self, group: str, user: str, text: str, extra_keywords=()) -> Decision:
        now = self.clock()
        key = self._key(group, user)
        text = (text or "").strip()
        lower = text.lower()

        # 1) 关键词：静态配置表 + 该群运行时动态新增的命中即踢词
        keywords = list(self.keywords)
        keywords.extend(str(k).strip().lower() for k in extra_keywords if str(k).strip())
        if keywords and self.keyword_action != "off":
            for kw in keywords:
                if kw in lower:
                    decision = self._decide(key, self.keyword_action, f"触发关键词「{kw}」", now)
                    if decision.hit:
                        return decision

        # 2) 链接 / 广告话术
        if self.ban_link and LINK_RE.search(text):
            newcomer = self.is_newcomer(group, user)
            action = self.newbie_link_action if newcomer else self.link_action
            reason = "新成员发送链接或广告" if newcomer else "发送链接或广告内容"
            if action != "off":
                decision = self._decide(key, action, reason, now)
                if decision.hit:
                    return decision

        # 3) 记录本次发言并做滑动窗口统计
        history = self._history.setdefault(key, [])
        horizon = max(self.msg_window * 3, 60)
        history[:] = [item for item in history if now - item[0] <= horizon]
        history.append((now, text))
        window = [item for item in history if now - item[0] <= self.msg_window]

        if self.flood_action != "off" and len(window) >= self.msg_threshold:
            decision = self._decide(
                key, self.flood_action, f"{self.msg_window} 秒内发言 {len(window)} 条", now
            )
            if decision.hit:
                return decision

        if self.repeat_action != "off" and text:
            same = sum(1 for _, value in window if value == text)
            if same >= self.repeat_threshold:
                decision = self._decide(
                    key, self.repeat_action, f"重复发送同一内容 {same} 次", now
                )
                if decision.hit:
                    return decision

        return Decision()

    def _decide(self, key: str, action: str, reason: str, now: float) -> Decision:
        if action == "off":
            return Decision()
        last = self._acted.get(key)
        if last is not None and (now - last) < self.cooldown:
            return Decision()
        self._acted[key] = now
        seconds = self.mute_seconds if action == "mute" else 0
        return Decision(action=action, reason=reason, mute_seconds=seconds)


OPERATORS = "+-*/"


class ArithVerify:
    """入群算术验证：出 50 以内加减乘除题，限时作答，超时未答即应踢。

    官方平台没有「成员入群」事件，因此以「机器人第一次在该群看到该成员发言」
    作为入群锚点（需开启消息接收设置才能对首条消息生效）。
    """

    def __init__(self, config: Optional[dict] = None, clock=time.time, rng=random):
        config = dict(config or {})
        self.rng = rng
        self.clock = clock
        self.timeout = max(30, int(config.get("verify_timeout_sec", 600)))  # 10 分钟
        self.max_number = max(1, int(config.get("verify_max_number", 50)))
        # { (group, user): {"expr", "answer", "expire", "sent"} }
        self._pending = {}

    def _generate(self) -> tuple:
        """生成 1..max_number 范围内的算式；除法保证整除无余数。"""
        a = self.rng.randint(1, self.max_number)
        op = self.rng.choice(OPERATORS)
        if op == "-":
            b = self.rng.randint(1, a)
            return f"{a} - {b}", a - b
        if op == "*":
            b = self.rng.randint(1, max(1, self.max_number // max(1, a)))
            return f"{a} * {b}", a * b
        if op == "/":
            b = self.rng.randint(1, max(1, self.max_number // max(1, a)))
            dividend = a * b
            return f"{dividend} / {b}", a
        # "+"：保证 a、b 之和不超过 max_number
        a = self.rng.randint(1, max(1, self.max_number - 1))
        b = self.rng.randint(1, self.max_number - a)
        return f"{a} + {b}", a + b

    def challenge(self, group: str, user: str):
        """发起验证；返回 (算式, 答案, 限时秒)。已在进行中则返回 None。"""
        key = (group, user)
        if key in self._pending:
            return None
        expr, answer = self._generate()
        self._pending[key] = {
            "expr": expr,
            "answer": answer,
            "sent": True,
            "expire": self.clock() + self.timeout,
        }
        return expr, answer, self.timeout

    def answer(self, group: str, user: str, raw: str) -> str:
        """提交答案。返回 'ok' 通过 | 'wrong' 答错 | 'none' 无待验证。"""
        key = (group, user)
        item = self._pending.get(key)
        if not item:
            return "none"
        text = (raw or "").strip()
        is_correct = text.lstrip("-").isdigit() and int(text) == item["answer"]
        # 不论对错都结束验证，避免后续消息被重复处置
        del self._pending[key]
        return "ok" if is_correct else "wrong"

    def is_pending(self, group: str, user: str) -> tuple:
        """返回 (是否处于未过期待验证, 状态)。状态: 'pending' | 'timeout'。"""
        key = (group, user)
        item = self._pending.get(key)
        if not item or item.get("sent") is False:
            return False, None
        if self.clock() >= item["expire"]:
            return True, "timeout"
        return True, "pending"

    def force(self, group: str, user: str) -> bool:
        """移除验证状态（手动放行 / 超时后清理），返回是否确实存在。"""
        key = (group, user)
        if key in self._pending:
            del self._pending[key]
            return True
        return False

    def peek(self, group: str, user: str):
        """读取当前待验证条目（用于诊断/批准）。无则返回 None。"""
        return self._pending.get((group, user))


class StateStore:
    """JSON 持久化：每群开关、白名单、待办（踢人接口无权限时登记）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {"groups": {}, "pending": []}
        self.load()

    # ---------- IO ----------
    def load(self) -> None:
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.data["groups"] = raw.get("groups") or {}
                    self.data["pending"] = raw.get("pending") or []
        except Exception:
            self.data = {"groups": {}, "pending": []}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            pass

    # ---------- 群 ----------
    def group(self, group_id: str) -> dict:
        groups = self.data.setdefault("groups", {})
        item = groups.setdefault(str(group_id), {"enabled": True, "whitelist": []})
        item.setdefault("enabled", True)
        item.setdefault("whitelist", [])
        return item

    def is_enabled(self, group_id: str) -> bool:
        return bool(self.group(group_id).get("enabled", True))

    def set_enabled(self, group_id: str, value: bool) -> None:
        self.group(group_id)["enabled"] = bool(value)
        self.save()

    def whitelist(self, group_id: str) -> List[str]:
        return [str(x) for x in self.group(group_id).get("whitelist", [])]

    def add_white(self, group_id: str, user_id: str) -> bool:
        item = self.group(group_id)
        if user_id in item["whitelist"]:
            return False
        item["whitelist"].append(user_id)
        self.save()
        return True

    def remove_white(self, group_id: str, user_id: str) -> bool:
        item = self.group(group_id)
        if user_id not in item["whitelist"]:
            return False
        item["whitelist"].remove(user_id)
        self.save()
        return True

    # 每群「命中即踢」动态关键词表（运行时 /踢人加词 写入，持久化）
    def group_keywords(self, group_id: str) -> list:
        item = self.group(group_id)
        item.setdefault("keywords", [])
        return [str(x) for x in item.get("keywords", [])]

    def add_keyword(self, group_id: str, text: str) -> bool:
        item = self.group(group_id)
        item.setdefault("keywords", [])
        table = [str(x) for x in item.get("keywords", [])]
        if text in table:
            return False
        table.append(text)
        item["keywords"] = table
        self.save()
        return True

    def remove_keyword(self, group_id: str, text: str) -> bool:
        item = self.group(group_id)
        table = [str(x) for x in item.get("keywords", [])]
        if text not in table:
            return False
        table.remove(text)
        item["keywords"] = table
        self.save()
        return True

    # 每群「已通过入群算术验证」成员名单（持久化，重启/重装不丢失）
    def verified(self, group_id: str) -> List[str]:
        item = self.group(group_id)
        item.setdefault("verified", [])
        return [str(x) for x in item.get("verified", [])]

    def is_verified(self, group_id: str, user_id: str) -> bool:
        return str(user_id) in self.verified(group_id)

    def add_verified(self, group_id: str, user_id: str) -> None:
        item = self.group(group_id)
        item.setdefault("verified", [])
        table = [str(x) for x in item.get("verified", [])]
        if user_id not in table:
            table.append(str(user_id))
            item["verified"] = table
            self.save()

    def clear_verified(self, group_id: str) -> int:
        """清空某群已验证名单，返回清掉的条数（用于「重新验证全群」）。"""
        item = self.group(group_id)
        removed = len(item.get("verified", []))
        item["verified"] = []
        self.save()
        return removed

    def import_verified(self, group_id: str, openids: List[str]) -> int:
        """批量导入已验证 openid，返回新增条数（用于更新后恢复名单）。"""
        item = self.group(group_id)
        item.setdefault("verified", [])
        table = [str(x) for x in item.get("verified", [])]
        added = 0
        for raw in openids:
            value = str(raw or "").strip()
            if not value or value in table:
                continue
            table.append(value)
            added += 1
        item["verified"] = table
        if added:
            self.save()
        return added

    def toggle(self, group_id: str) -> bool:
        value = not self.is_enabled(group_id)
        self.set_enabled(group_id, value)
        return value

    # ---------- 待办 ----------
    def add_pending(self, group_id: str, user_id: str, reason: str) -> None:
        pending = self.data.setdefault("pending", [])
        pending.append({"group": str(group_id), "user": str(user_id), "reason": reason, "ts": time.time()})
        del pending[:-50]
        self.save()

    def list_pending(self, group_id: Optional[str] = None) -> List[dict]:
        rows = self.data.get("pending") or []
        if group_id is None:
            return list(rows)
        return [row for row in rows if str(row.get("group")) == str(group_id)]

    def clear_pending(self, group_id: Optional[str] = None) -> int:
        rows = self.data.get("pending") or []
        if group_id is None:
            count = len(rows)
            self.data["pending"] = []
        else:
            keep = [r for r in rows if str(r.get("group")) != str(group_id)]
            count = len(rows) - len(keep)
            self.data["pending"] = keep
        self.save()
        return count
