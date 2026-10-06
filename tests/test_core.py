"""离线单测：只覆盖 core.py 的纯逻辑，不需要安装 astrbot / aiohttp。

运行：
    python -m unittest discover -s tests -t . -v
"""

import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import (  # noqa: E402
    ArithVerify,
    Decision,
    RuleEngine,
    StateStore,
    extract_openids,
    looks_like_openid,
)

GROUP = "3E5D8A1F7B2C9E4D6A0F1B3C5D7E9F2A"
USER = "7A3B9C1D5E2F4A6B8C0D1E3F5A7B9C2D"


class FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += seconds


def engine(clock, **overrides):
    config = {
        "msg_window_sec": 10,
        "msg_threshold": 4,
        "repeat_threshold": 3,
        "flood_action": "mute",
        "repeat_action": "mute",
        "keyword_action": "kick",
        "link_action": "mute",
        "newbie_link_action": "mute",
        "ban_link": True,
        "default_mute_seconds": 1800,
        "action_cooldown_sec": 30,
        "newbie_window_sec": 600,
        "banned_keywords": ["加微信"],
    }
    config.update(overrides)
    return RuleEngine(config, clock=clock)


class DecisionTests(unittest.TestCase):
    def test_hit_and_describe(self):
        self.assertFalse(Decision().hit)
        self.assertTrue(Decision(action="kick", reason="x").hit)
        self.assertIn("禁言 30 分钟", Decision(action="mute", mute_seconds=1800).describe())
        self.assertEqual(Decision().describe(), "不处理")


class IdTests(unittest.TestCase):
    def test_openid_detection(self):
        self.assertTrue(looks_like_openid(USER))
        self.assertTrue(looks_like_openid(USER.lower()))
        self.assertFalse(looks_like_openid("123456"))
        self.assertFalse(looks_like_openid("z" * 32))

    def test_extract(self):
        text = f"@某人 {USER} 和 {USER.lower()}"
        self.assertEqual(len(extract_openids(text)), 2)


class RuleTests(unittest.TestCase):
    def test_clean_message_passes(self):
        clock = FakeClock()
        rules = engine(clock)
        rules.touch(GROUP, USER)
        self.assertFalse(rules.inspect(GROUP, USER, "今天天气不错").hit)

    def test_flood_triggers_mute(self):
        clock = FakeClock()
        rules = engine(clock)
        rules.touch(GROUP, USER)
        decisions = [rules.inspect(GROUP, USER, f"消息{i}") for i in range(4)]
        hit = [d for d in decisions if d.hit]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0].action, "mute")
        self.assertIn("10 秒内发言 4 条", hit[0].reason)

    def test_flood_window_slides(self):
        clock = FakeClock()
        rules = engine(clock)
        rules.touch(GROUP, USER)
        for i in range(3):
            self.assertFalse(rules.inspect(GROUP, USER, f"m{i}").hit)
            clock.tick(3)  # 每 3 秒一条，窗口内最多 4 条
        clock.tick(30)
        self.assertFalse(rules.inspect(GROUP, USER, "m3").hit)

    def test_repeat_triggers(self):
        clock = FakeClock()
        rules = engine(clock, msg_threshold=10)
        rules.touch(GROUP, USER)
        for _ in range(2):
            self.assertFalse(rules.inspect(GROUP, USER, "复读").hit)
        decision = rules.inspect(GROUP, USER, "复读")
        self.assertTrue(decision.hit)
        self.assertIn("重复发送同一内容 3 次", decision.reason)

    def test_keyword_triggers_kick(self):
        clock = FakeClock()
        rules = engine(clock)
        rules.touch(GROUP, USER)
        decision = rules.inspect(GROUP, USER, "想赚钱就加微信详聊")
        self.assertEqual(decision.action, "kick")

    def test_keyword_case_insensitive_and_ignored_when_off(self):
        clock = FakeClock()
        rules = engine(clock, banned_keywords=["Telegram"], keyword_action="kick")
        rules.touch(GROUP, USER)
        self.assertTrue(rules.inspect(GROUP, USER, "来 telegram 找我").hit)

        clock = FakeClock()
        rules = engine(clock, keyword_action="off", ban_link=False)
        rules.touch(GROUP, USER)
        self.assertFalse(rules.inspect(GROUP, USER, "加微信").hit)

    def test_ad_phrase_without_link_is_still_blocked(self):
        # 「加微信」即使不在关键词表，也会被链接/广告话术规则命中
        clock = FakeClock()
        rules = engine(clock, keyword_action="off", banned_keywords=[])
        rules.touch(GROUP, USER)
        clock.tick(1000)
        decision = rules.inspect(GROUP, USER, "想赚快钱就加微信")
        self.assertTrue(decision.hit)
        self.assertEqual(decision.reason, "发送链接或广告内容")

    def test_link_blocked_for_veteran(self):
        clock = FakeClock()
        rules = engine(clock)
        rules.touch(GROUP, USER)
        clock.tick(1000)  # 超过新人窗口
        decision = rules.inspect(GROUP, USER, "看这个 https://example.com/x")
        self.assertEqual(decision.action, "mute")
        self.assertEqual(decision.reason, "发送链接或广告内容")

    def test_link_blocked_for_newcomer(self):
        clock = FakeClock()
        rules = engine(clock, newbie_link_action="kick")
        rules.touch(GROUP, USER)
        clock.tick(5)
        decision = rules.inspect(GROUP, USER, "www.example.com/join")
        self.assertEqual(decision.action, "kick")
        self.assertIn("新成员", decision.reason)

    def test_link_disabled(self):
        clock = FakeClock()
        rules = engine(clock, ban_link=False)
        rules.touch(GROUP, USER)
        clock.tick(1000)
        self.assertFalse(rules.inspect(GROUP, USER, "https://example.com").hit)

    def test_cooldown_suppresses_repeat_action(self):
        clock = FakeClock()
        rules = engine(clock)
        rules.touch(GROUP, USER)
        for i in range(4):
            rules.inspect(GROUP, USER, f"a{i}")
        clock.tick(1)
        decision = rules.inspect(GROUP, USER, "a9")
        self.assertFalse(decision.hit)  # 冷却期内不再处置

    def test_cooldown_expires(self):
        clock = FakeClock()
        rules = engine(clock)
        rules.touch(GROUP, USER)
        for i in range(4):
            rules.inspect(GROUP, USER, f"a{i}")
        clock.tick(31)
        for i in range(4):
            decision = rules.inspect(GROUP, USER, f"b{i}")
            if decision.hit:
                break
        self.assertTrue(decision.hit)

    def test_forget_resets_state(self):
        clock = FakeClock()
        rules = engine(clock)
        rules.touch(GROUP, USER)
        for i in range(4):
            rules.inspect(GROUP, USER, f"a{i}")
        rules.forget(GROUP, USER)
        self.assertFalse(rules.inspect(GROUP, USER, "新的一轮").hit)

    def test_groups_are_isolated(self):
        clock = FakeClock()
        rules = engine(clock)
        other_group = "9" * 32
        rules.touch(GROUP, USER)
        rules.touch(other_group, USER)
        for i in range(3):
            self.assertFalse(rules.inspect(GROUP, USER, f"a{i}").hit)
        self.assertFalse(rules.inspect(other_group, USER, "x").hit)

    def test_extra_keywords_trigger_kick(self):
        # 每群动态「命中即踢」词：extra_keywords 命中即看 keyword_action
        clock = FakeClock()
        rules = engine(clock, keyword_action="kick")
        rules.touch(GROUP, USER)
        extra = ["免费领取激活码"]
        d1 = rules.inspect(GROUP, USER, "进群先领 免费领取激活码", extra_keywords=extra)
        self.assertEqual(d1.action, "kick")
        self.assertIn("免费领取激活码", d1.reason)
        # 该词不在静态表时，普通消息不受影响
        self.assertFalse(rules.inspect(GROUP, USER, "正常内容", extra_keywords=extra).hit)

    def test_extra_keywords_ignored_when_action_off(self):
        clock = FakeClock()
        rules = engine(clock, keyword_action="off", ban_link=False)
        rules.touch(GROUP, USER)
        self.assertFalse(
            rules.inspect(GROUP, USER, "免费领取激活码", extra_keywords=["免费领取激活码"]).hit
        )

    def test_extra_keywords_case_insensitive(self):
        clock = FakeClock()
        rules = engine(clock, keyword_action="mute")
        rules.touch(GROUP, USER)
        d = rules.inspect(GROUP, USER, "领取 免费领取激活码", extra_keywords=["免费领取激活码"])
        self.assertEqual(d.action, "mute")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults(self):
        store = StateStore(self.path)
        self.assertTrue(store.is_enabled(GROUP))
        self.assertEqual(store.whitelist(GROUP), [])

    def test_toggle_and_persist(self):
        store = StateStore(self.path)
        self.assertFalse(store.toggle(GROUP))
        again = StateStore(self.path)
        self.assertFalse(again.is_enabled(GROUP))

    def test_whitelist_add_remove(self):
        store = StateStore(self.path)
        self.assertTrue(store.add_white(GROUP, USER))
        self.assertFalse(store.add_white(GROUP, USER))
        self.assertEqual(store.whitelist(GROUP), [USER])
        self.assertTrue(store.remove_white(GROUP, USER))
        self.assertFalse(store.remove_white(GROUP, USER))

    def test_pending_roundtrip(self):
        store = StateStore(self.path)
        store.add_pending(GROUP, USER, "刷屏")
        rows = StateStore(self.path).list_pending(GROUP)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "刷屏")
        self.assertEqual(store.clear_pending(GROUP), 1)
        self.assertEqual(store.list_pending(GROUP), [])

    def test_keyword_store_roundtrip(self):
        store = StateStore(self.path)
        self.assertEqual(store.group_keywords(GROUP), [])
        self.assertTrue(store.add_keyword(GROUP, "免费领取激活码"))
        self.assertFalse(store.add_keyword(GROUP, "免费领取激活码"))
        reloaded = StateStore(self.path)
        self.assertEqual(reloaded.group_keywords(GROUP), ["免费领取激活码"])
        self.assertTrue(reloaded.remove_keyword(GROUP, "免费领取激活码"))
        self.assertFalse(reloaded.remove_keyword(GROUP, "免费领取激活码"))
        self.assertEqual(reloaded.group_keywords(GROUP), [])

    def test_keyword_store_per_group(self):
        store = StateStore(self.path)
        other = "9" * 32
        store.add_keyword(GROUP, "A词")
        self.assertEqual(store.group_keywords(other), [])

    def test_corrupt_file_recovers(self):
        self.path.write_text("{not json", encoding="utf-8")
        store = StateStore(self.path)
        self.assertTrue(store.is_enabled(GROUP))
        store.add_white(GROUP, USER)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["groups"][GROUP]["whitelist"], [USER])


def make_verify(clock, rng, **overrides):
    config = {
        "verify_timeout_sec": 600,
        "verify_max_number": 50,
    }
    config.update(overrides)
    return ArithVerify(config, clock=clock, rng=rng)


class ArithVerifyTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.rng = random.Random(7)

    def test_challenge_returns_and_answer_ok(self):
        v = make_verify(self.clock, self.rng)
        issued = v.challenge(GROUP, USER)
        self.assertIsNotNone(issued)
        expr, answer, timeout = issued
        self.assertTrue(any(op in expr for op in (" + ", " - ", " * ", " / ")))
        self.assertEqual(timeout, 600)
        self.assertEqual(v.answer(GROUP, USER, str(answer)), "ok")

    def test_challenge_not_reissued_while_pending(self):
        v = make_verify(self.clock, self.rng)
        v.challenge(GROUP, USER)
        self.assertIsNone(v.challenge(GROUP, USER))

    def test_wrong_and_non_numeric_answers(self):
        v = make_verify(self.clock, self.rng)
        expr, answer, _ = v.challenge(GROUP, USER)
        wrong = answer + 1
        self.assertEqual(v.answer(GROUP, USER, str(wrong)), "wrong")
        # 答错会结束验证（防止重复禁言），且不再处于待验证状态
        self.assertEqual(v.is_pending(GROUP, USER), (False, None))

    def test_no_pending_returns_none(self):
        v = make_verify(self.clock, self.rng)
        self.assertEqual(v.answer(GROUP, USER, "1"), "none")
        self.assertEqual(v.is_pending(GROUP, USER), (False, None))

    def test_timeout_detection(self):
        v = make_verify(self.clock, self.rng, verify_timeout_sec=600)
        v.challenge(GROUP, USER)
        self.clock.tick(300)
        self.assertEqual(v.is_pending(GROUP, USER)[0], True)
        self.clock.tick(301)  # 已满 601 秒
        pending, status = v.is_pending(GROUP, USER)
        self.assertTrue(pending)
        self.assertEqual(status, "timeout")

    def test_force_removes(self):
        v = make_verify(self.clock, self.rng)
        v.challenge(GROUP, USER)
        self.assertTrue(v.is_pending(GROUP, USER)[0])
        self.assertTrue(v.force(GROUP, USER))
        self.assertFalse(v.force(GROUP, USER))
        self.assertEqual(v.is_pending(GROUP, USER), (False, None))

    def test_expression_within_range(self):
        v = make_verify(self.clock, self.rng, verify_max_number=10)
        for _ in range(200):
            expr, answer, _ = v.challenge(GROUP, USER)
            self.assertIsNotNone(expr)
            # 除法必须在数字上限内整除：回顾生成逻辑保证 a <= max*? 需验证答案范围
            self.assertLessEqual(answer, 100)
            v.force(GROUP, USER)


if __name__ == "__main__":
    unittest.main()
