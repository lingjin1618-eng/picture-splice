# Picture Splice 项目总结（v1.0 快照）

> **快照日期**：2026-11-08  
> **对应源码**：`pic_splicer.py`（5,796 行，约 197 KB）  
> **文档用途**：后续升级先读本文，不必重读全部源码；本文记录了开发流程、全部功能、实现结构、已知限制和升级路线。  
> **行号说明**：文中的行号是 2026-11-08 快照行号，只作定位参考；代码变动后请优先按“函数名 / 类名”搜索。  
> **维护规则**：每次升级后同步更新第 3、4、7、8 节和文末“版本记录”。

---

## 0. 一页速览

| 项目 | 内容 |
|---|---|
| 工具名 | **Picture Splice**（用户界面、文档、桌面快捷方式统一用这个名字，不写 `.exe` 后缀） |
| 用途 | 把多张照片拼成佳能 6 寸相纸可打印的版面（2 张左右 / 4 张田字格 / 自动混排） |
| 技术栈 | Python 3.13 + Tkinter（自绘界面）+ Pillow；可选 `tkinterdnd2`；PyInstaller 打包 |
| 输出 | 1800 × 1200 px，300 dpi，3:2，约 15.24 × 10.16 cm；JPEG（质量 98、4:4:4）或 PNG |
| 核心特点 | 本地离线、不压缩原图、导出时按原文件全分辨率重拼、智能分组、自动旋转、后台导入/导出 |
| 当前形态 | 单文件 `pic_splicer.py`；Linux/WSL 用 `./run.sh`；Windows 桌面版为 `C:\Users\Administrator\Desktop\Picture Splice.exe` |
| 版本状态 | v1.0 功能快照；已托管到 GitHub（`lingjin1618-eng/picture-splice`，MIT），仍无自动化测试、无版本号/安装包 |
| 主要入口 | `PhotoSplicerApp`（`pic_splicer.py:1943`）和 `main()`（`pic_splicer.py:5780`） |
| 最重要约束 | 原图只读；界面只缓存 1600 px 预览图；导出时才重新读取原文件；Tk 控件只在主线程操作 |

---

## 1. 项目概览

### 1.1 定位

一个**本地运行、离线、无需联网**的照片拼版小工具，面向“把旅行/家庭照片批量拼成 6 寸相纸”的场景。它不修改原图，也不上传照片；导入时只读取并缓存屏幕预览图，导出时按原文件全分辨率重新裁剪、重采样、拼接。

### 1.2 当前文件清单

| 文件/目录 | 作用 |
|---|---|
| `pic_splicer.py` | 主程序：图像分析、拼接核心、全部 UI、导入/导出、WSL 文件对话框、平台判断 |
| `run.sh` | Linux/WSL 启动脚本，优先使用项目内 `.venv/bin/python` |
| `environment.yml` | 可选的 conda 环境：Python 3.13、Pillow、带 Xft 的 Tk 8.6.13（中文显示需要） |
| `requirements.txt` | `Pillow>=10.0`、`tkinterdnd2>=0.4`（后者可选，仅 Windows 外部拖入用） |
| `build_windows.sh` | WSL 入口：把源码复制到 Windows 临时目录，调用 PowerShell 打包，把 exe 放到桌面 |
| `build_windows.ps1` | 真正的 PyInstaller 打包脚本（Windows 侧执行） |
| `build_icon.py` | 用 Pillow 生成 16–256 px 多尺寸 `.ico` 图标 |
| `README.md` | 面向用户的使用说明（含功能徽章、界面截图、运行/使用方法、隐私说明） |
| `PROJECT_SUMMARY.md` | 本文档，面向后续开发/升级 |
| `LICENSE` | MIT 许可证（2026 lingjin1618-eng） |
| `docs/screenshot.png` | README 使用的界面截图（示例照片为生成的示意图） |
| `.venv/` | 本地开发环境（Python 3.13.18、Pillow 13.0.0、Tk 8.6），已在 `.gitignore` 中 |
| `.claude/settings.local.json` | 开发期积累的 Bash 权限白名单（运行、截图、PowerShell、测试脚本等） |
| `.gitignore` | 忽略 `.venv/`、`__pycache__/`、`sample_photos/`、构建产物、`app_icon.ico`、`.claude/settings.local.json` |

项目已于 2026-11-08 初始化 git 仓库（首次提交 `ce5997f`）并推送到 GitHub：<https://github.com/lingjin1618-eng/picture-splice>。本文的“迭代脉络”是根据现有代码、README、文件时间戳和开发记忆重建的。

### 1.3 当前版本边界（重要）

- 有：2/4/自动混排、自定义拼接、拖拽、智能分组、自动旋转、单张取景、后台导入/导出、WSL 原生对话框、Windows 桌面版、外部文件拖入（仅 Windows）。
- 没有：工程保存/恢复、撤销/重做、更多版式、更多纸张尺寸、HEIC/AVIF、PDF 输出、多语言、自动化测试、版本号、安装包/签名。
- 详细清单见第 7 节。

---

## 2. 开发流程

### 2.1 实际工作循环

这个项目是**单文件、快速迭代、手动验收**的开发方式，典型循环如下：

1. **需求口述 / 看实际界面反馈**  
   用户描述要改的交互或视觉细节，通常直接在 WSL 里运行看效果。
2. **只改 `pic_splicer.py`（必要时改 README）**  
   功能、样式、平台适配都在同一个文件里；新增依赖才动 `requirements.txt` / `build_windows.ps1`。
3. **本地静态检查**  
   ```bash
   cd /home/gin_sco9/gin_proj/proj/pic_splice
   .venv/bin/python -m py_compile pic_splicer.py build_icon.py
   .venv/bin/python -c "import pic_splicer; print('import OK')"
   ```
4. **WSL 里跑起来看界面**  
   ```bash
   timeout 6 ./run.sh
   # 退出码 124 表示窗口正常存活到超时；重点是看终端有没有 traceback
   ```
   开发期常用 `/tmp` 下的临时 harness 脚本实例化 `PhotoSplicerApp`，直接调用内部方法、`root.update()`、`app.render_preview()` 来验证布局/拖拽/缩放；也用 `xwd` / `PIL.ImageGrab` 截图检查界面。
5. **需要 Windows 行为时重新打包**  
   ```bash
   ./build_windows.sh
   ```
   然后在 Windows 桌面双击 `Picture Splice` 验证：资源管理器拖入、Tk 原生文件对话框、外框/最小化、PyInstaller 下的字体和图标。
6. **回归检查 + 文档**  
   参考第 9 节验收清单；更新 `README.md`、开发记忆，以及本文档。

> 注意：没有把 `/tmp` 下的测试 harness 纳入仓库，也没有 pytest/CI；这是当前最大的工程短板之一（见 7.5）。

### 2.2 本地运行环境（WSL / Linux）

- 项目内 `.venv` 是主要开发环境：Python 3.13.18、Pillow 13.0.0、Tk 8.6。
- **为什么必须用 Xft 版 Tk**：WSL 自带的 conda Tk 不带 Xft 时，中文会显示成 `\uXXXX`。`environment.yml` 明确使用 `tk=8.6.13=xft*`，`run.sh` 优先使用项目内 `.venv`。
- `run.sh` 逻辑：
  1. `cd` 到脚本所在目录；
  2. 若 `.venv/bin/python` 可执行，直接 `exec` 它运行 `pic_splicer.py`；
  3. 否则提示安装依赖并用系统 `python3`。
- 在其他电脑上首次运行：
  ```bash
  python3 -m pip install -r requirements.txt
  python3 pic_splicer.py
  ```
- 中文字体选择顺序（两处相同的候选列表）：
  `Noto Sans CJK SC` → `Microsoft YaHei UI` → `Microsoft YaHei` → `PingFang SC` → `Source Han Sans SC` → `WenQuanYi Micro Hei` → `DejaVu Sans`。
- 普通 Linux 桌面用 Tk 原生文件对话框；WSL 用 Windows 原生对话框（见 4.8）。
- Linux/WSL 下**不支持**从资源管理器拖文件进来（WSLg 不桥接 Explorer 的拖放），只能用“添加照片”导入后从左侧列表拖。

### 2.3 Windows 桌面版构建流程

**入口**：`./build_windows.sh`（必须在 WSL 中运行，且 Windows 能调用 `powershell.exe`）。

`build_windows.sh` 做的事情：

1. 找到 Windows 的 `%TEMP%`，在 WSL 侧映射为路径；
2. 把 `pic_splicer.py`、`build_icon.py`、`requirements.txt`、`build_windows.ps1` 复制到  
   `%TEMP%\pic_splice_build_scripts\`；
3. 用 PowerShell `-ExecutionPolicy Bypass` 执行 `build_windows.ps1`；
4. 脚本结束后提示桌面上的 Picture Splice 可以双击。

`build_windows.ps1` 做的事情：

1. 优先用 Windows 的 `py -3`，其次 `python`；要求 Python 3.10+；
2. 检查 PyInstaller，没有就 `pip install --user --upgrade pyinstaller`（第一次需要联网）；
3. 检查 `tkinterdnd2`，没有就尝试安装；装成功则给 PyInstaller 加 `--collect-all tkinterdnd2`，失败则禁用外部拖入但不影响其他功能；
4. 在 `%TEMP%\pic_splice_build_<GUID>\` 建立每次构建的独立目录，复制源码和图标脚本；
5. 运行 `build_icon.py` 生成 `app_icon.ico`；
6. 运行 PyInstaller：
   ```text
   --noconfirm --clean --onefile --windowed
   --name "Picture Splice"
   --icon app_icon.ico
   --collect-all tkinterdnd2   # 仅当可用
   ```
7. 把 `dist\Picture Splice.exe` 复制到桌面（默认 `[Environment]::GetFolderPath("Desktop")`）。

**产物位置**：`C:\Users\Administrator\Desktop\Picture Splice.exe`  
**注意**：

- exe 没有数字签名，首次运行可能触发 SmartScreen：选择“更多信息 → 仍要运行”。
- 所有中间文件都在 Windows 临时目录，项目仓库只保留源码和构建脚本。
- **每次改完 `pic_splicer.py`，都要重新跑 `./build_windows.sh`** 才能更新桌面版。
- 用户可见文案统一叫 **Picture Splice**，不要写成 `Picture Splice.exe`；exe 文件名本身仍必须保留 `.exe`。
- 构建环境当时使用 Windows 上的 Python 3.10+（构建记录为 3.14）；本地开发环境是 Python 3.13。代码本身要求 Python 3.10+。

### 2.4 验证手段（现状）

开发期实际用过的方式：

| 方式 | 用途 |
|---|---|
| `py_compile` / `import pic_splicer` | 语法、导入、依赖检查 |
| `timeout 5 ./run.sh` | 启动 smoke test，看 traceback |
| `/tmp/test_*.py` 临时 harness | 构造 `PhotoSplicerApp`，调用 `_refresh_all()`、`render_preview()`、模拟拖拽/切页/缩放 |
| `xwd` / `ImageGrab` 截图 | 检查自绘窗口、圆角、面板折叠、预览 |
| PowerShell 脚本 | 启动 Windows exe、发送快捷键、截图、操作/关闭文件对话框 |
| 手动操作 | 拖拽、滚轮、旋转、导出后检查像素尺寸/DPI/文件大小 |

**建议以后补上**：把关键 harness 固化为 `tests/` 下的 pytest + 一个 UI smoke test；至少覆盖 `layout_cells`、`crop_geometry`、`order_similar`、`_save_image`、WSL 路径转换。

### 2.5 迭代脉络（重建，不是 git 历史）

从代码和文档可以看出大致演进顺序：

1. **拼接核心**：`layout_cells` / `crop_geometry` / `compose_page` / 导出，先解决 2 张和 4 张的 6 寸拼版。
2. **三栏工作区**：左侧单张照片、中间自动拼接、右侧自定义拼接 + 大预览；可折叠、可拖分栏线。
3. **浅色视觉与无边框窗口**：淡紫 + 藕粉、自绘标题栏、药丸按钮、圆角相纸、最小化恢复胶囊、右下角缩放手柄。
4. **智能能力**：图像统计分类（人物/风景/建筑）、感知哈希同场景、色系兜底、最近邻排序、自动混排、自动旋转。
5. **交互完善**：从列表拖照片进格子、交换/替换、浮层缩略图、滚轮锚点缩放、拖动取景残影、手动旋转/复位。
6. **性能与质量**：后台并行导入、可取消、进度条；导出后台执行；预览缓存；导出时按原图全分辨率重拼；导出本页/全部。
7. **平台适配**：WSLg 下用 Windows 原生文件对话框；最小化改用“隐藏主窗口 + 悬浮胶囊”；Windows PyInstaller 构建、图标、桌面投放；Windows 专属资源管理器拖入。
8. **文档与记忆**：README、“Picture Splice”命名约定、WSLg 对话框/外部拖入/桌面路径等开发记忆，以及本文档。

### 2.6 开发约定与踩坑记录

- **原图只读**：导入只生成最长边 1600 px 的预览图；不要为了省内存去改/覆盖原文件。
- **导出必须重新读原文件**：所以导入后不要移动/改名/删除原图，否则导出会失败。
- **Tk 线程规则**：PIL 解码、分析、排序、合成、保存可以放后台线程；所有 Tk 控件操作必须回到主线程（本项目用 `queue.Queue` + `root.after` 轮询）。
- **WSLg 文件对话框**：`overrideredirect(True)` 会让 Tk 自带对话框被主窗口盖住；WSL 下必须走 PowerShell 的 Windows Forms 对话框 + `wslpath -u` 路径转换。不要换回普通 `filedialog.*`，除非重新验证 WSLg 的窗口层级。
- **WSLg 最小化**：对 override-redirect 窗口调用系统 `iconify` 会断开 X 连接；所以用“隐藏主窗口 + 右下角 Toplevel 胶囊”的实现。
- **外部拖入只在 Windows**：`OS_FILE_DND = tkinterdnd2 可导入 and sys.platform == "win32"`；WSLg 无法桥接 Explorer 拖放，不要假设它能用。
- **应用名**：所有用户可见位置写 **Picture Splice**，不写 `.exe`。
- **中文显示**：使用带 Xft 的 Tk；字体候选列表已在 `_pick_font()` 和 `WindowControls._font_family()` 中维护。

---

## 3. 功能全览

### 3.1 窗口与工作区

- **无系统边框窗口**：`root.overrideredirect(True)`，自绘浅色标题栏；初始 1320 × 880，居中偏上；最小 1080 × 740。
- **标题栏**：Logo + “Picture Splice” + 副标题“柔和拼版 · 300 dpi · 同场景优先”；按住空白处拖动窗口；双击标题栏最大化/还原。
- **窗口按钮**：右上角最小化 `–`、最大化/还原 `□ / ❐`、关闭 `✕`，Canvas 自绘圆形悬停效果。
- **顶部操作按钮**：`添加照片`、`清空`、`导出本页`、`导出全部`。
- **四区布局**（`tk.PanedWindow`，可拖分栏线）：
  1. 左：单张照片列表（默认 300 px，最小 180）；
  2. 中：自动拼接页列表（默认 225 px，最小 190）；
  3. 右：自定义拼接页列表（默认 235 px，最小 190）；
  4. 最右：大预览区（最小 440 px）。
- **面板折叠**：每栏标题右侧 `‹` 收起为 38 px 的竖排标签，再点 `›` 恢复原宽度。
- **最小化**：主窗口隐藏，在窗口右下角（并夹紧到屏幕内）显示“Picture Splice / 已最小化 · 点此恢复”胶囊；点击恢复。这样不依赖系统标题栏。
- **调整大小**：右下角斜纹手柄拖动改变窗口尺寸。
- **最大化**：直接使用主屏幕宽高 `+0+0`，会覆盖任务栏；还原时恢复之前的 geometry。
- **关闭**：直接销毁 root；没有“未保存”确认（见 7.1/7.6）。

### 3.2 导入照片

- **入口**：`添加照片` 按钮、`Ctrl+O`、Windows 下资源管理器拖入、自定义拼接缩略图上的文件拖入、预览格子上的文件拖入。
- **文件选择**：多选；支持 `.jpg .jpeg .png .webp .bmp .tif .tiff`；WSL 下用 Windows 打开文件对话框（默认定位“图片”文件夹）。
- **后台并行解码**：
  - 线程池大小 `IMPORT_WORKERS = max(2, min(6, CPU 核数))`；
  - JPEG 先用 `Image.draft()` 按 1/2、1/4、1/8 降采样解码，再缩到预览尺寸；
  - 应用 EXIF 方向（`ImageOps.exif_transpose`）；
  - 透明图铺白底并转 RGB；
  - 预览最长边 1600 px，原图不动。
- **进度与取消**：左侧列表上方显示圆角进度条 + “正在导入 x / N 张” + `取消`；取消后已解码的照片保留，未开始的解码任务丢弃（`shutdown(wait=False, cancel_futures=True)`）。
- **智能排序**：全部加载完后，后台线程调用 `order_similar()` 生成排序；UI 显示“正在按场景智能分组（N 张）…”。
- **放置规则**：
  - 普通导入：先把新照片按排序填进自动拼接页现有的空格，剩余照片再按当前版式新建页；
  - 外部文件拖到自定义拼接：除了进入左侧照片池/自动页，还会复制进目标自定义格；多文件依次填格，当前页填满后自动新建**同版式**的空白页；
  - 自定义页全满时，外部拖入会自动新建同版式页；内部从左侧拖到缩略图则只提示“已放满”，不会自动新建（行为差异见 7.6）。
- **错误处理**：单张失败不影响其他；结束后弹出警告，最多列 8 条错误，其余显示“另有 N 张无法读取”；全部失败/取消时在状态栏提示。
- **格式限制**：不支持 HEIC/AVIF/RAW/GIF；多页 TIFF 只取当前打开页；动图只取静态帧。

### 3.3 单张照片列表（左栏）

- 每张卡片显示：
  - 88 × 66 圆角缩略图（按目标格子方向旋转，方便预览）；
  - 序号 + 文件名；
  - 类别（人物/建筑/风景）；
  - 在自动拼接中的位置（第几页 · 左/右/左上…），或“未使用 · 可拖入拼接图”；
  - 右侧 `×` 移除按钮。
- **点击卡片**：右侧大图切换为这张照片的单张预览（不按格子裁剪，完整显示，带文件名和原始像素尺寸）。
- **双击卡片**：把这张照片放入当前选中的自动格（或自定义格）。
- **拖动卡片**：超过 6 px 阈值后进入拖拽，显示 150 × 112 半透明浮层缩略图；拖到预览格子上放手。
- **拖到预览格子**：
  - 自动拼接页：空格放入；已占用则两张照片交换位置；如果来源不在任何页（未使用），被替换的照片变成“未使用”。
  - 自定义拼接页：放入/替换该格；照片以**独立副本**放入，之后调整取景不影响自动页。
  - 拖到右侧自定义拼接缩略图：自动放进该页第一个空格；页面满时只提示。
- **悬停/选中**：卡片有浅色悬停高亮和选中边框。
- **多张时性能**：超过 80 张照片，卡片按每批 5 个、间隔约 1 ms 生成，避免界面长时间卡住。
- **滚动**：鼠标滚轮在卡片任意子控件上都能滚动列表。

### 3.4 自动拼接与智能分组

- **版式**：
  - 2 张：左右两个竖向格子；
  - 4 张：田字格（四个横向格子）；
  - 自动混排（默认）：每页随机 2 或 4 张，4 张页概率 30%（即约占 1/4 ～ 1/3）。
- **自动混排的分页规则**（`_build_pages`）：
  - 先按类别把照片分桶：**人物 → 风景 → 建筑**；
  - 同类内部用最近邻排序把相似照片串在一起；
  - 自动模式分页时尽量在**同一类别内部**翻页；若当前类别剩余 ≥ 4 张，按 30% 概率用 4 张页，否则 2 张页；剩余不足 4 张时用 2 张页；
  - 固定 2/4 模式则按排序结果依次切页，类别交界处可能出现一页混合。
- **智能分类（纯图像统计，离线）**：
  - **人物倾向**：
    - 64 × 64 小图上做 YCbCr + RGB 双重肤色判断；
    - 统计肤色占比、最大肤色连通块面积占比、连通块外接框填充率；
    - 常规人像：肤色占比 0.015–0.55、最大块占比 ≥ 0.006、填充率 ≥ 0.28；
    - 肤色背景人像（沙滩/米色背景）：肤色占比 > 0.55、深色结构占比 ≥ 0.04、长直线比例 < 0.08；
    - 米色墙面/格窗若结构非常明确（长直线 ≥ 0.11、边缘密度 ≥ 0.11、中性色 ≥ 0.40），会被排除出人物。
  - **建筑倾向**：长直线比例 ≥ 0.085、边缘密度 ≥ 0.085，并且天空占比 ≥ 0.10 或中性色 ≥ 0.30 或低饱和 ≤ 0.34；人物优先级高于建筑。
  - **风景**：其余。
  - **特征**还包括：色相直方图（12 bin，按饱和度/亮度加权）、平均饱和度、平均亮度、中性色比例、天空比例、8 × 8 差值哈希（dHash）。
- **相似度与排序**（`_group_similarity` / `_order_group`）：
  - 人物：`0.40 × 哈希距离 + 0.30 × 色相距离 + 0.20 × 饱和度差 + 0.10 × 亮度差`；
  - 风景：`0.55 × 色相 + 0.25 × 哈希 + 0.12 × 饱和度 + 0.08 × 亮度`；
  - 建筑：`0.45 × 哈希 + 0.35 × 色相 + 0.20 × 饱和度`；
  - 哈希距离 ≤ 0.18 视为同一场景（同机位/连拍），相似度再乘 0.45；
  - 组内最近邻：随机起点，每次从最近的 3 个候选中选，且只在 `best × 1.25 + 0.02` 范围内随机，既有变化又不打散分组。
- **重新随机**：仅自动混排模式可用；重新排序并重新分页，改变组内起点和近似候选顺序，但不会把明显不同类别/色系打散。
- **说明**：这是图像统计近似，不是人脸识别或建筑识别；光线/背景/颜色接近的照片可能被保守分到不同类，手动拖拽即可覆盖。
- **自动页管理**：
  - 中间列表显示所有自动拼接页缩略图、页号、张数、已用数量；
  - 点击缩略图切换预览；
  - 预览区 `＋ 新页` 新建空白页（自动模式下随机 2/4；固定模式用当前版式）；
  - `删除本页` 删除当前自动页；至少保留一页；有照片时弹确认，删除后照片回到左侧“未使用”；
  - `‹ / ›` 翻页。

### 3.5 自定义拼接

- 右侧栏初始为空；`＋ 2 张` / `＋ 4 张` 新建空白自定义拼接图。
- 点击缩略图切换到自定义预览；预览区显示空白网格（虚线空格）。
- **放照片方式**：
  - 从左侧列表把照片拖进预览格子（空格放入、已占用替换）；
  - 拖到自定义拼接缩略图上，自动放进第一个空格；
  - Windows 下从资源管理器把图片文件拖到预览格子或缩略图上；
  - 先点预览里的格子，再双击左侧照片，或点“放入选中格”。
- **副本语义**：自定义页里的照片是 `clone_for_export()` 出来的独立副本，缩放/取景/旋转只影响该副本，不影响自动页；原照片仍保留在左侧池中。
- **删除**：缩略图右下角 `×` 或预览区 `删除本页`；删自定义页不弹确认，也不会删除左侧原照片。
- **自定义页管理**：可任意数量；删除后若还有页则切换到相邻页，删空后自动回到自动拼接预览。
- **外部拖入的连续填充**：多张图片依次填满空格；当前页填满自动新建同版式空白页，并继续填。

### 3.6 预览区

- **自动/自定义拼接预览**：
  - 按 3:2 比例居中显示，带圆角相纸、两层浅投影和细边框；
  - 空格画虚线；当前选中空格画紫色虚线框；已占用格子画细边框；选中格子用藕粉色粗边框；
  - 从左侧拖照片时，鼠标下方格子高亮；
  - 拖动照片取景时，画布上叠 3 层半透明“残影拖尾”，最新的最实。
- **单张照片预览**：
  - 完整显示照片（fit，不裁剪），应用手动旋转，不应用格子自动旋转；
  - 显示文件名、原始像素尺寸；
  - `‹ / ›` 在照片之间切换。
- **标题与页码**：预览头部显示“自动拼接预览 / 自定义拼接预览 / 单张照片预览”、本页张数/版式、页码、前后翻页按钮。
- **空状态**：没有照片时提示“添加照片后，这里会显示拼接效果”；没有自定义页时提示新建。

### 3.7 取景编辑（缩放 / 平移 / 旋转 / 复位）

- **滚轮缩放**：
  - 在预览区把鼠标移到某格照片上滚动，以光标位置为锚点缩放；
  - 每次 ±10%（0.1），范围 100%–300%；
  - 鼠标所在照片会自动成为当前选中照片/格子。
- **缩放滑块**：左侧栏 100%–300%，作用于当前焦点照片。
- **拖动取景**：
  - 在预览格子里按住拖动可移动画面；
  - 只有放大后（裁剪窗口小于原图）才能平移；100% 时填满格子，会提示先放大；
  - 拖动带残影，松手后更新取景。
- **手动旋转**：`↻ 旋转 90°` 每次 +90°，自动与手动旋转叠加；旋转后交换 `offset_x / offset_y` 以尽量保持取景位置。
- **复位取景**：缩放回 100%、偏移归零。
- **焦点照片规则**：
  - 自动页：左侧选中的照片（可以在任意页）；
  - 自定义页：当前选中格里的副本；
  - 单张预览：无取景编辑。
- **裁剪规则**：所有格子都是 **cover**（填满格子、居中裁剪），不拉伸；导出和预览使用同一套几何参数。

### 3.8 自动旋转

- 复选框“自动旋转（横图转竖向）”默认开启。
- 规则：比较**原图方向**（EXIF 摆正后的宽高）和格子方向：
  - 横图放进竖向格子（2 张版式）→ +90°；
  - 竖图放进横向格子（4 张版式）→ +90°；
  - 方向一致 → 不旋转。
- 手动旋转与自动旋转叠加。
- 取景偏移在旋转后通过 `offset_x, offset_y = offset_y, -offset_x` 保持大致焦点。
- **已知边界**：自动旋转判断只看原图宽高，不看用户手动旋转后的方向；手动把竖图转成横向后，如果格子是横向，自动旋转仍会再加 90°（最终 180°）。详见 7.6。

### 3.9 版式、间距、边距

- **拼版方式**（底部 SegmentedControl）：
  - `2 张 · 左右`
  - `4 张 · 田字格`
  - `自动混排`（默认）
- 切换版式会**重新排序并重新分页自动页**，未使用的照片仍留在左侧池中；手动拖拽形成的自动页布局会被重排（见 7.6）。
- **间距/边距**：0–100 档，默认各 12 档；1 档 ≈ 0.25 mm，12 档 ≈ 3.0 mm；界面同时显示毫米数。
- 换算公式：`px = 档位 × 0.25 / 25.4 × 300 × 预览缩放`。
- 间距影响格子之间的距离，边距影响四周边框；对自动页和自定义页都生效。
- `重新随机` 按钮仅在自动混排模式下可用。

### 3.10 导出

- **两种导出**：
  - `导出本页`：只导出预览里当前显示的自动页或自定义页；
  - `导出全部`：导出所有**非空**自动页和自定义页。
- **单页时的文件选择**：
  - 只有一张要导出：弹出保存对话框，可选 JPEG/PNG；
  - 多张要导出：选择文件夹，按命名规则批量写文件；
  - 单张照片预览、空白页不能“导出本页”。
- **命名规则**：
  - 自动页：`六寸拼接_第01页_2张.jpg`
  - 自定义页：`自定义拼接_第01页_4张.jpg`
- **输出规格**：
  - 1800 × 1200 px，300 dpi，3:2；
  - 空槽位导出为纯白背景；
  - 预览中的虚线/边框不会进入导出图。
- **画质**：
  - 导出时按原文件全分辨率重新读取，EXIF 摆正，透明铺白底；
  - 使用完整 LANCZOS 重采样；
  - JPEG 质量 98、`subsampling=0`（4:4:4），写入 300 dpi；
  - PNG 无损，写入 300 dpi；
  - 路径没有扩展名时默认存成 `.jpg`。
- **后台执行**：导出线程逐页合成、保存；左侧显示页数进度；导出期间界面可继续操作，但“添加照片”和导出按钮禁用。
- **内存**：每页导出完调用 `release_full_image()` 释放全分辨率副本，避免一次性占用大量内存；导出前会对页面做 `clone_for_export()` 快照，避免导出过程中改参数影响结果。
- **错误**：原图被移动/删除、文件损坏、磁盘写入失败等会弹出“导出失败”；当前实现只要有一页报错，就显示错误并忽略成功页的汇总提示（见 7.1）。
- **不可取消**：导出进度条没有取消按钮（导入才有）。

### 3.11 快捷键、状态栏、进度

- `Ctrl+O`：添加照片；
- `Ctrl+E`：导出全部；
- 底部状态栏：显示当前操作结果/提示（选中位置、拖拽提示、导入/导出状态、错误提示等）。
- 左侧进度条同时服务导入和导出：
  - 导入：可取消；
  - 导出：只显示进度，不可取消。

### 3.12 视觉与自绘组件

- 配色：淡紫 + 藕粉浅色系，不使用纯白面板；关键色在文件顶部 `BG / PANEL_BG / ACCENT / ACCENT_BLUSH / DANGER` 等常量中。
- 自绘组件：
  - `SoftSlider`：圆角轨道 + 圆形手柄滑块；
  - `SegmentedControl`：药丸形分段选择器；
  - `TogglePill`：带小圆点的开关药丸；
  - `PillButton`：4 种样式（primary / soft / quiet / ghost）；
  - `ProgressBar`：圆角细进度条；
  - `WindowControls`：最小化/最大化/关闭；
  - `ResizeGrip`：右下角缩放手柄；
  - `RestoreBubble`：最小化恢复胶囊；
  - `FloatingGhost`：拖动照片时的半透明浮层。
- 照片缩略图：圆角、按格子方向旋转；列表缩略图 88 × 66，浮层 150 × 112，页面缩略图 150 × 100。
- ttk 主题使用 `clam`，并定制按钮/滚动条样式；进度条和滑块主要用 Canvas 自绘。

### 3.13 平台差异

| 能力 | Windows 桌面版 | WSL / Linux |
|---|---|---|
| 文件打开/保存/选文件夹 | Tk 原生对话框 | WSL：Windows Forms 原生对话框 + 路径转换；普通 Linux：Tk 原生对话框 |
| 从资源管理器拖文件进格子 | 支持（需 tkinterdnd2） | 不支持；只能先“添加照片”再从左侧拖 |
| 最小化 | 隐藏主窗口 + 右下角胶囊 | 同左（WSLg 不能安全使用系统 iconify） |
| 中文 | 使用系统字体 | 必须使用带 Xft 的 Tk，否则显示 `\uXXXX` |
| 外框/最大化 | 自绘，最大化覆盖任务栏 | 同左；WSLg 下窗口管理器基本不介入 |

---

## 4. 数据模型与核心实现

### 4.1 数据结构

#### `Photo`（`pic_splicer.py:453`）

| 字段 | 含义 |
|---|---|
| `path` | 原文件路径；导出时按它重新读取 |
| `preview` | 最长边 1600 px 的界面预览图（RGB，EXIF 已摆正，透明已铺白） |
| `width` / `height` | 原图（EXIF 摆正后）的像素宽高 |
| `zoom` | 1.0–3.0 |
| `offset_x` / `offset_y` | 裁剪中心归一化偏移，-1 靠左/上，1 靠右/下 |
| `rotation` | 手动旋转，0/90/180/270 |
| `profile` | `PhotoProfile`，分类/排序特征 |
| `_rotated_*` | 预览图旋转缓存 |
| `_full_*` | 原图全分辨率缓存（导出用，按需加载/释放） |

- `rotation_for_cell(cell_w, cell_h, auto_rotate)`：手动角度 +（可选）自动 90°。
- `oriented_image(angle, cache, full)`：返回旋转后的预览图或全分辨率图。
- `clone_for_export()`：复制路径、预览、尺寸、裁剪参数、分类特征；**不复制**全图缓存。用于自定义页和对导出做快照。
- `clear_rotation_cache()`：释放预览旋转缓存。

#### `Page`（`pic_splicer.py:564`）

- `mode`：2 或 4；
- `photos`：长度固定为 mode 的列表，元素为 `Photo | None`；
- `used_count()`：非空槽位数。

#### `Cell`（`pic_splicer.py:582`）

- 输出图像坐标系下的格子矩形 `x, y, width, height`；
- `box` 返回 `(x0, y0, x1, y1)`；
- `contains(px, py)` 用于命中测试。

#### `CropGeometry`（`pic_splicer.py:696`）

- 描述某张照片在某格子里的裁剪窗口（原图/预览图坐标）；
- 字段：`image`、`angle`、`crop_w/h`、`center_x/y`、`max_center_x/y`；
- `box` 返回裁剪框 `(left, top, right, bottom)`。

#### `PhotoProfile`（`pic_splicer.py:115`）

- `is_person`、`is_building`；
- `skin_ratio`、`largest_skin`、`dark_ratio`；
- `structure`（边缘密度）、`sky_ratio`、`neutral_ratio`；
- `hue_hist`（12 bin）、`saturation`、`brightness`；
- `dhash`（64 bit）。

### 4.2 导入流水线

```text
选择文件 / 拖入
  → _start_import(paths)
  → 后台线程 _import_worker：
        ThreadPoolExecutor(load_photo)
        每张 load_photo：
            Image.open → 读 EXIF 方向 → JPEG draft 降采样
            → exif_transpose → 铺白底转 RGB → thumbnail 到 1600
            → analyze_image
        全部完成后 order_similar 排序
  → queue 把 photo/error/ordered/done 发回主线程
  → _poll_import 每 40 ms 最多消费 12 条：
        追加到 self.photos、更新进度
  → _finish_import：
        _assign_new_photos(added, ordered)
        更新选中/预览/状态/错误提示
```

关键点：

- `load_photo` 只解码预览图；原图在 `full_image()` 时才读。
- JPEG 的 `draft()` 是“快速导入大图”的关键；其他格式会完整解码再缩略。
- `analyze_image` 在 64 × 64 图上做纯 Python 统计，不依赖 OpenCV。
- 取消时，已加载的照片保留，尚未开始的任务取消；排序不会执行，按到达顺序放置。

### 4.3 裁剪与缩放数学

`crop_geometry(photo, cell_w, cell_h, auto_rotate, cache, full)`：

1. 先按 `rotation_for_cell()` 得到角度，取旋转后的图（预览或全图）；
2. 计算 cover 基准裁剪框：使裁剪区域刚好覆盖格子的最大区域：
   - 图比格子“更宽” → 高度取满，宽度 = 高度 × 格子宽高比；
   - 否则宽度取满，高度 = 宽度 ÷ 格子宽高比；
3. `zoom`（≥ 1）缩小裁剪框：`crop_w = base_w / zoom`，`crop_h = base_h / zoom`；
4. 最大中心偏移：`max_center_x = (img_w - crop_w) / 2`，`max_center_y` 同理；
5. 实际中心：`img_w/2 + offset_x × max_center_x`，`offset_y` 同理；
6. 返回裁剪框和旋转后的图。

`render_cell`：

- 预览：`resize(..., BILINEAR, box=geometry.box, reducing_gap=2.0)`；
- 导出：`resize(..., LANCZOS, box=geometry.box, reducing_gap=None)`。

`zoom_photo_at(photo, cell_w, cell_h, auto_rotate, fraction_x, fraction_y, new_zoom)`：

- 记录光标下的原图点；
- 修改 zoom 后重新计算裁剪框；
- 反推新的 offset，使光标下的画面尽量保持不动；
- 最后 clamp 到 -1..1。

### 4.4 智能分组算法

1. `analyze_image(preview)` → `PhotoProfile`。
2. `photo_kind()`：
   - `is_person` → “人物”；
   - 否则 `is_building` → “建筑”；
   - 否则 “风景”。
3. `order_similar(photos, rng)`：
   - 分桶：人物、风景、建筑（固定顺序）；
   - 每桶 `_order_group()`：随机起点 + 最近邻；
   - 最近邻距离由 `_group_similarity()` 计算，同场景 dHash 距离 ≤ 0.18 时乘 0.45 拉近；
   - 每次从最近 3 个候选、距离 ≤ `best×1.25+0.02` 的池中随机选一个，保留随机性。
4. `_build_pages(ordered, layout)`：
   - 固定 2/4：按顺序切块；
   - 自动：在同类连续段内分页；剩余 ≥ 4 时 30% 概率 4 张页，否则 2 张；不足 4 时 2 张。
5. `_assign_new_photos()`：
   - 先把新照片填进已有自动页的空格（按页/槽顺序）；
   - 剩余照片再 `_build_pages(..., pre_ordered=True)` 新建页。

### 4.5 自动旋转规则

```text
image_portrait = photo.height > photo.width   # EXIF 摆正后的原图
cell_portrait  = cell_h > cell_w
mismatch = image_portrait != cell_portrait
angle = photo.rotation + (90 if auto_rotate and mismatch else 0)
```

- 手动旋转叠加在自动旋转之上。
- `_orientation_mismatch()` 只看原图宽高，不看 `photo.rotation`（见 7.6）。

### 4.6 输出规格与单位换算

| 常量 | 值 | 说明 |
|---|---|---|
| `OUTPUT_WIDTH` | 1800 | 输出宽 |
| `OUTPUT_HEIGHT` | 1200 | 输出高 |
| `OUTPUT_DPI` | 300 | 写入 JPEG/PNG 的 dpi |
| `JPEG_QUALITY` | 98 | JPEG 质量 |
| `PREVIEW_MAX_SIZE` | 1600 | 预览最长边 |
| `PAGE_THUMB_WIDTH/HEIGHT` | 150/100 | 页面缩略图 |
| `UNIT_MM` | 0.25 | 1 档 = 0.25 mm |
| `DEFAULT_GAP` / `DEFAULT_MARGIN` | 12 / 12 | 默认间距/边距档位 |
| `MAX_SPACING` | 100 | 滑块上限 |
| `MIXED_FOUR_CHANCE` | 0.3 | 自动混排 4 张页概率 |
| `MAX_ZOOM` | 3.0 | 最大放大倍率 |
| `ANALYSIS_SIZE` | 64 | 分类分析小图尺寸 |
| `HUE_BINS` | 12 | 色相直方图 bin 数 |
| `SAME_SCENE_HAMMING` | 0.18 | 同场景阈值 |
| `IMPORT_WORKERS` | `max(2, min(6, CPU))` | 导入线程数 |

换算：

```text
1 档 = 0.25 mm
12 档 = 3.0 mm
像素 = 档位 × 0.25 / 25.4 × 300 × (预览宽 / 1800)
```

### 4.7 线程模型

- 主线程：所有 Tk 控件创建/修改、Canvas 绘制、事件处理。
- 导入线程：
  - `_import_worker`：线程池解码 + 分析 + 排序；
  - 只把结果放进 `queue.Queue`；
  - 主线程 `_poll_import` 用 `root.after(40, ...)` 消费。
- 导出线程：
  - `_export_worker`：逐页 `compose_page(full=True)` + `_save_image`；
  - 主线程 `_poll_export` 用 `root.after(50/60, ...)` 消费。
- 好处：大图解码/合成/保存不阻塞 Tk 事件循环；窗口不会“未响应”。
- 注意：不要在后台上直接操作 `tk.Variable`、`ImageTk.PhotoImage` 或控件。

### 4.8 WSL 文件对话框

问题：主窗口 `overrideredirect(True)`，WSLg 下窗口管理器无法正确管理它，Tk 自带文件对话框会被主窗口盖住（虽然能获得焦点）。

解决：

- `running_in_wsl()`：检查 `WSL_DISTRO_NAME` / `WSL_INTEROP` / `/proc/version` 是否含 `microsoft`。
- `_run_windows_dialog(body)`：
  1. 把一段 PowerShell 脚本用 UTF-16LE 编码成 Base64；
  2. 调用 `powershell.exe -NoProfile -NonInteractive -STA -WindowStyle Hidden -EncodedCommand ...`；
  3. 超时 600 秒；
  4. 通过 `__PIC_SPLICE_PICK__` / `__PIC_SPLICE_CANCEL__` / `__PIC_SPLICE_ERROR__` 标记解析结果；
  5. 返回 `None` 表示 PowerShell 不可用，调用方回退 Tk 对话框。
- `_windows_pick_files`：`System.Windows.Forms.OpenFileDialog`，多选，默认“图片”文件夹。
- `_windows_save_file`：`SaveFileDialog`，默认扩展名 `jpg`，覆盖提示。
- `_windows_pick_folder`：`FolderBrowserDialog`。
- `_windows_path_to_wsl`：先 `wslpath -u`；失败时按 `C:\...` → `/mnt/c/...` 回退。
- Windows 桌面版和普通 Linux 不走这段逻辑，直接用 Tk 原生对话框。
- 对话框关闭后 `_refocus_after_dialog()` 把焦点还给主窗口。

### 4.9 缓存与批量构建

- `_thumb_cache[(path, angle)]`：左侧照片列表缩略图（`ImageTk.PhotoImage`）。
- `_page_thumb_cache[signature]`：拼接页缩略图；signature 包含版式、间距、边距、自动旋转、每张照片的路径/缩放/偏移/旋转；超过 400 条时清空。
- 图片列表 > 80 张：`_build_cards_batch()` 每批 5 个，`after(1, ...)`。
- 页面列表：`schedule_page_lists()` 延迟 80 ms；`_build_page_items_batch()` 每批 4 个。
- 预览：`schedule_preview()` 延迟 30 ms；窗口 resize 后延迟 70 ms。
- 预览渲染后，会清理**不在当前页**的照片旋转缓存，控制内存。

### 4.10 导出流水线

```text
export_all / export_current_page
  → 检查导入/导出状态
  → 收集非空页面（自动 + 自定义）
  → 单页：保存文件对话框；多页：选择文件夹
  → _snapshot_page：clone_for_export() 快照
  → _start_export：后台线程 + 进度条
  → _export_worker 每页：
        compose_page(photos, mode, gap, margin, preview=False, full=True)
        _save_image（JPEG q98/4:4:4 或 PNG，300 dpi）
        image.close()
        release_full_image()
  → _poll_export 更新进度
  → _finish_export 显示结果/错误
```

---

## 5. 代码地图（按函数名定位）

> 行号为 2026-11-08 快照；重构后请按名称搜索。

### 5.1 顶部与核心

| 行号 | 内容 |
|---|---|
| 1–11 | 模块 docstring（工作区、输出、导入/导出原则） |
| 13–40 | 导入、可选 `tkinterdnd2`、`OS_FILE_DND` |
| 42–50 | `IMAGE_EXTENSIONS` |
| 53–105 | 输出规格、线程数、缩略图尺寸、间距常量、颜色常量 |
| 108–446 | 图像分析：`PhotoProfile`、`_is_skin`、`_largest_skin_component`、`_structure_features`、`_dhash`、`analyze_image`、`_hue_distance`、`photo_kind`、`_group_similarity`、`_order_group`、`order_similar` |
| 449–558 | `Photo` 数据类 |
| 563–578 | `Page` |
| 581–593 | `Cell` |
| 596–651 | `_flatten_to_rgb`、`load_full_image`、`load_photo` |
| 654–887 | `unit_to_px`、`layout_cells`、`CropGeometry`、`crop_geometry`、`render_cell`、`zoom_photo_at`、`compose_page`、`slot_label` |
| 890–1069 | WSL 文件对话框与路径转换 |
| 1072–1147 | 圆角绘制、`_rounded_image`、`make_list_thumbnail` |

### 5.2 自绘控件

| 行号 | 类 |
|---|---|
| 1150–1281 | `SoftSlider` |
| 1284–1404 | `SegmentedControl` |
| 1407–1507 | `TogglePill` |
| 1510–1592 | `PillButton` |
| 1595–1657 | `ProgressBar` |
| 1660–1737 | `WindowControls` |
| 1740–1794 | `ResizeGrip` |
| 1797–1887 | `RestoreBubble` |
| 1890–1940 | `FloatingGhost` |

### 5.3 应用主体

| 行号 | 内容 |
|---|---|
| 1943–2057 | `PhotoSplicerApp.__init__`：窗口、状态、缓存、线程字段 |
| 2061–2220 | `_configure_styles`：字体、ttk 样式 |
| 2223–2334 | `_pick_font`、`_center_window`、`_build_ui`、`_make_panel` |
| 2336–2380 | `_collapse_button` / `_collapse_panel` / `_expand_panel` |
| 2382–2492 | `_build_titlebar`（按钮、窗口控制、拖动/双击最大化） |
| 2496–2543 | 窗口移动、最大化、最小化、恢复、关闭 |
| 2545–2736 | `_build_sidebar`：照片列表、进度、缩放/旋转/复位/移除控件 |
| 2738–2870 | 滚动绑定、`_make_page_list`、自动/自定义面板 |
| 2872–3095 | `_build_workspace`、预览 Canvas、`_build_settings_bar`、间距控件 |
| 3089–3232 | 状态辅助、页面构建：`_location_of`、`_random_page_mode`、`_build_pages`、`_assign_new_photos`、`_reflow_pages` |
| 3236–3293 | `_pick_photo_paths`、`_pick_save_path`、`_pick_export_folder` |
| 3297–3590 | `add_photos`、后台导入、进度、取消、完成处理 |
| 3592–3823 | 清空、移除、选中、放入格子、自定义格、移出 |
| 3825–3920 | 手动旋转、复位、翻页、加页、删页 |
| 3924–4281 | 刷新照片列表、缩略图缓存、页面列表、页面卡片、选中页、加/删自定义页 |
| 4283–4354 | 自定义页辅助、`_active_page`、`_active_slot_index`、`_control_photo` |
| 4356–4663 | 照片卡片、控件刷新、预览页导航 |
| 4666–4988 | 预览调度、几何、空状态、拼接页渲染、单张预览、拖动残影 |
| 4992–5176 | 缩放滑块、滚轮、间距/版式/自动旋转回调、重新随机、卡片拖拽 |
| 5178–5294 | 外部 DnD（预览区、自定义缩略图） |
| 5296–5459 | 命中测试、预览按下/拖动/松开取景 |
| 5461–5486 | 列表滚动 |
| 5490–5777 | 导出全部/本页、快照、按钮状态、后台导出、完成处理、保存 |
| 5780–5796 | `main()`：Tk/TkinterDnD root、Ctrl+O/Ctrl+E、mainloop |

### 5.4 关键状态字段

| 字段 | 含义 |
|---|---|
| `self.photos` | 左侧照片池；自动页引用池中的 `Photo` 对象 |
| `self.pages` | 自动拼接页列表，至少一页 |
| `self.custom_pages` | 自定义拼接页列表，可为空 |
| `self.selected_index` | 左侧选中的照片索引 |
| `self.active_slot` | 自动页当前选中格 `(page_index, slot_index)` |
| `self.active_custom_slot` | 自定义页当前选中格 |
| `self.current_page` / `current_custom_page` | 当前自动/自定义页索引 |
| `self.preview_kind` | `"auto"` / `"custom"` / `"photo"` |
| `self.preview_photo_index` | 单张预览的照片索引 |
| `self.mode` | `IntVar`：2 / 4 / 0（自动混排） |
| `self.auto_rotate` | `BooleanVar` |
| `self.gap_units` / `margin_units` | `IntVar`，0–100 |
| `self.zoom_value` | `IntVar`，100–300 |
| `self._drag` | 预览内拖动取景状态 |
| `self._card_drag` | 左侧卡片拖拽状态 |
| `self._drop_hover` | 拖拽悬停的格子索引 |
| `self._import_*` / `_export_*` | 后台任务、队列、进度、错误 |

---

## 6. 关键不变量（升级时不要破坏）

1. **原图只读**：导入不写、不缩、不覆盖原文件；只缓存预览图。
2. **预览图上限 1600 px**：界面/缩略图都用它；导出必须重新打开原文件。
3. **导出全分辨率 + LANCZOS**：不要为了速度把导出改成用预览图；JPEG 默认 q98 / 4:4:4。
4. **导出空槽纯白**：预览里的虚线/高亮/边框不能进入导出图。
5. **Tk 主线程规则**：后台线程只做 PIL/IO，结果通过 queue 回主线程。
6. **自定义页是副本**：`clone_for_export()` 的独立裁剪参数，不能直接共享 `Photo` 对象。
7. **WSL 对话框走 Windows 原生**：除非重新验证 WSLg 层级问题，不要换回 Tk 对话框。
8. **WSLg 最小化用胶囊**：不要改回系统 `iconify`。
9. **外部拖入只在 Windows**：`OS_FILE_DND` 的判断不要放宽到 WSL。
10. **应用名**：用户可见文案统一 `Picture Splice`，不加 `.exe`。
11. **输出规格**：1800 × 1200、300 dpi、3:2；改尺寸时同步改换算、预览比例、README。
12. **文件命名**：`六寸拼接_第NN页_N张.jpg` / `自定义拼接_第NN页_N张.jpg`；改命名要同步 README 和用户习惯。
13. **智能分组离线**：不联网、不上传；分类是统计近似，不要对外承诺人脸/建筑识别。
14. **自动分组顺序**：人物 → 风景 → 建筑；改顺序要同步 README 和 `order_similar`。
15. **构建入口**：`./build_windows.sh` 是唯一桌面发布入口；改构建参数要同时看 `build_windows.ps1`。

---

## 7. 已知限制与待升级点

### 7.1 数据安全 / 可靠性（优先）

1. **没有工程保存/恢复**
   - 关闭窗口后，照片池、自动页、自定义页、裁剪参数、版式、间距都会丢失。
   - 建议：新增 JSON 工程文件（记录照片路径、页面结构、每张照片的 zoom/offset/rotation、设置项），提供“保存工程 / 打开工程 / 自动保存”；对缺失原图做占位提示。
   - 涉及：`PhotoSplicerApp.__init__`、`_refresh_all`、`_snapshot_page`、新的 serialization 模块。

2. **原图依赖**
   - 导入后移动/改名/删除原图，导出会失败；这是“不复制原图、节省磁盘”的代价。
   - 建议：至少导出前批量检查文件存在性并给出缺失列表；可选“把原图复制到工程缓存目录”模式。
   - 涉及：`Photo.full_image()`、`_export_worker`。

3. **批量导出静默覆盖**
   - 选择文件夹导出多页时，同名文件会被直接覆盖，没有提示。
   - 建议：导出前检测冲突，提供“覆盖 / 跳过 / 自动加序号”。
   - 涉及：`export_all` 多页分支、`_save_image`。

4. **部分成功被错误掩盖**
   - 只要有一页报错，`_finish_export` 只显示错误，不汇总已成功页。
   - 建议：记录成功/失败清单，弹窗显示“成功 N 页，失败 M 页”，并保留已写文件。
   - 涉及：`_export_worker`、`_poll_export`、`_finish_export`。

5. **关闭无未保存确认**
   - 自绘关闭按钮直接 `root.destroy()`；没有“是否保存/放弃”提示。
   - 建议：有照片或自定义页时弹确认；未来接入工程保存后提示保存。
   - 涉及：`_close_window`。

6. **删除自定义页无确认**
   - 自定义页 `×` 直接删除；自动页有确认，行为不一致。
   - 建议：有内容时确认，或引入撤销。
   - 涉及：`remove_custom_page`、`_make_page_item`。

7. **没有撤销/重做**
   - 交换、删除页、清空、旋转、取景改变都不可撤销。
   - 建议：对页面/照片池/裁剪参数做命令式快照栈；先覆盖高风险操作（清空、删除、交换）。
   - 涉及：`_assign_photo_to_slot`、`_assign_photo_to_custom_slot`、`delete_page`、`clear_photos`、`remove_selected`。

8. **重复导入**
   - 同一文件多次导入会生成多个 `Photo` 对象；没有去重提示。
   - 建议：按规范化路径检测，询问“跳过重复 / 仍然添加”。

### 7.2 核心功能扩展

1. **更多版式**
   - 当前 `Page.mode` 只允许 2/4；没有 1、3、5、6、9 宫格，也没有上下分栏、自由网格、拼贴模板。
   - 涉及：`Page.__post_init__`、`layout_cells`、`slot_label`、`_random_page_mode`、`_build_pages`、版式分段控件、导出命名。

2. **更多输出规格**
   - 固定 1800 × 1200 / 300 dpi / 3:2；没有 5 寸、7 寸、A4、4R、方形、竖版/横版选择，也不能自定义像素/DPI。
   - 涉及：`OUTPUT_WIDTH/HEIGHT/DPI`、`unit_to_px`、`_preview_geometry`、`_save_image`、README。

3. **每格 fit/cover 切换**
   - 全部是 cover 裁剪；没有“完整显示 + 白边”的 fit 模式，也没有每格背景色/填充色。
   - 涉及：`crop_geometry`、`render_cell`、`compose_page`、取景 UI。

4. **更细的旋转/翻转**
   - 只有 90° 步进；没有 180° 快捷、水平/垂直翻转、任意角度微调。
   - 涉及：`Photo.rotate`、`oriented_image`、`rotate_selected`、单张预览。

5. **缩放下限**
   - 最小 100%（必须填满格子）；不能缩小露出背景。
   - 涉及：`MAX_ZOOM` 下限检查、`crop_geometry`、拖动取景。

6. **更多导出格式/质量设置**
   - 只有 JPEG/PNG；JPEG 质量固定 98；没有 PDF、TIFF、WebP、质量滑块、文件大小预估。
   - 涉及：`SAVE_FILETYPES`、`_pick_save_path`、`_save_image`、导出默认名。

7. **色彩管理**
   - 没有 ICC 配置文件处理；CMYK/AdobeRGB 图可能偏色；README 建议用 sRGB。
   - 建议：读取并保留 ICC，导出时统一转 sRGB（需要 ImageCms）。
   - 涉及：`load_full_image`、`_flatten_to_rgb`、`_save_image`。

8. **HEIC/AVIF/RAW**
   - 当前依赖 Pillow 的格式支持；HEIC 通常需要 `pillow-heif`，RAW 需要额外库。
   - 建议：可选安装 `pillow-heif` 并注册 opener；Windows 打包时 `--collect-all pillow_heif`；同步 `IMAGE_EXTENSIONS`。

9. **文件夹导入 / 排序**
   - 只能多选文件；没有“导入整个文件夹/子目录”，也不能按文件名/拍摄日期/类别排序。
   - 涉及：`_pick_photo_paths`、`_start_import`、左侧列表。

10. **页面管理**
    - 不能拖动页面排序、复制页、移动页、把自动页转成自定义页、批量删除空页、把某一页重新套用版式。
    - 涉及：页面列表 `_make_page_item`、`select_page`、`add_page`、`remove_custom_page`。

11. **批量照片操作**
    - 左侧列表只能单选；没有多选删除/移出/旋转/放入。
    - 涉及：`selected_index` 的单选模型、照片卡片事件。

12. **预览内拖动换位**
    - 只能从左侧列表拖到格子；不能把预览里的照片从一格拖到另一格，也不能拖出到“未使用”。
    - 涉及：`_on_preview_press/drag/release` 目前只用于取景平移，和卡片拖拽是两套逻辑。

13. **手动分组覆盖**
    - 不能手动标记“人物/风景/建筑/同一组”，也不能锁定照片不参与重新随机。
    - 涉及：`PhotoProfile`、`photo_kind`、`order_similar`、`_reflow_pages`。

14. **更强的同场景判断**
    - 目前只有 dHash 近似；没有利用拍摄时间/EXIF/地点/连拍序号；也没有本地人脸聚类。
    - 建议：先利用 EXIF 时间（同一天/几秒内）+ dHash；如要人脸聚类，使用本地模型并保持可选、离线。
    - 涉及：`load_photo`、`PhotoProfile`、`_group_similarity`。

15. **外部拖入的跨平台**
    - Linux/WSL 无法从文件管理器拖入；可以考虑“粘贴图片/文件夹导入/最近目录”作为替代。
    - 涉及：`_setup_external_dnd`、`OS_FILE_DND`。

16. **打印集成**
    - 只能导出文件再手动打印；没有系统打印对话框、出血线/裁切线、无边距打印提示。
    - 涉及：新的平台打印模块；注意 Windows/WSL 差异。

17. **记住上次设置**
    - 间距、边距、版式、自动旋转、导出目录、窗口尺寸都不持久化。
    - 涉及：`__init__` 中的 `IntVar/BooleanVar`、`_pick_*`、窗口 geometry。

18. **多语言**
    - 界面文案硬编码中文；没有 i18n。
    - 涉及：所有 UI 字符串；建议先抽出文案表。

### 7.3 性能

1. **纯 Python 像素分析**
   - `analyze_image`、`_structure_features`、`_largest_skin_component` 在 64 × 64 上逐像素循环；照片很多时导入会变慢。
   - 建议：缓存特征到 sidecar/工程文件；或用 NumPy/OpenCV（可选依赖）向量化；保持无依赖回退路径。

2. **组内最近邻 O(n²)**
   - `_order_group` 每步对剩余照片排序；数百张可接受，上千张会明显变慢。
   - 建议：分桶后限制候选集、使用近似索引，或先按 dHash/色相粗聚类再排序。

3. **预览重绘**
   - 每次拖动/缩放都会重新 `compose_page` 整页；预览尺寸大时可能掉帧。
   - 建议：只重绘变动格子；或在拖动时用低分辨率合成，松手后高质量重绘。

4. **导出内存峰值**
   - 每页导出会同时加载最多 4 张全分辨率图；超大图（>1 亿像素）可能内存吃紧。
   - 建议：限制解码最长边（可配置）、逐张裁剪后立即释放、或使用 `draft` 在保持质量前提下降低中间尺寸。

5. **缩略图缓存**
   - 页面缩略图缓存超过 400 条会整体清空；大量页面时重建成本高。
   - 建议：LRU 淘汰、磁盘缓存。

6. **列表批量构建**
   - 已有分批（照片 5/批、页面 4/批），但大量照片时总耗时仍长；可以合并刷新、虚拟滚动。

### 7.4 体验 / 平台

1. **高 DPI**
   - Windows 125%/150% 缩放下 Tk 可能模糊或尺寸错位；没有 `SetProcessDpiAwareness`。
   - 建议：启动时设置 DPI awareness，并按 `tk scaling` 调整布局/字号。

2. **多显示器 / 任务栏**
   - 最大化固定用主屏宽高 `+0+0`，会覆盖任务栏，且多显示器时只在主屏。
   - 建议：使用当前显示器工作区（Windows 可用 `SystemParametersInfo`/`MonitorFromWindow`），或恢复系统边框选项。

3. **无障碍 / 键盘**
   - 大量 Canvas 自绘控件不可 Tab 聚焦，没有焦点提示；主要靠鼠标。
   - 建议：为核心操作提供键盘快捷键和可见焦点；照片列表/页面列表支持方向键。

4. **窗口行为**
   - 无系统标题栏 = 无任务栏预览/贴边/Aero Snap；最小化靠胶囊。
   - 建议：保留“使用系统边框”选项，或在 Windows 上使用 `WM_NCHITTEST` 等原生方案（工作量大）。

5. **导出不可取消**
   - 导出大量页面时只能等；没有取消按钮。
   - 建议：导出 worker 检查 `threading.Event`，在页间安全退出并保留已完成文件。

6. **文件对话框目录记忆**
   - WSL 打开对话框总是从“图片”开始；没有最近目录。
   - 建议：记录上次目录，传给 PowerShell 对话框的 `InitialDirectory`。

7. **状态/错误文案**
   - 有些错误只在状态栏，容易被忽略；建议统一 toast/弹窗策略。

### 7.5 工程质量

1. **单文件过大**
   - `pic_splicer.py` 约 5,796 行，UI/算法/平台代码混在一起。
   - 建议拆分为：`constants.py`、`analysis.py`、`geometry.py`、`imaging.py`、`platform_dialogs.py`、`widgets.py`、`app.py`、`export.py`；注意同步 PyInstaller 入口和构建脚本。

2. **没有自动化测试**
   - 建议先给纯函数加 pytest：
     - `layout_cells`：2/4 版式、不同间距/边距、边界值；
     - `crop_geometry` / `zoom_photo_at`：cover、旋转、锚点缩放；
     - `order_similar`：稳定性/类别顺序/同场景优先；
     - `_save_image`：JPEG/PNG 尺寸、DPI、后缀；
     - `_windows_path_to_wsl`：盘符/UNC/引号；
     - 页面导入/导出的小型集成测试（用几张生成的测试图）。
   - UI 至少做一个 smoke test：构造 app、加载测试图、切页、导出，`root.update()`。

3. **没有类型检查/lint/格式化**
   - 建议引入 `ruff` + `mypy`（或 pyright），至少对纯函数和数据结构做类型约束。

4. **没有版本号/关于窗口**
   - 建议加 `__version__`、标题栏/关于窗口显示版本、构建日期；导出/日志里记录版本。

5. **没有日志**
   - 建议写本地日志（导入失败、导出失败、异常堆栈），方便用户反馈。

6. **版本控制**
   - 已完成：`git init` 并推送到 GitHub（`lingjin1618-eng/picture-splice`）。
   - 建议打 tag `v1.0`；后续每次升级提交并更新本文“版本记录”。

7. **打包**
   - `--onefile` 启动慢、每次解压；可评估 `--onedir` + 安装包；exe 无签名，SmartScreen 提示；没有自动更新。
   - 建议：加版本资源、签名（如有证书）、安装包（Inno Setup/MSIX）；至少提供 zip 版。

### 7.6 已知行为 / 易误解点（不一定是 bug）

| 行为 | 说明 |
|---|---|
| 自动旋转不看手动角度 | `_orientation_mismatch()` 只用原图宽高；手动旋转后仍按原图方向判断，可能叠加成 180°。 |
| 切换版式会重排自动页 | `_reflow_pages()` 只保留“已使用照片”，重新智能排序/分页；手动拖拽形成的自动页布局会丢失。 |
| 外部拖入的照片也会进自动页 | `_finish_import()` 总会调用 `_assign_new_photos()`；拖进自定义页的照片同时进入照片池并参与自动拼接。 |
| 外部拖入自动页不按格子放 | `_on_os_drop()` 在 `preview_kind != "custom"` 时只调用 `_start_import(paths)`，不会放入悬停格子；只有自定义页支持指定格。 |
| 自定义页是副本 | 在自定义页调整取景/旋转不影响自动页；反之亦然。 |
| 删除左侧原照片不删自定义副本 | 自定义页里的 `clone_for_export()` 仍保留路径，导出仍可读取原文件。 |
| 内部拖到满的自定义缩略图 | 只提示已满；外部文件拖入会自动新建同版式页。 |
| 批量导出覆盖同名文件 | 文件夹模式没有覆盖确认。 |
| 部分导出失败 | 有错误时只显示错误，已成功页仍在磁盘上。 |
| 单页“导出全部”用保存对话框 | 只有一张非空页时，`export_all` 也走单文件保存，而不是选文件夹。 |
| 单张预览不应用自动旋转 | 只应用手动旋转；它是“看原图”，不是“看格子效果”。 |
| 预览/导出空槽 | 预览显示虚线，导出纯白。 |
| 最小窗口尺寸 | 1080 × 740；小屏/高缩放下可能过大。 |
| 最大化覆盖任务栏 | 使用屏幕宽高 `+0+0`。 |
| 导出不可取消 | 只有导入有取消按钮。 |
| 原图移动/删除 | 导出时报错。 |
| 不支持 HEIC | 需要额外库；README 已提示先转 JPEG。 |

---

## 8. 常见升级任务的操作指南

### 8.1 新增一种版式（例如 3 张 / 6 张 / 9 张）

需要同步修改：

1. `Page.__post_init__`：允许新的 mode 值（当前只允许 2/4）。
2. `layout_cells()`：为新 mode 计算行列/格子。
3. `slot_label()`：为新 mode 提供位置名称。
4. `_random_page_mode()` / `_build_pages()`：自动混排如何选择新 mode、如何按类别分页。
5. 底部 `SegmentedControl`：增加选项。
6. 预览/缩略图：它们都通过 `compose_page`/`layout_cells`，通常自动适配；检查空槽虚线。
7. 导出命名 `N张` 和 README。
8. 单元测试：尺寸、间距、边距、空槽。

### 8.2 新增纸张/输出尺寸

1. 把 `OUTPUT_WIDTH/OUTPUT_HEIGHT/OUTPUT_DPI` 改成可配置（或新增规格表）。
2. `unit_to_px()` 已按 `OUTPUT_DPI` 计算；确认预览缩放 `scale = width / OUTPUT_WIDTH` 仍成立。
3. `_preview_geometry()` 用 `OUTPUT_WIDTH/OUTPUT_HEIGHT` 比例；改比例后自动适配，但检查 UI 布局。
4. `_save_image()` 的 dpi 参数跟随 `OUTPUT_DPI`。
5. 导出默认命名/README 中的 6 寸描述。
6. 如果支持多规格，建议在设置栏加下拉框，并把规格写进工程文件/导出参数。

### 8.3 新增导出格式或质量设置

1. `SAVE_FILETYPES` 增加类型（如 `("TIFF 图片", "*.tif")`）。
2. `_save_image()` 增加分支：
   - TIFF：`image.save(path, format="TIFF", dpi=(...))`；
   - WebP：注意 quality/lossless；
   - PDF：可能需要 `img2pdf` 或 Pillow 的 PDF 保存（注意多页）。
3. `_pick_save_path()` 的 `default_ext` 和默认文件名扩展名。
4. 质量滑块：新增 `tk.IntVar`，传入 `_start_export` / `_export_worker` / `_save_image`。
5. 更新 README 和导出完成提示。

### 8.4 新增一个设置项

以“每格 fit/cover”为例：

1. `__init__` 增加 `tk.BooleanVar`/`IntVar`。
2. `_build_settings_bar()` 增加控件（TogglePill/SegmentedControl/SoftSlider）。
3. 回调里调用 `schedule_preview()`、`schedule_page_lists()`。
4. 如果影响缩略图，更新 `_page_signature()`。
5. 如果影响导出，在 `export_all`/`export_current_page` 里快照参数并传给 `_start_export`。
6. 如果影响布局，更新 `crop_geometry`/`render_cell`。
7. 更新 README 和验收清单。

### 8.5 增强智能分组

1. `PhotoProfile` 加字段；`analyze_image()` 填充。
2. `photo_kind()` 调整分类。
3. `_group_similarity()` 调整权重/同场景判断。
4. `_order_group()` / `order_similar()` 调整排序策略。
5. `_build_pages()` 调整分页规则。
6. 用真实照片样本做回归：人物/风景/建筑混合、同场景连拍、纯色墙面、沙滩人像；确认不会把明显不同类打散。
7. 保持离线、不上传；如果引入模型，做成可选依赖并提供降级路径。

### 8.6 新增拖拽行为

- 左侧卡片拖拽：`_on_card_press` → `_on_card_motion` → `_on_card_release`；命中测试 `_cell_at_root` / `_custom_page_at_root`；放入逻辑 `_assign_photo_to_slot` / `_assign_photo_to_custom_slot`。
- 预览内取景拖动：`_on_preview_press` → `_on_preview_drag` → `_on_preview_release`；目前和“换位拖拽”是两套，若要支持格间拖动，需要区分“按住拖动是取景还是移动照片”（例如按 Shift/长按，或拖动缩略图把手）。
- 外部 DnD：`_setup_external_dnd`、`_on_os_drop*`、`_register_custom_item_drop`；只在 Windows 生效。

### 8.7 修改间距/边距换算

- 只改 `UNIT_MM`（如实际打印与 Moldiv 不一致）。
- 如果改默认值，改 `DEFAULT_GAP` / `DEFAULT_MARGIN`。
- 如果改滑块上限，改 `MAX_SPACING`。
- 界面毫米显示在 `_refresh_controls()`；README“间距和边距”同步。

### 8.8 增加工程保存/恢复（建议下一步优先做）

建议 JSON 结构（示例）：

```json
{
  "version": 1,
  "settings": {
    "mode": 0,
    "auto_rotate": true,
    "gap": 12,
    "margin": 12
  },
  "photos": [
    {
      "path": "/mnt/c/Users/.../a.jpg",
      "zoom": 1.0,
      "offset_x": 0.0,
      "offset_y": 0.0,
      "rotation": 0
    }
  ],
  "pages": [
    {"mode": 2, "slots": [0, 1]},
    {"mode": 4, "slots": [2, null, null, 3]}
  ],
  "custom_pages": [
    {"mode": 4, "slots": [{"photo": 0, "zoom": 1.2, "offset_x": 0.1, "offset_y": -0.2, "rotation": 90}, null, null, null]}
  ]
}
```

实现要点：

- 照片池按索引/路径引用；自定义页副本需要独立裁剪参数。
- 加载时检查文件存在性，缺失的显示占位/警告。
- 导入完成后重建 `Photo.preview` 和 `profile`（或缓存 profile 到工程文件）。
- 自动页的 `Photo` 引用池对象；自定义页用 `clone_for_export()`。
- 自动保存可用 `root.after` 定时或关键操作后防抖。

### 8.9 增加撤销/重做

- 最小可行：对 `photos` 的分配、`pages`、`custom_pages`、当前选择做深拷贝快照（照片对象本身不必复制，只复制页面槽位和裁剪参数）。
- 在每个会改变状态的方法入口 `push_undo()`；`Ctrl+Z` 恢复。
- 大图/预览不进入快照，避免内存暴涨。
- 优先覆盖：清空、删除页、交换、替换、旋转、复位。

### 8.10 重新打包/发布

```bash
cd /home/gin_sco9/gin_proj/proj/pic_splice
.venv/bin/python -m py_compile pic_splicer.py build_icon.py
timeout 6 ./run.sh          # 看 traceback
./build_windows.sh          # 重新生成桌面 exe
```

构建后到 Windows 桌面验证：启动、添加照片、拖入、导出、最小化/恢复、最大化、资源管理器拖入。

---

## 9. 版本验收清单

每次升级后至少跑一遍：

### 9.1 启动与环境

- [ ] `./run.sh` 正常启动，无 traceback。
- [ ] 中文正常显示（不是 `\uXXXX`）。
- [ ] 窗口无系统边框，标题栏可拖动，双击最大化/还原。
- [ ] 最小化后右下角出现胶囊，点击恢复。
- [ ] 右下角手柄可调整窗口大小，最小尺寸生效。

### 9.2 导入

- [ ] 多选 JPG/PNG/WebP/BMP/TIFF 可导入。
- [ ] 大 JPEG 导入速度可接受，窗口不“未响应”。
- [ ] 进度条/“x / N 张”/取消正常；取消后已导入照片保留。
- [ ] EXIF 旋转照片方向正确。
- [ ] 透明 PNG 铺白底。
- [ ] 导入后原图文件未被修改（大小/时间戳不变）。
- [ ] 损坏文件/不支持格式有提示，不影响其他照片。

### 9.3 自动拼接

- [ ] 2 张版式左右各一张，比例正确。
- [ ] 4 张版式田字格，间距/边距正确。
- [ ] 自动混排出现 2/4 张页，4 张页比例大致合理。
- [ ] 人物/风景/建筑分组顺序正确，同场景照片相邻。
- [ ] “重新随机”只改变组内顺序，不明显打散类别/色系。
- [ ] 切换版式会重排自动页，未使用照片仍保留。
- [ ] 添加新照片先填现有空格，再建新页。

### 9.4 自定义拼接

- [ ] ＋2/＋4 新建空白页，预览显示虚线格。
- [ ] 左侧拖入空格/已占用格：放入/替换正确。
- [ ] 自定义页取景/旋转不影响自动页。
- [ ] 删除自定义页不删除左侧照片。
- [ ] 自定义页为空时 `删除本页`/`×` 行为正确。

### 9.5 取景

- [ ] 滚轮以光标为锚点缩放，100%–300%。
- [ ] 放大后拖动取景，有残影；松手后位置保持。
- [ ] 100% 时拖动提示先放大。
- [ ] 手动旋转 90° 正确，自动+手动叠加符合预期。
- [ ] 复位取景恢复 100% 居中。
- [ ] 缩放滑块与滚轮同步。

### 9.6 自动旋转

- [ ] 横图进 2 张竖格自动转 90°。
- [ ] 竖图进 4 张横格自动转 90°。
- [ ] 方向一致不旋转。
- [ ] 关闭复选框后不自动旋转。
- [ ] 手动旋转可继续叠加。

### 9.7 预览

- [ ] 拼接页圆角/投影/边框正常。
- [ ] 空格虚线、选中格高亮、拖拽悬停高亮正常。
- [ ] 单张照片预览完整显示，带文件名/尺寸。
- [ ] 翻页/页码/本页张数正确。
- [ ] 窗口缩放后预览重新适配。

### 9.8 导出

- [ ] `导出本页` 只导出当前页；单张预览/空白页被拒绝。
- [ ] `导出全部` 多页时选文件夹，命名正确，空页跳过。
- [ ] 输出为 1800 × 1200，300 dpi（可用系统属性或 PIL 验证）。
- [ ] JPEG 质量 98、4:4:4；PNG 无损。
- [ ] 全分辨率重拼：放大看细节不是预览图放大。
- [ ] 空槽为纯白。
- [ ] 导出期间进度正常，界面可响应；导出后内存释放。
- [ ] 原图缺失时提示错误。
- [ ] 单页/多页导出完成提示正确。

### 9.9 平台

- [ ] WSL 下打开/保存/选文件夹对话框可见，路径转换正确。
- [ ] Windows exe 下 Tk 对话框正常。
- [ ] Windows exe 下资源管理器拖入图片到自定义格/缩略图正常。
- [ ] WSL 下不出现外部拖入相关报错。
- [ ] 外部拖入多文件时依次填格，满页自动新建。

### 9.10 构建

- [ ] `./build_windows.sh` 成功，桌面 exe 更新。
- [ ] exe 启动、图标、名称正确（用户可见文案无 `.exe`）。
- [ ] PyInstaller 下 tkinterdnd2 功能可用（或按预期禁用）。
- [ ] exe 导出、拖入、最小化/恢复正常。

---

## 10. 版本记录

| 版本 | 日期 | 内容 |
|---|---|---|
| v1.0 | 2026-11-08 | 当前版本快照：完成 2/4/自动混排、自定义拼接、智能分组、自动旋转、取景编辑、后台导入/导出、全分辨率导出、WSL 原生对话框、Windows 桌面版与外部拖入；整理本总结。 |

**后续升级模板**：

```markdown
| v1.1 | YYYY-MM-DD | 新增……；修复……；影响文件：pic_splicer.py / README.md；验收：第 9 节全部通过。 |
```

---

## 附录 A：常用命令

```bash
cd /home/gin_sco9/gin_proj/proj/pic_splice

# 运行（WSL/Linux）
./run.sh

# 语法/导入检查
.venv/bin/python -m py_compile pic_splicer.py build_icon.py
.venv/bin/python -c "import pic_splicer; print('import OK')"

# 启动 smoke test（124 = 正常超时退出）
timeout 6 ./run.sh

# 生成图标（测试）
.venv/bin/python build_icon.py /tmp/app_icon.ico

# 重新打包 Windows 桌面版
./build_windows.sh

# 查看 .venv 版本
.venv/bin/python --version
.venv/bin/python -c "import PIL, tkinter; print('Pillow', PIL.__version__, 'Tk', tkinter.TkVersion)"
```

## 附录 B：关键路径与命名

| 项 | 值 |
|---|---|
| 项目目录 | `/home/gin_sco9/gin_proj/proj/pic_splice` |
| 本地环境 | `.venv/bin/python`（Python 3.13.18，Pillow 13.0.0，Tk 8.6） |
| Windows exe | `C:\Users\Administrator\Desktop\Picture Splice.exe` |
| 构建入口 | `./build_windows.sh` → `build_windows.ps1` |
| 构建中间目录 | `%TEMP%\pic_splice_build_scripts\`、`%TEMP%\pic_splice_build_<GUID>\` |
| 输出规格 | 1800 × 1200 px @ 300 dpi，3:2 |
| JPEG 设置 | quality 98，subsampling 0（4:4:4） |
| 自动页命名 | `六寸拼接_第NN页_N张.jpg` |
| 自定义页命名 | `自定义拼接_第NN页_N张.jpg` |
| 应用显示名 | Picture Splice（不写 `.exe`） |

## 附录 C：升级时的阅读顺序

1. 本文第 0、6、7 节（目标、约束、待办）。
2. 本文第 4 节（数据模型与算法）。
3. 按第 5 节代码地图定位函数。
4. 只读相关函数及其直接调用者，不必通读 5,796 行。
5. 修改后按第 9 节验收，并更新本文第 10 节。
