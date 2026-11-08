#!/usr/bin/env bash
# 在 WSL 里一键打包 Windows 桌面版，并把 exe 放到 Windows 桌面。
#
# 依赖：Windows 上装有 Python 3.10+（py 启动器可用）且能联网安装 PyInstaller。
# 中间文件都放在 Windows 临时目录，仓库里只保留脚本和源码。
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v powershell.exe >/dev/null 2>&1; then
    echo "未找到 powershell.exe，这个脚本需要在 WSL 中运行。" >&2
    exit 1
fi

win_temp="$(powershell.exe -NoProfile -Command 'Write-Output $env:TEMP' | tr -d '\r')"
if [[ -z "$win_temp" ]]; then
    echo "无法获取 Windows 临时目录。" >&2
    exit 1
fi

stage="$(wslpath -u "$win_temp")/pic_splice_build_scripts"
rm -rf "$stage"
mkdir -p "$stage"
cp pic_splicer.py build_icon.py requirements.txt build_windows.ps1 "$stage"/

echo "==> 构建目录: $stage"
powershell.exe -NoProfile -ExecutionPolicy Bypass \
    -File "$(wslpath -w "$stage/build_windows.ps1")" \
    -ProjectDir "$(wslpath -w "$stage")"

echo
echo "完成。桌面上的「Picture Splice」可以直接双击运行。"
