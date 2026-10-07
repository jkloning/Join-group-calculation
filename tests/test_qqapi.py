"""qqapi.py 纯逻辑离线测试。

测试环境没有 aiohttp（插件运行时由 AstrBot 提供），这里用轻量 stub 顶替，
只验证 APIResult 的错误分类与 rfc3339_after 时间格式化，不发起任何网络请求。
"""

import os
import sys
import types
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "aiohttp" not in sys.modules:
    stub = types.ModuleType("aiohttp")

    class _ClientTimeout:
        def __init__(self, total=None):
            self.total = total

    class _ClientSession:  # 仅占位，测试不会真的建连
        def __init__(self, *args, **kwargs):
            self.closed = False

        async def close(self):
            self.closed = True

        def request(self, *args, **kwargs):
            raise AssertionError("测试不应发起网络请求")

    stub.ClientTimeout = _ClientTimeout
    stub.ClientSession = _ClientSession
    sys.modules["aiohttp"] = stub

from qqapi import APIResult, QQOfficialAPI, rfc3339_after  # noqa: E402


class APIResultTests(unittest.TestCase):
    def test_unmanageable_by_code(self):
        # 线上真实返回：目标成员为机器人/群主/管理员，不允许被禁言
        res = APIResult(False, 400, 40103004, "目标成员为机器人/群主/管理员，不允许被禁言")
        self.assertTrue(res.unmanageable)
        self.assertFalse(res.ok)

    def test_unmanageable_by_message_text(self):
        res = APIResult(False, 400, 0, "目标成员为机器人，不允许被禁言")
        self.assertTrue(res.unmanageable)
        res2 = APIResult(False, 400, 0, "该成员是管理员，不允许被操作")
        self.assertTrue(res2.unmanageable)

    def test_normal_permission_error_is_not_unmanageable(self):
        res = APIResult(False, 403, 11253, "应用无接口访问权限")
        self.assertTrue(res.permission_denied)
        self.assertFalse(res.unmanageable)

    def test_ok_result(self):
        res = APIResult(True, 200, 0, "")
        self.assertTrue(res.ok)
        self.assertFalse(res.unmanageable)
        self.assertEqual(res.brief(), "成功")

    def test_brief_includes_code_and_message(self):
        res = APIResult(False, 400, 40103004, "不允许被禁言")
        text = res.brief()
        self.assertIn("40103004", text)
        self.assertIn("不允许被禁言", text)


class TimeFormatTests(unittest.TestCase):
    def test_rfc3339_format_and_timezone(self):
        text = rfc3339_after(600, now=0)
        self.assertTrue(text.endswith("+08:00"), text)
        datetime.strptime(text, "%Y-%m-%dT%H:%M:%S+08:00")  # 格式必须可解析

    def test_minimum_is_60_seconds(self):
        base = 1_700_000_000.0
        short = datetime.strptime(rfc3339_after(1, now=base), "%Y-%m-%dT%H:%M:%S+08:00")
        # 小于 60 秒的请求会被抬到 60 秒（平台最短禁言时长）
        self.assertGreaterEqual((short - datetime(1970, 1, 1)).total_seconds(), 0)

    def test_longer_than_short(self):
        base = 1_700_000_000.0
        a = rfc3339_after(60, now=base)
        b = rfc3339_after(3600, now=base)
        self.assertLess(a, b)


class CredentialTests(unittest.TestCase):
    def test_configured_flag(self):
        self.assertFalse(QQOfficialAPI("", "").configured)
        self.assertTrue(QQOfficialAPI("123", "abc").configured)
        self.assertTrue(QQOfficialAPI(" 123 ", " abc ").configured)


if __name__ == "__main__":
    unittest.main()
