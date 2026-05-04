"""ContextBuilder: 为 ReAct AgentLoop 构建 LLM 消息上下文."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.agent.memory import WorkspaceMemory
from src.agent.skills import SkillsLoader
from src.agent.tools import ToolRegistry

if TYPE_CHECKING:
    from src.memory.persistent import PersistentMemory

logger = logging.getLogger(__name__)

# 系统提示词模板，包含技能数量、工具数量、内存摘要等占位符
_SYSTEM_PROMPT = """你是一个金融研究代理，拥有 {skill_count} 个专业技能、{tool_count} 个工具、5 个数据源（带自动回退）和 29 个多代理 swarm 团队。
你负责回测、因子分析、期权定价、风险审计、研究报告、文档/网页阅读、网络搜索和团队工作流。

## 工具

{tool_descriptions}

## 技能（使用 load_skill 读取完整文档）

{skill_descriptions}

## 状态

{memory_summary}

## 任务路由

根据请求决定使用哪个工作流：

**回测** — 用户想要创建、测试或优化交易策略：
1. `load_skill("strategy-generate")` — 读取 SignalEngine 合约
2. `write_file("config.json", ...)` — 数据源、代码、日期、参数
3. `write_file("code/signal_engine.py", ...)` — SignalEngine 类
4. 语法检查 → `backtest(run_dir=...)` → `read_file("artifacts/metrics.csv")`
5. 不要写 run_backtest.py。引擎是内置的。

**Swarm 团队** — 仅当用户明确要求团队/委员会/swarm 分析时：
- 调用 `run_swarm(prompt="<用户完整请求>")` — 它会自动选择正确的预设。
- 除非用户特别要求基于团队或委员会的分析，否则不要使用 swarm。

**分析/研究** — 用户想要因子分析、期权定价、市场数据或一般研究：
- 先加载相关技能，然后使用匹配的工具（factor_analysis、options_pricing、bash 执行自定义脚本）。

**文档/网页** — 用户提供 PDF 或 URL：
- 使用 `read_document(path=...)` 读取 PDF，`read_url(url=...)` 读取网页。

**交易日志** — 用户上传 CSV/Excel 经纪商导出（交割单）或要求分析自己的交易历史：
1. `load_skill("trade-journal")` — 读取分析方法和报告模板
2. `analyze_trade_journal(file_path=..., analysis_type="full")` — 解析 + 分析 + 行为诊断
3. 以技能中的 markdown 报告形式呈现结果。提供后续选项：时间段分析、标的深入、市场拆分。
4. 如果用户问"现在怎么办/我能做得更好吗/如果我有纪律会怎样"，切换到下面的 **Shadow Account** 流程。

**Shadow Account** — 用户要求提取策略、"训练 shadow"、多市场回测自己的盈利模式，或问"我放弃了多少"：
1. **必须** `load_skill("shadow-account")` 作为第一个工具调用，在任何 shadow_* 工具之前 — 技能定义了规则、方法论、归属语义，是必需上下文
2. 确认日志已被解析（同一会话或已知 `journal_path`）。如果没有，先运行 `analyze_trade_journal`
3. `extract_shadow_strategy(journal_path=...)` → 显示规则，请用户确认是否符合他们的行为
4. `run_shadow_backtest(shadow_id=..., journal_path=...)` → 多市场指标 + delta 归属
5. `render_shadow_report(shadow_id=...)` → 分享 html/pdf 路径，以第 5 节 "你 vs shadow" delta 开头
6. 可选：应要求 `scan_shadow_signals(shadow_id=...)`（始终附上研究免责声明）
**切勿** 在未先在同一会话中加载 `shadow-account` 技能的情况下调用 `extract_shadow_strategy` / `run_shadow_backtest` / `render_shadow_report` / `scan_shadow_signals`。

## 指南

- 在开始任何任务之前加载相关技能。技能包含精确的 API 合约和示例。
- 如果关键信息缺失（资产、日期、策略类型），请询问用户。不要猜测。
- 以 markdown 表格形式输出结果。回测后始终报告：total_return、sharpe、max_drawdown、trade_count。
- 所有文件路径都相对于 run_dir（自动注入）。
- 使用用户使用的相同语言回复。
- 你有跨会话的持久记忆（`remember` 工具）。当用户分享偏好、策略见解或重要发现时，保存它们以供将来会话使用。
- 当工作流成功时，你可以创建可重用的技能（`save_skill`），并在 API 变化时修复它们（`patch_skill`）。
{memory_section}
## 当前日期和时间

今天是 {current_datetime}。
"""

# 持久化内存部分的模板
_MEMORY_SECTION = """
## 持久化内存（跨会话）

{snapshot}

"""


class ContextBuilder:
    """为 AgentLoop 构建消息上下文.

    属性:
        registry: 工具注册表.
        memory: 工作区内存.
        skills_loader: 技能加载器.
    """

    def __init__(self, registry: ToolRegistry, memory: WorkspaceMemory,
                 skills_loader: Optional[SkillsLoader] = None,
                 persistent_memory: Optional[PersistentMemory] = None) -> None:
        """初始化 ContextBuilder.

        Args:
            registry: 工具注册表.
            memory: 工作区内存.
            skills_loader: 技能加载器（未提供则自动创建）.
            persistent_memory: 跨会话记忆的 PersistentMemory 实例.
        """
        self.registry = registry
        self.memory = memory
        self.skills_loader = skills_loader or SkillsLoader()
        self._persistent_memory = persistent_memory

    def build_system_prompt(self, user_message: str = "") -> str:
        """构建系统提示词.

        通过 get_descriptions 注入单行技能摘要；完整文档在需要时通过 load_skill 加载.
        PersistentMemory 快照在会话开始时冻结（保留提示词缓存）.

        Args:
            user_message: 用户消息（为保持 API 兼容性而保留）.

        Returns:
            系统提示词文本.
        """
        now = datetime.now()

        # 仅在有保存的记忆时构建内存部分
        memory_section = ""
        if self._persistent_memory and self._persistent_memory.snapshot:
            memory_section = _MEMORY_SECTION.format(
                snapshot=self._persistent_memory.snapshot,
            )

        return _SYSTEM_PROMPT.format(
            tool_count=len(self.registry._tools),
            skill_count=len(self.skills_loader.skills),
            tool_descriptions=self._format_tool_descriptions(),
            skill_descriptions=self.skills_loader.get_descriptions(),
            memory_summary=self.memory.to_summary(),
            memory_section=memory_section,
            current_datetime=now.strftime("%A, %B %d, %Y %H:%M (local)"),
        )

    def build_messages(self, user_message: str, history: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        """构建完整的消息列表.

        自动召回相关的持久记忆并将其作为上下文注入用户消息.
        这保持了系统提示词的稳定（可缓存）同时提供每个查询相关的记忆.

        Args:
            user_message: 用户消息.
            history: 之前的对话消息.

        Returns:
            OpenAI 格式的消息列表.
        """
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.build_system_prompt(user_message)},
        ]
        if history:
            messages.extend(history)

        # 自动召回：将相关记忆注入用户消息
        enriched = user_message
        if self._persistent_memory:
            try:
                recalls = self._persistent_memory.find_relevant(user_message, max_results=3)
                if recalls:
                    lines = [f"- **{r.title}** ({r.memory_type}): {r.body[:500]}" for r in recalls]
                    recall_block = "\n".join(lines)
                    enriched = (
                        f"<recalled-memories>\n{recall_block}\n</recalled-memories>\n\n"
                        f"{user_message}"
                    )
            except Exception as exc:
                logger.debug("自动召回失败: %s", exc)

        messages.append({"role": "user", "content": enriched})
        return messages

    def _format_tool_descriptions(self) -> str:
        """格式化工具描述."""
        lines = []
        for tool in self.registry._tools.values():
            params = tool.parameters.get("properties", {})
            required = tool.parameters.get("required", [])
            param_parts = []
            for pname, pschema in params.items():
                req = " (必填)" if pname in required else ""
                param_parts.append(f"    - {pname}: {pschema.get('description', pschema.get('type', ''))}{req}")
            param_text = "\n".join(param_parts) if param_parts else "    (无参数)"
            lines.append(f"### {tool.name}\n{tool.description}\n  参数:\n{param_text}")
        return "\n\n".join(lines)

    @staticmethod
    def format_tool_result(tool_call_id: str, tool_name: str, result: str) -> Dict[str, Any]:
        """将工具执行结果格式化为消息."""
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": result,
        }

    @staticmethod
    def format_assistant_tool_calls(
        tool_calls: list,
        content: Optional[str] = None,
        reasoning_content: Optional[str] = None,
    ) -> Dict[str, Any]:
        """格式化助手 tool_calls 消息，保留思考文本.

        Args:
            tool_calls: 工具调用对象列表.
            content: 助手的最终文本（对于将思考作为内容流式传输的提供商可能包含内联思考）.
            reasoning_content: 提供商特定的思考字段（Kimi K2.5、DeepSeek reasoner、Qwen thinking）.
                仅在非 None 时附加到输出消息，因此非思考提供商不会有任何变化.

        Returns:
            OpenAI 格式的助手消息.
        """
        message = {
            "role": "assistant",
            "content": content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in tool_calls
            ],
        }
        if reasoning_content is not None:
            message["reasoning_content"] = reasoning_content
        return message
