#!/usr/bin/env python
"""
OpenManus Web界面启动脚本
"""
import os
import sys
import subprocess
import logging
from pathlib import Path

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

def check_venv():
    """检查是否在虚拟环境中运行"""
    return hasattr(sys, 'real_prefix') or (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix)

def check_dependencies():
    """检查必要的依赖是否已安装"""
    try:
        import fastapi
        import uvicorn
        return True
    except ImportError:
        return False

def install_dependencies():
    """安装必要的依赖"""
    logger.info("正在安装必要的依赖...")

    # 检查uv是否可用
    try:
        subprocess.run(["uv", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        logger.info("使用uv安装依赖...")
        subprocess.run([sys.executable, "-m", "uv", "pip", "install", "fastapi", "uvicorn[standard]"], check=True)
    except (subprocess.SubprocessError, FileNotFoundError):
        logger.info("使用pip安装依赖...")
        subprocess.run([sys.executable, "-m", "pip", "install", "fastapi", "uvicorn[standard]"], check=True)

    logger.info("依赖安装完成")

def activate_venv():
    """激活虚拟环境"""
    base_dir = Path(__file__).resolve().parent
    venv_dir = base_dir / ".venv"

    if not venv_dir.exists():
        logger.error("找不到虚拟环境目录(.venv)，请先创建虚拟环境")
        sys.exit(1)

    # 导入虚拟环境的site-packages
    venv_site_packages = list(venv_dir.glob("lib/python*/site-packages"))
    if not venv_site_packages:
        logger.error("无法找到虚拟环境的site-packages目录")
        sys.exit(1)

    sys.path.insert(0, str(venv_site_packages[0]))

    # 修改环境变量
    os.environ["VIRTUAL_ENV"] = str(venv_dir)
    os.environ["PATH"] = f"{venv_dir}/bin:{os.environ['PATH']}"

    logger.info(f"已激活虚拟环境：{venv_dir}")

def main():
    """主函数"""
    logger.info("启动OpenManus Web界面...")

    # 检查是否在虚拟环境中运行
    if not check_venv():
        logger.warning("未检测到虚拟环境，尝试激活...")
        activate_venv()

    # 检查依赖
    if not check_dependencies():
        logger.warning("缺少必要的依赖，准备安装...")
        install_dependencies()

    # 导入Web界面模块并启动
    try:
        from app.web_interface import start
        logger.info("Web界面初始化完成，准备启动...")
        start()
    except Exception as e:
        logger.error(f"启动Web界面时出错: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("用户中断，程序退出")
    except Exception as e:
        logger.error(f"发生未处理的错误: {str(e)}")
        sys.exit(1)
