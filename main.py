"""QQ 官方机器人（Websocket）群自动踢人 / 禁言插件。

触发面（重要）：
  官方机器人默认只收到「@机器人」的群消息，需要在 QQ 开放平台申请并开启
  「消息接收设置 / 全量消息」后，才能对所有群消息做自动处置。

处置链路：
  真踢（batch_remove_members，平台内邀能力）
    └ 无权限(11253 等) → 自动降级为禁言（restrict_chat_setting，机器人需为群管理员）
        └ 也失败 → 记入「待办」，管理员用 /踢人待办 查看并手动移出
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .core import (
    ArithVerify,
    Decision,
    RuleEngine,
    StateStore,
    extract_openids,
    looks_like_openid,
)
from .qqapi import QQOfficialAPI

PLUGIN_NAME = "astrbot_plugin_qq_autokick"
OFFICIAL_HINTS = ("official", "qq_official", "官方")
ONEBOT_HINTS = ("aiocqhttp", "napcat", "onebot", "lagrange", "llob", "llonebot")

# 兼容不同 AstrBot 版本的事件枚举命名
_EVENT_TYPE = getattr(filter, "EventMessageType", None)
_WATCH_TYPE = getattr(_EVENT_TYPE, "ALL", None) or getattr(_EVENT_TYPE, "GROUP_MESSAGE", None)


def _watch(decorated):
    """按可用的事件类型枚举注册监听器；枚举缺失时退化为原函数。"""
    if _WATCH_TYPE is None:
        return decorated
    return filter.event_message_type(_WATCH_TYPE)(decorated)


@register(
    PLUGIN_NAME,
    "local",
    "QQ官方机器人群自动踢人/禁言：刷屏、关键词、广告链接、新人广告自动处置",
    "1.0.0",
)
class QQAutoKick(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config or {}
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.store = StateStore(data_dir / "state.json")
        self.engine = RuleEngine(self.config)
        self.api = QQOfficialAPI(
            self._value("appid", "app_id", "appId"),
            self._value("secret", "app_secret", "appSecret"),
        )
        self.verify = ArithVerify(self.config)
        self.verify_wrong_mute_seconds = max(60, int(self.config.get("verify_wrong_mute_seconds", 600)))
        self._verify_loop_task = None
        self._verify_lock = asyncio.Lock()
        self._ready = asyncio.Event()

    # ==================== 生命周期 ====================
    async def initialize(self):
        if not self.api.configured:
            appid, secret = self._from_global_config()
            if appid and secret:
                self.api.appid = appid
                self.api.secret = secret
                logger.info(f"[auto_kick] 已从 AstrBot 平台配置读取 AppID {appid[:4]}***")
        logger.info(
            "[auto_kick] 已加载；AppID 配置："
            + ("已就绪" if self.api.configured else "缺失（请在插件配置填写 appid / secret）")
        )
        if self._verify_enabled_anywhere():
            self._verify_loop_task = asyncio.create_task(
                self._verify_loop(), name="astrbot-autokick-verify"
            )
        self._ready.set()

    async def terminate(self):
        if self._verify_loop_task:
            self._verify_loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._verify_loop_task
        with suppress(Exception):
            await self.api.close()

    # ==================== 配置读取 ====================
    def _value(self, *names):
        for name in names:
            value = self.config.get(name) if hasattr(self.config, "get") else None
            if value:
                return str(value).strip()
        return ""

    def _from_global_config(self):
        """在 AstrBot 全局配置里找 QQ 官方平台的 appid/secret。"""
        try:
            raw = self.context.get_config()
            config = raw if isinstance(raw, dict) else dict(raw)
        except Exception as exc:
            logger.warning(f"[auto_kick] 读取全局配置失败：{type(exc).__name__}: {exc}")
            return "", ""

        found = {}

        def walk(node):
            if found:
                return
            if isinstance(node, dict):
                lowered = {str(k).lower(): v for k, v in node.items()}
                appid = lowered.get("appid") or lowered.get("app_id")
                secret = lowered.get("secret") or lowered.get("app_secret")
                if appid and secret:
                    found["appid"] = str(appid)
                    found["secret"] = str(secret)
                    return
                for value in node.values():
                    walk(value)
            elif isinstance(node, (list, tuple)):
                for value in node:
                    walk(value)

        with suppress(Exception):
            walk(config)
        return found.get("appid", ""), found.get("secret", "")

    # ==================== 平台判断 ====================
    @staticmethod
    def _platform_name(event) -> str:
        try:
            return str(event.get_platform_name() or "").lower()
        except Exception:
            return ""

    def _is_official(self, event) -> bool:
        return any(hint in self._platform_name(event) for hint in OFFICIAL_HINTS)

    # ==================== 目标解析 ====================
    @staticmethod
    def _target_openid(event, arg: str) -> str:
        arg = (arg or "").strip()
        if looks_like_openid(arg):
            return arg.upper()
        # 消息链里的 @ 组件（AstrBot 会把官方平台的提及转成 At）
        with suppress(Exception):
            for comp in event.message_obj.message:
                text = str(getattr(comp, "qq", "") or "")
                if looks_like_openid(text):
                    return text.upper()
        found = extract_openids(event.message_str or "")
        return found[0].upper() if found else ""

    # ==================== 处置 ====================
    async def _enforce(self, event, group: str, user: str, decision: Decision, manual: bool = False):
        action = decision.action or ("kick" if manual else "")
        if not action:
            return
        reason = decision.reason or "手动操作"
        notes = []

        if not self._is_official(event):
            notes.append(await self._enforce_onebot(event, group, user, action, decision, reason))
        elif not self.api.configured:
            notes.append("未配置 AppID/AppSecret，无法调用官方接口；请在插件配置里填写。")
        else:
            fallback_seconds = max(
                60, int(self.config.get("fallback_mute_seconds", self.engine.mute_seconds))
            )
            if action == "kick":
                res = await self.api.kick_members(
                    group, [user], bool(self.config.get("blacklist_on_kick", False))
                )
                if res.ok:
                    notes.append(f"已移出群聊（{reason}）")
                elif res.permission_denied:
                    mute = await self.api.mute_member(group, user, fallback_seconds)
                    if mute.ok:
                        self.store.add_pending(group, user, reason)
                        notes.append(
                            f"踢人接口未开通（{res.brief()}），已降级为禁言 "
                            f"{max(1, fallback_seconds // 86400)} 天（{reason}）；"
                            "该能力属平台内邀权限，可联系 QQ 开放平台运营开通，"
                            "或用 /踢人待办 查看后手动移出。"
                        )
                    else:
                        self.store.add_pending(group, user, reason)
                        notes.append(f"踢人与禁言均失败：{mute.brief()}；已记入待办，请管理员手动移出。")
                else:
                    self.store.add_pending(group, user, reason)
                    notes.append(f"踢人失败：{res.brief()}；已记入待办。")
            else:
                seconds = decision.mute_seconds or fallback_seconds
                res = await self.api.mute_member(group, user, seconds)
                if res.ok:
                    notes.append(f"已禁言 {max(1, seconds // 60)} 分钟（{reason}）")
                else:
                    notes.append(f"禁言失败：{res.brief()}（机器人需为群管理员）")

        if notes:
            with suppress(Exception):
                await event.send(event.plain_result("【自动管理】" + "；".join(notes)))
        self.engine.forget(group, user)

    async def _enforce_onebot(self, event, group, user, action, decision, reason) -> str:
        """非官方适配器（NapCat / Lagrange 等 OneBot v11）走原生踢人接口。"""
        if action != "kick":
            return "当前适配器不支持通过官方接口禁言，请用 OneBot 侧禁言。"
        try:
            await event.bot.call_api(
                "set_group_kick",
                group_id=int(group),
                user_id=int(user),
                reject_add_request=False,
            )
            return f"已移出群聊（{reason}）"
        except Exception as exc:
            try:
                minutes = max(1, (decision.mute_seconds or self.engine.mute_seconds) // 60)
                await event.bot.call_api(
                    "set_group_ban", group_id=int(group), user_id=int(user), duration=minutes * 60
                )
                return f"踢人失败（{type(exc).__name__}），已改为禁言 {minutes} 分钟（{reason}）"
            except Exception as exc2:
                return f"踢人与禁言均失败：{type(exc2).__name__}: {exc2}"

    # ==================== 入群算术验证 ====================
    def _verify_enabled_anywhere(self) -> bool:
        try:
            return bool(self.config.get("verify_enabled", True))
        except Exception:
            return True

    def _verify_enabled(self, group: str) -> bool:
        """总开关 && 本群开关。本群开关存于 store。"""
        if not self._verify_enabled_anywhere():
            return False
        try:
            return bool(self.store.group(group).get("verify", True))
        except Exception:
            return True

    @staticmethod
    def _at_openid(openid: str) -> str:
        """官方群消息中 @ 某成员。平台不保证渲染，但带 openid 便于识别作答对象。"""
        return f"<@!{openid}>"

    async def _enforce_no_event(self, group: str, user: str, decision: Decision) -> str:
        """后台定时器踢人：不依赖事件对象，走官方接口真踢→降级禁言。"""
        if not self.api.configured:
            self.store.add_pending(group, user, decision.reason or "验证超时")
            return "验证超时，但因未配置 AppID/AppSecret，已记入待办。"
        fallback = max(60, int(self.config.get("fallback_mute_seconds", self.engine.mute_seconds)))
        if decision.action == "kick":
            res = await self.api.kick_members(
                group, [user], bool(self.config.get("blacklist_on_kick", False))
            )
            if res.ok:
                return f"验证超时未答，已移出群聊。"
            if res.permission_denied:
                mute = await self.api.mute_member(group, user, fallback)
                note = f"验证超时未答；踢人无权限({res.code})，已改为禁言 {fallback // 86400} 天。"
                if mute.ok:
                    self.store.add_pending(group, user, decision.reason or "验证超时")
                else:
                    note += f" 禁言也失败（{mute.brief()}）。"
                return note
            self.store.add_pending(group, user, decision.reason or "验证超时")
            return f"验证超时未答，但踢人失败：{res.brief()}；已记入待办。"
        return ""

    async def _kick_overdue(self, group: str, user: str) -> str:
        async with self._verify_lock:
            pending, status = self.verify.is_pending(group, user)
            if not pending:
                return ""  # 已作答或已处理，跳过
            self.verify.force(group, user)  # 移除，防止重复踢
        note = await self._enforce_no_event(
            group, user, Decision(action="kick", reason="入群算术验证超时未答")
        )
        logger.info(f"[auto_kick] 验证超时 -> 踢 {group}/{user}: {note}")
        return note

    async def _verify_loop(self):
        """周期性扫描全群过期验证项，保证无人说话也能在超时点踢人。"""
        while True:
            try:
                await asyncio.sleep(5)
                overdue = await asyncio.to_thread(self._overdue_items)
                for group, user in list(overdue)[:20]:
                    try:
                        await self._kick_overdue(group, user)
                    except Exception as exc:
                        logger.warning(f"[auto_kick] 验证超时踢人失败 {group}/{user}: {type(exc).__name__}")
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[auto_kick] 验证扫描异常：{type(exc).__name__}: {exc}")

    def _overdue_items(self):
        """返回过期且未处理的白名单外待验证项 [(group, user)]。"""
        now = self.clock_now()
        items = []
        for key, item in list(self.verify._pending.items()).copy():
            if item.get("sent") is False:
                continue
            if now < item.get("expire", 0):
                continue
            group, user = key
            if user in self.store.whitelist(group):
                continue
            items.append((group, user))
        return items

    def clock_now(self):
        return time.time()

    async def _handle_verify(self, event, group: str, user: str, text: str) -> bool:
        """驱动算术验证。返回 True 表示该消息已被验证逻辑处理。"""
        if not self._verify_enabled(group):
            return False
        pending, status = self.verify.is_pending(group, user)

        # 该成员已在验证 → 检查是否作答
        if pending:
            result = self.verify.answer(group, user, text)
            if result == "ok":
                await event.send(
                    event.plain_result(f"{self._at_openid(user)} 验证通过，欢迎入群。")
                )
            elif result == "wrong":
                # 答错 / 答非数字 → 直接禁言（走官方接口，失败则记待办）
                await self._enforce(
                    event,
                    group,
                    user,
                    Decision(
                        action="mute",
                        reason="入群算术验证答错",
                        mute_seconds=self.verify_wrong_mute_seconds,
                    ),
                )
            elif result == "wait":
                await event.send(
                    event.plain_result(f"{self._at_openid(user)} 请先回答入群算术题。")
                )
            return True

        # 不在验证中，且是首次观测到该成员 → 发起验证
        first_seen = self.engine.touch(group, user)
        if not first_seen:
            return False
        issued = self.verify.challenge(group, user)
        if issued is None:
            return False
        expr, answer, timeout = issued
        await event.send(
            event.plain_result(
                f"{self._at_openid(user)} 欢迎入群！请在 {timeout // 60} 分钟内回答："
                f"{expr} = ?（直接回复答案数字即可）"
            )
        )
        return True

    @_watch
    async def watch_group_message(self, event: AstrMessageEvent):
        try:
            group = str(event.get_group_id() or "")
            if not group:
                return
            if not self.store.is_enabled(group):
                return
            text = (event.message_str or "").strip()
            if text.startswith("/") or text.startswith("／"):
                return  # 指令交给下面的命令处理
            sender = str(event.get_sender_id() or "")
            if not sender or sender in self.store.whitelist(group):
                return
            # 入群算术验证：首次发言触发出题；作答期拦截其他处置
            if await self._handle_verify(event, group, sender, text):
                return
            # 欢迎提醒（仅当算术验证关闭时使用；否则由出题消息承担欢迎）
            if (
                not self._verify_enabled(group)
                and self.engine.touch(group, sender)
                and bool(self.config.get("announce_newcomer", False))
            ):
                with suppress(Exception):
                    await event.send(
                        event.plain_result("欢迎新成员，请勿发送广告、链接或刷屏。")
                    )
            decision = self.engine.inspect(
                group, sender, text, extra_keywords=self.store.group_keywords(group)
            )
            if not decision.hit:
                return
            logger.info(f"[auto_kick] 群 {group} 成员 {sender} -> {decision.describe()}")
            await self._enforce(event, group, sender, decision)
        except Exception as exc:
            logger.error(f"[auto_kick] 处理消息异常：{type(exc).__name__}: {exc}")

    # ==================== 指令 ====================
    def _require_admin(self, event) -> str:
        try:
            if event.is_admin():
                return ""
        except Exception:
            pass
        return "该操作仅限 AstrBot 管理员（在 AstrBot WebUI 或配置中设置的管理员）。"

    @filter.command("踢人开", alias={"kick_on"})
    async def kick_on(self, event: AstrMessageEvent):
        """开启本群自动踢人。"""
        event.stop_event()
        group = str(event.get_group_id() or "")
        if not group:
            yield event.plain_result("请在目标 QQ 群内 @机器人使用。")
            return
        if (msg := self._require_admin(event)):
            yield event.plain_result(msg)
            return
        self.store.set_enabled(group, True)
        yield event.plain_result("已开启本群自动踢人/禁言。")

    @filter.command("踢人关", alias={"kick_off"})
    async def kick_off(self, event: AstrMessageEvent):
        """关闭本群自动踢人。"""
        event.stop_event()
        group = str(event.get_group_id() or "")
        if not group:
            yield event.plain_result("请在目标 QQ 群内 @机器人使用。")
            return
        if (msg := self._require_admin(event)):
            yield event.plain_result(msg)
            return
        self.store.set_enabled(group, False)
        yield event.plain_result("已关闭本群自动踢人。")

    @filter.command("踢人状态", alias={"kick_status"})
    async def kick_status(self, event: AstrMessageEvent):
        """查看当前群开关与规则概览。"""
        event.stop_event()
        group = str(event.get_group_id() or "")
        if not group:
            yield event.plain_result("请在群内使用。")
            return
        lines = [
            f"开关：{'开启' if self.store.is_enabled(group) else '关闭'}",
            f"凭据：{'已配置' if self.api.configured else '缺失（请在插件配置填写 appid / secret）'}",
            f"平台：{self._platform_name(event) or '未知'}",
            f"刷屏：{self.engine.msg_window} 秒内 {self.engine.msg_threshold} 条 → {self.engine.flood_action}",
            f"重复：{self.engine.msg_window} 秒内重复 {self.engine.repeat_threshold} 次 → {self.engine.repeat_action}",
            f"关键词：{len(self.engine.keywords)} 个 → {self.engine.keyword_action}",
            f"链接：{'拦截' if self.engine.ban_link else '放行'} → 老成员 {self.engine.link_action} / 新人 {self.engine.newbie_link_action}",
            f"白名单：{len(self.store.whitelist(group))} 人",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("踢人规则", alias={"kick_rules"})
    async def kick_rules(self, event: AstrMessageEvent):
        """查看完整规则文本。"""
        event.stop_event()
        text = (
            "触发条件（命中即处置，同一成员 30 秒内不重复处置）：\n"
            f"1. 刷屏：{self.engine.msg_window} 秒内发言 ≥ {self.engine.msg_threshold} 条\n"
            f"2. 复读：{self.engine.msg_window} 秒内同一内容 ≥ {self.engine.repeat_threshold} 次\n"
            f"3. 关键词：{self.engine.keywords or '（未配置）'}\n"
            f"4. 链接/广告话术：{'启用' if self.engine.ban_link else '关闭'}\n"
            "处置链路：真踢 → 无权限则禁言 → 仍失败记入待办。"
        )
        yield event.plain_result(text)

    @filter.command("踢", alias={"kick"})
    async def kick(self, event: AstrMessageEvent, target: str = ""):
        """手动踢人：/踢 <openid>，或引用/提及该成员后直接发送 /踢。"""
        event.stop_event()
        group = str(event.get_group_id() or "")
        if not group:
            yield event.plain_result("请在群内使用。")
            return
        if (msg := self._require_admin(event)):
            yield event.plain_result(msg)
            return
        user = self._target_openid(event, target)
        if not user:
            yield event.plain_result(
                "未识别到目标。用法：/踢 <32位openid>（官方平台只提供 openid，不是 QQ 号）。"
            )
            return
        await self._enforce(event, group, user, Decision(action="kick", reason="管理员手动"), manual=True)

    @filter.command("禁言", alias={"mute"})
    async def mute(self, event: AstrMessageEvent, target: str = "", minutes: str = ""):
        """手动禁言：/禁言 <openid> <分钟>。"""
        event.stop_event()
        group = str(event.get_group_id() or "")
        if not group:
            yield event.plain_result("请在群内使用。")
            return
        if (msg := self._require_admin(event)):
            yield event.plain_result(msg)
            return
        user = self._target_openid(event, target)
        if not user:
            yield event.plain_result("未识别到目标。用法：/禁言 <32位openid> <分钟>。")
            return
        try:
            seconds = max(60, int(float(minutes or 1) * 60))
        except ValueError:
            yield event.plain_result("分钟数需为数字。")
            return
        if not self.api.configured:
            yield event.plain_result("未配置 AppID/AppSecret，无法调用官方禁言接口。")
            return
        res = await self.api.mute_member(group, user, seconds)
        yield event.plain_result(
            f"已禁言 {seconds // 60} 分钟。" if res.ok else f"禁言失败：{res.brief()}（机器人需为群管理员）"
        )

    @filter.command("解禁", alias={"unmute"})
    async def unmute(self, event: AstrMessageEvent, target: str = ""):
        """解除禁言：/解禁 <openid>。"""
        event.stop_event()
        group = str(event.get_group_id() or "")
        if not group:
            yield event.plain_result("请在群内使用。")
            return
        if (msg := self._require_admin(event)):
            yield event.plain_result(msg)
            return
        user = self._target_openid(event, target)
        if not user:
            yield event.plain_result("未识别到目标。")
            return
        if not self.api.configured:
            yield event.plain_result("未配置 AppID/AppSecret，无法调用官方接口。")
            return
        res = await self.api.unmute_member(group, user)
        yield event.plain_result("已解除禁言。" if res.ok else f"解除失败：{res.brief()}")

    @filter.command("白名单", alias={"kick_white"})
    async def add_white(self, event: AstrMessageEvent, target: str = ""):
        """加入白名单：/白名单 <openid>。"""
        event.stop_event()
        group = str(event.get_group_id() or "")
        if not group:
            yield event.plain_result("请在群内使用。")
            return
        if (msg := self._require_admin(event)):
            yield event.plain_result(msg)
            return
        user = self._target_openid(event, target)
        if not user:
            yield event.plain_result("未识别到目标。用法：/白名单 <32位openid>。")
            return
        added = self.store.add_white(group, user)
        yield event.plain_result("已加入白名单。" if added else "该成员已在白名单中。")

    @filter.command("白名单删", alias={"kick_unwhite"})
    async def del_white(self, event: AstrMessageEvent, target: str = ""):
        """移出白名单：/白名单删 <openid>。"""
        event.stop_event()
        group = str(event.get_group_id() or "")
        if not group:
            yield event.plain_result("请在群内使用。")
            return
        if (msg := self._require_admin(event)):
            yield event.plain_result(msg)
            return
        user = self._target_openid(event, target)
        if not user:
            yield event.plain_result("未识别到目标。")
            return
        removed = self.store.remove_white(group, user)
        yield event.plain_result("已移出白名单。" if removed else "该成员不在白名单中。")

    @filter.command("白名单列表", alias={"kick_whitelist"})
    async def list_white(self, event: AstrMessageEvent):
        """查看白名单。"""
        event.stop_event()
        group = str(event.get_group_id() or "")
        if not group:
            yield event.plain_result("请在群内使用。")
            return
        rows = self.store.whitelist(group)
        yield event.plain_result("白名单：\n" + "\n".join(rows) if rows else "白名单为空。")

    @filter.command("踢人加词", alias={"kick_add"})
    async def kick_add(self, event: AstrMessageEvent, text: str = ""):
        """把一段文本加入本群「命中即踢」规则：/踢人加词 免费领取激活码。"""
        event.stop_event()
        group = str(event.get_group_id() or "")
        if not group:
            yield event.plain_result("请在目标 QQ 群内 @机器人使用。")
            return
        if (msg := self._require_admin(event)):
            yield event.plain_result(msg)
            return
        text = text.strip()
        if not text:
            yield event.plain_result("用法：/踢人加词 <成员发送该内容就踢>")
            return
        ok = self.store.add_keyword(group, text)
        yield event.plain_result(f"已加入「命中即踢」：{text}" if ok else f"该词已在规则中：{text}")

    @filter.command("踢人删词", alias={"kick_del"})
    async def kick_del(self, event: AstrMessageEvent, text: str = ""):
        """从本群「命中即踢」规则移除一段文本：/踢人删词 免费领取激活码。"""
        event.stop_event()
        group = str(event.get_group_id() or "")
        if not group:
            yield event.plain_result("请在目标 QQ 群内 @机器人使用。")
            return
        if (msg := self._require_admin(event)):
            yield event.plain_result(msg)
            return
        if not text.strip():
            yield event.plain_result("用法：/踢人删词 <要移除的内容>")
            return
        ok = self.store.remove_keyword(group, text.strip())
        yield event.plain_result(f"已移除「命中即踢」：{text}" if ok else f"规则中不存在：{text}")

    @filter.command("踢人词表", alias={"kick_words"})
    async def kick_words(self, event: AstrMessageEvent):
        """查看本群「命中即踢」动态词表（不含配置里的静态关键词）。"""
        event.stop_event()
        group = str(event.get_group_id() or "")
        if not group:
            yield event.plain_result("请在群内使用。")
            return
        words = self.store.group_keywords(group)
        yield event.plain_result(
            "本群「命中即踢」词表：\n" + "\n".join(words) if words else "词表为空。用 /踢人加词 添加。"
        )

    @filter.command("验证开", alias={"verify_on"})
    async def verify_on(self, event: AstrMessageEvent):
        """开启本群入群算术验证。"""
        event.stop_event()
        group = str(event.get_group_id() or "")
        if not group:
            yield event.plain_result("请在目标 QQ 群内 @机器人使用。")
            return
        if (msg := self._require_admin(event)):
            yield event.plain_result(msg)
            return
        self.store.group(group)["verify"] = True
        self.store.save()
        yield event.plain_result("已开启本群入群算术验证：新成员首次发言会被出题，限时作答。")

    @filter.command("验证关", alias={"verify_off"})
    async def verify_off(self, event: AstrMessageEvent):
        """关闭本群入群算术验证。"""
        event.stop_event()
        group = str(event.get_group_id() or "")
        if not group:
            yield event.plain_result("请在目标 QQ 群内 @机器人使用。")
            return
        if (msg := self._require_admin(event)):
            yield event.plain_result(msg)
            return
        self.store.group(group)["verify"] = False
        self.store.save()
        yield event.plain_result("已关闭本群入群算术验证。")

    @filter.command("验证状态", alias={"verify_status"})
    async def verify_status(self, event: AstrMessageEvent):
        """查看本群验证开关，以及当前待验证人数。"""
        event.stop_event()
        group = str(event.get_group_id() or "")
        if not group:
            yield event.plain_result("请在群内使用。")
            return
        on = self._verify_enabled(group)
        lines = [
            f"算术验证：{'开启' if on else '关闭'}",
            f"限时：{self.verify.timeout // 60} 分钟",
            f"数字范围：1 - {self.verify.max_number}",
        ]
        if on:
            count = sum(1 for key in self.verify._pending if key[0] == group)
            lines.append(f"待验证成员：{count} 人")
        yield event.plain_result("\n".join(lines))

    @filter.command("通过", alias={"verify_pass"})
    async def verify_pass(self, event: AstrMessageEvent, target: str = ""):
        """管理员直接放行某成员验证：/通过 <openid>。"""
        event.stop_event()
        group = str(event.get_group_id() or "")
        if not group:
            yield event.plain_result("请在群内使用。")
            return
        if (msg := self._require_admin(event)):
            yield event.plain_result(msg)
            return
        user = self._target_openid(event, target)
        if not user:
            yield event.plain_result("未识别到目标 openid。用法：/通过 <32位openid>。")
            return
        removed = self.verify.force(group, user)
        yield event.plain_result("已放行该成员。" if removed else "该成员不在待验证状态。")

    @filter.command("踢人待办", alias={"kick_pending"})
    async def pending(self, event: AstrMessageEvent, action: str = ""):
        """查看/清空待办：/踢人待办 [清空]。"""
        event.stop_event()
        group = str(event.get_group_id() or "")
        if not group:
            yield event.plain_result("请在群内使用。")
            return
        if (msg := self._require_admin(event)):
            yield event.plain_result(msg)
            return
        if action.strip() in ("清空", "clear"):
            count = self.store.clear_pending(group)
            yield event.plain_result(f"已清空 {count} 条待办。")
            return
        rows = self.store.list_pending(group)
        if not rows:
            yield event.plain_result("当前没有需要人工移出的成员。")
            return
        lines = ["需人工移出（踢人接口无权限时的降级记录）："]
        for row in rows[-10:]:
            stamp = time.strftime("%m-%d %H:%M", time.localtime(row.get("ts", 0)))
            lines.append(f"{row.get('user')}｜{stamp}｜{row.get('reason')}")
        lines.append("处理后发送 /踢人待办 清空。")
        yield event.plain_result("\n".join(lines))

    @filter.command("踢人帮助", alias={"kick_help"})
    async def kick_help(self, event: AstrMessageEvent):
        event.stop_event()
        yield event.plain_result(
            "群内 @我 后发送：\n"
            "/踢人开  开启本群自动处置\n"
            "/踢人关  关闭\n"
            "/踢人状态  查看开关、凭据、规则\n"
            "/踢人规则  查看完整规则\n"
            "/踢 <openid>  手动移出（无权限时自动降级禁言）\n"
            "/禁言 <openid> <分钟>\n"
            "/解禁 <openid>\n"
            "/白名单 <openid>｜/白名单删 <openid>｜/白名单列表\n"
            "/踢人加词 <内容>  命中该内容即踢（本群动态规则）\n"
            "/踢人删词 <内容>  移除一条命中即踢规则\n"
            "/踢人词表  查看本群命中即踢词表\n"
            "/验证开 开启入群算术验证｜/验证关 关闭\n"
            "/验证状态 查看验证开关、限时与待验证人数\n"
            "/通过 <openid> 管理员手动放行验证\n"
            "/踢人待办 [清空]\n"
            "注意：官方平台只提供 openid（32 位十六进制），不是 QQ 号。"
        )
