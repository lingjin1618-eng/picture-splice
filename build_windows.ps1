param(
    [string]$ProjectDir = "",
    [string]$DesktopDir = "",
    [switch]$SkipDesktopCopy
)

$ErrorActionPreference = "Continue"

if (-not $ProjectDir) {
    $ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if (-not $DesktopDir) {
    $DesktopDir = [Environment]::GetFolderPath("Desktop")
}

$exeName = "Picture Splice"
$sourceFile = Join-Path $ProjectDir "pic_splicer.py"
$iconScript = Join-Path $ProjectDir "build_icon.py"

if (-not (Test-Path $sourceFile)) {
    throw "找不到源文件: $sourceFile"
}
if (-not (Test-Path $iconScript)) {
    throw "找不到图标脚本: $iconScript"
}

# 优先使用 Windows 的 py 启动器，其次才是 PATH 里的 python。
$pythonExe = "python"
$pythonArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $pythonExe = "py"
    $pythonArgs = @("-3")
} elseif (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "没有找到 Python，请先安装 Python 3.10+。"
}

Write-Host "==> 源文件目录: $ProjectDir"
Write-Host ("==> Python: " + (& $pythonExe @pythonArgs -c "import sys; print(sys.executable)"))
if ($LASTEXITCODE -ne 0) { throw "Python 无法运行" }

# 检查 / 安装 PyInstaller。
$hasPyInstaller = $false
& $pythonExe @pythonArgs -c "import PyInstaller" 2>$null | Out-Null
$hasPyInstaller = ($LASTEXITCODE -eq 0)
if (-not $hasPyInstaller) {
    Write-Host "==> 安装 PyInstaller（第一次构建需要联网）..."
    & $pythonExe @pythonArgs -m pip install --user --upgrade pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 安装失败" }
}

# tkinterdnd2 用于从 Windows 资源管理器直接拖图片进自定义拼接格；装不上时不影响其他功能。
$extraArgs = @()
& $pythonExe @pythonArgs -c "import tkinterdnd2" 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "==> 安装 tkinterdnd2（外部拖入照片文件）..."
    & $pythonExe @pythonArgs -m pip install --user --upgrade tkinterdnd2
}
& $pythonExe @pythonArgs -c "import tkinterdnd2" 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    $extraArgs += @("--collect-all", "tkinterdnd2")
} else {
    Write-Host "==> 未安装 tkinterdnd2，将不启用资源管理器拖入功能。"
}

# 所有中间文件都放在 Windows 临时目录，避免 UNC 路径和权限问题。
$stage = Join-Path $env:TEMP ("pic_splice_build_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $stage -Force | Out-Null
Copy-Item $sourceFile $stage
Copy-Item $iconScript $stage
$requirements = Join-Path $ProjectDir "requirements.txt"
if (Test-Path $requirements) {
    Copy-Item $requirements $stage
}

Write-Host "==> 生成图标..."
& $pythonExe @pythonArgs (Join-Path $stage "build_icon.py") (Join-Path $stage "app_icon.ico")
if ($LASTEXITCODE -ne 0) { throw "图标生成失败" }

Write-Host "==> 打包 Windows 可执行文件..."
Push-Location $stage
try {
    & $pythonExe @pythonArgs -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name $exeName `
        --icon (Join-Path $stage "app_icon.ico") `
        --distpath (Join-Path $stage "dist") `
        --workpath (Join-Path $stage "build") `
        --specpath $stage `
        @extraArgs `
        (Join-Path $stage "pic_splicer.py")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }
} finally {
    Pop-Location
}

$exe = Join-Path $stage ("dist\" + $exeName + ".exe")
if (-not (Test-Path $exe)) {
    throw "打包结束但没有找到 $exe"
}

$sizeMb = (Get-Item $exe).Length / 1MB
Write-Host ("==> 已生成: $exe ({0:N1} MB)" -f $sizeMb)

if (-not $SkipDesktopCopy) {
    if (-not (Test-Path $DesktopDir)) {
        New-Item -ItemType Directory -Path $DesktopDir -Force | Out-Null
    }
    $dest = Join-Path $DesktopDir ($exeName + ".exe")
    Copy-Item $exe $dest -Force
    Write-Host "==> 已放到桌面: $dest"
    Write-Host "==> 直接双击桌面上的 $exeName 即可运行。"
}
