"""AgentLoop: ReAct 核心循环

五层上下文管理：
  Layer 1 (microcompact)     — 每次迭代静默清除旧的工具结果
  Layer 2 (context_collapse) — 无需 LLM 调用即可折叠长文本块（零成本）
  Layer 3 (auto_compact)     — 带 token 预算尾部保护的 LLM 结构化摘要
  Layer 4 (compact tool)    — 模型显式调用 compact 工具触发 L3
  Layer 5 (iterative update) — 第 N 次压缩时更新之前的摘要，而非重新开始

工具执行：
  - 读/写批处理：连续只读工具通过线程并行运行
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import time as _time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.agent.context import ContextBuilder
from src.agent.memory import WorkspaceMemory
from src.agent.tools import ToolRegistry
from src.agent.trace import TraceWriter
from src.core.state import RunStateStore
from src.providers.chat import ChatLLM
from src.tools.background_tools import get_background_manager

RUNS_DIR = Path(__file__).resolve().parents[2] / "runs"
TOKEN_THRESHOLD = int(os.getenv("TOKEN_THRESHOLD", "40000"))
KEEP_RECENT = 3
TOOL_RESULT_LIMIT = 10_000

# Layer 2: Context collapse thresholds
COLLAPSE_THRESHOLD = int(TOKEN_THRESHOLD * 0.7)
COLLAPSE_PRESERVE_RECENT = 6
COLLAPSE_TEXT_MIN = 2400
COLLAPSE_HEAD = 900
COLLAPSE_TAIL = 500

# Layer 3: Token-budget tail protection
TAIL_TOKEN_BUDGET = 20_000

logger = logging.getLogger(__name__)


def estimate_tokens(messages: list) -> int:
    """Rough token count estimate (~4 chars/token).

    Args:
        messages: Message list.

    Returns:
        Estimated token count.
    """
    return len(json.dumps(messages, default=str, ensure_ascii=False)) // 4


def _microcompact(messages: list) -> None:
    """Layer 1: 静默清除旧的工具结果，只保留最近 N 条完整内容

    Args:
        messages: 消息列表（原地修改）
    """
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    if len(tool_msgs) <= KEEP_RECENT:
        return
    for msg in tool_msgs[:-KEEP_RECENT]:
        content = msg.get("content", "")
        if isinstance(content, str) and len(content) > 100:
            msg["content"] = "[cleared]"


def _context_collapse(messages: list) -> None:
    """Layer 2: 对旧消息中的长文本块进行折叠，无需 LLM 调用

    保留大段文本的头部和尾部，中间部分折叠。
    零 API 成本 — 纯字符串操作。

    Args:
        messages: 消息列表（原地修改）
    """
    if len(messages) <= COLLAPSE_PRESERVE_RECENT + 1:
        return
    for msg in messages[1:-COLLAPSE_PRESERVE_RECENT]:
        content = msg.get("content")
        if not isinstance(content, str) or len(content) <= COLLAPSE_TEXT_MIN:
            continue
        if content == "[cleared]":
            continue
        head = content[:COLLAPSE_HEAD]
        tail = content[-COLLAPSE_TAIL:]
        trimmed = len(content) - COLLAPSE_HEAD - COLLAPSE_TAIL
        msg["content"] = f"{head}\n\n...[{trimmed} chars collapsed]...\n\n{tail}"


def _fix_tool_pairs(messages: list) -> None:
    """压缩后修复孤立的 tool_call / tool_result 配对

    保持 tool_call 和 tool_result 配对完整，避免：

    模型看到没有结果的调用（会困惑）
    模型看到没有对应调用的结果（同样困惑）
    压缩后插入的 stub 是合理的占位符，让模型知道这个调用曾经发生且有结果，只是在更早的上下文中被压缩了。

    两种修复：
      1. 删除匹配的工具调用已被压缩掉的结果
      2. 为结果已被压缩掉的工具调用插入存根结果

    Args:
        messages: 消息列表（原地修改）
    """
    # 从 assistant 消息中收集所有 tool_call ID
    call_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls", []):
                tc_id = tc.get("id", "")
                if tc_id:
                    call_ids.add(tc_id)

    # 删除孤立的结果
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "tool" and msg.get("tool_call_id") not in call_ids:
            messages.pop(i)
        else:
            i += 1

    # 收集现有的结果 ID
    result_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "tool":
            tcid = msg.get("tool_call_id", "")
            if tcid:
                result_ids.add(tcid)

    # 为孤立的 tool_calls 插入存根结果
    inserts: list[tuple[int, dict]] = []
    for idx, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls", []):
            tc_id = tc.get("id", "")
            if tc_id and tc_id not in result_ids:
                stub = {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": tc.get("function", {}).get("name", "unknown"),
                    "content": "[Result from earlier context — see summary above]",
                }
                inserts.append((idx + 1, stub))
                result_ids.add(tc_id)

    for pos, stub in reversed(inserts):
        messages.insert(pos, stub)


# -- Structured summary templates ------------------------------------------

_STRUCTURED_SUMMARY_PROMPT = """\
Summarize this conversation for handoff to a fresh context window.
This summary is the ONLY context available — omitted information is lost.

Use EXACTLY this structure:

## Goal
What the user is trying to accomplish.

## Constraints & Preferences
User-stated requirements: risk tolerance, strategy parameters, asset preferences.

## Progress
### Done
- Completed steps with key results and specific numbers.
### In Progress
- Current work when compression triggered.

## Key Decisions
Choices made and rationale.

## Resolved Questions
Questions already answered — do NOT re-answer these.

## Pending User Asks
Unfinished requests still needing action.

## Relevant Files
File paths, run_dir, signal engines, artifact locations.

## Remaining Work
What still needs to be done (background reference, NOT active instructions).

## Critical Context
Specific numbers, parameters, error messages, configuration values.

## Tools & Patterns
Which tools worked, what failed, effective approaches.

IMPORTANT: This is a handoff — background reference, NOT active instructions.
Preserve ALL specific numbers, file paths, and parameter values.
{focus_section}
Conversation to summarize:
"""

_FOCUS_SECTION = """
FOCUS TOPIC: {topic}
Allocate 60-70% of the summary budget to content related to this topic.
Aggressively compress unrelated content to make room.
"""

_ITERATIVE_UPDATE_PROMPT = """\
Update the existing summary with new conversation turns.

PREVIOUS SUMMARY:
{previous_summary}

NEW TURNS TO INCORPORATE:
{new_turns}

Rules:
- PRESERVE all existing information from the previous summary.
- ADD new progress, decisions, and findings.
- Move "In Progress" items to "Done" when completed.
- Move answered questions to "Resolved Questions".
- Keep the same section structure.
- Do NOT drop any critical context from the previous summary.
{focus_section}"""


def _is_tool_success(result: str) -> bool:
    """Return True if the tool result does not look like an error response."""
    try:
        data = json.loads(result)
        if isinstance(data, dict) and data.get("status") == "error":
            return False
    except (json.JSONDecodeError, TypeError):
        pass
    return True


def _normalize_tool_run_dir(args: dict[str, Any], memory_run_dir: str | None) -> dict[str, Any]:
    """Normalize ``run_dir`` in tool args to an absolute path when possible.

    If the model supplies a relative ``run_dir`` (for example ``"."`` or
    ``"risk_parity_run"``), resolve it against the active run directory.
    """
    normalized = dict(args)
    if not memory_run_dir:
        return normalized

    if "run_dir" not in normalized:
        normalized["run_dir"] = memory_run_dir
        return normalized

    run_dir_value = str(normalized["run_dir"]).strip()
    if not run_dir_value:
        normalized["run_dir"] = memory_run_dir
        return normalized

    candidate = Path(run_dir_value)
    if not candidate.is_absolute():
        normalized["run_dir"] = str((Path(memory_run_dir) / candidate).resolve())
    return normalized


class AgentLoop:
    """ReAct Agent core loop.

    Attributes:
        registry: Tool registry.
        llm: ChatLLM client.
        memory: Workspace memory.
        max_iterations: Maximum number of iterations.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        llm: ChatLLM,
        memory: Optional[WorkspaceMemory] = None,
        event_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        max_iterations: int = 50,
        persistent_memory: Optional[Any] = None,
    ) -> None:
        """Initialize AgentLoop.

        Args:
            registry: Tool registry.
            llm: ChatLLM client.
            memory: Workspace memory (created fresh if not provided).
            event_callback: Event callback (event_type, data).
            max_iterations: Maximum number of loop iterations.
            persistent_memory: PersistentMemory for cross-session recall.
        """
        self.registry = registry
        self.llm = llm
        self.memory = memory or WorkspaceMemory()
        self._event_callback = event_callback
        self.max_iterations = max_iterations
        self._called_ok: set[str] = set()
        self._cancelled: bool = False
        self._previous_summary: str = ""
        self._persistent_memory = persistent_memory

    def cancel(self) -> None:
        """Cancel the current loop.

        The main loop exits on the next iteration check.
        """
        self._cancelled = True

    def run(self, user_message: str, history: Optional[List[Dict[str, Any]]] = None, session_id: str = "") -> Dict[str, Any]:
        """同步运行 ReAct 循环

        Args:
            user_message: 用户消息
            history: 之前的对话消息
            session_id: 会话 ID

        Returns:
            执行结果字典

        1.初始化
        重置状态（取消标记、已调用工具集合）
        创建运行目录并保存请求
        构建消息上下文（通过 ContextBuilder）
        
        2.ReAct 循环（最多 max_iterations 次迭代）
        Layer 1: _microcompact — 精简每次迭代的消息
        Layer 2: _context_collapse — token 超过阈值时折叠长文本
        Layer 3: auto_compact — 超过 token 上限时的压缩

        3.LLM 调用
        流式调用 llm.stream_chat，收集思考文本
        无工具调用时直接返回结果

        4. 工具执行
        _process_tool_calls 执行工具调用（支持读/写批处理）
        工具执行后可触发手动压缩

        5.收尾
        写入 trace 和状态（success/failed/cancelled）
        6.返回结果字典
        """
        # 重置每次运行的状态（支持多次调用 run()）
        self._cancelled = False
        self._called_ok = set()
        self._previous_summary = ""

        state_store = RunStateStore()
        RUNS_DIR.mkdir(parents=True, exist_ok=True)

        # 确定运行目录：优先使用 memory 中已存在的，否则创建新的
        if self.memory.run_dir and Path(self.memory.run_dir).exists():
            run_dir = Path(self.memory.run_dir)
        else:
            run_dir = state_store.create_run_dir(RUNS_DIR)
            self.memory.run_dir = str(run_dir)

        state_store.save_request(run_dir, user_message, {"session_id": session_id})

        context = ContextBuilder(self.registry, self.memory,
                                  persistent_memory=self._persistent_memory)
        messages = context.build_messages(user_message, history)
        react_trace: List[Dict[str, Any]] = []

        trace = TraceWriter(run_dir)
        trace.write({"type": "start", "prompt": user_message[:500]})

        iteration = 0
        final_content = ""

        try:
            while iteration < self.max_iterations:
                if self._cancelled:
                    trace.write({"type": "cancelled", "iter": iteration})
                    logger.info("AgentLoop 被用户取消")
                    break

                iteration += 1

                # 注入后台任务通知
                bg = get_background_manager()
                notifs = bg.drain_notifications()
                if notifs:
                    notif_text = "\n".join(f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs)
                    messages.append({"role": "user", "content": f"<background-results>\n{notif_text}\n</background-results>"})
                    messages.append({"role": "assistant", "content": "Noted background results."})

                # Layer 1: microcompact（每次迭代）
                _microcompact(messages)

                # Layer 2: context collapse（折叠长文本，零 API 成本）
                tokens = estimate_tokens(messages)
                if tokens > COLLAPSE_THRESHOLD:
                    _context_collapse(messages)
                    tokens = estimate_tokens(messages)

                # Layer 3: auto_compact（超过 token 阈值时触发）
                if tokens > TOKEN_THRESHOLD:
                    logger.info(f"Auto compact 触发: {tokens} tokens > {TOKEN_THRESHOLD}")
                    self._auto_compact(messages, run_dir, trace)

                logger.info(f"ReAct 迭代 {iteration}/{self.max_iterations}")

                # 流式输出 + 收集思考文本
                thinking_chunks: List[str] = []

                def _on_text_chunk(delta: str) -> None:
                    thinking_chunks.append(delta)
                    self._emit("text_delta", {"delta": delta, "iter": iteration})

                response = self.llm.stream_chat(
                    messages,
                    tools=self.registry.get_definitions(),
                    on_text_chunk=_on_text_chunk,
                )

                thinking_text = "".join(thinking_chunks)
                if thinking_text:
                    trace.write({"type": "thinking", "iter": iteration, "content": thinking_text[:2000]})
                    self._emit("thinking_done", {"iter": iteration, "content": thinking_text[:500]})

                # 无工具调用时，直接返回结果
                if not response.has_tool_calls:
                    final_content = response.content or ""
                    trace.write({"type": "answer", "iter": iteration, "content": final_content[:2000]})
                    react_trace.append({"type": "answer", "content": final_content[:500]})
                    break

                messages.append(
                    context.format_assistant_tool_calls(
                        response.tool_calls,
                        content=response.content,
                        reasoning_content=response.reasoning_content or thinking_text or None,
                    )
                )

                # 执行工具（支持读/写批处理）
                compact_requested, focus_topic = self._process_tool_calls(
                    response.tool_calls, context, messages, trace, react_trace, iteration,
                )

                # Layer 3: 所有工具执行完成后进行压缩
                if compact_requested:
                    logger.info("模型触发了手动压缩")
                    self._auto_compact(messages, run_dir, trace, focus_topic=focus_topic)

        except Exception as exc:
            logger.exception(f"AgentLoop 错误: {exc}")
            trace.write({"type": "end", "status": "error", "reason": str(exc), "iterations": iteration})
            trace.close()
            state_store.mark_failure(run_dir, str(exc))
            return {
                "status": "failed",
                "reason": str(exc),
                "run_dir": str(run_dir),
                "run_id": run_dir.name,
                "content": "",
                "react_trace": react_trace,
            }

        # 确定最终状态
        if self._cancelled:
            state_store.mark_failure(run_dir, "cancelled by user")
            final_status = "cancelled"
        elif (run_dir / "artifacts" / "metrics.csv").exists() or final_content:
            state_store.mark_success(run_dir)
            final_status = "success"
        else:
            state_store.mark_failure(run_dir, "pipeline did not complete")
            final_status = "failed"

        trace.write({"type": "end", "status": final_status, "iterations": iteration})
        trace.close()

        return {
            "status": final_status,
            "run_dir": str(run_dir),
            "run_id": run_dir.name,
            "content": final_content,
            "react_trace": react_trace,
        }

    # -- Tool execution with read/write batching --------------------------------

    def _process_tool_calls(
        self,
        tool_calls: list,
        context: ContextBuilder,
        messages: list,
        trace: TraceWriter,
        react_trace: list,
        iteration: int,
    ) -> tuple[bool, str]:
        """预处理工具调用：处理 compact、过滤重复、批处理执行

        Args:
            tool_calls: LLM 响应中的原始工具调用
            context: 用于格式化消息的 ContextBuilder
            messages: 对话消息（原地追加）
            trace: TraceWriter
            react_trace: React 追踪列表
            iteration: 当前迭代次数

        Returns:
            (compact_requested, focus_topic) 元组
        """
        compact_requested = False
        focus_topic = ""
        to_execute = []

        for tc in tool_calls:
            # Layer 4: compact 工具 — 标记后延迟执行
            if tc.name == "compact":
                compact_requested = True
                focus_topic = tc.arguments.get("focus_topic", "")
                messages.append(context.format_tool_result(tc.id, "compact", '{"status":"ok","message":"Compressing..."}'))
                trace.write({"type": "compact_requested", "iter": iteration})
                continue

            tool_def = self.registry.get(tc.name)
            is_repeatable = tool_def.repeatable if tool_def else False
            # 阻止已成功执行的非重复工具（防止重复调用）
            if tc.name in self._called_ok and not is_repeatable:
                logger.warning(f"阻止重复调用: {tc.name} (已成功执行过)")
                skip_msg = json.dumps({"skipped": True, "reason": f"{tc.name} already completed successfully. Use the previous result."})
                messages.append(context.format_tool_result(tc.id, tc.name, skip_msg))
                trace.write({"type": "tool_skipped", "iter": iteration, "tool": tc.name})
                react_trace.append({"type": "tool_skipped", "tool": tc.name})
                continue

            to_execute.append(tc)

        if not to_execute:
            return compact_requested, focus_topic

        # 批处理执行：单个直接执行，多个则分批处理
        if len(to_execute) == 1:
            self._execute_single(to_execute[0], context, messages, trace, react_trace, iteration)
        else:
            self._batch_execute(to_execute, context, messages, trace, react_trace, iteration)

        return compact_requested, focus_topic

    def _batch_execute(
        self,
        tool_calls: list,
        context: ContextBuilder,
        messages: list,
        trace: TraceWriter,
        react_trace: list,
        iteration: int,
    ) -> None:
        """使用读/写批处理执行工具

        连续只读工具通过 ThreadPoolExecutor 并行运行
        写工具在只读批次之间串行运行

        Args:
            tool_calls: 要执行的工具调用
            context: ContextBuilder
            messages: 对话消息
            trace: TraceWriter
            react_trace: React 追踪列表
            iteration: 当前迭代次数
        """
        # 分批：连续的只读工具 → 并行执行，写工具 → 串行执行
        batches: list[tuple[str, list]] = []
        current_ro: list = []

        for tc in tool_calls:
            tool_def = self.registry.get(tc.name)
            if tool_def and tool_def.is_readonly:
                current_ro.append(tc)
            else:
                if current_ro:
                    batches.append(("parallel", current_ro))
                    current_ro = []
                batches.append(("serial", [tc]))
        if current_ro:
            batches.append(("parallel", current_ro))

        for mode, batch in batches:
            if mode == "parallel" and len(batch) > 1:
                self._execute_parallel(batch, context, messages, trace, react_trace, iteration)
            else:
                for tc in batch:
                    self._execute_single(tc, context, messages, trace, react_trace, iteration)

    def _execute_parallel(
        self,
        tool_calls: list,
        context: ContextBuilder,
        messages: list,
        trace: TraceWriter,
        react_trace: list,
        iteration: int,
    ) -> None:
        """Execute readonly tools in parallel using threads.

        Args:
            tool_calls: Readonly tool calls to execute in parallel.
            context: ContextBuilder.
            messages: Conversation messages.
            trace: TraceWriter.
            react_trace: React trace list.
            iteration: Current iteration.
        """
        # Prepare args + emit events
        runnable: list[tuple] = []
        for tc in tool_calls:
            args = _normalize_tool_run_dir(tc.arguments, self.memory.run_dir)
            self._emit("tool_call", {"tool": tc.name, "arguments": {k: str(v)[:200] for k, v in args.items()}, "iter": iteration})
            trace.write({"type": "tool_call", "iter": iteration, "tool": tc.name, "args": {k: str(v)[:200] for k, v in args.items()}})
            runnable.append((tc, args))

        # Execute in parallel
        def _run(tc_args: tuple) -> tuple:
            tc, args = tc_args
            t0 = _time.perf_counter()
            result = self.registry.execute(tc.name, args)
            elapsed_ms = int((_time.perf_counter() - t0) * 1000)
            return tc, result, elapsed_ms

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(runnable), 8)) as pool:
            futures = [pool.submit(_run, item) for item in runnable]
            results = []
            for i, f in enumerate(futures):
                try:
                    results.append(f.result())
                except Exception as exc:
                    tc = runnable[i][0]
                    results.append((tc, json.dumps({"status": "error", "error": str(exc)}), 0))

        # Process results in order
        for tc, result, elapsed_ms in results:
            self._finalize_tool_result(tc, result, elapsed_ms, context, messages, trace, react_trace, iteration)

    def _execute_single(
        self,
        tc: Any,
        context: ContextBuilder,
        messages: list,
        trace: TraceWriter,
        react_trace: list,
        iteration: int,
    ) -> None:
        """Execute a single tool call.

        Args:
            tc: Tool call object.
            context: ContextBuilder.
            messages: Conversation messages.
            trace: TraceWriter.
            react_trace: React trace list.
            iteration: Current iteration.
        """
        args = _normalize_tool_run_dir(tc.arguments, self.memory.run_dir)

        self._emit("tool_call", {"tool": tc.name, "arguments": {k: str(v)[:200] for k, v in args.items()}, "iter": iteration})
        trace.write({"type": "tool_call", "iter": iteration, "tool": tc.name, "args": {k: str(v)[:200] for k, v in args.items()}})
        logger.info(f"Tool call: {tc.name}({list(args.keys())})")

        t0 = _time.perf_counter()
        result = self.registry.execute(tc.name, args)
        elapsed_ms = int((_time.perf_counter() - t0) * 1000)

        self._finalize_tool_result(tc, result, elapsed_ms, context, messages, trace, react_trace, iteration)

    def _finalize_tool_result(
        self,
        tc: Any,
        result: str,
        elapsed_ms: int,
        context: ContextBuilder,
        messages: list,
        trace: TraceWriter,
        react_trace: list,
        iteration: int,
    ) -> None:
        """Record a tool result: update memory, append message, write trace, emit event.

        Args:
            tc: Tool call object.
            result: Raw tool result string.
            elapsed_ms: Execution time in milliseconds.
            context: ContextBuilder.
            messages: Conversation messages.
            trace: TraceWriter.
            react_trace: React trace list.
            iteration: Current iteration.
        """
        self._update_memory(tc.name)

        success = _is_tool_success(result)
        if success:
            self._called_ok.add(tc.name)

        status = "ok" if success else "error"
        truncated = result[:TOOL_RESULT_LIMIT]
        messages.append(context.format_tool_result(tc.id, tc.name, truncated))

        trace.write({"type": "tool_result", "iter": iteration, "tool": tc.name, "status": status, "elapsed_ms": elapsed_ms, "preview": result[:200]})
        react_trace.append({"type": "tool_call", "tool": tc.name, "result_preview": result[:200]})
        self._emit("tool_result", {"tool": tc.name, "status": status, "elapsed_ms": elapsed_ms, "preview": result[:200]})

    # -- Context compression ---------------------------------------------------

    def _auto_compact(self, messages: list, run_dir: Path, trace: TraceWriter,
                      focus_topic: str = "") -> None:
        """Layer 3/4/5: 带 token 预算尾部保护的结构化 LLM 摘要

        相比原始版本的升级：
          - Token 预算尾部：保留约 20K token 的最近消息（而非固定数量）
          - 结构化摘要模板：保留目标、进度、决策、文件等信息
          - 迭代更新：第 N 次压缩时更新之前的摘要，零信息衰减
          - 工具配对修复：压缩后修复孤立的 tool_call/tool_result
          - Focus-topic：可选择在摘要中优先处理特定主题

        Args:
            messages: 消息列表（原地替换）
            run_dir: 运行目录
            trace: TraceWriter
            focus_topic: 可选，摘要中优先处理的主题
        """
        # 压缩前保存完整记录
        transcript_path = run_dir / f"transcript_{int(_time.time())}.jsonl"
        with open(transcript_path, "w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")

        system_msg = messages[0]
        body = messages[1:]

        # Token 预算尾部：从后向前遍历，找出要保留多少最近消息
        accumulated = 0
        cut_idx = len(body)
        for i in range(len(body) - 1, -1, -1):
            content = body[i].get("content", "")
            msg_tokens = (len(str(content)) // 4) + 10
            if accumulated + msg_tokens > TAIL_TOKEN_BUDGET:
                cut_idx = i + 1
                break
            accumulated += msg_tokens
            cut_idx = i

        # 避免在 tool_call/tool_result 配对中间切割
        while 0 < cut_idx < len(body) and body[cut_idx].get("role") == "tool":
            cut_idx += 1

        head = body[:cut_idx]
        tail = body[cut_idx:]

        if not head:
            # 所有内容都在尾部预算内 — 强制分割以避免无限循环
            if len(body) > 2:
                cut_idx = max(1, len(body) // 2)
                head = body[:cut_idx]
                tail = body[cut_idx:]
            else:
                logger.warning("Auto compact: 无需压缩（内容太少）")
                return

        # 构建 focus 部分
        focus_section = _FOCUS_SECTION.format(topic=focus_topic) if focus_topic else ""

        # 构建摘要提示（结构化模板或迭代更新）
        conv_text = json.dumps(head, default=str, ensure_ascii=False)[:80000]

        if self._previous_summary:
            # 迭代更新：基于之前的摘要继续更新
            prompt = _ITERATIVE_UPDATE_PROMPT.format(
                previous_summary=self._previous_summary,
                new_turns=conv_text,
                focus_section=focus_section,
            )
        else:
            # 首次压缩：使用结构化摘要模板
            prompt = _STRUCTURED_SUMMARY_PROMPT.format(focus_section=focus_section) + conv_text

        summary_resp = self.llm.chat([{"role": "user", "content": prompt}])
        summary = summary_resp.content or ""
        self._previous_summary = summary

        tokens_before = estimate_tokens(messages)
        trace.write({"type": "compact", "tokens_before": tokens_before, "summary": summary[:500],
                      "focus_topic": focus_topic or "(none)"})
        self._emit("compact", {"tokens_before": tokens_before, "summary": summary[:200]})

        # 重建消息结构：system + 摘要 + 确认 + 保留的尾部
        state_summary = self.memory.to_summary()
        compressed = f"[Conversation compressed — handoff summary. Transcript: {transcript_path}]\n\n{summary}"
        if state_summary and state_summary != "(empty state)":
            compressed += f"\n\nCurrent agent state:\n{state_summary}"

        messages.clear()
        messages.append(system_msg)
        messages.append({"role": "user", "content": compressed})
        messages.append({"role": "assistant", "content": "Understood. Continuing from the summary."})
        messages.extend(tail)

        # 修复重建后消息列表中的孤立工具配对
        _fix_tool_pairs(messages)

    def _emit(self, event_type: str, data: Dict[str, Any]) -> None:
        """Fire an event via the callback."""
        if self._event_callback:
            try:
                self._event_callback(event_type, data)
            except Exception:
                pass

    def _update_memory(self, tool_name: str) -> None:
        """Update workspace memory counters after tool execution."""
        self.memory.increment(tool_name)
