#!/usr/bin/env bash
# LightAIBox Windows 打包脚本（Git Bash / MSYS 环境）。
#
# 前置条件：已安装项目依赖（pip install -r requirements.txt）。
# 用途：一键安装 PyInstaller 并生成 Windows exe 到 dist/LightAIBox/。
set -euo pipefail

cd "$(dirname "$0")"

echo "==> 安装/升级 PyInstaller ..."
python -m pip install --upgrade "pyinstaller>=6.0"

echo "==> 清理旧构建产物 ..."
rm -rf build dist

echo "==> 执行打包（spec 模式，onedir） ..."
python -m PyInstaller --clean --noconfirm lightaibox.spec

echo ""
echo "==> 完成。产物位于:"
echo "    dist/LightAIBox/LightAIBox.exe"
echo ""
echo "    人工验证要点:"
echo "    1. 双击启动，确认 GUI 正常弹出，无错误弹窗。"
echo "    2. 添加一个 provider 后重启，确认数据仍在（数据库应位于用户数据目录）。"
echo "    3. 切换暗/亮主题，确认 QSS 正常加载（无样式丢失）。"
echo "    4. curl http://127.0.0.1:8765/v1/models 确认内置 HTTP 网关随应用启动。"