#!/usr/bin/env bash
# LightAIBox Windows 打包 + 签名脚本（Git Bash / MSYS 环境）。
#
# 与 build_windows.sh 的区别：打包完成后额外对产物做代码签名。
# 前置条件：已安装项目依赖（pip install -r requirements.txt）。
#          已通过 sign_artifacts.ps1 -GenCert 生成证书（cert/codesign.pfx）。
#
# 用法:
#   bash build_windows_signed.sh            # 打包 + 签名 + 验证
#   SKIP_SIGN=1 bash build_windows_signed.sh  # 仅打包，跳过签名（退回 build_windows.sh 行为）
set -euo pipefail

cd "$(dirname "$0")"

# ---- 可配置项 ----
# 签名证书密码（与 sign_artifacts.ps1 中的 -Password 保持一致）
# 换成商业 OV/EV 证书时，仅需改动此处的 PFX 密码（以及替换 cert/codesign.pfx）。
SIGN_PASSWORD="${SIGN_PASSWORD:-lightaibox}"

echo "==> 安装/升级 PyInstaller ..."
python -m pip install --upgrade "pyinstaller>=6.0"

echo "==> 清理旧构建产物 ..."
rm -rf build dist

echo "==> 执行打包（spec 模式，onedir） ..."
python -m PyInstaller --clean --noconfirm lightaibox.spec

if [ "${SKIP_SIGN:-0}" = "1" ]; then
    echo ""
    echo "==> SKIP_SIGN=1，跳过签名步骤。"
else
    # 无需签名时的友好降级：证书缺失则跳过（不中断打包结果）
    if [ ! -f "cert/codesign.pfx" ]; then
        echo ""
        echo "==> 未找到 cert/codesign.pfx，跳过签名。"
        echo "    如需签名，先运行: powershell -ExecutionPolicy Bypass -File .\\sign_artifacts.ps1 -GenCert"
    else
        echo ""
        echo "==> 对产物批量签名（cert/codesign.pfx） ..."
        # MSYS_NO_PATHCONV 避免 Git Bash 把 / 开头参数转成路径
        MSYS_NO_PATHCONV=1 powershell -NoProfile -ExecutionPolicy Bypass \
            -File sign_artifacts.ps1 -Password "$SIGN_PASSWORD"

        echo ""
        echo "==> 验证签名 ..."
        MSYS_NO_PATHCONV=1 powershell -NoProfile -ExecutionPolicy Bypass \
            -File sign_artifacts.ps1 -Verify
    fi
fi

echo ""
echo "==> 完成。产物位于:"
echo "    dist/LightAIBox/LightAIBox.exe"
echo ""
echo "    人工验证要点:"
echo "    1. 双击启动，确认 GUI 正常弹出，无错误弹窗。"
echo "    2. 添加一个 provider 后重启，确认数据仍在（数据库应位于用户数据目录）。"
echo "    3. 切换暗/亮主题，确认 QSS 正常加载（无样式丢失）。"
echo "    4. curl http://127.0.0.1:8765/v1/models 确认内置 HTTP 网关随应用启动。"
if [ "${SKIP_SIGN:-0}" != "1" ] && [ -f "cert/codesign.pfx" ]; then
    echo "    5. 右键 exe -> 属性 -> 数字签名，确认有 LightAIBox 签名条目（自签名证书属正常）。"
fi