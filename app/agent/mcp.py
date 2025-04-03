from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from pydantic import Field

from app.agent.toolcall import ToolCallAgent
from app.logger import logger
from app.prompt.mcp import MULTIMEDIA_RESPONSE_PROMPT, NEXT_STEP_PROMPT, SYSTEM_PROMPT
from app.schema import AgentState, Message
from app.tool.base import ToolResult
from app.tool.mcp import MCPClients


class MCPAgent(ToolCallAgent):
    """Agent for interacting with MCP (Model Context Protocol) servers.

    This agent connects to an MCP server using either SSE or stdio transport
    and makes the server's tools available through the agent's tool interface.
    """

    name: str = "mcp_agent"
    description: str = "An agent that connects to an MCP server and uses its tools."

    system_prompt: str = SYSTEM_PROMPT
    next_step_prompt: str = NEXT_STEP_PROMPT

    # Initialize MCP tool collection
    mcp_clients: MCPClients = Field(default_factory=MCPClients)
    available_tools: MCPClients = None  # Will be set in initialize()

    max_steps: int = 20
    connection_type: str = "stdio"  # "stdio" or "sse"

    # Track tool schemas to detect changes
    tool_schemas: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    _refresh_tools_interval: int = 5  # Refresh tools every N steps

    # Special tool names that should trigger termination
    special_tool_names: List[str] = Field(default_factory=lambda: ["terminate"])

    # 思考回调函数列表
    _thinking_callbacks: List[Callable[[str], None]] = []

    async def initialize(
        self,
        connection_type: str = "stdio",
        server_url: Optional[str] = None,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
    ) -> None:
        """Initialize the agent with the specified connection to the MCP server."""
        self.connection_type = connection_type
        self.mcp_clients = MCPClients()

        try:
            if connection_type == "stdio":
                if not command:
                    raise ValueError("Command is required for stdio connection")
                await self.mcp_clients.connect_stdio(command, args or [])
            elif connection_type == "sse":
                if not server_url:
                    raise ValueError("Server URL is required for SSE connection")
                await self.mcp_clients.connect_sse(server_url)
            else:
                raise ValueError(f"Unsupported connection type: {connection_type}")
        except Exception as e:
            logger.error(f"Failed to initialize MCP agent: {str(e)}")
            raise

        self._thinking_callbacks = []

        # Set available_tools to our MCP instance
        self.available_tools = self.mcp_clients

        # Store initial tool schemas
        await self._refresh_tools()

        # Add system message about available tools
        tool_names = list(self.mcp_clients.tool_map.keys())
        tools_info = ", ".join(tool_names)

        # Add system prompt and available tools information
        self.memory.add_message(
            Message.system_message(
                f"{self.system_prompt}\n\nAvailable MCP tools: {tools_info}"
            )
        )

    def add_thinking_callback(self, callback: Callable[[str], None]) -> None:
        """添加思考过程回调函数"""
        self._thinking_callbacks.append(callback)

    def _notify_thinking(self, step: str) -> None:
        """通知所有回调函数有新的思考步骤"""
        for callback in self._thinking_callbacks:
            try:
                callback(step)
            except Exception as e:
                logger.error(f"Error in thinking callback: {str(e)}")

    async def _refresh_tools(self) -> Tuple[List[str], List[str]]:
        """Refresh the list of available tools from the MCP server.

        Returns:
            A tuple of (added_tools, removed_tools)
        """
        if not self.mcp_clients.session:
            return [], []

        # Get current tool schemas directly from the server
        response = await self.mcp_clients.session.list_tools()
        current_tools = {tool.name: tool.inputSchema for tool in response.tools}

        # Determine added, removed, and changed tools
        current_names = set(current_tools.keys())
        previous_names = set(self.tool_schemas.keys())

        added_tools = list(current_names - previous_names)
        removed_tools = list(previous_names - current_names)

        # Check for schema changes in existing tools
        changed_tools = []
        for name in current_names.intersection(previous_names):
            if current_tools[name] != self.tool_schemas.get(name):
                changed_tools.append(name)

        # Update stored schemas
        self.tool_schemas = current_tools

        # Log and notify about changes
        if added_tools:
            logger.info(f"Added MCP tools: {added_tools}")
            self.memory.add_message(
                Message.system_message(f"New tools available: {', '.join(added_tools)}")
            )
        if removed_tools:
            logger.info(f"Removed MCP tools: {removed_tools}")
            self.memory.add_message(
                Message.system_message(
                    f"Tools no longer available: {', '.join(removed_tools)}"
                )
            )
        if changed_tools:
            logger.info(f"Changed MCP tools: {changed_tools}")

        return added_tools, removed_tools

    async def think(self) -> bool:
        """Process current state and decide next action."""
        # Check MCP session and tools availability
        if not self.mcp_clients.session or not self.mcp_clients.tool_map:
            logger.info("MCP service is no longer available, ending interaction")
            self.state = AgentState.FINISHED
            return False

        # Refresh tools periodically
        if self.current_step % self._refresh_tools_interval == 0:
            await self._refresh_tools()
            # All tools removed indicates shutdown
            if not self.mcp_clients.tool_map:
                logger.info("MCP service has shut down, ending interaction")
                self.state = AgentState.FINISHED
                return False

        # 在调用父类think方法前，设置拦截器捕获思考过程
        original_log_info = logger.info

        def info_interceptor(message):
            # 捕获思考过程并通知回调
            if "✨" in message and "thoughts" in message:
                # 提取思考内容
                thought = message.split("✨")[1].strip()
                self._notify_thinking(thought)
            # 继续原始的日志输出
            return original_log_info(message)

        # 替换logger的info方法
        logger.info = info_interceptor

        try:
            # Use the parent class's think method
            return await super().think()
        finally:
            # 恢复原始logger方法
            logger.info = original_log_info

    async def _handle_special_tool(self, name: str, result: Any, **kwargs) -> None:
        """Handle special tool execution and state changes"""
        # First process with parent handler
        await super()._handle_special_tool(name, result, **kwargs)

        # Handle multimedia responses
        if isinstance(result, ToolResult) and result.base64_image:
            self.memory.add_message(
                Message.system_message(
                    MULTIMEDIA_RESPONSE_PROMPT.format(tool_name=name)
                )
            )

    def _should_finish_execution(self, name: str, **kwargs) -> bool:
        """Determine if tool execution should finish the agent"""
        # Terminate if the tool name is 'terminate'
        return name.lower() == "terminate"

    async def cleanup(self) -> None:
        """Clean up MCP connection when done."""
        if self.mcp_clients.session:
            await self.mcp_clients.disconnect()
            logger.info("MCP connection closed")
        self.mcp_clients = None
        self._thinking_callbacks = []

    async def run(self, prompt: str) -> str:
        """Run the agent with the given prompt."""
        if not self.mcp_clients:
            raise RuntimeError("Agent not initialized")

        self._notify_thinking("正在规划执行步骤...")

        # 存储当前任务的取消检查函数
        self._cancelled = False

        # 定义一个检查是否取消的函数
        def is_cancelled():
            return self._cancelled

        # 取消当前任务的函数
        def cancel_run():
            self._cancelled = True
            self.state = AgentState.FINISHED
            logger.info("用户取消了任务执行")

        # 存储这些函数以便外部调用
        setattr(self, "is_cancelled", is_cancelled)
        setattr(self, "cancel_run", cancel_run)

        try:
            # 第一步思考：分析用户需求
            self._notify_thinking("分析用户需求，确定执行计划...")

            # 检查工具可用性
            bash_tool = self.mcp_clients.get_tool("bash")
            browser_tool = self.mcp_clients.get_tool("browser")

            # 检查是否需要使用特定工具
            if browser_tool and ("搜索" in prompt or "浏览" in prompt or "网页" in prompt):
                self._notify_thinking("检查是否需要浏览器能力...")
                self._notify_thinking("准备使用浏览器能力处理请求...")

            # 准备工具调用
            self._notify_thinking("确定最适合的工具和方法...")

            # 添加用户消息
            self.memory.add_message(Message.user_message(prompt))

            # 执行实际任务 - 使用父类ToolCallAgent的方法
            self._notify_thinking("开始执行用户请求...")

            # 使用父类的执行方法 - 使用BaseAgent的核心执行流程
            results = []
            self.current_step = 0
            async with self.state_context(AgentState.RUNNING):
                while (
                    self.current_step < self.max_steps and
                    self.state != AgentState.FINISHED and
                    not self._cancelled
                ):
                    # 检查任务是否被取消
                    if self._cancelled:
                        logger.info("检测到任务取消请求，停止执行")
                        break

                    self.current_step += 1
                    logger.info(f"执行步骤 {self.current_step}/{self.max_steps}")
                    step_result = await self.step()

                    # 检查是否卡住
                    if self.is_stuck():
                        self.handle_stuck_state()

                    results.append(step_result)

                    # 再次检查任务是否被取消
                    if self._cancelled:
                        logger.info("执行步骤后发现任务取消请求，停止执行")
                        break

            # 如果任务被取消，直接返回
            if self._cancelled:
                return "任务已被用户终止"

            # 最终思考：总结结果
            self._notify_thinking("任务执行完毕，正在总结结果...")

            # 获取最终响应 - 优先使用最后一个助手消息
            final_response = self.memory.get_last_assistant_message()
            if final_response:
                return final_response.content

            # 如果没有助手消息，则返回步骤结果
            if results:
                return "\n".join(results)

            return "完成，但没有生成响应。"

        except Exception as e:
            logger.error(f"Error running MCP agent: {str(e)}")
            return f"执行出错: {str(e)}"
        finally:
            # 清理取消相关属性
            if hasattr(self, "is_cancelled"):
                delattr(self, "is_cancelled")
            if hasattr(self, "cancel_run"):
                delattr(self, "cancel_run")
