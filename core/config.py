from __future__ import annotations

import copy
import hashlib
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .util import CN_TZ, MAX_REPLY_CHARS, atomic_json, dump, read_json


class ConfigError(ValueError):
    pass


DEFAULT_SPLIT_CHARS = 300


def field(key, label, default, desc, *, low=None, high=None, choices=None, widget=None):
    kind = {
        bool: "boolean",
        int: "integer",
        float: "number",
        str: "string",
        list: "array",
        dict: "object",
    }[type(default)]
    out = dict(key=key, label=label, default=default, description=desc, type=kind)
    if low is not None:
        out.update(min=low, max=high)
    if choices:
        out["choices"] = choices
    if widget:
        out["widget"] = widget
    return out


SCHEMA = [
    {
        "key": "access",
        "label": "启用与访问控制",
        "fields": [
            field(
                "mode",
                "运行模式",
                "off",
                "off 不接管；observe 仅记录白名单消息、模拟判断（不发回复）；active 正式接管。",
                choices=["off", "observe", "active"],
            ),
            field(
                "allowed_groups",
                "群聊白名单",
                [],
                "填 QQ 群 OpenID 或 AstrBot /sid 显示的完整 UMO，每行一项。空列表不处理群聊。",
                widget="lines",
            ),
            field(
                "allow_all_groups",
                "允许全部群聊",
                False,
                "仅在已获得群成员知情授权时启用；默认关闭。",
            ),
            field(
                "private_enabled",
                "接管私聊",
                False,
                "私聊另行启用，并且需要填写私聊用户白名单。",
            ),
            field(
                "private_users",
                "私聊用户白名单",
                [],
                "填接口提供的用户 OpenID（或 OneBot QQ 号），空时不接管。",
                widget="lines",
            ),
            field(
                "blocked_users",
                "用户黑名单",
                [],
                "匹配平台发送者 ID；不保存、不学习、不回复。",
                widget="lines",
            ),
            field(
                "allowed_platforms",
                "平台类型",
                ["qq_official", "qq_official_webhook"],
                "需要 OneBot 时添加 aiocqhttp。",
                widget="lines",
            ),
            field(
                "command_prefixes",
                "指令前缀",
                ["/", "!", "！"],
                "让现有插件和 AstrBot 优先处理命令；已识别的注册命令也会让行。",
                widget="lines",
            ),
        ],
    },
    {
        "key": "persona",
        "label": "人格与上下文",
        "fields": [
            field(
                "persona_id",
                "固定人格 ID",
                "",
                "留空跟随 AstrBot 实际会话人格，可明确选择。不同人格的记忆隔离。",
                widget="persona",
            ),
            field(
                "fallback_prompt",
                "备用人格",
                "你是大肥鱼，一位喜欢吃白饭、活泼自然的蓝发小鲸鱼娘。群聊以自然、简洁、友善的方式交流。",
                "仅在未配置有效人格时使用。",
                widget="textarea",
            ),
            field(
                "extra_prompt",
                "额外回复要求",
                "平常聊天简短自然。认真回答明确的问题，事实和数值不确定就查工具，别编造。",
                "管理员配置的长期规则；学习结果不覆盖本项。",
                widget="textarea",
            ),
            field(
                "names",
                "名字与别名",
                ["肥鱼", "大肥鱼"],
                "名字被提及时优先进入判断；真实 @ 不依赖这些关键词。",
                widget="lines",
            ),
            field(
                "context_messages",
                "回复上下文消息数",
                45,
                "按时间保留同群或同私聊近期聊天，包含没回复过的消息。",
                low=5,
                high=150,
            ),
            field(
                "context_chars",
                "回复上下文字符预算",
                8500,
                "保留最新话题及本批次触发消息。",
                low=1500,
                high=30000,
            ),
            field(
                "raw_retention_days",
                "短期消息保留天数",
                7,
                "定期清理短期聊天原文（不是长期记忆/日志）。",
                low=1,
                high=90,
            ),
            field(
                "raw_max_per_scope",
                "每会话短期消息上限",
                2000,
                "超限清除最老的消息；注意学习积压。",
                low=200,
                high=30000,
            ),
        ],
    },
    {
        "key": "models",
        "label": "模型分工",
        "fields": [
            field(
                "chat_provider",
                "主聊天模型 Provider ID",
                "",
                "空值使用会话当前聊天模型。",
                widget="chat_provider",
            ),
            field(
                "decision_provider",
                "读空气模型 Provider ID",
                "",
                "建议非思考的快速模型；留空跟随主聊天模型。",
                widget="chat_provider",
            ),
            field(
                "memory_provider",
                "记忆提取模型 Provider ID",
                "",
                "留空跟随主聊天模型。",
                widget="chat_provider",
            ),
            field(
                "style_provider",
                "表达学习模型 Provider ID",
                "",
                "留空跟随记忆模型。",
                widget="chat_provider",
            ),
            field(
                "vision_provider",
                "视觉模型 Provider ID",
                "",
                "选择已配置、真实支持图片的模型（如 Qwen VL）。空值不主动分析图片。",
                widget="chat_provider",
            ),
            field("research_provider", "自主学习模型 Provider ID", "",
                  "留空使用记忆模型或主聊天模型。只用于有限的后台公开资料阅读。",
                  widget="chat_provider"),
            field(
                "embedding_provider",
                "记忆语义检索 Embedding ID",
                "",
                "可选；留空使用本地中文关键词检索，无额外 embedding 成本。",
                widget="embedding_provider",
            ),
            field(
                "timeout_seconds",
                "回复模型单次超时（秒）",
                55,
                "超时停止等待；底层服务可能仍已计费。",
                low=10,
                high=180,
            ),
            field(
                "max_parallel_requests",
                "同时模型请求数",
                3,
                "限流以保护 Windows ECS；后台任务每次只处理一批。",
                low=1,
                high=12,
            ),
        ],
    },
    {
        "key": "decision",
        "label": "读空气与消息合并",
        "fields": [
            field(
                "enabled",
                "启用非 @ 读空气",
                True,
                "关闭时仍处理 @、引用机器人、名字策略和私聊。",
            ),
            field(
                "ordinary_check_probability",
                "普通话题判断采样比例",
                0.35,
                "普通消息批次进入 AI 判断的比例；名字/续聊不受采样影响。1=每批均判断。",
                low=0,
                high=1,
            ),
            field(
                "name_mode",
                "名字触发策略",
                "smart",
                "smart 让 AI 判断是否在跟肥鱼说话；force 直接回复；off 不特殊处理。",
                choices=["smart", "force", "off"],
            ),
            field(
                "poke_reply",
                "实际戳一戳事件直接回复",
                True,
                "不参与概率判断；仅能处理适配器真实提供且目标是本机器人的事件。",
            ),
            field(
                "poke_reply_text",
                "戳一戳固定回复",
                "唔！谁在戳肥鱼呀？",
                "真实戳一戳可直接发送固定文字，不消耗模型 Token；留空则交给聊天模型。仍遵守硬限频。",
            ),
            field(
                "confidence_threshold",
                "非直接回复置信度",
                0.62,
                "模型返回 reply=true 且置信度达到此阈值。",
                low=0,
                high=1,
            ),
            field(
                "decision_messages",
                "决策上下文消息数",
                20,
                "保留近期多人对话；避免每次把全部长历史交给判断模型。",
                low=5,
                high=60,
            ),
            field(
                "decision_chars",
                "决策字符预算",
                3300,
                "只给判断模型最近消息和参与者线索。",
                low=600,
                high=12000,
            ),
            field(
                "timeout_seconds",
                "判断超时（秒）",
                15,
                "普通判断失败时保持安静，不影响直接请求。",
                low=3,
                high=60,
            ),
            field(
                "merge_wait_seconds",
                "连续消息静默等待（秒）",
                1.5,
                "短时间连续消息合并处理。",
                low=0,
                high=10,
            ),
            field(
                "merge_max_wait_seconds",
                "最大合并等待（秒）",
                5.0,
                "群消息持续不断时不无限等待。",
                low=0.1,
                high=20,
            ),
            field(
                "max_batch_messages",
                "单批消息上限",
                12,
                "每次决策所涉及的新消息数。",
                low=1,
                high=40,
            ),
            field(
                "max_pending_messages",
                "每群等待队列上限",
                80,
                "优先保留明确请求，溢出普通消息只存上下文。",
                low=12,
                high=400,
            ),
            field(
                "continuation_seconds",
                "续聊窗口（秒）",
                105,
                "最近与肥鱼交谈的用户提高进入判断优先级。",
                low=0,
                high=600,
            ),
            field(
                "unsolicited_window_seconds",
                "普通插话统计窗口（秒）",
                300,
                "滚动窗口限制群聊非直接回复密度。",
                low=30,
                high=3600,
            ),
            field(
                "max_unsolicited_replies",
                "窗口内最大普通回复数",
                3,
                "不含直接 @ 或私聊；0 表示禁止非直接插话。",
                low=0,
                high=100,
            ),
            field(
                "unsolicited_gap_seconds",
                "普通插话最短间隔（秒）",
                12,
                "不影响明确请求。",
                low=0,
                high=300,
            ),
            field(
                "max_replies_per_minute",
                "每会话每分钟回复上限",
                12,
                "硬限频，包含 @ 和戳一戳，避免刷屏。",
                low=1,
                high=60,
            ),
            field(
                "stale_seconds",
                "普通旧话题最长等待（秒）",
                45,
                "模型完成时普通话题已经过去则放弃发送；直接请求不因此放弃。",
                low=10,
                high=180,
            ),
            field(
                "extra_prompt",
                "读空气补充规则",
                "两人明显在互相聊天时不要硬插话；相关话题、针对你的问题或自然接续时才回复。不要机械回复每条消息。",
                "额外判断原则。",
                widget="textarea",
            ),
        ],
    },
    {
        "key": "reply",
        "label": "回复与图片",
        "fields": [
            field(
                "split_enabled",
                "启用分段发送",
                True,
                "关闭时每轮只提交一条消息。",
            ),
            field(
                "split_on_newline", "按模型换行发送", True,
                "聊天正文中的换行可开始下一条消息。榜单、表格和代码保留排版。",
            ),
            field(
                "split_chars", "长段兜底长度", DEFAULT_SPLIT_CHARS,
                "同一段超过目标长度时优先在句末拆开。最多段数和完整代码块可使单段超过目标。",
                low=40, high=2000,
            ),
            field(
                "max_parts",
                "最多发送段数",
                5,
                "换行较多时合并相邻段落，长回复尽量均摊。设为 1 时整条发送。",
                low=1,
                high=8,
            ),
            field(
                "gap_min", "段间最短延迟（秒）", 1.5, "随机延迟下限。", low=0, high=10
            ),
            field(
                "gap_max", "段间最长延迟（秒）", 3.5, "随机延迟上限。", low=0, high=10
            ),
            field(
                "max_reply_chars",
                "单轮回复字符上限",
                1800,
                "过长回复截到上限并提示，可按需再次问。",
                low=160,
                high=MAX_REPLY_CHARS,
            ),
            field(
                "max_tool_rounds",
                "最多工具轮次",
                3,
                "达到上限后进行无工具最终总结。协议异常时每轮聊天最多额外恢复一次模型请求，仍受预算限制。",
                low=0,
                high=6,
            ),
            field(
                "max_tool_calls",
                "单轮最多工具调用",
                6,
                "包括记忆、知识库和网页搜索。",
                low=0,
                high=12,
            ),
            field(
                "tool_result_chars",
                "单工具返回字符上限",
                4200,
                "超过预算时提示已裁剪。",
                low=500,
                high=18000,
            ),
            field(
                "image_enabled",
                "启用图片理解",
                True,
                "关闭后停止读取图片和使用旧描述。需要选择可接收图像的 Provider。",
            ),
            field(
                "vision_strategy", "图片理解时机", "economy",
                "economy：决定回复后识图。balanced：正式模式下通过本地判断且文字不足时先识图。full：正式模式下对近期相关图片限量主动识别。",
                choices=["economy", "balanced", "full"],
            ),
            field(
                "vision_observe_daily_calls", "完整模式每日主动识图次数", 6,
                "所有授权会话合计，单独计数并共用视觉与总预算。设为 0 停止主动识别。",
                low=0, high=100,
            ),
            field(
                "vision_observe_interval_seconds", "同一会话主动识图最小间隔（秒）", 60,
                "仅完整模式使用。每日调用额度跨重启保留。",
                low=15, high=3600,
            ),
            field(
                "images_per_reply",
                "每轮最多图片",
                3,
                "同一批的多图一次处理或复用缓存。",
                low=1,
                high=6,
            ),
            field(
                "image_lookback_seconds",
                "图片关联窗口（秒）",
                45,
                "解决先发图后发文字；同群、近期参与者图片才可能被取用。",
                low=0,
                high=180,
            ),
            field(
                "image_cache_entries",
                "图片描述缓存数量",
                500,
                "按图像内容哈希、作用域和模型分离，原图不写入记忆。",
                low=0,
                high=5000,
            ),
        ],
    },
    {
        "key": "memory",
        "label": "分层记忆",
        "fields": [
            field(
                "enabled",
                "启用记忆系统",
                True,
                "记忆只在当前群或当前私聊、当前人格中使用。",
            ),
            field(
                "batch_messages",
                "记忆学习批量",
                40,
                "每累计这么多新消息进行一次语义提取。",
                low=8,
                high=100,
            ),
            field(
                "batch_chars",
                "每批记忆原文字符上限",
                13000,
                "避免过长批次，未处理的下一批继续。",
                low=2000,
                high=40000,
            ),
            field(
                "flush_minutes",
                "不足一批的最迟整理时间（分钟）",
                30,
                "低活跃群也有机会形成记忆。",
                low=5,
                high=720,
            ),
            field(
                "explicit_immediate",
                "明确记住请求优先",
                True,
                "当本人明确要求记住时，优先整理该消息（仍受证据与预算约束）。",
            ),
            field(
                "core_blur_after_days",
                "核心事实精度模糊天数",
                0,
                "0=永远保留精确事实；非0时较久未提及的非置顶核心记忆只显示 gist，深层保留精确值。",
                low=0,
                high=3650,
            ),
            field(
                "important_threshold",
                "重要经历保护阈值",
                70,
                "达到此重要度的经历至少保留事件要旨，不会自然消失。",
                low=40,
                high=100,
            ),
            field(
                "experience_clear_days",
                "经历清楚阶段天数",
                30,
                "从最后真实回忆/记录起计；超过开始使用简要语义。",
                low=1,
                high=3650,
            ),
            field(
                "experience_fuzzy_days",
                "经历模糊阶段天数",
                90,
                "保留事件核心要旨。",
                low=2,
                high=3650,
            ),
            field(
                "experience_archive_days",
                "普通经历归档天数",
                365,
                "非重要经历进入深层存档、不自动召回。",
                low=3,
                high=3650,
            ),
            field(
                "ordinary_days",
                "普通琐事最长活跃天数",
                3,
                "默认不保留无长期价值的琐事；已入库的 ordinary 到期归档或清理。",
                low=1,
                high=30,
            ),
            field(
                "ordinary_archive_days",
                "琐事深层保留天数",
                7,
                "0=归档时立即移除琐事；其余到期清理。核心事实/重要经历不受影响。",
                low=0,
                high=90,
            ),
            field(
                "default_task_days",
                "无明确期限临时事项默认天数",
                2,
                "到期标记已失效，不继续当作当前计划。",
                low=1,
                high=30,
            ),
            field(
                "auto_recall_count",
                "自动相关记忆条数",
                3,
                "仅注入有相关性且可见的简短活跃记忆；0=全部按需回忆。",
                low=0,
                high=12,
            ),
            field(
                "auto_recall_chars",
                "自动记忆字符预算",
                800,
                "不机械加载整套记忆卡。",
                low=100,
                high=6000,
            ),
            field(
                "recall_top_k",
                "主动召回默认条数",
                6,
                "工具可按人物、关键词、时间和深层模式筛选。",
                low=1,
                high=20,
            ),
            field(
                "minimum_confidence",
                "自动保存置信度下限",
                0.72,
                "无法核实的传闻、玩笑、机器人自言不得成为个人事实。",
                low=0.4,
                high=1,
            ),
            field(
                "semantic_similarity",
                "语义召回最低相似度",
                0.60,
                "仅配置 embedding 模型时使用。",
                low=0.2,
                high=0.99,
            ),
            field(
                "capture_private",
                "私聊记忆学习",
                True,
                "关闭后私聊只维持短期上下文，不提取持久记忆。",
            ),
        ],
    },
    {
        "key": "learning",
        "label": "表达、黑话与关系",
        "fields": [
            field(
                "enabled",
                "启用语言学习",
                True,
                "从群聊批量学习可重用语言模式，不直接复制人格。",
            ),
            field(
                "batch_messages",
                "表达学习批量",
                100,
                "每个群独立采样；不重复消耗已成功处理的消息。",
                low=20,
                high=300,
            ),
            field(
                "flush_minutes",
                "表达学习最长等待（分钟）",
                360,
                "低活跃群按该时间处理，至少有一定消息量。",
                low=30,
                high=1440,
            ),
            field(
                "auto_approve",
                "自动批准高置信度模式",
                False,
                "默认必须在管理页审核；批准结果才可影响回复。",
            ),
            field(
                "max_injected",
                "最多注入表达模式",
                6,
                "优先当前话题相关的已批准表达和黑话。",
                low=0,
                high=15,
            ),
            field(
                "inject_chars",
                "表达注入字符预算",
                650,
                "同时限制相关黑话和表达示例。",
                low=100,
                high=3000,
            ),
            field(
                "affinity_enabled",
                "启用基础好感度",
                True,
                "按实际友好互动小幅调整，不推断私人性格或敏感信息；可手动校正。",
            ),
        ],
    },
    {
        "key": "tools",
        "label": "知识库与网页工具",
        "fields": [
            field(
                "contextual_tools", "按聊天场景提供工具", True,
                "按本轮问题及近期话题提供相关工具。模型可在正常工具预算内申请展开其他已授权工具；关闭则提供完整工具集。不增加固定判断调用。",
            ),
            field(
                "kb_enabled",
                "启用文档检索工具",
                True,
                "必须配置允许使用的知识库；不进行每条消息的自动 RAG。",
            ),
            field(
                "kb_bindings",
                "知识库工具映射",
                [],
                "每项 {alias,name,description,allowed_groups,allow_private}。name 为 AstrBot 现有库名称/ID，alias 为模型可见别名。群白名单为空时不公开。",
                widget="kb_bindings",
            ),
            field(
                "kb_top_k",
                "知识库每次检索片段数",
                5,
                "仅向选定知识库发起检索。",
                low=1,
                high=12,
            ),
            field(
                "web_enabled",
                "复用 AstrBot 原生网页搜索",
                True,
                "在 AstrBot 当前配置文件开启、配置搜索服务，由肥鱼按需调用。不会接入系统 Shell、任意 URL 抓取或其他高权限工具。",
            ),
            field(
                "web_cache_minutes",
                "公开搜索缓存（分钟）",
                15,
                "同作用域同关键词短时复用。0=关闭缓存。",
                low=0,
                high=120,
            ),
        ],
    },
    {
        "key": "dota",
        "label": "Dota 2 数据工具",
        "fields": [
            field(
                "enabled",
                "启用 Dota 2",
                True,
                "查询英雄、技能、装备、胜率排名、对抗关系与历史变化。",
            ),
            field(
                "auto_sync",
                "每天自动同步 Dota 统计（OpenDota / Valve）",
                True,
                "正式运行后，在设定时间检查三个独立来源；今日缺失的来源自动同步，失败时保留已校验数据并间隔重试。不消耗模型 Token。",
            ),
            field("sync_hour", "北京时间同步小时", 3, "每天到点检查。", low=0, high=23),
            field(
                "existing_snapshots_dir",
                "旧 Valve 对抗 JSON 快照目录",
                "",
                "选填：之前 dota-stats-sync-data/snapshots 的绝对路径；只读复用对抗数据，不能作为英雄总体胜率，不修改原文件。",
            ),
            field(
                "existing_catalog_md",
                "旧英雄装备 Markdown 路径",
                "",
                "选填：之前的 Dota2-Heroes.md 文件或所在目录的绝对路径，网络失败时可用作本地补充。",
            ),
            field(
                "default_rank",
                "默认 Valve 对抗 rank 编号",
                0,
                "仅用于对抗/同队统计。英雄自身胜率默认 OpenDota 公开比赛总体样本，与本项无关。Valve rank 段位映射未核实。",
                low=0,
                high=6,
            ),
            field(
                "rank_labels",
                "已核实的 Valve 对抗段位映射",
                {},
                '仅对抗 rank 可以填写 {"0":"已核实标签"}；不自动映射 OpenDota 段位或 Valve rank_chunk。',
                widget="json",
            ),
            field(
                "snapshot_retention_days",
                "插件内部统计快照保留天数",
                180,
                "旧脚本目录不清理；历史趋势至少需要保留 30 天。",
                low=30,
                high=730,
            ),
            field(
                "detail_ttl_hours",
                "英雄/物品详情缓存小时",
                24,
                "本地缓存按官方补丁及目录时间刷新。",
                low=1,
                high=168,
            ),
            field(
                "knowledge_auto_sync", "自动构建版本化英雄与装备资料", True,
                "正式模式下分批同步 Valve 官方原始详情，英雄与装备分别验收；断点续传，保留已发布补丁，不消耗模型 Token。",
            ),
            field(
                "knowledge_batch_size", "每批官方详情同步数量", 24,
                "先英雄后装备，每批结束核对补丁；强制手动同步也遵守批量，失败时自动退避。", low=4, high=60,
            ),
            field(
                "skill_aliases", "自定义技能别名", {},
                '按稳定技能内部代码维护，如 {"antimage_mana_break":["烧蓝","削蓝"]}。用于对应英雄内的检索；不覆盖其他同名技能。',
                widget="json",
            ),
            field(
                "skill_slots", "人工核实的技能按键", {},
                '选填特殊英雄的按键映射，如 {"npc_dota_hero_antimage":{"Q":"antimage_mana_break"}}。仅当前详情中存在的代码生效；多形态英雄建议直接使用技能名。',
                widget="json",
            ),
            field(
                "patchnotes_json", "本地版本公告 JSON（可选）", "",
                "Valve 版本公告接口不可用时，只读导入原版公告 JSON 或 OpenDota/dotaconstants patchnotes.json；缺少指定版本时不猜测。",
            ),
            field(
                "network_timeout",
                "Dota 数据单次请求超时（秒）",
                18,
                "OpenDota 与 Valve 分别校验和缓存，单个来源失败不覆盖其旧数据，也不影响其他来源。",
                low=5,
                high=45,
            ),
        ],
    },
    {
        "key": "social",
        "label": "独立人格与表达",
        "fields": [
            field("enabled", "启用场景化表达", True, "自然聊天行为不覆盖事实判断、权限及管理员设置的人格核心。"),
            field("relationships_enabled", "区分熟悉程度与近期互动", True, "以真实互动次数为依据，和已有好感度分别维护。"),
            field("energy_enabled", "轻量社交精力", True, "随时间恢复，连续参与后下降；只影响普通闲聊，不影响认真请求。"),
            field("interests", "人格关注话题", ["Dota 2", "刀塔", "CS2", "游戏", "白饭"], "当前话题兴趣，不是不断累积的情绪分数。", widget="lines"),
            field("examples_count", "场景示例上限", 3, "合计注入上限；不固定复读 AstrBot 人格的全部示例。0=不注入对话示例。", low=0, high=6),
            field("examples_chars", "场景示例总字符预算", 700, "包括场景例子和 AstrBot 人格示例。", low=100, high=2400),
            field("examples", "自定义场景示例", [], "JSON 数组：每项 scene、user、assistant；只参考表达，不作为事实或指令。", widget="json"),
            field(
                "style_prompt", "表达风格规范",
                "像现代群友发消息。闲聊允许半句话、语气词、省标点、换行、附和和自然跑题；不用每句都总结、反问或强行推进话题。有偏好有分歧，可以直接说不知道。避免「这话我记下了」「这笔账我记着」「我心里有数」「我认」「睡吧，我守着」「你继续说，我听着」之类空洞的装酷套话、翻译腔、网文动作旁白或假装有现实经历。说之前想想真实群友会不会这么发。认真求助、事实与数字仍需准确清楚，正常引用不受文风约束。",
                "可编辑的文风规则；不替代 AstrBot 基础人格，也不覆盖明确问题、事实、隐私及权限。",
                widget="textarea",
            ),
            field("negative_examples_count", "场景反例上限", 1, "反例作为明确标注的非推荐表达，不作为助手历史消息；与正例共享字符预算。0=不注入反例。", low=0, high=3),
            field(
                "negative_examples", "自定义风格反例",
                [
                    {"scene":"casual","user":"我先去睡了","avoid":"睡吧，我守着。","prefer":"晚安啊"},
                    {"scene":"banter","user":"哈哈又翻车了","avoid":"这笔账我记着。","prefer":"又来？？"},
                    {"scene":"support","user":"这几天好累","avoid":"你继续说，我听着。","prefer":"这几天忙什么呢"},
                    {"scene":"question","user":"你怎么看这个说法","avoid":"你这话是在问别人，还是在问自己？","prefer":"我倒不太认同"},
                    {"scene":"correction","user":"我刚才说错了","avoid":"这话我记下了。","prefer":"哦哦 原来是这样"},
                ],
                "JSON 数组：scene、user、avoid、prefer。编辑正反例可直接改善鱼八股；只按当前场景提供少量示例。",
                widget="json",
            ),
            field("casual_silence", "允许普通闲聊自然不接话", True, "仅普通群聊、无明确求助、无工具查询时允许最终模型选择不发送；私聊、点名、@、继续对话和明确记忆请求不适用。"),
            field("phrase_control", "口头禅冷却", True, "依据最近实际发出的消息降低重复表达，不改变事实和引用。"),
            field("phrases", "容易复读的表达", ["确实", "笑死", "啊这", "我记下了", "这波", "绷不住了"], "仅统计这些短语而非所有中文词汇。", widget="lines"),
            field("phrase_window", "检查近期发送段数", 20, "只检查本会话内真实发送记录。", low=5, high=100),
            field("phrase_limit", "触发冷却次数", 2, "窗口内达到此次数，提示换一种自然表达。", low=2, high=10),
            field("phrase_cooldown_minutes", "复读检查时间窗（分钟）", 30, "过期表达不参与本次冷却。", low=5, high=1440),
            field("light_filter", "闲聊轻量风格清理", True, "只去掉明显模板式开头/结尾；认真问题、代码、引用不处理，不进行模型重写。"),
        ],
    },
    {
        "key": "habits",
        "label": "习惯记忆",
        "fields": [
            field("enabled", "启用自愿习惯归纳", True, "仅记录同意此项的群友主动分享的兴趣和粗略生活印象；每人可独立开启或撤回。"),
            field("min_days", "形成印象的不同日期数", 3, "同一天反复提及只计一次。", low=3, high=15),
            field("window_days", "观察归纳时间窗", 45, "过久观察不参与新习惯推断，不长期保存每日轨迹。", low=14, high=90),
            field("stale_days", "未获新证据的有效期（天）", 30, "超过后归为历史印象，召回不会自动延长有效期。", low=7, high=90),
            field("max_per_batch", "每批最多习惯候选", 8, "复用原记忆模型批次，不新增固定模型请求。", low=1, high=20),
        ],
    },
    {
        "key": "research",
        "label": "自主学习",
        "fields": [
            field("enabled", "启用自主学习", False, "正式运行且拥有允许的研究会话时，后台自行选择公开游戏主题；不主动定时发群。"),
            field("session_umo", "研究所属会话 UMO", "", "选填已观察到的授权群聊 UMO，用于选择人格和已有 Provider；空值取最近符合条件的群聊。"),
            field("daily_topics", "每日自主主题数", 1, "0=暂停。以北京时间计；同一任务的重试/备用选题不另占次数，最终失败仍记录一次尝试。已知无可用来源时不发起选题。", low=0, high=10),
            field("start_hour", "每日最早查阅时间（北京时间）", 12, "后台低优先级；聊天繁忙时让行。", low=0, high=23),
            field("interval_minutes", "主题间隔（分钟）", 60, "多主题也按间隔执行，避免集中消耗。", low=15, high=720),
            field("daily_tokens", "自主学习每日 Token 预算", 10000, "本地预估预约与供应商返回用量联合控制；仍受全局预算约束。", low=1000, high=200000),
            field("data_requests", "每次学习任务公开请求上限", 3, "同一任务中公告切换、网络重试、备用选题、比赛详情合计；只访问内置固定接口，不访问模型提供的 URL。", low=1, high=3),
            field("news_transport", "官方公告数据来源", "rss_first",
                  "rss_first=优先 Steam 官方 RSS，失败再试新闻 API；api_first=相反顺序；rss_only / api_only=仅对应接口。",
                  choices=["rss_first","api_first","rss_only","api_only"]),
            field("network_timeout", "学习数据单次请求超时（秒）", 30,
                  "独立于 Dota 统计网络超时，只影响自主学习公开接口及手动检测。", low=5, high=90),
            field("model_timeout_seconds", "自主学习单次模型超时（秒）", 90,
                  "选题和整理分别使用本超时；模型超时不自动重复付费请求，不影响聊天模型时限。", low=10, high=180),
            field("network_retries", "同一数据接口额外重试次数", 1,
                  "仅临时网络故障可重试，优先可用的备用接口；重试仍计入公开请求上限。", low=0, high=1),
            field("source_cooldown_minutes", "失败来源冷却（分钟）", 60,
                  "暂时排除失败的共享接口，状态跨重启保留；到期才自动再尝试。手动检测可验证网络恢复。", low=5, high=1440),
            field("source_failover", "资料失败时尝试备用主题", True,
                  "同主题接口切换失败后，最多切换到一个其他已授权来源及对应选题；使用原选题模型给出的备选，必要时再选一次。不增加固定调用。"),
            field("sources", "允许的自主学习来源", ["dota_news", "dota_pro", "dota_meta", "cs2_news"], "可选 dota_news / dota_pro / dota_meta / cs2_news。留空不执行。", widget="lines"),
            field("interests", "自主研究兴趣", ["Dota 2 职业比赛、英雄评价与版本", "CS2 版本及电竞动态"], "由人格自行选择具体问题；不设每天必须完成的主题。", widget="lines"),
            field("dedupe_days", "相同主题/证据冷却天数", 7, "同一段时间重复的主题或数据不再整理。", low=1, high=30),
            field("note_days", "知识笔记有效天数", 30, "过期内容保留可查，但不自动当成当前版本事实。", low=1, high=180),
            field("recall_count", "相关见闻自动注入上限", 2, "只在相关话题注入，独立于人物记忆。0=关闭自动注入。", low=0, high=5),
            field("recall_chars", "见闻注入字符预算", 900, "包含来源和日期；不发送阅读原文。", low=200, high=3000),
        ],
    },
    {
        "key": "relay",
        "label": "跨会话消息",
        "fields": [
            field("enabled", "允许受控私聊转达", False, "明确授权的私聊用户可以要求向指定群发送文字。"),
            field("allowed_users", "有转达权限的私聊用户 ID", [], "与聊天人格和管理员身份无关，且须在私聊白名单中。", widget="lines"),
            field("group_aliases", "群聊别名与完整 UMO", {}, '例如 {"刀塔群":"bot_id:GroupMessage:group_openid"}，目标必须是同平台实例、同人格、已观察并在白名单内的群。', widget="json"),
            field("daily_limit", "每日转达任务上限（每个授权私聊）", 5, "0=暂停；提交失败也计入，不自动重发。", low=0, high=30),
            field("max_chars", "单条转达字符上限", 600, "仅普通文字，不解释 CQ/At 标记。", low=20, high=1500),
            field("confirmation_minutes", "改写草稿确认有效期", 5, "明确要求原文时直接发送；需要组织语言时须本人确认最终正文。", low=1, high=30),
        ],
    },
    {
        "key": "interrupt",
        "label": "轻量打断",
        "fields": [
            field("enabled", "启用本地打断", True, "明确停止或当前提问者明确补充时取消未发送部分，不增加专用模型判断。"),
            field("supplement_enabled", "接收明确补充与纠正", True, "每轮至多一次重新组织；无关新消息不打断，已发送段落不撤回。"),
        ],
    },
    {
        "key": "budget",
        "label": "Token 与预算",
        "fields": [
            field(
                "daily_tokens",
                "每日 Token 预算（0=不限）",
                0,
                "输入、缓存输入和输出合计；缺失用量时使用估算，非结算金额。",
                low=0,
                high=100000000,
            ),
            field(
                "soft_ratio",
                "后台任务暂停比例",
                0.8,
                "达到此比例先暂停语言学习、非紧急整理，聊天保留至硬预算。",
                low=0.3,
                high=0.99,
            ),
            field(
                "module_call_limits",
                "每日各模块调用上限",
                {
                    "decision": 0,
                    "reply": 0,
                    "memory": 0,
                    "style": 0,
                    "vision": 0,
                    "embedding": 0,
                    "research": 0,
                },
                "0=不限制。预算按模块统计，模型重试/多轮工具均计入插件发起的请求。",
                widget="json",
            ),
            field(
                "pricing",
                "每百万 Token 价格（估算）",
                {},
                '可填 {"provider_id":{"input":0.0,"cached":0.0,"output":0.0}}，单位 CNY；不内置可能过期的报价。',
                widget="json",
            ),
            field(
                "balance_enabled",
                "启用 DeepSeek 余额监测",
                False,
                "仅使用直接 DeepSeek 官方 Provider 的已有 Key；不将第三方服务 Key 发给 DeepSeek。",
            ),
            field(
                "balance_provider",
                "DeepSeek API Provider ID",
                "",
                "选择官方 API Base URL 为 https://api.deepseek.com（或 /v1）的模型。",
                widget="chat_provider",
            ),
            field(
                "balance_threshold",
                "低余额提醒阈值",
                5.0,
                "指定币种金额低于阈值时提醒。",
                low=0,
                high=100000,
            ),
            field(
                "balance_currency",
                "余额币种",
                "CNY",
                "使用官方返回的指定币种，不自行换汇。",
                choices=["CNY", "USD"],
            ),
            field(
                "balance_interval_minutes",
                "余额检查间隔（分钟）",
                30,
                "固定 GET 检查，不需要 LLM Token。",
                low=5,
                high=1440,
            ),
            field(
                "balance_target_umo",
                "余额提醒发送目标 UMO",
                "",
                "只能是已观察到且当前允许的群/私聊 UMO；空值只记录状态。",
            ),
            field(
                "balance_mention_id",
                "余额提醒 @ 用户 ID",
                "",
                "选填已在目标群中出现的 OpenID；私聊忽略。",
            ),
            field(
                "balance_remind_hours",
                "低余额重复提醒间隔（小时）",
                12,
                "成功发送后持久化节流。",
                low=1,
                high=168,
            ),
        ],
    },
    {
        "key": "logs",
        "label": "日志与运维",
        "fields": [
            field(
                "level",
                "日志等级",
                "INFO",
                "独立日志记录决策原因、用量和异常类别，不包含 API Key 或完整提示词。",
                choices=["DEBUG", "INFO", "WARNING", "ERROR"],
            ),
            field(
                "retention_days",
                "日志保留天数",
                15,
                "每天清理过期的本插件日志文件，不动记忆/配置/AstrBot 日志。",
                low=1,
                high=90,
            ),
            field(
                "max_file_mb",
                "单日日志文件最大 MB",
                8,
                "超限轮转为同一天的追加分片。",
                low=1,
                high=50,
            ),
        ],
    },
]


def defaults() -> dict:
    return {
        g["key"]: {f["key"]: copy.deepcopy(f["default"]) for f in g["fields"]}
        for g in SCHEMA
    }


def bounded_strings(value, path, max_items=300, max_len=4000):
    if (
        not isinstance(value, list)
        or len(value) > max_items
        or any(
            not isinstance(v, str) or len(v) > max_len or not v.strip() for v in value
        )
    ):
        raise ConfigError(f"{path}：需要非空字符串数组（最多 {max_items} 项）")
    return list(dict.fromkeys(v.strip() for v in value))


def validate(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError("配置根必须是 JSON 对象")
    cfg = defaults()
    unknown = set(raw) - set(cfg)
    if unknown:
        raise ConfigError("未知配置模块: " + ", ".join(sorted(unknown)))
    for g in SCHEMA:
        section = raw.get(g["key"], {})
        if not isinstance(section, dict):
            raise ConfigError(f"{g['key']} 必须是对象")
        bad_keys = set(section) - {f["key"] for f in g["fields"]}
        if bad_keys:
            raise ConfigError(f"{g['key']} 未知字段: {', '.join(sorted(bad_keys))}")
        for f in g["fields"]:
            k = f["key"]
            if k not in section:
                continue
            v = section[k]
            path = f"{g['key']}.{k}"
            types = {
                "boolean": (bool,),
                "integer": (int,),
                "number": (int, float),
                "string": (str,),
                "array": (list,),
                "object": (dict,),
            }
            if type(v) not in types[f["type"]]:
                raise ConfigError(f"{path} 类型错误（需要 {f['type']}）")
            if "min" in f and not f["min"] <= v <= f["max"]:
                raise ConfigError(f"{path} 范围 {f['min']}～{f['max']}")
            if "choices" in f and v not in f["choices"]:
                raise ConfigError(f"{path} 必须为 {f['choices']} 之一")
            if isinstance(v, str) and len(v) > 16000:
                raise ConfigError(f"{path} 内容过长")
            if f.get("widget") == "lines":
                v = bounded_strings(v, path)
            cfg[g["key"]][k] = copy.deepcopy(v)
    a, d, r, m, b = [
        cfg[k] for k in ("access", "decision", "reply", "memory", "budget")
    ]
    legacy_reply = raw.get("reply", {})
    if "split_on_newline" not in legacy_reply and legacy_reply.get("split_chars") == 150:
        r["split_chars"] = DEFAULT_SPLIT_CHARS
    if any(
        x not in {"qq_official", "qq_official_webhook", "aiocqhttp"}
        for x in a["allowed_platforms"]
    ):
        raise ConfigError("access.allowed_platforms 包含不支持的平台")
    if (
        d["max_pending_messages"] < d["max_batch_messages"]
        or d["merge_max_wait_seconds"] < d["merge_wait_seconds"]
    ):
        raise ConfigError("消息队列和最大等待必须不小于单批数量及静默等待")
    if r["gap_min"] > r["gap_max"]:
        raise ConfigError("段间最短延迟不能超过最长延迟")
    if (
        not m["experience_clear_days"]
        < m["experience_fuzzy_days"]
        < m["experience_archive_days"]
    ):
        raise ConfigError("经历记忆天数须依次递增")
    if m["ordinary_archive_days"] and m["ordinary_archive_days"] < m["ordinary_days"]:
        raise ConfigError("琐事存档天数应不小于活跃天数，或者为0")
    social, habits, research, relay = (cfg[k] for k in ("social", "habits", "research", "relay"))
    for key in ("interests", "phrases"):
        social[key] = bounded_strings(social[key], "social." + key, max_items=50, max_len=80)
    research["interests"] = bounded_strings(research["interests"], "research.interests", max_items=20, max_len=120)
    if set(research["sources"]) - {"dota_news", "dota_pro", "dota_meta", "cs2_news"}:
        raise ConfigError("research.sources 存在未支持的数据来源")
    if habits["min_days"] > habits["window_days"] or habits["stale_days"] > habits["window_days"]:
        raise ConfigError("习惯日期阈值与有效期不能超过观察时间窗")
    if len(social["examples"]) > 80 or any(
        not isinstance(x, dict) or set(x) != {"scene", "user", "assistant"}
        or x["scene"] not in ("casual", "banter", "support", "question", "technical", "game", "correction", "coordination")
        or any(not isinstance(x[k], str) or not x[k].strip() or len(x[k]) > 350 for k in ("user", "assistant"))
        for x in social["examples"]
    ):
        raise ConfigError("social.examples 需要有效场景及简短 user/assistant 文本，最多80项")
    if len(social["style_prompt"]) > 1500:
        raise ConfigError("social.style_prompt 最多1500字符")
    if len(social["negative_examples"]) > 80 or any(
        not isinstance(x, dict) or set(x) != {"scene", "user", "avoid", "prefer"}
        or x["scene"] not in ("casual", "banter", "support", "question", "technical", "game", "correction", "coordination")
        or any(not isinstance(x[k], str) or not x[k].strip() or len(x[k]) > 250 for k in ("user", "avoid", "prefer"))
        for x in social["negative_examples"]
    ):
        raise ConfigError("social.negative_examples 需要有效 scene/user/avoid/prefer，最多80项")
    dota = cfg["dota"]
    if len(dota["skill_aliases"]) > 1600 or any(
        not isinstance(code,str) or not re.fullmatch(r"[a-zA-Z0-9_]{1,100}", code)
        for code in dota["skill_aliases"]
    ):
        raise ConfigError("dota.skill_aliases 需要技能内部代码，最多1600项")
    dota["skill_aliases"] = {
        code: bounded_strings(names, "dota.skill_aliases." + code, max_items=20, max_len=80)
        for code, names in dota["skill_aliases"].items()
    }
    if len(dota["skill_slots"]) > 200 or any(
        not isinstance(code,str) or not re.fullmatch(r"(?:npc_dota_hero_)?[a-zA-Z0-9_]{1,80}", code)
        or not isinstance(mapping, dict) or not mapping or set(mapping) - {"Q", "W", "E", "R", "D", "F"}
        or any(not isinstance(v, str) or not re.fullmatch(r"[a-zA-Z0-9_]{1,100}", v) for v in mapping.values())
        for code, mapping in dota["skill_slots"].items()
    ):
        raise ConfigError("dota.skill_slots 需要英雄内部代码及 Q/W/E/R/D/F 到技能代码的映射")
    if len(relay["group_aliases"]) > 30:
        raise ConfigError("跨会话群别名最多30项")
    normalized_aliases = {}
    for alias, umo in relay["group_aliases"].items():
        if not isinstance(alias,str) or not re.fullmatch(r"[\w\-·]{1,40}", alias) or alias.casefold() in normalized_aliases:
            raise ConfigError("群别名须唯一且为短名称")
        if not isinstance(umo, str) or not re.fullmatch(r"[^:\s]{1,100}:GroupMessage:[^:\s]{1,200}", umo):
            raise ConfigError("群别名必须映射至完整 GroupMessage UMO")
        normalized_aliases[alias.casefold()] = umo
    bindings = cfg["tools"]["kb_bindings"]
    if len(bindings) > 24:
        raise ConfigError("最多 24 个知识库映射")
    aliases = set()
    normalized_bindings = []
    for x in bindings:
        if not isinstance(x, dict) or set(x) - {
            "alias",
            "name",
            "description",
            "allowed_groups",
            "allow_private",
        }:
            raise ConfigError("知识库映射必须是指定字段的对象")
        alias = x.get("alias", "")
        if (
            not isinstance(alias, str)
            or not re.fullmatch(r"[\w-]{1,40}", alias)
            or alias in aliases
        ):
            raise ConfigError(
                "知识库 alias 必须唯一（字母、数字、汉字、下划线、连字符）"
            )
        aliases.add(alias)
        if (
            not isinstance(x.get("name"), str)
            or not x["name"].strip()
            or len(x["name"]) > 200
        ):
            raise ConfigError("知识库 name 不能为空")
        if (
            type(x.get("allow_private", False)) is not bool
            or not isinstance(x.get("description", ""), str)
            or len(x.get("description", "")) > 600
        ):
            raise ConfigError("知识库 description 或 allow_private 类型错误")
        groups = bounded_strings(
            x.get("allowed_groups", []), f"知识库 {alias} 群白名单"
        )
        normalized_bindings.append(
            dict(
                alias=alias,
                name=x["name"].strip(),
                description=x.get("description", ""),
                allowed_groups=groups,
                allow_private=x.get("allow_private", False),
            )
        )
    cfg["tools"]["kb_bindings"] = normalized_bindings
    allowed_modules = set(defaults()["budget"]["module_call_limits"])
    if set(b["module_call_limits"]) - allowed_modules or any(
        type(v) is not int or not 0 <= v <= 100000
        for v in b["module_call_limits"].values()
    ):
        raise ConfigError("module_call_limits 仅支持已列出的模块，值应为非负整数")
    b["module_call_limits"] = {
        x: b["module_call_limits"].get(x, 0) for x in sorted(allowed_modules)
    }
    if len(b["pricing"]) > 100:
        raise ConfigError("价格映射过多")
    for p, rates in b["pricing"].items():
        if (
            not p
            or not isinstance(rates, dict)
            or set(rates) != {"input", "cached", "output"}
            or any(
                type(x) not in (float, int) or not 0 <= x <= 100000
                for x in rates.values()
            )
        ):
            raise ConfigError("pricing 必须提供 input/cached/output 非负数字")
    for k, val in cfg["dota"]["rank_labels"].items():
        if (
            k not in [str(i) for i in range(7)]
            or not isinstance(val, str)
            or not val.strip()
            or len(val) > 60
        ):
            raise ConfigError("rank_labels 的键必须是 0..6，标签为短字符串")
    if len(dump(cfg)) > 200000:
        raise ConfigError("配置体积超限")
    return cfg


class ConfigStore:
    """Single authoritative configuration with revision checks and atomic saves."""

    def __init__(self, root: Path):
        self.path = root / "config.json"
        self.backup_dir = root / "config_backups"
        self.lock = threading.RLock()
        self.error = ""
        if self.path.exists():
            try:
                self.data = validate(read_json(self.path))
            except Exception:
                # Never overwrite a broken file at startup. Safe off mode.
                self.data = defaults()
                self.error = (
                    "原配置不可读取；插件已安全停用。请备份原文件后通过管理页重存。"
                )
        else:
            self.data = defaults()
            atomic_json(self.path, self.data)

    def snapshot(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.data)

    def revision(self) -> str:
        return hashlib.sha256(dump(self.data).encode()).hexdigest()[:20]

    def info(self) -> dict:
        with self.lock:
            return {
                "config": self.snapshot(),
                "revision": self.revision(),
                "schema": SCHEMA,
                "error": self.error,
                "backups": self.backups(),
            }

    def backups(self) -> list[str]:
        return [
            p.name
            for p in sorted(self.backup_dir.glob("config-*.json"), reverse=True)[:20]
        ]

    def save(self, raw: Any, expected: str) -> dict:
        with self.lock:
            if expected != self.revision():
                raise ConfigError("配置已被修改，请刷新后重新编辑")
            cfg = validate(raw)
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            tag = datetime.now(CN_TZ).strftime("%Y%m%d-%H%M%S-%f")
            if self.path.exists():
                # Preserve invalid prior bytes too, using an exact local copy.
                import shutil

                shutil.copy2(self.path, self.backup_dir / f"config-{tag}.json")
            atomic_json(self.path, cfg)
            self.data = cfg
            self.error = ""
            for p in sorted(self.backup_dir.glob("config-*.json"), reverse=True)[20:]:
                p.unlink(missing_ok=True)
            return self.info()

    def restore(self, backup: str, expected: str) -> dict:
        if backup not in self.backups():
            raise ConfigError("配置备份不存在")
        return self.save(read_json(self.backup_dir / backup), expected)
