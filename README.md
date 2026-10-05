# FragMend · 分合

把大文件切成若干小切片（便于通过限制单文件大小的平台逐个发送）；接收方拿到全部切片后，
先逐片校验 SHA-256 完整性，再流式合并还原原文件。校验不过不出文件，绝不拼出损坏的产物。

桌面界面基于 **Tauri 2 + Vue 3**，切片 / 校验 / 合并逻辑用 Python 实现，且与界面完全解耦。

## 架构

```
Vue 3 界面 (sliceforge-tauri/src)
   │ invoke: start_task / cancel_task / query
   ▼
Rust 外壳 (sliceforge-tauri/src-tauri)   只做进程管理与事件转发，无业务逻辑
   │ spawn 子进程，逐行转发 NDJSON 事件
   ▼
backend.py   命令行桥接层（NDJSON 协议，见文件头注释）
   ▼
core.py      切片 / SHA-256 / 清单 / 流式合并（唯一逻辑实现，零 GUI 依赖）
```

## 功能

- **切割**：每片大小支持 `5M` / `512K` / `2G` / `100MB` 等写法或纯字节数，实时换算提示；
  输出 `文件名.parts/` 文件夹（切片 + `manifest.json` 清单：原始文件名、大小、每片
  大小、总片数、每片 SHA-256）；全程流式读写（4 MB 缓冲），任意大文件不占内存；
  随时取消，未完成切片自动清理。
- **合并**：选 `.parts` 文件夹即预读清单并提示缺片；逐片 SHA-256 校验，缺片 / 损坏 /
  改名都会明确指出是哪一片并中止；先写临时文件再原子替换；合并后核对总大小。

## 运行（开发模式）

```bash
# 前置：Rust 1.77+、Node 18+、WebKitGTK 系统库、python3 ≥ 3.8
cd sliceforge-tauri
pnpm install
pnpm tauri dev
```

Python 侧只用标准库，系统 `python3` 即可；可用环境变量覆盖：

- `SLICEFORGE_PYTHON`：Python 解释器路径（默认 `python3`）
- `SLICEFORGE_BACKEND`：backend.py 路径（默认 `src-tauri/../backend.py`）

## 发布构建

```bash
pnpm tauri build      # 单文件二进制，约 6 MB，前端已内嵌
```

> 注意：二进制运行时仍需 `backend.py` / `core.py` 与一个 `python3`（按上面的查找规则
> 定位）。要做成完全自包含的分发包，需把逻辑移植进 Rust 或打包 Python，暂未做。

## CI 打包（多平台）

推送 `v*` 标签（如 `git tag v1.1.0 && git push origin v1.1.0`）会触发
`.github/workflows/build.yml`：四个平台构建任务各自上传 artifact，再由独立的
publish 任务统一发布正式 Release：

| 平台 | 产物 |
|---|---|
| Linux (ubuntu-22.04 构建) | `.deb`、`.rpm`、`.AppImage` |
| Windows | `.msi`、`.exe`（NSIS） |
| macOS | Intel (x86_64) 与 Apple Silicon (aarch64) 的 `.dmg` |

也可在 Actions 页面手动触发（产物以 workflow artifact 提供）。
`backend.py` / `core.py` 已通过 `bundle.resources` 随包分发，应用按
「环境变量 → 应用资源目录 → 开发目录」的顺序定位它们。

**运行时依赖**：Linux 一般自带 `python3`，开箱即用；Windows / macOS 需要用户
自行安装 Python 3.8+（后续可改为 PyInstaller 打包后端，实现零依赖分发）。

## 测试

```bash
python -m unittest discover -p "test_*.py"     # 80 项：core 73 + 桥接协议 7
```

- `test_sliceforge.py`：大小解析、命名规则、切割、清单、校验、合并、取消
- `test_backend.py`：NDJSON 子进程协议（事件顺序、预读、错误路径、优雅取消与清理）

## 目录

```
core.py               唯一的逻辑实现（零 GUI 依赖）
backend.py            Tauri ↔ core 的 NDJSON 桥接层
sliceforge-tauri/     Tauri 2 外壳 + Vue 3 前端（详见其 README）
```
