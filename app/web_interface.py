from fastapi import FastAPI, WebSocket, Request, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import asyncio
import json
import os
import logging
import sys
import time
import uuid
import uvicorn
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime

from app.agent.mcp import MCPAgent
from app.agent.manus import Manus
from app.logger import logger

# 创建FastAPI应用
app = FastAPI(title="OpenManus Web Interface")

# 配置模板目录
BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

# 创建templates目录和历史记录目录
os.makedirs(BASE_DIR / "app" / "templates", exist_ok=True)
os.makedirs(BASE_DIR / "app" / "static", exist_ok=True)
HISTORY_DIR = BASE_DIR / "app" / "history"
os.makedirs(HISTORY_DIR, exist_ok=True)

# 配置静态文件
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")

# 存储每个连接的agent和任务状态
active_connections = {}
active_agents = {}
active_tasks = {}
cancel_flags = {}

class ChatHistory:
    """聊天历史记录管理类"""

    @staticmethod
    def save_conversation(client_id: str, messages: List[Dict[str, Any]]) -> str:
        """保存会话历史记录到文件"""
        history_id = str(uuid.uuid4())
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_{history_id}.json"

        # 创建历史记录数据
        history_data = {
            "id": history_id,
            "timestamp": datetime.now().isoformat(),
            "client_id": client_id,
            "messages": messages
        }

        # 保存到文件
        file_path = HISTORY_DIR / filename
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(history_data, f, ensure_ascii=False, indent=2)

        return history_id

    @staticmethod
    def get_all_conversations() -> List[Dict[str, Any]]:
        """获取所有历史会话的元数据"""
        conversations = []

        for file in HISTORY_DIR.glob("*.json"):
            try:
                with open(file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    conversations.append({
                        "id": data.get("id", str(uuid.uuid4())),
                        "timestamp": data.get("timestamp", file.stem.split("_")[0]),
                        "preview": data.get("messages", [])[0]["content"][:50] + "..." if data.get("messages") else "空会话"
                    })
            except Exception as e:
                logger.error(f"读取历史记录文件 {file} 时出错: {str(e)}")

        # 按时间戳倒序排序
        conversations.sort(key=lambda x: x["timestamp"], reverse=True)
        return conversations

    @staticmethod
    def get_conversation(history_id: str) -> Optional[Dict[str, Any]]:
        """根据ID获取特定会话的完整历史记录"""
        for file in HISTORY_DIR.glob("*.json"):
            try:
                with open(file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("id") == history_id:
                        return data
            except Exception as e:
                logger.error(f"读取历史记录文件 {file} 时出错: {str(e)}")

        return None

@app.get("/", response_class=HTMLResponse)
async def get_home(request: Request):
    """返回主页面"""
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/history", response_class=HTMLResponse)
async def get_history_page(request: Request):
    """返回历史记录页面"""
    conversations = ChatHistory.get_all_conversations()
    return templates.TemplateResponse(
        "history.html",
        {"request": request, "conversations": conversations}
    )

@app.get("/api/history", response_class=JSONResponse)
async def get_history_list():
    """API: 获取历史会话列表"""
    return ChatHistory.get_all_conversations()

@app.get("/api/history/{history_id}", response_class=JSONResponse)
async def get_history_detail(history_id: str):
    """API: 获取特定历史会话详情"""
    conversation = ChatHistory.get_conversation(history_id)
    if conversation:
        return conversation
    return {"error": "历史记录不存在"}

async def process_agent_response(agent, prompt, websocket, client_id):
    """处理代理响应，并展示思考过程"""
    # 设置标志以便可以取消
    cancel_flags[client_id] = False

    try:
        # 发送正在处理的消息
        await websocket.send_json({
            "type": "system",
            "content": "正在处理您的请求..."
        })

        # 添加思考步骤回调
        thinking_steps = []

        def thinking_callback(step):
            # 记录思考步骤
            thinking_steps.append(step)
            # 通知前端
            asyncio.create_task(
                websocket.send_json({
                    "type": "thinking",
                    "content": step
                })
            )

        # 如果是MCPAgent，为其添加思考回调
        if hasattr(agent, 'add_thinking_callback'):
            agent.add_thinking_callback(thinking_callback)

        # 异步执行agent请求
        logger.info(f"客户端 {client_id} 开始执行请求: {prompt[:50]}...")
        task = asyncio.create_task(agent.run(prompt))
        active_tasks[client_id] = task

        # 等待任务完成或被取消
        try:
            response = await task
            logger.info(f"客户端 {client_id} 的请求执行完成")
        except asyncio.CancelledError:
            logger.info(f"客户端 {client_id} 的请求被取消")
            # 确保Agent内部也知道任务被取消
            if hasattr(agent, 'cancel_run'):
                agent.cancel_run()
            raise
        except Exception as e:
            logger.error(f"执行请求时出错: {str(e)}", exc_info=True)
            await websocket.send_json({
                "type": "error",
                "content": f"执行出错: {str(e)}"
            })
            raise

        # 如果任务被取消，则不发送响应
        if cancel_flags[client_id]:
            await websocket.send_json({
                "type": "system",
                "content": "任务已被终止"
            })
            return None

        # 发送最终响应
        await websocket.send_json({
            "type": "response",
            "content": response
        })

        return {
            "prompt": prompt,
            "response": response,
            "thinking_steps": thinking_steps
        }

    except asyncio.CancelledError:
        logger.info(f"客户端 {client_id} 的请求处理被取消")
        # 不需要发送任何消息，因为这已经在WebSocket处理中处理了
        return None
    except Exception as e:
        error_msg = f"处理请求时出错: {str(e)}"
        logger.error(error_msg, exc_info=True)
        # 已经在外层捕获处理，这里不再发送错误消息
        raise
    finally:
        # 清理任务引用
        if client_id in active_tasks:
            del active_tasks[client_id]
        if client_id in cancel_flags:
            del cancel_flags[client_id]

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """WebSocket处理端点，用于与客户端通信"""
    await websocket.accept()
    active_connections[client_id] = websocket

    agent = None
    chat_messages = []

    try:
        # 使用MCPAgent，因为它提供了更多工具
        logger.info(f"为客户端 {client_id} 初始化MCPAgent...")
        agent = MCPAgent()
        active_agents[client_id] = agent

        await websocket.send_json({
            "type": "system",
            "content": "正在连接到服务器..."
        })

        # 初始化Agent
        try:
            await agent.initialize(connection_type="stdio", command=sys.executable, args=["-m", "app.mcp.server"])
            logger.info(f"客户端 {client_id} 的MCPAgent初始化成功")
        except Exception as init_err:
            logger.error(f"初始化MCPAgent时出错: {str(init_err)}")
            await websocket.send_json({
                "type": "error",
                "content": f"连接服务器时出错: {str(init_err)}"
            })
            raise

        await websocket.send_json({
            "type": "system",
            "content": "连接成功！请输入您的指令..."
        })

        while True:
            data = await websocket.receive_text()
            data = json.loads(data)

            if data["type"] == "prompt":
                prompt = data["content"]

                # 记录用户消息
                chat_messages.append({
                    "role": "user",
                    "content": prompt,
                    "timestamp": datetime.now().isoformat()
                })

                # 处理请求
                try:
                    result = await process_agent_response(agent, prompt, websocket, client_id)

                    if result:
                        # 记录响应消息
                        chat_messages.append({
                            "role": "assistant",
                            "content": result["response"],
                            "thinking": result.get("thinking_steps", []),
                            "timestamp": datetime.now().isoformat()
                        })

                        # 保存每次对话后的历史记录
                        if len(chat_messages) > 0:
                            ChatHistory.save_conversation(client_id, chat_messages)
                except Exception as e:
                    logger.error(f"处理客户端 {client_id} 的请求时出错: {str(e)}", exc_info=True)
                    await websocket.send_json({
                        "type": "error",
                        "content": f"处理请求时出错: {str(e)}"
                    })

            elif data["type"] == "cancel":
                # 处理取消请求
                if client_id in active_tasks and not active_tasks[client_id].done():
                    logger.info(f"客户端 {client_id} 请求终止任务")
                    cancel_flags[client_id] = True

                    # 首先尝试调用agent的取消方法（内部取消）
                    agent_in_use = active_agents.get(client_id)
                    if agent_in_use and hasattr(agent_in_use, 'cancel_run'):
                        try:
                            agent_in_use.cancel_run()
                            logger.info(f"已调用客户端 {client_id} 的agent取消方法")
                        except Exception as e:
                            logger.error(f"调用agent取消方法时出错: {str(e)}")

                    # 然后取消任务（外部取消）
                    task = active_tasks[client_id]
                    try:
                        task.cancel()
                        logger.info(f"已取消客户端 {client_id} 的任务")
                    except Exception as e:
                        logger.error(f"取消任务时出错: {str(e)}")

                    await websocket.send_json({
                        "type": "system",
                        "content": "正在终止任务..."
                    })
                else:
                    await websocket.send_json({
                        "type": "system",
                        "content": "没有正在运行的任务可终止"
                    })

    except WebSocketDisconnect:
        logger.info(f"客户端 {client_id} 断开连接")
    except asyncio.CancelledError:
        logger.info(f"客户端 {client_id} 的任务被取消")
    except Exception as e:
        error_msg = f"WebSocket处理时出错: {str(e)}"
        logger.error(error_msg, exc_info=True)
        try:
            if websocket.client_state.CONNECTED:
                await websocket.send_json({
                    "type": "error",
                    "content": error_msg
                })
        except:
            pass  # 如果发送失败，不再尝试
    finally:
        # 保存聊天历史
        if len(chat_messages) > 0:
            try:
                ChatHistory.save_conversation(client_id, chat_messages)
            except Exception as e:
                logger.error(f"保存聊天历史时出错: {str(e)}")

        # 清理资源
        logger.info(f"清理客户端 {client_id} 的资源")
        if agent:
            try:
                await agent.cleanup()
            except Exception as e:
                logger.error(f"清理Agent时出错: {str(e)}")

        if client_id in active_connections:
            del active_connections[client_id]
        if client_id in active_agents:
            del active_agents[client_id]
        if client_id in active_tasks:
            del active_tasks[client_id]
        if client_id in cancel_flags:
            del cancel_flags[client_id]

def start():
    """启动Web服务器"""
    uvicorn.run("app.web_interface:app", host="0.0.0.0", port=8080, reload=True)

if __name__ == "__main__":
    start()
