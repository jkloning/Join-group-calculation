# QQ 官方机器人 群自动踢人 / 禁言插件

面向 AstrBot + **QQ 官方机器人（Websocket，推荐）** 适配器。命中规则后自动处置群成员：真踢 → 无权限则降级禁言 → 仍失败记入待办。

## 一、平台能力对照（先看这个）

| 能力 | 官方接口 | 普通机器人可用性 |
| --- | --- | --- |
| 群成员批量移除（真踢） | `POST /v2/groups/{group_openid}/batch_remove_members` | **需内邀/白名单权限**，未开通返回错误码 `11253`（应用无接口访问权限） |
| 群成员禁言 | `POST /v2/groups/{group_openid}/restrict_chat_setting` | 可用，**机器人需为群管理员**，最长 30 天 |
| 群消息撤回 | `DELETE /v2/groups/{group_openid}/messages/{message_id}` | 可用（本插件预留，默认不调用） |
| 群黑名单 | `/v2/groups/{group_openid}/member_blacklist` | 与批量移除配套 |
| 按 QQ 号踢人 | 无 | 官方平台只下发 `member_openid`（32 位十六进制），**拿不到 QQ 号** |

因此本插件的默认策略是「能踢则踢，踢不动就 30 天禁言」，并把需要人工移出的成员记入 `/踢人待办`。如果你必须稳定真踢人，把适配器换成 NapCat / Lagrange 等 OneBot 实现即可 —— 插件检测到非官方平台会自动改用 `set_group_kick`。

## 二、安装

```text
AstrBot/data/plugins/astrbot_plugin_qq_autokick/
├── main.py           # 事件监听 + 指令
├── core.py           # 规则判定 + 状态持久化（纯标准库）
├── qqapi.py          # 官方 OpenAPI 客户端（aiohttp）
├── _conf_schema.json # 配置项
├── metadata.yaml
├── requirements.txt
└── tests/test_core.py
```

1. 把整个 `astrbot_plugin_qq_autokick` 目录放进 `AstrBot/data/plugins/`。
2. 在 AstrBot WebUI → 插件管理 → 重载插件（首次会自动安装 `aiohttp`）。
3. 填写插件配置 `appid` / `secret`（QQ 开放平台 → 开发设置 → 机器人资料里的 AppID 与 AppSecret）。留空时插件会尝试从 AstrBot 的平台配置里自动读取。
4. 在群里 @机器人 发送 `/踢人开`。

## 三、开放平台侧必须做的三件事

1. **消息接收设置**：官方机器人默认只收到「@机器人」的群消息。要自动处置全部群消息，需在 QQ 开放平台申请并开启全量消息接收（`GROUP_MSG_RECEIVE`）。未开通前，插件只在成员 @机器人 时判定 —— 刷屏、广告一般不会 @机器人，触发面会很窄，这一点是平台限制，不是插件问题。
2. **机器人管理员身份**：把机器人设为群管理员，否则 `restrict_chat_setting` 会失败。
3. **批量移除权限（可选）**：如需真踢人，向 QQ 开放平台运营申请 `batch_remove_members` 内邀权限。

## 四、指令（群内 @机器人）

| 指令 | 说明 |
| --- | --- |
| `/踢人开` `/踢人关` | 开关本群的自动处置 |
| `/踢人状态` | 查看开关、凭据、平台、规则概览 |
| `/踢人规则` | 查看完整规则文本 |
| `/踢 <openid>` | 手动移出；无权限自动降级禁言 |
| `/禁言 <openid> <分钟>` | 手动禁言 |
| `/解禁 <openid>` | 解除禁言 |
| `/白名单 <openid>` / `/白名单删 <openid>` / `/白名单列表` | 白名单管理 |
| `/踢人加词 <内容>` / `/踢人删词 <内容>` / `/踢人词表` | 「命中即踢」动态词表管理 |
| `/验证开` `/验证关` `/验证状态` | 入群算术验证开关与状态 |
| `/通过 <openid>` | 管理员手动放行某成员验证 |
| `/验证导出` | 导出本群已验证名单（并写备份文件），更新前备份用 |
| `/验证导入 [openid列表]` | 更新/迁移后恢复名单；不带参数时读备份文件 |
| `/验证清除 确认` | 清空本群已验证名单（全群重新验证） |
| `/忽略 <openid>` / `/忽略删 <openid>` / `/忽略列表` | 忽略名单：不验证也不处置（其他机器人、群主、管理员） |
| `/踢人待办 [清空]` | 查看/清空需人工移出的降级记录 |
| `/踢人帮助` | 指令总览 |

开关、白名单、待办、验证、词表类指令仅限 AstrBot 管理员（`event.is_admin()`）。

## 五、自动规则

| 规则 | 默认阈值 | 默认处置 |
| --- | --- | --- |
| 刷屏 | 10 秒内发言 ≥ 6 条 | 禁言 |
| 复读 | 10 秒内同一内容 ≥ 3 次 | 禁言 |
| 关键词 | `banned_keywords` 命中 | 踢 |
| 动态命中即踢词（`/踢人加词`） | 按群存储，包含即命中 | 踢 |
| 链接 / 广告话术 | `https://`、`.com/`、`t.me/`、`加群`、`加微信` 等 | 老成员禁言 / 新人禁言 |
| 新人广告 | 机器人首次看到该成员发言起 600 秒内 | 可单独配成踢 |

同一成员 30 秒冷却，避免重复处置。白名单成员不参与判定。

### 忽略名单：其他机器人 / 群主 / 管理员不被验证与处置

群里其他机器人（如 **Q群管家**）会自己发欢迎语，**绝不能**当成新成员去出题验证——它答不了题，10 分钟后会被踢掉。插件用三重机制排除：

1. **昵称关键词**：昵称含 `管家` / `机器人` / `小助手` / `bot` 等（配置项 `bot_name_keywords` 可改），直接跳过，不验证不出题。
2. **平台错误码自学习**：一旦平台返回 `40103004 目标成员为机器人/群主/管理员，不允许被禁言`，插件自动把该成员加入忽略名单，之后不再对它出题或处置。
3. **手动忽略**：`/忽略 <openid>`、`/忽略删 <openid>`、`/忽略列表`。

忽略名单持久化在 `state.json` 的 `ignored` 表，重载/更新不丢。

## 六、入群算术验证（防广告机器人）

新成员「入群」时，机器人在群里 @ 他出一道 **50 以内加减乘除题**，限时 **10 分钟**：答对放行、**答错直接禁言**、**超时未答自动踢**（真踢 → 无权限降级禁言 → 记待办）。

> **平台限制说明**：QQ 官方机器人**没有「普通成员入群」事件**（官方只给机器人自己被拉入/移出的通知）。所以本功能用**「机器人第一次在该群看到该成员发言」当作入群锚点**——这是官方平台下最接近「入群即验证」的可行方案。想卡住入群那一刻，需换带完整群事件的 NapCat/Lagrange 适配器（本插件的验证逻辑在 OneBot 下同样可用，且可直接 `set_group_kick` 踢人）。

> **重启/重装不重复验证**：已通过验证（含 `/通过` 手动放行）的成员会写入本群持久化名单（`state.json` 的 `verified` 表），插件重载、AstrBot 重启、重新安装都**不会**让这些人再次被出题。白名单成员天然不参与验证。

### 服务器部署：更新插件如何保住「已通过」名单

名单**不在插件目录里**，而在 AstrBot 数据目录，所以「更新插件」不会删它：

```text
插件代码：/AstrBot/data/plugins/astrbot_plugin_qq_autokick/      ← 更新会覆盖
状态数据：/AstrBot/data/plugin_data/astrbot_plugin_qq_autokick/state.json  ← 更新不动
```

插件启动时会把这行打进日志，服务器上直接看日志就能确认路径：

```text
[auto_kick] 状态文件：/AstrBot/data/plugin_data/astrbot_plugin_qq_autokick/state.json
```

**三重保险**：

1. **指定自定义路径**：配置项 `state_file` 填绝对路径（如 `/data/autokick/state.json`），把数据放到挂载卷/备份盘，与插件目录彻底解耦。
2. **导出备份**：群里发 `/验证导出`，会把名单打印出来并同时写入备份文件 `verified_backup_<群尾号>.txt`（在状态文件同目录），服务器上可直接 `scp`。
3. **更新后恢复**：发一次 `/验证导入`（自动读同目录备份文件），或 `/验证导入 <openid列表>` 手动贴。

```bash
# 服务器上更新前的推荐做法
cp /AstrBot/data/plugin_data/astrbot_plugin_qq_autokick/state.json /root/autokick-state.bak
# 更新插件后
cp /root/autokick-state.bak /AstrBot/data/plugin_data/astrbot_plugin_qq_autokick/state.json
# 重启 AstrBot / 重载插件即可，名单原样恢复
```

> 配置项（appid/secret/关键词/阈值）是 AstrBot 存的插件配置，更新时若被重置，装之前先备份 `_conf_schema.json` 对应的那份配置；`state.json` 只管**名单与开关**。

执行流程：

```
成员在群里第一次发言（需开启「消息接收设置」）
  → 群里 @ 出题「5 + 7 = ?」
  → 10 分钟计时开始（后台每秒轮询该群所有待验证项）
  → 答对：@ 放行，标记已验证
  → 答错数字：直接禁言（默认 10 分钟，可配），本次验证结束
  → 发图片/表情/闲聊：**不算作答**，不判错不禁言，仅在 60 秒节流下提醒一次「请回复答案数字」
  → 超时：自动踢 → 无权限降级禁言 → 仍失败记入待办
```

每位成员**最多只会被出一次题**；答对后再发言不受验证逻辑拦截，正常走自动规则。答错会结束验证（不会重复禁言同一人）。相关指令：`/验证开`、`/验证关`、`/验证状态`、`/通过 <openid>`。验证相关配置见下节。

## 七、配置项（`_conf_schema.json`）

关键项：

```json
{
  "appid": "",
  "secret": "",
  "msg_window_sec": 10,
  "msg_threshold": 6,
  "flood_action": "mute",
  "banned_keywords": ["加微信", "兼职", "刷单", "贷款", "代刷", "出售账号"],
  "keyword_action": "kick",
  "ban_link": true,
  "link_action": "mute",
  "newbie_link_action": "mute",
  "default_mute_seconds": 2592000,
  "fallback_mute_seconds": 2592000,
  "blacklist_on_kick": false,
  "verify_enabled": true,
  "verify_timeout_sec": 600,
  "verify_max_number": 50,
  "verify_wrong_mute_seconds": 600
}
```

`*_action` 取值：`kick` / `mute` / `off`。验证配置：`verify_enabled`（总开关）、`verify_timeout_sec`（限时秒，默认 600=10 分钟，超时踢）、`verify_max_number`（数字上限，默认 50）、`verify_wrong_mute_seconds`（答错禁言时长秒，默认 600=10 分钟）。状态位置：`state_file`（留空则用 AstrBot 数据目录）。

## 八、让 AstrBot 自动识别插件更新

### 8.1 AstrBot 的判定链路（源码依据）

以 AstrBot `v4.28.2` 为准，更新检测分两层：

**① 是否允许更新** —— `astrbot/dashboard/services/plugin_service.py`：

```python
updates_enabled = (install_method in {"market", "repository"} and not plugin.reserved)
```

| 安装方式 | install_method | 能否更新 |
| --- | --- | --- |
| 插件市场安装 | `market` | ✅ 可检测 + 可更新 |
| GitHub 仓库安装 | `repository` | ✅ 可更新（凭 `repo` 字段拉取），但**不出「有新版本」提示** |
| zip / 上传安装 | `upload` | ❌ 提示「该插件不是通过插件市场安装，无法检测或执行更新。」 |
| 早期安装（无来源记录） | 隐式记录 | ❌ 提示「请先选择插件安装源后再更新。」 |

**② 是否有新版本** —— 前端 `dashboard/src/views/extension/useExtensionPage.js` 的 `checkUpdate()`：

```js
if (!extension.updates_enabled || !source || source.install_method !== "market") return;
```

**只有 `install_method === "market"` 才会去比对版本**。匹配市场记录的优先级是
`install_source.market_plugin_id` → `install_source.repo` → `插件名（下划线转连字符）`，
命中后比较 `metadata.yaml` 的 `version` 与市场记录里的 `version`，前者更小才标红。

### 8.2 三个必要条件

1. **`metadata.yaml` 必须有 `repo` 字段**（否则插件不知道自己该从哪更新）。
2. **每次发版必须递增 `version`**，且必须与市场记录里的 `version` **完全一致**（市场规范强制校验 `author`/`name`/`version` 三字段与 `metadata.yaml` 相等）。
3. **插件必须能在某个「插件源」里被找到**，否则永远比较不出新版本。

### 8.3 三种做法（选一个）

**方案 A：发布到官方插件市场（最省心）**

插件代码推到 GitHub 后，去 [AstrBot 插件发布页](https://cloud.astrbot.app/publish) 提交（需 AstrBot Cloud 账号）。
之后官方市场 JSON 里就有你的记录，用户安装即为 `market` 方式，更新自动提示。

**方案 B：自建插件源 + 绑定（不用审核，本仓库已带模板）**

仓库里已经放了 [`market/plugins.json`](market/plugins.json)，符合[插件市场 JSON 规范](https://docs.astrbot.app/dev/plugin-market/2026-06-27.html)。
用它的 raw 地址作为自定义源：

```text
https://raw.githubusercontent.com/jkloning/qq-/main/market/plugins.json
```

WebUI 操作：**插件 → 插件市场 → 管理插件源 → 添加** 填上面的 URL；
再对已安装的插件点 **更换/绑定插件源**，选中该源。
绑定后 `install_method` 变为 `market`，版本比对生效（首次绑定要求市场记录的 `repo` 与本地一致，本仓库两者都是 `https://github.com/jkloning/qq-`，可直接通过）。

> 自定义源就是**直接 GET 你填的那个 URL**，返回插件市场 JSON 即可。
> 它还会尝试 GET 同目录的 `plugins-md5.json` 做缓存校验；该文件可选，缺失只会导致每次都重新拉取。

**方案 C：只让「更新」按钮可用**

以 **GitHub 仓库方式**安装（WebUI → 安装插件 → 填 `https://github.com/jkloning/qq-`），
`install_method` 为 `repository`，更新按钮会按 `metadata.yaml` 的 `repo` 直接拉最新代码 —— 但没有「有新版本」红点提示，需要自己点。

### 8.4 每次发版的固定动作

```bash
# 1) 改 metadata.yaml 的 version（例如 1.1.0 → 1.1.1）
# 2) 同步改 market/plugins.json 里的 version（方案 B 必须，否则比对不出更新）
# 3) 在 CHANGELOG.md 顶部追加一段（AstrBot 插件详情页会读取根目录 CHANGELOG.md）
git add -A && git commit -m "release 1.1.1" && git push
```

版本号用 `1.1.1` 这种纯数字语义化写法，不要带 `v` 前缀。

## 九、故障排查

| 现象 | 原因与处理 |
| --- | --- |
| 日志出现 `踢人接口未开通（HTTP 403 / 错误码 11253）` | 正常现象：批量移除是内邀能力。插件已自动降级为禁言并写入 `/踢人待办` |
| `禁言失败` | 机器人不是群管理员，或 `mute_expire_at` 超过 30 天 |
| 群里刷屏没反应 | 未开启「消息接收设置」，机器人收不到非 @ 消息；或 `/踢人关` 状态 |
| 新成员进群没被出题 | 官方平台无「入群事件」，需该成员首条发言（且开了消息接收）才触发；或者 `/验证关` 状态 |
| `未配置 AppID/AppSecret` | 在插件配置填写，或在 AstrBot 平台配置里确认 `appid`/`secret` 字段名 |
| 想按 QQ 号操作 | 官方平台只提供 openid，用 `/白名单列表`、`/踢人待办` 里的 openid 操作 |
| 换回 NapCat 后行为异常 | 插件会自动走 `set_group_kick` / `set_group_ban`，openid 概念在 OneBot 下等同于 QQ 号 |
| 日志出现 `40103004 目标成员为机器人/群主/管理员` | 正常现象：插件已自动把该成员加入忽略名单，之后不再验证/处置 |
| 群管家/其他机器人被出题 | 已由 `bot_name_keywords` 与忽略名单拦截；若昵称特殊，用 `/忽略 <openid>` 手动加 |
| 插件列表里没有「有更新」提示 | 安装方式不是 `market`。见第八节：用自建插件源绑定，或发布到官方插件市场 |
| 点更新提示「请先选择插件安装源后再更新」 | 该插件是早期安装、没有来源记录。在插件卡片上「更换插件源」绑定一个源即可 |
| 点更新提示「不是通过插件市场安装」 | 安装方式是 zip 上传。改用 GitHub 仓库安装或市场安装 |

## 十、本地自检

```bash
cd astrbot_plugin_qq_autokick
python -m unittest discover -s tests -t . -v
```

51 条用例覆盖刷屏窗口滑动、复读、关键词开关、命中即踢词表、算术验证出题/判题/超时/放行、已验证名单持久化与更新后恢复（导入/清除/去重）、忽略名单（机器人/群主/管理员）、作答数字解析（图片与闲聊不算作答）、平台错误码分类（40103004 不可管理）、禁言到期时间格式、链接与新人判定、冷却、状态持久化与容错读取。
