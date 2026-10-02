# SliceForge · 文件切片工坊

**Tauri 2 + Vue 3** 桌面应用：把大文件切成若干小切片（便于通过限制单文件
大小的平台逐个发送），接收方校验 SHA-256 完整性后流式合并还原原文件。

切片 / 校验 / 合并逻辑**只此一份**（项目根的 `core.py`）：Tauri 外壳拉起
Python 桥接层 `../backend.py`（NDJSON 子进程协议）执行任务，前端只负责
展示与交互。

## 运行（开发模式）

```bash
# 前置：Rust 1.77+、Node 18+、WebKitGTK 系统库（Ubuntu 24.04 均已具备）
pnpm install
pnpm tauri dev
```

Python 侧只用标准库，系统 `python3` 即可；如需指定解释器或脚本位置：

- `SLICEFORGE_PYTHON`：Python 解释器路径（默认 `python3`）
- `SLICEFORGE_BACKEND`：backend.py 路径（默认取 `src-tauri/../backend.py`）

## 发布构建

```bash
pnpm tauri build      # 产物约 6 MB，前端已内嵌
```

注意：二进制运行时仍需 `backend.py` / `core.py` 与一个 `python3`
（按上面的查找规则定位）；要做成完全自包含的分发包需另行打包，暂未做。

## 项目结构

```
core.py               切片 / SHA-256 / 清单 / 合并（与界面无关，唯一逻辑实现）
backend.py            命令行桥接层（NDJSON 协议，协议见文件头注释）
sliceforge-tauri/     本目录：Tauri 2 外壳 + Vue 3 前端
test_sliceforge.py    core.py 单元测试
test_backend.py       backend.py 协议测试
```

跑全部测试（项目根目录）：`python -m unittest discover -p "test_*.py"`

