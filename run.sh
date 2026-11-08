#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# 当前 WSL 的自带 conda Tk 不带 Xft，中文会显示成 \uXXXX。
# 项目内的 .venv 使用带 Xft 的 Tk，优先使用它。
if [[ -x ".venv/bin/python" ]]; then
    exec .venv/bin/python pic_splicer.py
fi

echo "未找到项目内的 .venv，尝试使用系统 python3。"
echo "如果界面中文显示异常或缺少 Pillow，请先安装："
echo "  python3 -m pip install -r requirements.txt"
exec python3 pic_splicer.py
