#!/usr/bin/env python
import os
import sys
import logging
import argparse

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description='启动OpenManus Web界面')
    parser.add_argument('--host', default='0.0.0.0', help='绑定主机 (默认: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=8080, help='监听端口 (默认: 8080)')
    parser.add_argument('--reload', action='store_true', help='启用热重载 (开发模式)')
    return parser.parse_args()

def main():
    """主函数"""
    args = parse_args()

    try:
        # 检查依赖
        import fastapi
        import uvicorn
    except ImportError:
        logger.error("缺少必要的依赖，正在安装...")
        os.system(f"{sys.executable} -m pip install fastapi uvicorn[standard]")
        logger.info("依赖安装完成，请重新运行此脚本")
        return

    # 打印使用信息
    logger.info("启动OpenManus Web界面...")
    logger.info(f"服务将在 http://{args.host if args.host != '0.0.0.0' else 'localhost'}:{args.port} 上运行")
    logger.info("按下 Ctrl+C 停止服务")

    from app.web_interface import start

    # 启动Web服务器
    import uvicorn
    uvicorn.run(
        "app.web_interface:app",
        host=args.host,
        port=args.port,
        reload=args.reload
    )

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("服务已停止")
    except Exception as e:
        logger.error(f"发生错误: {str(e)}", exc_info=True)
