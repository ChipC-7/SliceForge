// SliceForge · Tauri 后端
// ========================
// 职责只有两件事：
//   1. 把切片 / 合并任务交给 Python 桥接层（backend.py，NDJSON 协议）执行；
//   2. 把后端逐行输出的事件转发给前端（backend-event），并提供取消。
// 所有切片 / 校验 / 合并逻辑都在 core.py，这里不实现任何业务逻辑。

use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use tauri::{AppHandle, Emitter, State};

/// 当前子进程句柄；None 表示空闲。同一时间只允许一个任务（与旧版界面一致）。
struct TaskState(Mutex<Option<Child>>);

/// 定位 backend.py（core.py 与它同目录，import 依赖这点）。
///
/// 查找顺序：
///   1. 环境变量 SLICEFORGE_BACKEND（显式指定）；
///   2. 开发环境：src-tauri 的上一级目录（CARGO_MANIFEST_DIR 编译期已知）。
/// 打包分发时再改为随包携带，这里先按本地开发约定来。
fn backend_path() -> Result<PathBuf, String> {
    if let Ok(p) = std::env::var("SLICEFORGE_BACKEND") {
        let p = PathBuf::from(p);
        if p.is_file() {
            return Ok(p);
        }
        return Err(format!("SLICEFORGE_BACKEND 指向的文件不存在：{}", p.display()));
    }
    let p = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent() // sliceforge-tauri/
        .and_then(|p| p.parent()) // 项目根（backend.py 与 core.py 所在）
        .ok_or("无法定位项目根目录")?
        .join("backend.py");
    if p.is_file() {
        return Ok(p);
    }
    Err(format!("找不到 backend.py（尝试过 {}）", p.display()))
}

#[tauri::command]
fn start_task(
    app: AppHandle,
    state: State<'_, TaskState>,
    kind: String,
    path: String,
    chunk: Option<String>,
    overwrite: bool,
) -> Result<(), String> {
    let backend = backend_path()?;

    let mut guard = state.0.lock().map_err(|e| e.to_string())?;
    if let Some(child) = guard.as_mut() {
        if child.try_wait().map_err(|e| e.to_string())?.is_none() {
            return Err("已有任务正在执行，请等待完成或先取消。".into());
        }
    }

    let python =
        std::env::var("SLICEFORGE_PYTHON").unwrap_or_else(|_| "python3".to_string());
    let mut cmd = Command::new(python);
    cmd.arg(&backend).arg(&kind);
    match kind.as_str() {
        "split" => {
            cmd.arg("--src").arg(&path);
            if let Some(c) = chunk {
                cmd.arg("--chunk").arg(c);
            }
        }
        "merge" => {
            cmd.arg("--dir").arg(&path);
        }
        other => return Err(format!("未知任务类型：{}", other)),
    }
    if overwrite {
        cmd.arg("--overwrite");
    }
    if let Some(dir) = backend.parent() {
        cmd.current_dir(dir);
    }
    cmd.stdin(Stdio::piped()).stdout(Stdio::piped());

    let mut child = cmd.spawn().map_err(|e| format!("启动后端失败：{}", e))?;
    let stdout = child.stdout.take().ok_or("无法读取后端输出")?;
    *guard = Some(child);
    drop(guard); // 读取线程里不再需要锁

    // 逐行读取 NDJSON 并转发给前端；进程退出后补发 backend-exit
    std::thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for line in reader.lines() {
            let Ok(line) = line else { break };
            if line.trim().is_empty() {
                continue;
            }
            let Ok(value) = serde_json::from_str::<serde_json::Value>(&line) else {
                continue;
            };
            let _ = app.emit("backend-event", value);
        }
        let _ = app.emit("backend-exit", ());
    });
    Ok(())
}

/// 优雅取消：向后端 stdin 写一行 cancel，由 core 的取消检查点完成清理。
#[tauri::command]
fn cancel_task(state: State<'_, TaskState>) -> Result<(), String> {
    let mut guard = state.0.lock().map_err(|e| e.to_string())?;
    if let Some(child) = guard.as_mut() {
        if child.try_wait().map_err(|e| e.to_string())?.is_none() {
            if let Some(stdin) = child.stdin.as_mut() {
                stdin
                    .write_all(b"cancel\n")
                    .and_then(|_| stdin.flush())
                    .map_err(|e| format!("发送取消请求失败：{}", e))?;
            }
        }
    }
    Ok(())
}

/// 一次性预读：plan（切割页）/ inspect（合并页），同步返回单行 JSON。
#[tauri::command]
fn query(kind: String, path: String) -> Result<serde_json::Value, String> {
    let backend = backend_path()?;
    let python =
        std::env::var("SLICEFORGE_PYTHON").unwrap_or_else(|_| "python3".to_string());
    let mut cmd = Command::new(python);
    cmd.arg(&backend).arg(&kind);
    match kind.as_str() {
        "plan" => cmd.arg("--src").arg(&path),
        "inspect" => cmd.arg("--dir").arg(&path),
        other => return Err(format!("未知查询类型：{}", other)),
    };
    if let Some(dir) = backend.parent() {
        cmd.current_dir(dir);
    }
    let output = cmd
        .stdin(Stdio::null())
        .output()
        .map_err(|e| format!("执行预读失败：{}", e))?;
    let text = String::from_utf8_lossy(&output.stdout);
    let line = text
        .lines()
        .rev()
        .find(|l| !l.trim().is_empty())
        .ok_or_else(|| {
            format!(
                "预读没有输出：{}",
                String::from_utf8_lossy(&output.stderr).trim()
            )
        })?;
    serde_json::from_str(line).map_err(|e| format!("预读输出不是合法 JSON：{}", e))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(TaskState(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![start_task, cancel_task, query])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
