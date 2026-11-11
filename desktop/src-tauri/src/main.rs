use rand::{distributions::Alphanumeric, Rng};
use base64::Engine;
use serde::Serialize;
use std::collections::BTreeMap;
#[cfg(test)]
use std::fs;
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::process::CommandExt;
use std::{
    env,
    net::TcpStream,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tauri::{Emitter, Manager};
use tauri_plugin_dialog::DialogExt;
mod credential_cache;
mod accessibility;
mod assistant_notifications;
mod macos_calendar;
mod assistant_media;
mod human_input;
mod ocr;
mod perception_controller;
mod screen_capture;
mod visual_tracking;

struct BackendProcessManager {
    child: Mutex<Option<Child>>,
    startup_lock: Mutex<()>,
    token: String,
    port: u16,
}
const CREDENTIAL_BUNDLE_MIGRATION_MARKER: &str = "__olcr_legacy_migration_v1";
const EXTERNAL_CREDENTIAL_HANDLES: &str =
    include_str!("../../../backend/olcr_api/external_credential_handles.txt");
const KEYCHAIN_BRIDGE_IDENTIFIER: &str = "local.olcr.keychain-bridge";
const KEYCHAIN_BRIDGE_CERTIFICATE_ROOT: &str = "969372f8911d3a2a85ea3339df864961a663f683";
const MAX_CREDENTIAL_BUNDLE_BYTES: usize = 256 * 1024;
// OAuth credentials contain several tokens plus account metadata and can
// legitimately exceed the old 4 KiB API-key-only limit.
const MAX_CREDENTIAL_VALUE_BYTES: usize = 16 * 1024;
// Bound Keychain consent below the desktop bootstrap timeout. A delayed system
// prompt must not keep the window at STARTING until the launcher kills OLCR;
// on timeout, restore_web_credentials records the failure and backend startup
// continues without credentials, leaving credential-gated providers safely
// unavailable until the user retries authorization.
const KEYCHAIN_BRIDGE_TIMEOUT: Duration = Duration::from_secs(120);

fn acquire_single_instance_lock(path: &Path) -> Result<File, String> {
    let file = OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .mode(0o600)
        .open(path)
        .map_err(|error| format!("instance_lock_open_failed: {error}"))?;
    // Keep this descriptor alive for the full desktop lifetime. flock is released
    // by the kernel on normal exit, crash, or forced termination, so stale lock
    // files never prevent a later legitimate launch.
    let result = unsafe {
        libc::flock(
            std::os::fd::AsRawFd::as_raw_fd(&file),
            libc::LOCK_EX | libc::LOCK_NB,
        )
    };
    if result != 0 {
        return Err("another_olcr_desktop_instance_is_running".into());
    }
    Ok(file)
}

fn default_instance_lock_path() -> PathBuf {
    std::env::temp_dir().join(format!("olcr-desktop-{}.lock", unsafe { libc::getuid() }))
}

fn expected_cua_profile_for_executable(executable: &Path) -> Option<PathBuf> {
    let temp_root = Path::new("/private/tmp");
    let target = executable.ancestors().find(|ancestor| {
        ancestor.parent() == Some(temp_root)
            && ancestor
                .file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.starts_with("olcr-e2e-") && name.ends_with("-target"))
    })?;
    let target_name = target.file_name()?.to_str()?.strip_suffix("-target")?;
    Some(target.parent()?.join(format!("{target_name}-profile")))
}

fn validate_isolated_cua_profile(
    executable: &Path,
    app_support: Option<&std::ffi::OsStr>,
) -> Result<(), &'static str> {
    let Some(expected) = expected_cua_profile_for_executable(executable) else {
        return Ok(());
    };
    let Some(actual) = app_support.map(PathBuf::from) else {
        return Err("OLCR_CUA_APP_SUPPORT_REQUIRED_FOR_ISOLATED_BUNDLE");
    };
    if actual != expected {
        return Err("OLCR_CUA_APP_SUPPORT_PATH_MISMATCH");
    }
    Ok(())
}

#[derive(Debug, PartialEq, Eq)]
struct ExistingOlcrProcess {
    pid: u32,
    parent_pid: u32,
    desktop: bool,
}

fn parse_existing_olcr_processes(
    snapshot: &str,
    current_pid: u32,
) -> Result<Vec<ExistingOlcrProcess>, String> {
    let mut parsed = 0usize;
    let mut matches = Vec::new();
    for line in snapshot
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
    {
        let pid_end = line
            .find(char::is_whitespace)
            .ok_or("olcr_process_snapshot_ambiguous")?;
        let remainder = line[pid_end..].trim_start();
        let parent_end = remainder
            .find(char::is_whitespace)
            .ok_or("olcr_process_snapshot_ambiguous")?;
        let command = remainder[parent_end..].trim_start();
        let pid_text = &line[..pid_end];
        let parent_text = &remainder[..parent_end];
        let pid: u32 = pid_text
            .trim()
            .parse()
            .map_err(|_| "olcr_process_snapshot_ambiguous")?;
        let parent_pid: u32 = parent_text
            .trim()
            .parse()
            .map_err(|_| "olcr_process_snapshot_ambiguous")?;
        let command = command.trim();
        if command.is_empty() {
            return Err("olcr_process_snapshot_ambiguous".into());
        }
        parsed += 1;
        if pid == current_pid {
            continue;
        }
        let args: Vec<&str> = command.split_whitespace().collect();
        let desktop = command.contains(".app/Contents/MacOS/olcr-desktop");
        let python_backend = args.first().is_some_and(|value| value.ends_with("/Python"))
            && args
                .windows(3)
                .any(|part| part == ["-m", "uvicorn", "olcr_api.app:app"]);
        if desktop || python_backend {
            matches.push(ExistingOlcrProcess {
                pid,
                parent_pid,
                desktop,
            });
        }
    }
    if parsed == 0 {
        return Err("olcr_process_snapshot_empty".into());
    }
    Ok(matches)
}

fn ensure_no_existing_olcr_processes() -> Result<(), String> {
    let output = Command::new("ps")
        .args(["-axo", "pid=,ppid=,command="])
        .output()
        .map_err(|error| format!("olcr_process_snapshot_failed: {error}"))?;
    if !output.status.success() {
        return Err("olcr_process_snapshot_failed".into());
    }
    let snapshot =
        String::from_utf8(output.stdout).map_err(|_| "olcr_process_snapshot_invalid_utf8")?;
    let found = parse_existing_olcr_processes(&snapshot, std::process::id())?;
    let desktops: Vec<_> = found.iter().filter(|process| process.desktop).collect();
    if !desktops.is_empty() {
        let pids = desktops
            .iter()
            .map(|process| process.pid.to_string())
            .collect::<Vec<_>>()
            .join(",");
        return Err(format!("another_olcr_desktop_is_running pids={pids}"));
    }
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let override_dir = env::var_os("OLCR_BACKEND_DIR").map(PathBuf::from);
    let (backend_dir, _) = resolve_backend_dir(&manifest_dir, override_dir.as_deref())?;
    for process in found {
        if process.parent_pid != 1 || !is_backend_running_from(&process, &backend_dir) {
            return Err(format!(
                "another_olcr_process_is_running pids={}",
                process.pid
            ));
        }
        terminate_orphaned_backend(&process.pid)?;
    }
    Ok(())
}

fn is_backend_running_from(process: &ExistingOlcrProcess, backend_dir: &Path) -> bool {
    let Ok(output) = Command::new("lsof")
        .args(["-a", "-p", &process.pid.to_string(), "-d", "cwd", "-Fn"])
        .output()
    else {
        return false;
    };
    if !output.status.success() {
        return false;
    }
    let expected = backend_dir
        .canonicalize()
        .unwrap_or_else(|_| backend_dir.to_path_buf());
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(|line| line.strip_prefix('n'))
        .any(|path| Path::new(path) == expected)
}

fn terminate_orphaned_backend(pid: &u32) -> Result<(), String> {
    if unsafe { libc::kill(*pid as i32, libc::SIGTERM) } != 0 {
        return Err(format!(
            "orphaned_olcr_backend_termination_failed pid={pid}"
        ));
    }
    for _ in 0..50 {
        if unsafe { libc::kill(*pid as i32, 0) } != 0 {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    Err(format!("orphaned_olcr_backend_did_not_exit pid={pid}"))
}

#[tauri::command]
async fn set_web_credential(provider: String, key: String) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || set_web_credential_sync(provider, key))
        .await
        .map_err(|_| "credential_store_task_failed".to_string())?
}

fn set_web_credential_sync(provider: String, key: String) -> Result<(), String> {
    if !matches!(provider.as_str(), "brave" | "tavily") {
        return Err("unsupported web provider".into());
    }
    if key.trim().is_empty() {
        return Err("API key cannot be empty".into());
    }
    if key.len() > MAX_CREDENTIAL_VALUE_BYTES { return Err("invalid web credential".into()); }
    update_credential_bundle(&format!("web:{provider}"), Some(key.trim()), true)
}
#[tauri::command]
async fn clear_web_credential(provider: String) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || clear_web_credential_sync(provider))
        .await
        .map_err(|_| "credential_store_task_failed".to_string())?
}

fn clear_web_credential_sync(provider: String) -> Result<(), String> {
    if !matches!(provider.as_str(), "brave" | "tavily") {
        return Err("unsupported web provider".into());
    }
    update_credential_bundle(&format!("web:{provider}"), None, true)
}

#[tauri::command]
async fn save_export_file(
    app: tauri::AppHandle,
    file_name: String,
    contents: Vec<u8>,
) -> Result<Option<String>, String> {
    if contents.len() > 64 * 1024 * 1024 {
        return Err("EXPORT_FILE_TOO_LARGE".into());
    }
    let file_name = Path::new(&file_name)
        .file_name()
        .and_then(|name| name.to_str())
        .filter(|name| !name.is_empty())
        .ok_or_else(|| "INVALID_EXPORT_FILE_NAME".to_string())?
        .to_owned();
    tauri::async_runtime::spawn_blocking(move || {
        let selected = app
            .dialog()
            .file()
            .set_file_name(&file_name)
            .blocking_save_file();
        let Some(selected) = selected else { return Ok(None) };
        let path = selected.into_path().map_err(|_| "INVALID_EXPORT_PATH".to_string())?;
        std::fs::write(&path, contents).map_err(|error| format!("EXPORT_WRITE_FAILED: {error}"))?;
        Ok(Some(path.to_string_lossy().into_owned()))
    })
    .await
    .map_err(|_| "EXPORT_SAVE_TASK_FAILED".to_string())?
}

type CredentialBundle = BTreeMap<String, String>;
static CREDENTIAL_CACHE: credential_cache::CredentialCache<CredentialBundle> = credential_cache::CredentialCache::new();
fn is_supported_bundle_key(key: &str) -> bool {
    matches!(
        key,
        "web:brave" | "web:tavily" | CREDENTIAL_BUNDLE_MIGRATION_MARKER
    ) || EXTERNAL_CREDENTIAL_HANDLES
        .lines()
        .any(|allowed| allowed == key)
}
fn parse_credential_bundle(raw: &str) -> Result<CredentialBundle, String> {
    let parsed: CredentialBundle =
        serde_json::from_str(raw).map_err(|_| "credential_bundle_invalid")?;
    Ok(parsed
        .into_iter()
        .filter(|(key, value)| is_supported_bundle_key(key) && !value.is_empty())
        .collect())
}
fn run_captured_command_with_timeout(
    command: &mut Command,
    timeout: Duration,
    max_stdout_bytes: usize,
    execution_error: &'static str,
    timeout_error: &'static str,
) -> Result<std::process::Output, String> {
    run_captured_command_with_input(command, timeout, max_stdout_bytes, execution_error, timeout_error, None)
}
fn run_captured_command_with_input(
    command: &mut Command, timeout: Duration, max_stdout_bytes: usize,
    execution_error: &'static str, timeout_error: &'static str, input: Option<&[u8]>,
) -> Result<std::process::Output, String> {
    command.stdin(if input.is_some() { Stdio::piped() } else { Stdio::null() });
    let mut child = command
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| execution_error)?;
    if let Some(input) = input {
        let written = child.stdin.take().ok_or(execution_error).and_then(|mut pipe| pipe.write_all(input).map_err(|_| execution_error));
        if written.is_err() { let _=child.kill();let _=child.wait();return Err(execution_error.into()); }
    }
    let Some(stdout) = child.stdout.take() else {
        let _ = child.kill();
        let _ = child.wait();
        return Err(execution_error.into());
    };
    let stdout_reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        stdout
            .take(max_stdout_bytes.saturating_add(1) as u64)
            .read_to_end(&mut bytes)
            .map(|_| bytes)
    });
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let stdout = stdout_reader
                    .join()
                    .map_err(|_| execution_error)?
                    .map_err(|_| execution_error)?;
                return Ok(std::process::Output {
                    status,
                    stdout,
                    stderr: Vec::new(),
                });
            }
            Ok(None) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(25));
            }
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = stdout_reader.join();
                return Err(timeout_error.into());
            }
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = stdout_reader.join();
                return Err(execution_error.into());
            }
        }
    }
}
fn read_credential_bundle() -> Result<CredentialBundle, String> {
    CREDENTIAL_CACHE.read(false, || invoke_credential_bridge(false, None))
}
fn update_credential_bundle(key: &str, value: Option<&str>, interactive: bool) -> Result<(), String> {
    CREDENTIAL_CACHE.update(|| invoke_credential_bridge(interactive, Some(serde_json::json!({"key":key,"value":value}))))
}
fn invoke_credential_bridge(interactive: bool, update: Option<serde_json::Value>) -> Result<CredentialBundle, String> {
    let bridge_path = env::var_os("OLCR_KEYCHAIN_BRIDGE_PATH")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            env::var_os("HOME")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("/"))
                .join("Library/Application Support/OLCR/KeychainBridge/olcr-keychain-bridge")
        });
    if !bridge_path.is_absolute() {
        return Err("credential_bundle_bridge_path_invalid".into());
    }
    let bridge_path = bridge_path
        .canonicalize()
        .map_err(|_| "credential_bundle_bridge_unavailable")?;
    let requirement = format!(
        "=identifier \"{KEYCHAIN_BRIDGE_IDENTIFIER}\" and certificate root = H\"{KEYCHAIN_BRIDGE_CERTIFICATE_ROOT}\""
    );
    let signature = Command::new("/usr/bin/codesign")
        .arg("--verify")
        .arg("--strict")
        .arg(format!("--requirements={requirement}"))
        .arg(&bridge_path)
        .output()
        .map_err(|_| "credential_bundle_bridge_verification_failed")?;
    if !signature.status.success() {
        return Err("credential_bundle_bridge_verification_failed".into());
    }

    let allowlist = std::iter::once("web:brave")
        .chain(std::iter::once("web:tavily"))
        .chain(
            EXTERNAL_CREDENTIAL_HANDLES
                .lines()
                .filter(|handle| !handle.is_empty()),
        )
        .collect::<Vec<_>>();
    let allowlist_json =
        serde_json::to_string(&allowlist).map_err(|_| "credential_bundle_allowlist_invalid")?;
    let input = update.as_ref().map(serde_json::to_vec).transpose().map_err(|_| "credential_bundle_serialize_failed")?;
    let mut command = Command::new(&bridge_path);
    command.arg(format!("--allowlist-json={allowlist_json}"));
    command.arg(if interactive { "--interaction=allow" } else { "--interaction=forbid" });
    if input.is_some() { command.arg("--update-stdin"); }
    let output = run_captured_command_with_input(
        &mut command,
        KEYCHAIN_BRIDGE_TIMEOUT,
        MAX_CREDENTIAL_BUNDLE_BYTES,
        "credential_bundle_bridge_execution_failed",
        "credential_bundle_bridge_timeout",
        input.as_deref(),
    )?;
    if output.status.code() == Some(2) { return Err("credential_bundle_authorization_required".into()); }
    if !output.status.success() || output.stdout.len() > MAX_CREDENTIAL_BUNDLE_BYTES {
        return Err("credential_bundle_bridge_read_failed".into());
    }
    let raw = std::str::from_utf8(&output.stdout)
        .map_err(|_| "credential_bundle_bridge_output_invalid")?;
    parse_credential_bundle(raw)
}
#[tauri::command]
async fn set_external_credential(handle: String, value: String, interactive: Option<bool>) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || set_external_credential_sync(handle, value, interactive.unwrap_or(true)))
        .await
        .map_err(|_| "credential_store_task_failed".to_string())?
}

fn set_external_credential_sync(handle: String, value: String, interactive: bool) -> Result<(), String> {
    validate_external_credential_input(&handle, &value)?;
    update_credential_bundle(&handle, Some(value.trim()), interactive)
}
fn validate_external_credential_input(handle: &str, value: &str) -> Result<(), String> {
    if value.trim().is_empty() || value.len() > MAX_CREDENTIAL_VALUE_BYTES {
        return Err("invalid external credential".into());
    }
    if !is_supported_bundle_key(handle) || handle.starts_with("web:") || handle == CREDENTIAL_BUNDLE_MIGRATION_MARKER {
        return Err("unsupported external credential".into());
    }
    Ok(())
}
fn restore_web_credentials() -> Result<(usize, usize), String> {
    // Computer-use and headless verification must not block on a macOS
    // Keychain consent dialog. Production launches restore by default; a
    // controlled test may opt out explicitly through its launch environment.
    if skip_keychain_restore() {
        return Ok((0, 0));
    }
    let bundle = read_credential_bundle()?;
    let mut web_restored = 0;
    let mut external_restored = 0;
    for (provider, name) in [
        ("brave", "OLCR_WEB_BRAVE_API_KEY"),
        ("tavily", "OLCR_WEB_TAVILY_API_KEY"),
    ] {
        if let Some(value) = bundle.get(&format!("web:{provider}")) {
            std::env::set_var(name, value);
            web_restored += 1;
        }
    }
    for handle in EXTERNAL_CREDENTIAL_HANDLES
        .lines()
        .filter(|handle| !handle.is_empty())
    {
        if let Some(value) = bundle.get(handle) {
            std::env::set_var(handle, value);
            external_restored += 1;
        }
    }
    Ok((web_restored, external_restored))
}
fn skip_keychain_restore_from(value: Option<&str>) -> bool {
    value == Some("true")
}
fn skip_keychain_restore() -> bool {
    skip_keychain_restore_from(env::var("OLCR_SKIP_KEYCHAIN_RESTORE").ok().as_deref())
}
fn startup_diagnostic(message: &str) {
    eprintln!("{message}");
    let path = env::var("OLCR_LAUNCH_DIAGNOSTIC_LOG").ok()
        .or_else(|| option_env!("OLCR_LAUNCH_DIAGNOSTIC_LOG").map(str::to_owned));
    if let Some(path) = path {
        append_startup_diagnostic(Path::new(&path), message);
    }
}
fn append_startup_diagnostic(path: &Path, message: &str) {
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(file, "{message}");
    }
}
fn backend_startup_timeout() -> Duration {
    let configured = env::var("OLCR_BACKEND_STARTUP_TIMEOUT_SECONDS")
        .ok()
        .or_else(|| option_env!("OLCR_BACKEND_STARTUP_TIMEOUT_SECONDS").map(str::to_owned));
    backend_startup_timeout_from(configured.as_deref())
}
fn backend_startup_timeout_from(configured: Option<&str>) -> Duration {
    let seconds = configured
        .and_then(|value| value.parse::<u64>().ok())
        .filter(|seconds| (5..=300).contains(seconds));
    Duration::from_secs(seconds.unwrap_or(180))
}
#[tauri::command]
fn open_external_url(url: String) -> Result<(), String> {
    let local_path = url.starts_with("/Users/")
        || url.starts_with("/private/")
        || url.starts_with("/tmp/")
        || url.starts_with("/var/")
        || url.starts_with("/Volumes/");
    let valid = (url.starts_with("https://")
        || url.starts_with("http://")
        || url.starts_with("file://")
        || local_path)
        && !url
            .chars()
            .any(|c| c.is_control() || c == '\n' || c == '\r');
    if !valid {
        return Err("external_url_scheme_not_allowed".into());
    }
    std::process::Command::new("open")
        .arg(&url)
        .status()
        .map_err(|e| format!("open_external_url_failed: {e}"))
        .and_then(|status| {
            if status.success() {
                Ok(())
            } else {
                Err(format!("open_external_url_failed: {status}"))
            }
        })
}
#[tauri::command]
fn open_local_file(path: String) -> Result<(), String> {
    let valid_root = ["/Users/", "/private/", "/tmp/", "/var/", "/Volumes/"]
        .iter()
        .any(|root| path.starts_with(root));
    if !valid_root || path.chars().any(|c| c.is_control()) {
        return Err("local_file_path_not_allowed".into());
    }
    let path = std::path::Path::new(&path)
        .canonicalize()
        .map_err(|e| format!("local_file_not_found: {e}"))?;
    std::process::Command::new("open")
        .arg(&path)
        .status()
        .map_err(|e| format!("open_local_file_failed: {e}"))
        .and_then(|status| {
            if status.success() {
                Ok(())
            } else {
                Err(format!("open_local_file_failed: {status}"))
            }
        })
}
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct DroppedFile {
    #[serde(skip_serializing_if = "Option::is_none")]
    kind: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    directory_path: Option<String>,
    name: String,
    mime_type: String,
    content: String,
    data_url: Option<String>,
    bytes_base64: String,
}
fn base64_encode(bytes: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut output = String::with_capacity((bytes.len() + 2) / 3 * 4);
    for chunk in bytes.chunks(3) {
        let a = chunk[0];
        let b = *chunk.get(1).unwrap_or(&0);
        let c = *chunk.get(2).unwrap_or(&0);
        output.push(TABLE[(a >> 2) as usize] as char);
        output.push(TABLE[(((a & 3) << 4) | (b >> 4)) as usize] as char);
        output.push(if chunk.len() > 1 {
            TABLE[(((b & 15) << 2) | (c >> 6)) as usize] as char
        } else {
            '='
        });
        output.push(if chunk.len() > 2 {
            TABLE[(c & 63) as usize] as char
        } else {
            '='
        });
    }
    output
}
#[tauri::command]
fn read_dropped_file(path: String) -> Result<DroppedFile, String> {
    let candidate = PathBuf::from(&path);
    let metadata =
        std::fs::metadata(&candidate).map_err(|_| "dropped_file_unavailable".to_string())?;
    if metadata.is_dir() {
        let canonical = candidate.canonicalize().map_err(|_| "dropped_folder_unavailable".to_string())?;
        let name = canonical.file_name().and_then(|value| value.to_str()).unwrap_or("Folder").to_string();
        return Ok(DroppedFile {
            kind: Some("folder".to_string()),
            directory_path: Some(canonical.to_string_lossy().into_owned()),
            name,
            mime_type: "application/x-olcr-working-folder".to_string(),
            content: String::new(), data_url: None, bytes_base64: String::new(),
        });
    }
    if !metadata.is_file() {
        return Err("dropped_path_is_not_a_file".into());
    }
    if metadata.len() == 0 {
        return Err("dropped_file_empty".into());
    }
    if metadata.len() > 50_000_000 {
        return Err("dropped_file_too_large".into());
    }
    let name = candidate
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or_else(|| "dropped_file_name_invalid".to_string())?
        .to_string();
    let extension = candidate
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    let mime_type = match extension.as_str() {
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "webp" => "image/webp",
        "gif" => "image/gif",
        _ => "application/octet-stream",
    }
    .to_string();
    let bytes = std::fs::read(&candidate).map_err(|_| "dropped_file_unreadable".to_string())?;
    let bytes_base64 = base64_encode(&bytes);
    if mime_type.starts_with("image/") {
        return Ok(DroppedFile {
            kind: None, directory_path: None,
            name,
            mime_type: mime_type.clone(),
            content: String::new(),
            data_url: Some(format!("data:{mime_type};base64,{bytes_base64}")),
            bytes_base64,
        });
    }
    let content = String::from_utf8(bytes).unwrap_or_default();
    Ok(DroppedFile {
        kind: None, directory_path: None,
        name,
        mime_type,
        content,
        data_url: None,
        bytes_base64,
    })
}
#[derive(Serialize)]
struct BackendConfig {
    api_url: String,
    session_token: String,
}

fn authenticated_ready(port: u16, token: &str) -> bool {
    let Ok(mut stream) = TcpStream::connect(("127.0.0.1", port)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(1)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(1)));
    let request = format!(
        "GET /api/projects HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-OLCR-Session: {token}\r\nConnection: close\r\n\r\n"
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut response = [0; 128];
    match stream.read(&mut response) {
        Ok(n) => response[..n].starts_with(b"HTTP/1.1 200 "),
        Err(_) => false,
    }
}

fn valid_backend_dir(path: &Path) -> bool {
    path.is_dir() && path.join("olcr_api").is_dir() && path.join("olcr_cli").is_dir()
}

fn resolve_backend_dir(
    manifest_dir: &Path,
    override_dir: Option<&Path>,
) -> Result<(PathBuf, &'static str), String> {
    if let Some(path) = override_dir {
        if valid_backend_dir(path) {
            return Ok((path.to_path_buf(), "override"));
        }
        return Err("backend_directory_invalid".into());
    }
    let repository_root = manifest_dir
        .parent()
        .and_then(Path::parent)
        .ok_or_else(|| "backend_directory_missing".to_string())?;
    let candidate = repository_root.join("backend");
    if valid_backend_dir(&candidate) {
        Ok((candidate, "manifest"))
    } else {
        Err("backend_directory_missing".into())
    }
}

fn repository_root(manifest_dir: &Path) -> Result<PathBuf, String> {
    manifest_dir
        .parent()
        .and_then(Path::parent)
        .map(Path::to_path_buf)
        .ok_or_else(|| "repository_root_missing".to_string())
}

fn resolve_python(
    manifest_dir: &Path,
    override_path: Option<&Path>,
) -> Result<(PathBuf, &'static str), String> {
    if let Some(path) = override_path {
        if path.is_file() {
            return Ok((path.to_path_buf(), "override"));
        }
        return Err("python_runtime_invalid".into());
    }
    let root = repository_root(manifest_dir)?;
    for (relative, source) in [
        (".venv312/bin/python", "manifest_venv312"),
        (".venv/bin/python", "manifest_venv"),
    ] {
        let candidate = root.join(relative);
        if candidate.is_file() {
            return Ok((candidate, source));
        }
    }
    Err("python_runtime_missing".into())
}

impl BackendProcessManager {
    fn sync_keychain_credentials(&self, authorize: bool) -> Result<usize, String> {
        self.ensure_started()?;
        let bundle = CREDENTIAL_CACHE.read(authorize, || invoke_credential_bridge(authorize, None))?;
        let credentials = bundle
            .into_iter()
            .filter(|(handle, _)| {
                handle == "web:brave"
                    || handle == "web:tavily"
                    || EXTERNAL_CREDENTIAL_HANDLES
                        .lines()
                        .any(|allowed| allowed == handle.as_str())
            })
            .collect::<BTreeMap<_, _>>();
        let credential_count = credentials.len();
        let body = serde_json::to_vec(&serde_json::json!({"credentials": credentials}))
            .map_err(|_| "credential_restore_request_invalid")?;
        let mut stream = TcpStream::connect(("127.0.0.1", self.port))
            .map_err(|_| "credential_restore_backend_unavailable")?;
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .map_err(|_| "credential_restore_backend_unavailable")?;
        stream
            .set_write_timeout(Some(Duration::from_secs(5)))
            .map_err(|_| "credential_restore_backend_unavailable")?;
        write!(
            stream,
            "PUT /api/external-tools/keychain-restore HTTP/1.1\r\nHost: 127.0.0.1\r\nX-OLCR-Session: {}\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: {}\r\n\r\n",
            self.token,
            body.len()
        )
        .and_then(|_| stream.write_all(&body))
        .map_err(|_| "credential_restore_backend_unavailable")?;
        let mut response = Vec::new();
        stream
            .take(4096)
            .read_to_end(&mut response)
            .map_err(|_| "credential_restore_backend_unavailable")?;
        let status = std::str::from_utf8(&response)
            .ok()
            .and_then(|response| response.lines().next())
            .unwrap_or_default();
        if !status.starts_with("HTTP/1.1 200 ") && !status.starts_with("HTTP/1.0 200 ") {
            return Err("credential_restore_backend_rejected".into());
        }
        Ok(credential_count)
    }

    fn ensure_started(&self) -> Result<(), String> {
        let _startup_guard = self
            .startup_lock
            .lock()
            .map_err(|_| "backend startup lock unavailable")?;
        {
            let mut child = self
                .child
                .lock()
                .map_err(|_| "backend manager unavailable")?;
            if let Some(process) = child.as_mut() {
                match process
                    .try_wait()
                    .map_err(|_| "backend_process_status_unavailable".to_string())?
                {
                    None => return Ok(()),
                    Some(_) => {
                        *child = None;
                    }
                }
            }
        }
        startup_diagnostic("KEYCHAIN_RESTORE started=true source=stable_signed_keychain_bridge");
        match restore_web_credentials() {
            Ok((web_restored, external_restored)) => startup_diagnostic(&format!(
                "KEYCHAIN_RESTORE completed=true web_credentials={web_restored} external_credentials={external_restored} source=stable_signed_keychain_bridge"
            )),
            Err(reason) => {
                startup_diagnostic(&format!("KEYCHAIN_RESTORE completed=false reason={reason}"))
            }
        }
        self.start()
    }
    fn start(&self) -> Result<(), String> {
        if self
            .child
            .lock()
            .map_err(|_| "backend manager unavailable")?
            .is_some()
        {
            return Ok(());
        }
        let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let python_override = env::var_os("OLCR_PYTHON").map(PathBuf::from);
        let (python, python_source) = resolve_python(&manifest_dir, python_override.as_deref())?;
        let override_dir = env::var_os("OLCR_BACKEND_DIR").map(PathBuf::from);
        let (backend_dir, resolution_source) =
            resolve_backend_dir(&manifest_dir, override_dir.as_deref())?;
        let root = repository_root(&manifest_dir)?;
        startup_diagnostic(&format!("backend_resolution_source={resolution_source}"));
        startup_diagnostic("backend_directory_valid=true");
        startup_diagnostic(&format!("python_resolution_source={python_source}"));
        let node_root = env::var_os("OLCR_NODE_MCP_RUNTIME_ROOT")
            .map(PathBuf::from)
            .unwrap_or_else(|| backend_dir.parent().unwrap().to_path_buf());
        let node_root = node_root.canonicalize().unwrap_or(node_root);
        startup_diagnostic(&format!("DESKTOP_NODE_MCP_ROOT={}", node_root.display()));
        startup_diagnostic("OPTIONAL_COMPONENTS_DELIVERY=PINNED_DOWNLOADS");
        let laya_cache = env::var_os("OLCR_LAYA_CACHE")
            .map(PathBuf::from)
            .unwrap_or_else(|| root.join("models/laya"));
        let manifest = env::var_os("OLCR_MODEL_MANIFEST")
            .map(PathBuf::from)
            .unwrap_or_else(|| root.join("models/model_manifest.json"));
        // GUI E2E uses a unique build ID for each isolated profile. It is run
        // metadata, so resolve it from the Launch Services runtime environment
        // rather than baking a different ID into each app executable. Keep the
        // compile-time value as a release/dev fallback only.
        let build_id = env::var("OLCR_API_REPAIR_BUILD_ID")
            .ok()
            .filter(|value| !value.trim().is_empty())
            .or_else(|| option_env!("OLCR_API_REPAIR_BUILD_ID").map(str::to_owned))
            .unwrap_or_else(|| "olcr-japanese-api-routing-evidence-r46".to_string());
        let bundled_reader = env::current_exe().ok().and_then(|path| path.parent().and_then(|folder| folder.parent()).map(|contents| contents.join("Resources/knowledge/olcr-knowledge-reader")));
        let knowledge_reader = bundled_reader.filter(|path| path.is_file()).unwrap_or_else(|| manifest_dir.join("binaries/olcr-knowledge-reader"));
        let bundled_calendar = env::current_exe().ok().and_then(|path| path.parent().and_then(|folder| folder.parent()).map(|contents| contents.join("Resources/calendar/olcr-macos-calendar")));
        let calendar_helper = bundled_calendar.filter(|path| path.is_file()).unwrap_or_else(|| manifest_dir.join("binaries/olcr-macos-calendar"));
        let bundled_mail = env::current_exe().ok().and_then(|path| path.parent().and_then(|folder| folder.parent()).map(|contents| contents.join("Resources/mail/olcr-macos-mail")));
        let mail_helper = bundled_mail.filter(|path| path.is_file()).unwrap_or_else(|| manifest_dir.join("binaries/olcr-macos-mail"));
        let mut command = Command::new(python);
        command
            .args([
                "-m",
                "uvicorn",
                "olcr_api.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                &self.port.to_string(),
                "--log-level",
                "warning",
            ])
            .current_dir(&backend_dir)
            .env("OLCR_GUI_SESSION_TOKEN", &self.token)
            .env("OLCR_BACKEND_PORT", self.port.to_string())
            .env("OLCR_BACKEND_DIR", &backend_dir)
            .env("OLCR_KNOWLEDGE_READER", &knowledge_reader)
            .env("OLCR_MACOS_CALENDAR_HELPER", &calendar_helper)
            .env("OLCR_MACOS_MAIL_HELPER", &mail_helper)
            .env("OLCR_API_REPAIR_BUILD_ID", &build_id)
            .env("PYTHONUNBUFFERED", "1");
        // Desktop OAuth client IDs are public app metadata. Release builders
        // can bake the OLCR-owned client ID into the app so regular users can
        // start Google sign-in without visiting Cloud Console.
        if let Some(client_id) = env::var_os("OLCR_CONNECTOR_GOOGLE_CLIENT_ID")
            .or_else(|| option_env!("OLCR_CONNECTOR_GOOGLE_CLIENT_ID").map(std::ffi::OsString::from))
        {
            command.env("OLCR_CONNECTOR_GOOGLE_CLIENT_ID", client_id);
        }
        // An explicit developer override remains authoritative. Normal starts
        // let the backend resolve profile-owned, downloaded components.
        if let Some(node_override) = env::var_os("OLCR_NODE_MCP_RUNTIME_ROOT") {
            command.env("OLCR_NODE_MCP_RUNTIME_ROOT", node_override);
        }
        // Launch Services starts GUI apps with a low soft descriptor limit
        // (commonly 256 on macOS). The backend loads local model runtimes and
        // browser tooling that need more descriptors; raise only the child
        // backend's limit before exec so the desktop app's limit is unchanged.
        unsafe {
            command.pre_exec(|| {
                let mut limits = std::mem::zeroed::<libc::rlimit>();
                if libc::getrlimit(libc::RLIMIT_NOFILE, &mut limits) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                let desired = 4096 as libc::rlim_t;
                if limits.rlim_cur < desired {
                    limits.rlim_cur = desired.min(limits.rlim_max);
                    if libc::setrlimit(libc::RLIMIT_NOFILE, &limits) != 0 {
                        return Err(std::io::Error::last_os_error());
                    }
                }
                Ok(())
            });
        }
        if laya_cache.join("multilingual/model.safetensors").is_file() {
            command.env("OLCR_LAYA_CACHE", &laya_cache);
            command.env("OLCR_LAYA_CHECKPOINTS_READY", "true");
        }
        if manifest.is_file() {
            command.env("OLCR_MODEL_MANIFEST", &manifest);
        }
        let mut child = command
            // Backend diagnostics use stderr. Discard library stdout so an
            // unread pipe cannot fill and stall startup or a coding task.
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| format!("Could not start OLCR backend: {e}"))?;
        if let Some(stderr) = child.stderr.take() {
            std::thread::spawn(move || {
                for line in BufReader::new(stderr).lines().flatten() {
                    startup_diagnostic(&format!("OLCR_BACKEND_STDERR {line}"));
                }
            });
        }
        startup_diagnostic("backend_spawn_started=true");
        startup_diagnostic("backend_child_file_descriptor_soft_limit=4096");
        startup_diagnostic(&format!("backend_spawn_pid={}", child.id()));
        let mut ready = false;
        let startup_deadline = Instant::now() + backend_startup_timeout();
        while Instant::now() < startup_deadline {
            std::thread::sleep(Duration::from_millis(200));
            if let Some(status) = child.try_wait().map_err(|e| e.to_string())? {
                let error = format!("OLCR backend exited during startup: {status}");
                startup_diagnostic(&error);
                return Err(error);
            }
            if authenticated_ready(self.port, &self.token) {
                ready = true;
                break;
            }
        }
        if !ready {
            let _ = child.kill();
            let _ = child.wait();
            let error = "backend_api_ready_timeout";
            startup_diagnostic(error);
            return Err(error.into());
        }
        startup_diagnostic("backend_api_ready=true");
        *self
            .child
            .lock()
            .map_err(|_| "backend manager unavailable")? = Some(child);
        Ok(())
    }
    fn stop(&self) {
        if let Ok(mut slot) = self.child.lock() {
            if let Some(mut child) = slot.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}
impl Drop for BackendProcessManager {
    fn drop(&mut self) {
        self.stop()
    }
}
fn backend_config_for_manager(state: &BackendProcessManager) -> Result<BackendConfig, String> {
    let mut child = state
        .child
        .lock()
        .map_err(|_| "backend manager unavailable".to_string())?;
    let Some(process) = child.as_mut() else {
        return Err("backend_process_unavailable".into());
    };
    match process
        .try_wait()
        .map_err(|_| "backend_process_status_unavailable".to_string())?
    {
        Some(_) => {
            *child = None;
            return Err("backend_process_unavailable".into());
        }
        None => {}
    }
    let api_url = format!("http://127.0.0.1:{}/api", state.port);
    eprintln!(
        "FRONTEND_BACKEND_CONFIG_REQUESTED=true BACKEND_HOST=127.0.0.1 BACKEND_PORT={} FRONTEND_BACKEND_BASE={}",
        state.port, api_url
    );
    Ok(BackendConfig {
        api_url,
        session_token: state.token.clone(),
    })
}
#[tauri::command]
async fn backend_config(
    state: tauri::State<'_, Arc<BackendProcessManager>>,
) -> Result<BackendConfig, String> {
    if option_env!("OLCR_UI_ONLY_BUILD") == Some("models") {
        return Err("UI_ONLY_BUILD_BACKEND_DISABLED".into());
    }
    startup_diagnostic("FRONTEND_BACKEND_CONFIG_INVOKE_RECEIVED=true");
    let manager = Arc::clone(state.inner());
    tauri::async_runtime::spawn_blocking(move || {
        manager.ensure_started()?;
        backend_config_for_manager(&manager)
    })
    .await
    .map_err(|error| format!("backend_startup_worker_failed: {error}"))?
}

#[derive(Serialize, Clone)]
struct MacSpeechEvent {
    session_id: String,
    text: String,
}

struct TemporaryAudioFile(PathBuf);
impl Drop for TemporaryAudioFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

#[tauri::command]
async fn transcribe_macos_audio(
    app: tauri::AppHandle,
    audio_base64: String,
    language: String,
    session_id: String,
) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        if !matches!(language.as_str(), "ja" | "en")
            || session_id.is_empty()
            || session_id.len() > 64
            || !session_id.chars().all(|character| character.is_ascii_alphanumeric())
        {
            return Err("MACOS_SPEECH_REQUEST_INVALID".into());
        }
        let wav = base64::engine::general_purpose::STANDARD
            .decode(&audio_base64)
            .map_err(|_| "MACOS_SPEECH_AUDIO_INVALID".to_string())?;
        if wav.len() < 44 || wav.len() > 400_000 || &wav[..4] != b"RIFF" || &wav[8..12] != b"WAVE" {
            return Err("MACOS_SPEECH_AUDIO_INVALID".into());
        }
        #[cfg(not(target_os = "macos"))]
        {
            let _ = (app, wav);
            return Err("MACOS_SPEECH_UNAVAILABLE".into());
        }
        #[cfg(target_os = "macos")]
        {
            let id: String = rand::thread_rng()
                .sample_iter(&Alphanumeric)
                .take(24)
                .map(char::from)
                .collect();
            let path = env::temp_dir().join(format!("olcr-speech-{id}.wav"));
            let mut options = OpenOptions::new();
            options.write(true).create_new(true).mode(0o600);
            let mut file = options.open(&path).map_err(|_| "MACOS_SPEECH_TEMPFILE_FAILED".to_string())?;
            let temporary_audio = TemporaryAudioFile(path.clone());
            file.write_all(&wav).map_err(|_| "MACOS_SPEECH_TEMPFILE_FAILED".to_string())?;
            drop(file);
            let resources = app.path().resource_dir().map_err(|_| "MACOS_SPEECH_HELPER_MISSING")?;
            let helper = resources.join("speech/olcr-macos-speech");
            if !helper.is_file() {
                return Err("MACOS_SPEECH_HELPER_MISSING".into());
            }
            let mut child = Command::new(helper)
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .spawn()
                .map_err(|_| "MACOS_SPEECH_HELPER_START_FAILED")?;
            let payload = serde_json::json!({"audio_path": path, "language": language});
            if let Some(mut stdin) = child.stdin.take() {
                serde_json::to_writer(&mut stdin, &payload).map_err(|_| "MACOS_SPEECH_HELPER_START_FAILED")?;
                stdin.write_all(b"\n").map_err(|_| "MACOS_SPEECH_HELPER_START_FAILED")?;
            }
            let stdout = child.stdout.take().ok_or("MACOS_SPEECH_HELPER_START_FAILED")?;
            let mut transcript = String::new();
            for line in BufReader::new(stdout).lines().take(520) {
                let line = line.map_err(|_| "MACOS_SPEECH_RECOGNITION_FAILED")?;
                let event: serde_json::Value = serde_json::from_str(&line)
                    .map_err(|_| "MACOS_SPEECH_HELPER_RESPONSE_INVALID")?;
                match event.get("type").and_then(|value| value.as_str()) {
                    Some("text") | Some("done") => {
                        transcript = event.get("text").and_then(|value| value.as_str()).unwrap_or("").to_string();
                        app.emit("olcr-macos-speech-partial", MacSpeechEvent {
                            session_id: session_id.clone(),
                            text: transcript.clone(),
                        }).map_err(|_| "MACOS_SPEECH_EVENT_FAILED")?;
                    }
                    Some("error") => {
                        let error = event.get("error").and_then(|value| value.as_str()).unwrap_or("MACOS_SPEECH_RECOGNITION_FAILED");
                        let _ = child.kill();
                        return Err(error.to_string());
                    }
                    _ => return Err("MACOS_SPEECH_HELPER_RESPONSE_INVALID".into()),
                }
            }
            let status = child.wait().map_err(|_| "MACOS_SPEECH_RECOGNITION_FAILED")?;
            drop(temporary_audio);
            if !status.success() { return Err("MACOS_SPEECH_RECOGNITION_FAILED".into()); }
            if transcript.is_empty() { return Err("MACOS_SPEECH_NO_SPEECH".into()); }
            Ok(transcript)
        }
    })
    .await
    .map_err(|_| "MACOS_SPEECH_WORKER_FAILED".to_string())?
}

#[tauri::command]
async fn restore_external_api_credentials(
    state: tauri::State<'_, Arc<BackendProcessManager>>,
    authorize: Option<bool>,
) -> Result<usize, String> {
    let manager = Arc::clone(state.inner());
    tauri::async_runtime::spawn_blocking(move || manager.sync_keychain_credentials(authorize.unwrap_or(false)))
        .await
        .map_err(|_| "credential_restore_task_failed".to_string())?
}
#[tauri::command]
async fn start_screen_capture(
    app: tauri::AppHandle,
    target_application: Option<screen_capture::CaptureTargetApplication>,
    state: tauri::State<'_, Arc<screen_capture::ScreenCaptureManager>>,
    perception: tauri::State<'_, Arc<Mutex<perception_controller::PerceptionController>>>,
) -> Result<screen_capture::ScreenCaptureStatus, String> {
    let capture = Arc::clone(state.inner());
    let perception = Arc::clone(perception.inner());
    tauri::async_runtime::spawn_blocking(move || {
        capture.start(app, target_application)?;
        // Read after stream startup so policy signals received while ScreenCaptureKit
        // is starting cannot be lost behind a stale initial snapshot.
        let policy = perception
            .lock()
            .map_err(|_| "PERCEPTION_CONTROLLER_LOCK_POISONED".to_string())?
            .snapshot(perception_controller::monotonic_now_ms());
        capture.apply_capture_policy(
            policy.target_fps,
            policy.compute_level_target,
            policy.revision,
        )?;
        capture.status()
    })
    .await
    .map_err(|error| format!("screen_capture_start_worker_failed: {error}"))?
}

fn stop_capture_and_release_grounded_track(
    capture: &screen_capture::ScreenCaptureManager,
    perception: &Arc<Mutex<perception_controller::PerceptionController>>,
) -> Result<
    (
        screen_capture::ScreenCaptureStatus,
        perception_controller::PerceptionSnapshot,
    ),
    String,
> {
    let status = capture.stop()?;
    let snapshot = perception
        .lock()
        .map_err(|_| "PERCEPTION_CONTROLLER_LOCK_POISONED".to_string())?
        .signal(
            "grounded_track_stopped",
            None,
            perception_controller::monotonic_now_ms(),
        );
    // Stop has removed the stream, so this only synchronizes the lease revision;
    // ScreenCaptureManager treats policy updates without an active stream as a no-op.
    capture.apply_capture_policy(
        snapshot.target_fps,
        snapshot.compute_level_target,
        snapshot.revision,
    )?;
    Ok((status, snapshot))
}

#[tauri::command]
async fn stop_screen_capture(
    app: tauri::AppHandle,
    state: tauri::State<'_, Arc<screen_capture::ScreenCaptureManager>>,
    perception: tauri::State<'_, Arc<Mutex<perception_controller::PerceptionController>>>,
) -> Result<screen_capture::ScreenCaptureStatus, String> {
    let capture = Arc::clone(state.inner());
    let controller = Arc::clone(perception.inner());
    tauri::async_runtime::spawn_blocking(move || {
        let (status, snapshot) = stop_capture_and_release_grounded_track(&capture, &controller)?;
        let _ = app.emit("perception-state", &snapshot);
        Ok(status)
    })
    .await
    .map_err(|error| format!("screen_capture_stop_worker_failed: {error}"))?
}
#[tauri::command]
fn screen_capture_status(
    state: tauri::State<'_, Arc<screen_capture::ScreenCaptureManager>>,
) -> Result<screen_capture::ScreenCaptureStatus, String> {
    state.status()
}
#[tauri::command]
fn latest_screen_frame(
    state: tauri::State<'_, Arc<screen_capture::ScreenCaptureManager>>,
) -> Result<Option<screen_capture::LatestScreenFrame>, String> {
    state.latest_frame()
}
#[tauri::command]
fn latest_target_window_preview(
    state: tauri::State<'_, Arc<screen_capture::ScreenCaptureManager>>,
) -> Result<Option<screen_capture::LatestScreenFrame>, String> {
    state.latest_target_window_preview()
}
#[tauri::command]
fn pin_latest_grounding_frame(
    state: tauri::State<'_, Arc<screen_capture::ScreenCaptureManager>>,
) -> Result<Option<screen_capture::LatestScreenFrame>, String> {
    state.pin_latest_grounding_frame()
}
#[tauri::command]
fn accessibility_permission_status() -> bool {
    accessibility::is_trusted()
}
#[tauri::command]
fn screen_capture_permission_status() -> bool {
    screen_capture::screen_capture_access_status()
}
#[tauri::command]
async fn request_screen_capture_permission() -> Result<bool, String> {
    tauri::async_runtime::spawn_blocking(screen_capture::request_screen_capture_permission)
        .await
        .map_err(|error| format!("screen_capture_permission_worker_failed: {error}"))
}
#[tauri::command]
async fn request_accessibility_permission() -> Result<bool, String> {
    tauri::async_runtime::spawn_blocking(accessibility::request_permission)
        .await
        .map_err(|error| format!("accessibility_permission_worker_failed: {error}"))
}
#[tauri::command]
async fn observe_focused_accessibility() -> Result<accessibility::AccessibilityObservation, String>
{
    tauri::async_runtime::spawn_blocking(accessibility::observe_focused)
        .await
        .map_err(|error| format!("accessibility_snapshot_worker_failed: {error}"))?
}
#[tauri::command]
async fn list_running_accessibility_applications(
) -> Result<Vec<accessibility::RunningAccessibilityApplication>, String> {
    tauri::async_runtime::spawn_blocking(accessibility::list_running_applications)
        .await
        .map_err(|error| format!("accessibility_application_list_worker_failed: {error}"))?
}
#[tauri::command]
async fn list_installed_computer_use_applications(
    bundle_ids: Vec<String>,
) -> Result<Vec<accessibility::RunningAccessibilityApplication>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        accessibility::list_installed_applications(bundle_ids)
    })
    .await
    .map_err(|error| format!("computer_use_application_inventory_failed: {error}"))?
}

#[tauri::command]
async fn launch_computer_use_application(application_bundle_id: String) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        accessibility::launch_application_by_bundle_id(&application_bundle_id)
    })
    .await
    .map_err(|error| format!("computer_use_application_launch_worker_failed: {error}"))?
}
#[tauri::command]
async fn launch_computer_use_application_at_path(
    application_bundle_id: String,
    application_path: String,
) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        accessibility::launch_application_at_path(&application_bundle_id, &application_path)
    })
    .await
    .map_err(|error| format!("computer_use_application_launch_worker_failed: {error}"))?
}
#[tauri::command]
async fn observe_accessibility_application(
    application_pid: i32,
    include_menu_bar: Option<bool>,
) -> Result<accessibility::AccessibilityObservation, String> {
    tauri::async_runtime::spawn_blocking(move || {
        accessibility::observe_application(application_pid, include_menu_bar.unwrap_or(true))
    })
    .await
    .map_err(|error| format!("accessibility_snapshot_worker_failed: {error}"))?
}
#[tauri::command]
async fn press_accessibility_disclosure(
    observation_id: String,
    session_id: String,
    application_pid: i32,
    element_id: String,
    label: String,
) -> Result<accessibility::AccessibilityActionResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        accessibility::press_fresh_disclosure(
            observation_id,
            session_id,
            application_pid,
            element_id,
            label,
        )
    })
    .await
    .map_err(|error| format!("accessibility_action_worker_failed: {error}"))?
}
#[tauri::command]
async fn press_accessibility_button(
    observation_id: String,
    session_id: String,
    application_pid: i32,
    element_id: String,
    label: String,
) -> Result<accessibility::AccessibilityActionResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        accessibility::press_fresh_button(
            observation_id,
            session_id,
            application_pid,
            element_id,
            label,
        )
    })
    .await
    .map_err(|error| format!("accessibility_action_worker_failed: {error}"))?
}
#[tauri::command]
async fn enter_accessibility_text(
    observation_id: String,
    session_id: String,
    application_pid: i32,
    element_id: String,
    label: String,
    input_text: String,
) -> Result<accessibility::AccessibilityTextEntryResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        accessibility::enter_fresh_text(
            observation_id,
            session_id,
            application_pid,
            element_id,
            label,
            input_text,
        )
    })
    .await
    .map_err(|error| format!("accessibility_text_entry_worker_failed: {error}"))?
}
#[tauri::command]
async fn press_accessibility_tab(
    observation_id: String,
    session_id: String,
    application_pid: i32,
    current_focus_element_id: String,
    current_focus_label: String,
    target_element_id: String,
    target_label: String,
) -> Result<accessibility::AccessibilityKeyResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        accessibility::press_fresh_tab(
            observation_id,
            session_id,
            application_pid,
            current_focus_element_id,
            current_focus_label,
            target_element_id,
            target_label,
        )
    })
    .await
    .map_err(|error| format!("accessibility_keyboard_worker_failed: {error}"))?
}

#[tauri::command]
async fn press_accessibility_key(
    observation_id: String,
    session_id: String,
    application_pid: i32,
    element_id: String,
    label: String,
    key: String,
) -> Result<accessibility::AccessibilityKeyResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        accessibility::press_fresh_key(
            observation_id,
            session_id,
            application_pid,
            element_id,
            label,
            key,
        )
    })
    .await
    .map_err(|error| format!("accessibility_keypress_worker_failed: {error}"))?
}

#[tauri::command]
async fn scroll_accessibility_area(
    observation_id: String,
    session_id: String,
    application_pid: i32,
    area_element_id: String,
    area_label: String,
    scrollbar_element_id: String,
    before_scroll_value: f64,
    direction: String,
) -> Result<accessibility::AccessibilityScrollResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        accessibility::scroll_fresh_area(
            observation_id,
            session_id,
            application_pid,
            area_element_id,
            area_label,
            scrollbar_element_id,
            before_scroll_value,
            direction,
        )
    })
    .await
    .map_err(|error| format!("accessibility_scroll_worker_failed: {error}"))?
}
#[tauri::command]
async fn click_accessibility_disclosure(
    observation_id: String,
    session_id: String,
    application_pid: i32,
    element_id: String,
    label: String,
) -> Result<accessibility::AccessibilityActionResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        accessibility::click_fresh_disclosure(
            observation_id,
            session_id,
            application_pid,
            element_id,
            label,
        )
    })
    .await
    .map_err(|error| format!("accessibility_pointer_worker_failed: {error}"))?
}
#[tauri::command]
fn pointer_event_permission_status() -> bool {
    accessibility::pointer_event_permission_status()
}
#[tauri::command]
fn request_pointer_event_permission() -> bool {
    accessibility::request_pointer_event_permission()
}
#[tauri::command]
async fn activate_accessibility_find_shortcut(
    observation_id: String,
    session_id: String,
    application_pid: i32,
    element_id: String,
    label: String,
) -> Result<accessibility::AccessibilityShortcutResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        accessibility::activate_find_shortcut(
            observation_id,
            session_id,
            application_pid,
            element_id,
            label,
        )
    })
    .await
    .map_err(|error| format!("accessibility_shortcut_worker_failed: {error}"))?
}
#[tauri::command]
async fn start_accessibility_monitoring(
    app: tauri::AppHandle,
    application_pid: i32,
    state: tauri::State<'_, Arc<accessibility::AccessibilityMonitor>>,
) -> Result<accessibility::AccessibilityMonitorStatus, String> {
    let monitor = Arc::clone(&state);
    tauri::async_runtime::spawn_blocking(move || monitor.start(app, application_pid))
        .await
        .map_err(|error| format!("accessibility_monitor_start_worker_failed: {error}"))?
}
#[tauri::command]
async fn stop_accessibility_monitoring(
    state: tauri::State<'_, Arc<accessibility::AccessibilityMonitor>>,
) -> Result<accessibility::AccessibilityMonitorStatus, String> {
    let monitor = Arc::clone(&state);
    tauri::async_runtime::spawn_blocking(move || monitor.stop())
        .await
        .map_err(|error| format!("accessibility_monitor_stop_worker_failed: {error}"))?
}
#[tauri::command]
fn accessibility_monitor_status(
    state: tauri::State<'_, Arc<accessibility::AccessibilityMonitor>>,
) -> Result<accessibility::AccessibilityMonitorStatus, String> {
    state.status()
}
#[tauri::command]
fn perception_status(
    state: tauri::State<'_, Arc<Mutex<perception_controller::PerceptionController>>>,
) -> Result<perception_controller::PerceptionSnapshot, String> {
    state
        .lock()
        .map_err(|_| "PERCEPTION_CONTROLLER_LOCK_POISONED".to_string())
        .map(|mut controller| controller.snapshot(perception_controller::monotonic_now_ms()))
}

fn apply_perception_signal(
    app: &tauri::AppHandle,
    controller: &Arc<Mutex<perception_controller::PerceptionController>>,
    capture: &screen_capture::ScreenCaptureManager,
    signal: &str,
    pixel_change_ratio: Option<f64>,
) -> Result<perception_controller::PerceptionSnapshot, String> {
    if !perception_controller::supports_signal(signal) {
        return Err("PERCEPTION_SIGNAL_UNSUPPORTED".into());
    }
    if pixel_change_ratio.is_some_and(|ratio| !ratio.is_finite() || !(0.0..=1.0).contains(&ratio)) {
        return Err("PERCEPTION_PIXEL_RATIO_INVALID".into());
    }
    let snapshot = controller
        .lock()
        .map_err(|_| "PERCEPTION_CONTROLLER_LOCK_POISONED".to_string())?
        .signal(
            signal,
            pixel_change_ratio,
            perception_controller::monotonic_now_ms(),
        );
    let _ = app.emit("perception-state", &snapshot);
    capture.apply_capture_policy(
        snapshot.target_fps,
        snapshot.compute_level_target,
        snapshot.revision,
    )?;
    Ok(snapshot)
}

#[tauri::command]
async fn signal_perception(
    app: tauri::AppHandle,
    signal: String,
    pixel_change_ratio: Option<f64>,
    state: tauri::State<'_, Arc<Mutex<perception_controller::PerceptionController>>>,
    capture_state: tauri::State<'_, Arc<screen_capture::ScreenCaptureManager>>,
) -> Result<perception_controller::PerceptionSnapshot, String> {
    let capture = Arc::clone(capture_state.inner());
    let controller = Arc::clone(state.inner());
    Ok(tauri::async_runtime::spawn_blocking(move || {
        apply_perception_signal(&app, &controller, &capture, &signal, pixel_change_ratio)
    })
    .await
    .map_err(|error| format!("screen_capture_policy_update_worker_failed: {error}"))??)
}
#[tauri::command]
async fn recognize_image_text(data_url: String) -> Result<ocr::OcrObservation, String> {
    let bytes = ocr::decode_image_data_url(&data_url)?;
    tauri::async_runtime::spawn_blocking(move || ocr::recognize_image_bytes(&bytes))
        .await
        .map_err(|error| format!("OCR_WORKER_FAILED: {error}"))?
}
#[tauri::command]
async fn crop_ocr_grounded_roi(
    data_url: String,
    text_bbox_normalized: [f32; 4],
) -> Result<ocr::OcrGroundedRoi, String> {
    tauri::async_runtime::spawn_blocking(move || {
        ocr::crop_ocr_grounded_roi(&data_url, text_bbox_normalized)
    })
    .await
    .map_err(|error| format!("OCR_ROI_WORKER_FAILED: {error}"))?
}
#[tauri::command]
async fn track_grounded_screen_region(
    first_frame_data_url: String,
    next_frame_data_url: String,
    seed_bbox_normalized: [f32; 4],
) -> Result<visual_tracking::VisualTrackObservation, String> {
    let first_frame = ocr::decode_image_data_url(&first_frame_data_url)?;
    let next_frame = ocr::decode_image_data_url(&next_frame_data_url)?;
    tauri::async_runtime::spawn_blocking(move || {
        visual_tracking::track_object_between_images(
            &first_frame,
            &next_frame,
            seed_bbox_normalized,
        )
    })
    .await
    .map_err(|error| format!("VISION_TRACKING_WORKER_FAILED: {error}"))?
}
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct TrackedCaptureRegion {
    capture_session_id: String,
    target_application: Option<screen_capture::CaptureTargetApplication>,
    first_sequence: u64,
    first_observed_at: f64,
    next_sequence: u64,
    next_observed_at: f64,
    width: usize,
    height: usize,
    display_geometry: screen_capture::CaptureDisplayGeometry,
    tracking: visual_tracking::VisualTrackObservation,
    continuous_tracking_armed: bool,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct GroundedPointerAlignment {
    status: &'static str,
    capture_session_id: String,
    target_application: screen_capture::CaptureTargetApplication,
    seed_sequence: u64,
    verified_sequence: u64,
    verified_at: f64,
    display_id: u32,
    tracking_source: &'static str,
    tracking_confidence: f32,
    target_window_contains_point: bool,
    mapped_global_point: [f64; 2],
    pointer_origin_global: Option<[f64; 2]>,
    pointer_current_global: Option<[f64; 2]>,
    distance_points: Option<f64>,
    display_geometry: screen_capture::CaptureDisplayGeometry,
    target_bbox_normalized: [f32; 4],
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct GroundedPointerDisclosureResult {
    alignment: GroundedPointerAlignment,
    action: accessibility::AccessibilityActionResult,
}

const GROUNDED_POINTER_FRAME_MAX_AGE_SECONDS: f64 = 2.5;
// Match the capture manager's bounded grounding-seed window. The seed is
// deliberately pinned before the vision model runs; optical flow must be
// allowed to carry it forward across that inference latency. The tracked
// frame itself remains tightly bounded immediately before pointer input.
const GROUNDED_POINTER_SEED_MAX_AGE_SECONDS: f64 = 10.0;

fn validate_grounded_pointer_frame_freshness(
    first_observed_at: f64,
    next_observed_at: f64,
    now: f64,
) -> Result<(), String> {
    if !now.is_finite()
        || !first_observed_at.is_finite()
        || !next_observed_at.is_finite()
        || first_observed_at <= 0.0
        || next_observed_at <= first_observed_at
        || first_observed_at > now + 1.0
        || next_observed_at > now + 1.0
        // The seed was pinned before model inference and can be up to the
        // capture manager's 10-second tracking window old. CV tracking must
        // carry it to a recent frame; the tracked frame itself stays within
        // the short 2.5-second pointer-input freshness window.
        || now - first_observed_at > GROUNDED_POINTER_SEED_MAX_AGE_SECONDS
        || now - next_observed_at > GROUNDED_POINTER_FRAME_MAX_AGE_SECONDS
    {
        return Err("VISION_POINTER_TRACKED_FRAME_STALE".into());
    }
    Ok(())
}

fn calculate_grounded_pointer_alignment(
    capture: &screen_capture::ScreenCaptureManager,
    capture_session_id: &str,
    seed_sequence: u64,
    seed_bbox_normalized: [f32; 4],
    candidate_confidence: f32,
    target_application: screen_capture::CaptureTargetApplication,
) -> Result<(GroundedPointerAlignment, [f64; 2]), String> {
    if !candidate_confidence.is_finite() || !(0.65..=1.0).contains(&candidate_confidence) {
        return Err("VISION_POINTER_GROUNDING_CONFIDENCE_INSUFFICIENT".into());
    }
    if seed_bbox_normalized
        .iter()
        .any(|value| !value.is_finite() || !(0.0..=1.0).contains(value))
        || seed_bbox_normalized[0] >= seed_bbox_normalized[2]
        || seed_bbox_normalized[1] >= seed_bbox_normalized[3]
    {
        return Err("VISION_POINTER_GROUNDING_BOX_INVALID".into());
    }
    let pair = capture
        .tracking_pair_for_grounding_seed(seed_sequence)?
        .ok_or("SCREEN_CAPTURE_TRACKING_FRAMES_NOT_READY")?;
    if pair.capture_session_id != capture_session_id
        || pair.target_application.as_ref() != Some(&target_application)
        || pair.first_sequence != seed_sequence
    {
        return Err("VISION_POINTER_GROUNDING_PROVENANCE_MISMATCH".into());
    }
    let freshness_now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| "SCREEN_CAPTURE_CLOCK_UNAVAILABLE")?
        .as_secs_f64();
    validate_grounded_pointer_frame_freshness(
        pair.first_observed_at,
        pair.next_observed_at,
        freshness_now,
    )?;
    let tracking = visual_tracking::track_object_between_images(
        &pair.first_jpeg,
        &pair.next_jpeg,
        seed_bbox_normalized,
    )?;
    if !matches!(
        tracking.source,
        "APPLE_VISION_OBJECT_TRACKING" | "OPENCV_LUCAS_KANADE"
    ) || !tracking.confidence.is_finite()
        || !(0.75..=1.0).contains(&tracking.confidence)
        || !tracking.processing_ms.is_finite()
        || tracking.processing_ms < 0.0
    {
        return Err("VISION_POINTER_TRACKING_EVIDENCE_INSUFFICIENT".into());
    }
    let point = screen_capture::map_normalized_bbox_to_global_point(
        pair.display_geometry,
        pair.width,
        pair.height,
        tracking.bbox_normalized.map(f64::from),
    )
    .map_err(str::to_owned)?;
    accessibility::validate_grounded_pointer_target(
        target_application.application_pid,
        &target_application.application_bundle_id,
        point,
        pair.display_geometry.display_id,
        (
            pair.display_geometry.origin_x,
            pair.display_geometry.origin_y,
            pair.display_geometry.width_points,
            pair.display_geometry.height_points,
        ),
    )?;
    // Stamp the evidence after optical-flow and window-containment checks;
    // using the pre-tracking timestamp could make a valid pair appear stale
    // before the planning request reaches the backend.
    let verified_at = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| "SCREEN_CAPTURE_CLOCK_UNAVAILABLE")?
        .as_secs_f64();
    let pointer_origin_global = accessibility::pointer_location();
    let distance_points =
        pointer_origin_global.map(|origin| (point[0] - origin[0]).hypot(point[1] - origin[1]));
    Ok((
        GroundedPointerAlignment {
            status: "READY",
            capture_session_id: pair.capture_session_id,
            target_application,
            seed_sequence: pair.first_sequence,
            verified_sequence: pair.next_sequence,
            verified_at,
            display_id: pair.display_geometry.display_id,
            tracking_source: tracking.source,
            tracking_confidence: tracking.confidence,
            target_window_contains_point: true,
            mapped_global_point: point,
            pointer_origin_global,
            pointer_current_global: pointer_origin_global,
            distance_points,
            display_geometry: pair.display_geometry,
            target_bbox_normalized: tracking.bbox_normalized,
        },
        point,
    ))
}

const GROUNDED_POINTER_MAX_TRAVEL_POINTS: f64 = 3_000.0;
const GROUNDED_POINTER_STEP_POINTS: f64 = 32.0;
const GROUNDED_POINTER_MAX_STEPS: usize = 96;

fn aim_pointer_to_grounded_target(
    app: &tauri::AppHandle,
    alignment: &mut GroundedPointerAlignment,
    element_id: &str,
    label: &str,
    expected_role: &str,
) -> Result<accessibility::AccessibilityObservation, String> {
    let result = (|| {
        let origin = alignment
            .pointer_current_global
            .ok_or("POINTER_MOVE_INITIAL_POSITION_UNAVAILABLE")?;
        let target = alignment.mapped_global_point;
        let distance = (target[0] - origin[0]).hypot(target[1] - origin[1]);
        if !distance.is_finite() || distance > GROUNDED_POINTER_MAX_TRAVEL_POINTS {
            return Err("POINTER_MOVE_TRAVEL_LIMIT_EXCEEDED".into());
        }
        let steps = ((distance / GROUNDED_POINTER_STEP_POINTS).ceil() as usize)
            .clamp(1, GROUNDED_POINTER_MAX_STEPS);
        for step in 1..=steps {
            let progress = step as f64 / steps as f64;
            let requested = [
                origin[0] + (target[0] - origin[0]) * progress,
                origin[1] + (target[1] - origin[1]) * progress,
            ];
            let actual = accessibility::move_pointer_one_step(
                alignment.target_application.application_pid,
                &alignment.target_application.application_bundle_id,
                requested,
            )?;
            alignment.pointer_current_global = Some(actual);
            alignment.distance_points = Some((target[0] - actual[0]).hypot(target[1] - actual[1]));
            emit_pointer_feedback(app, "AIMING", alignment);
        }
        let actual = alignment
            .pointer_current_global
            .ok_or("POINTER_MOVE_POSITION_UNAVAILABLE")?;
        let geometry = alignment.display_geometry;
        let [left, top, right, bottom] = alignment.target_bbox_normalized.map(f64::from);
        let tracked_visual_frame = accessibility::AccessibilityRect {
            x: geometry.origin_x + left * geometry.width_points,
            y: geometry.origin_y + top * geometry.height_points,
            width: (right - left) * geometry.width_points,
            height: (bottom - top) * geometry.height_points,
        };
        let observation = accessibility::observe_application(
            alignment.target_application.application_pid,
            false,
        )?;
        accessibility::validate_moved_pointer_target(
            &observation,
            alignment.target_application.application_pid,
            &alignment.target_application.application_bundle_id,
            element_id,
            label,
            expected_role,
            actual,
            Some(&tracked_visual_frame),
        )?;
        Ok(observation)
    })();
    if result.is_err() {
        if let Some(actual) = accessibility::pointer_location() {
            alignment.pointer_current_global = Some(actual);
            alignment.distance_points = Some(
                (alignment.mapped_global_point[0] - actual[0])
                    .hypot(alignment.mapped_global_point[1] - actual[1]),
            );
        }
        emit_pointer_feedback(app, "FAILED", alignment);
    }
    result
}

fn emit_pointer_feedback(
    app: &tauri::AppHandle,
    phase: &str,
    alignment: &GroundedPointerAlignment,
) {
    eprintln!(
        "COMPUTER_USE_POINTER_PHASE={} TARGET_PID={} VERIFIED_FRAME={} DISTANCE_POINTS={:?} TARGET_POINT=({:.2},{:.2}) DISPLAY_ORIGIN=({:.2},{:.2}) DISPLAY_SIZE=({:.2},{:.2})",
        phase,
        alignment.target_application.application_pid,
        alignment.verified_sequence,
        alignment.distance_points,
        alignment.mapped_global_point[0],
        alignment.mapped_global_point[1],
        alignment.display_geometry.origin_x,
        alignment.display_geometry.origin_y,
        alignment.display_geometry.width_points,
        alignment.display_geometry.height_points,
    );
    let _ = app.emit(
        "computer-use-pointer",
        serde_json::json!({"phase": phase, "alignment": alignment}),
    );
}

#[tauri::command]
async fn validate_grounded_pointer_target(
    app: tauri::AppHandle,
    capture_session_id: String,
    seed_sequence: u64,
    seed_bbox_normalized: [f32; 4],
    candidate_confidence: f32,
    target_application: screen_capture::CaptureTargetApplication,
    state: tauri::State<'_, Arc<screen_capture::ScreenCaptureManager>>,
) -> Result<GroundedPointerAlignment, String> {
    let capture = Arc::clone(state.inner());
    tauri::async_runtime::spawn_blocking(move || {
        calculate_grounded_pointer_alignment(
            &capture,
            &capture_session_id,
            seed_sequence,
            seed_bbox_normalized,
            candidate_confidence,
            target_application,
        )
        .map(|(alignment, _point)| {
            emit_pointer_feedback(&app, "PLANNED", &alignment);
            alignment
        })
    })
    .await
    .map_err(|error| format!("VISION_POINTER_PREFLIGHT_WORKER_FAILED: {error}"))?
}

/// Cooperatively transfer focus from OLCR to the already validated target app.
/// AppKit activation must run on the main thread; the worker waits until the
/// exact PID/bundle pair is actually frontmost before posting pointer input.
#[cfg(target_os = "macos")]
fn yield_activation_to_pointer_target(
    app: &tauri::AppHandle,
    target_pid: i32,
    target_bundle_id: &str,
) -> Result<(), String> {
    let target_bundle_id = target_bundle_id.to_owned();
    let handoff_bundle_id = target_bundle_id.clone();
    let (sender, receiver) = std::sync::mpsc::sync_channel::<Result<(), String>>(1);
    app.run_on_main_thread(move || {
        let result = (|| {
            use objc2::MainThreadMarker;
            use objc2_app_kit::{
                NSApplication, NSRunningApplication, NSWorkspace, NSWorkspaceOpenConfiguration,
            };
            let marker =
                MainThreadMarker::new().ok_or("POINTER_ACTIVATION_MAIN_THREAD_REQUIRED")?;
            let source = NSRunningApplication::currentApplication();
            let target = NSRunningApplication::runningApplicationWithProcessIdentifier(target_pid)
                .ok_or("POINTER_TARGET_APPLICATION_NOT_RUNNING")?;
            if target.isTerminated()
                || target_pid == source.processIdentifier()
                || !target
                    .bundleIdentifier()
                    .is_some_and(|value| value.to_string() == handoff_bundle_id)
            {
                return Err("POINTER_TARGET_APPLICATION_IDENTITY_MISMATCH".into());
            }
            if accessibility::target_application_is_frontmost(target_pid, &handoff_bundle_id) {
                return Ok(());
            }
            // Use Launch Services for the exact already-running bundle. A
            // cooperative activate request alone can be denied when OLCR is
            // in the background. Never substitute a same-named app or request
            // a second instance; the worker still verifies this PID afterward.
            NSApplication::sharedApplication(marker).yieldActivationToApplication(&target);
            let url = target
                .bundleURL()
                .ok_or("POINTER_TARGET_APPLICATION_PATH_UNAVAILABLE")?;
            let configuration = NSWorkspaceOpenConfiguration::configuration();
            configuration.setActivates(true);
            configuration.setCreatesNewApplicationInstance(false);
            configuration.setAllowsRunningApplicationSubstitution(false);
            configuration.setAddsToRecentItems(false);
            configuration.setPromptsUserIfNeeded(false);
            NSWorkspace::sharedWorkspace().openApplicationAtURL_configuration_completionHandler(
                &url,
                &configuration,
                None,
            );
            eprintln!(
                "POINTER_ACTIVATION_METHOD=WORKSPACE TARGET_PID={}",
                target_pid
            );
            Ok(())
        })();
        let _ = sender.send(result);
    })
    .map_err(|error| format!("POINTER_ACTIVATION_MAIN_THREAD_DISPATCH_FAILED: {error}"))?;
    receiver
        .recv_timeout(Duration::from_secs(2))
        .map_err(|_| "POINTER_ACTIVATION_MAIN_THREAD_TIMEOUT")??;

    let deadline = Instant::now() + Duration::from_secs(2);
    while !accessibility::target_application_is_frontmost(target_pid, &target_bundle_id) {
        if Instant::now() >= deadline {
            return Err("POINTER_TARGET_APPLICATION_ACTIVATION_FAILED".into());
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    Ok(())
}

#[cfg(not(target_os = "macos"))]
fn yield_activation_to_pointer_target(
    _app: &tauri::AppHandle,
    _target_pid: i32,
    _target_bundle_id: &str,
) -> Result<(), String> {
    Err("COMPUTER_USE_POINTER_UNSUPPORTED_PLATFORM".into())
}

#[tauri::command]
async fn click_vision_grounded_disclosure(
    app: tauri::AppHandle,
    capture_session_id: String,
    seed_sequence: u64,
    seed_bbox_normalized: [f32; 4],
    candidate_confidence: f32,
    target_application: screen_capture::CaptureTargetApplication,
    observation_id: String,
    session_id: String,
    application_pid: i32,
    element_id: String,
    label: String,
    candidate_label: String,
    state: tauri::State<'_, Arc<screen_capture::ScreenCaptureManager>>,
) -> Result<GroundedPointerDisclosureResult, String> {
    let capture = Arc::clone(state.inner());
    drop((observation_id, session_id)); // The moved pointer must be authorized by a newer AX snapshot.
    tauri::async_runtime::spawn_blocking(move || {
        if application_pid != target_application.application_pid {
            return Err("VISION_POINTER_AX_TARGET_IDENTITY_MISMATCH".into());
        }
        if candidate_label.trim().to_lowercase() != label.trim().to_lowercase() {
            return Err("VISION_POINTER_CANDIDATE_LABEL_MISMATCH".into());
        }
        yield_activation_to_pointer_target(
            &app,
            application_pid,
            &target_application.application_bundle_id,
        )?;
        let (mut alignment, point) = calculate_grounded_pointer_alignment(
            &capture,
            &capture_session_id,
            seed_sequence,
            seed_bbox_normalized,
            candidate_confidence,
            target_application,
        )?;
        let fresh = aim_pointer_to_grounded_target(
            &app,
            &mut alignment,
            &element_id,
            &label,
            "AXDisclosureTriangle",
        )?;
        let action_result = accessibility::click_vision_grounded_disclosure(
            fresh.observation_id,
            fresh.session_id,
            fresh
                .application_pid
                .ok_or("POINTER_MOVE_FRESH_AX_PID_UNAVAILABLE")?,
            element_id,
            label,
            point,
        );
        let phase = if action_result
            .as_ref()
            .is_ok_and(|action| action.execution_attempted)
        {
            "CLICKED"
        } else {
            "FAILED"
        };
        emit_pointer_feedback(&app, phase, &alignment);
        let action = action_result?;
        Ok(GroundedPointerDisclosureResult { alignment, action })
    })
    .await
    .map_err(|error| format!("VISION_POINTER_ACTION_WORKER_FAILED: {error}"))?
}

#[tauri::command]
async fn click_vision_grounded_button(
    app: tauri::AppHandle,
    capture_session_id: String,
    seed_sequence: u64,
    seed_bbox_normalized: [f32; 4],
    candidate_confidence: f32,
    target_application: screen_capture::CaptureTargetApplication,
    observation_id: String,
    session_id: String,
    application_pid: i32,
    element_id: String,
    label: String,
    candidate_label: String,
    state: tauri::State<'_, Arc<screen_capture::ScreenCaptureManager>>,
) -> Result<GroundedPointerDisclosureResult, String> {
    let capture = Arc::clone(state.inner());
    drop((observation_id, session_id)); // The moved pointer must be authorized by a newer AX snapshot.
    tauri::async_runtime::spawn_blocking(move || {
        if application_pid != target_application.application_pid {
            return Err("VISION_POINTER_AX_TARGET_IDENTITY_MISMATCH".into());
        }
        if candidate_label.trim().to_lowercase() != label.trim().to_lowercase() {
            return Err("VISION_POINTER_CANDIDATE_LABEL_MISMATCH".into());
        }
        yield_activation_to_pointer_target(
            &app,
            application_pid,
            &target_application.application_bundle_id,
        )?;
        let (mut alignment, point) = calculate_grounded_pointer_alignment(
            &capture,
            &capture_session_id,
            seed_sequence,
            seed_bbox_normalized,
            candidate_confidence,
            target_application,
        )?;
        let fresh =
            aim_pointer_to_grounded_target(&app, &mut alignment, &element_id, &label, "AXButton")?;
        let action_result = accessibility::click_vision_grounded_button(
            fresh.observation_id,
            fresh.session_id,
            fresh
                .application_pid
                .ok_or("POINTER_MOVE_FRESH_AX_PID_UNAVAILABLE")?,
            element_id,
            label,
            point,
        );
        let phase = if action_result
            .as_ref()
            .is_ok_and(|action| action.execution_attempted)
        {
            "CLICKED"
        } else {
            "FAILED"
        };
        emit_pointer_feedback(&app, phase, &alignment);
        let action = action_result?;
        Ok(GroundedPointerDisclosureResult { alignment, action })
    })
    .await
    .map_err(|error| format!("VISION_POINTER_ACTION_WORKER_FAILED: {error}"))?
}

#[tauri::command]
async fn track_latest_grounded_screen_region(
    app: tauri::AppHandle,
    seed_sequence: u64,
    seed_bbox_normalized: [f32; 4],
    state: tauri::State<'_, Arc<screen_capture::ScreenCaptureManager>>,
    perception: tauri::State<'_, Arc<Mutex<perception_controller::PerceptionController>>>,
) -> Result<TrackedCaptureRegion, String> {
    let capture = Arc::clone(state.inner());
    let controller = Arc::clone(perception.inner());
    tauri::async_runtime::spawn_blocking(move || {
        let pair = capture
            .tracking_pair_for_grounding_seed(seed_sequence)?
            .ok_or("SCREEN_CAPTURE_TRACKING_FRAMES_NOT_READY")?;
        let tracking = visual_tracking::track_object_between_images(
            &pair.first_jpeg,
            &pair.next_jpeg,
            seed_bbox_normalized,
        )?;
        let mut continuous_tracking_armed = false;
        if tracking.source == "OPENCV_LUCAS_KANADE" && tracking.confidence >= 0.5 {
            if let Err(error) =
                apply_perception_signal(&app, &controller, &capture, "grounded_track_active", None)
            {
                let _ = apply_perception_signal(
                    &app,
                    &controller,
                    &capture,
                    "grounded_track_stopped",
                    None,
                );
                return Err(error);
            }
            match capture.arm_grounded_opencv_tracking(pair.next_sequence, tracking.bbox_normalized)
            {
                Ok(true) => continuous_tracking_armed = true,
                Ok(false) => {
                    apply_perception_signal(
                        &app,
                        &controller,
                        &capture,
                        "grounded_track_stopped",
                        None,
                    )?;
                }
                Err(error) => {
                    let _ = apply_perception_signal(
                        &app,
                        &controller,
                        &capture,
                        "grounded_track_stopped",
                        None,
                    );
                    return Err(error);
                }
            }
        }
        Ok(TrackedCaptureRegion {
            capture_session_id: pair.capture_session_id,
            target_application: pair.target_application,
            first_sequence: pair.first_sequence,
            first_observed_at: pair.first_observed_at,
            next_sequence: pair.next_sequence,
            next_observed_at: pair.next_observed_at,
            width: pair.width,
            height: pair.height,
            display_geometry: pair.display_geometry,
            tracking,
            continuous_tracking_armed,
        })
    })
    .await
    .map_err(|error| format!("VISION_TRACKING_WORKER_FAILED: {error}"))?
}
fn main() {
    if let Ok(executable) = env::current_exe() {
        if let Err(error) =
            validate_isolated_cua_profile(&executable, env::var_os("OLCR_APP_SUPPORT").as_deref())
        {
            startup_diagnostic(&format!("OLCR_ISOLATED_PROFILE_GUARD=FAIL error={error}"));
            std::process::exit(78);
        }
    }
    let lock_path = default_instance_lock_path();
    let _instance_lock = match acquire_single_instance_lock(&lock_path) {
        Ok(lock) => lock,
        Err(error) => {
            startup_diagnostic(&format!("OLCR_SINGLE_INSTANCE_GUARD=FAIL error={error}"));
            std::process::exit(78);
        }
    };
    if let Err(error) = ensure_no_existing_olcr_processes() {
        startup_diagnostic(&format!("OLCR_PROCESS_IDENTITY_GUARD=FAIL error={error}"));
        std::process::exit(78);
    }
    startup_diagnostic(&format!(
        "OLCR_SINGLE_INSTANCE_GUARD=PASS lock_path={}",
        lock_path.display()
    ));
    let token: String = rand::thread_rng()
        .sample_iter(&Alphanumeric)
        .take(48)
        .map(char::from)
        .collect();
    let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("loopback allocation failed");
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let manager = Arc::new(BackendProcessManager {
        child: Mutex::new(None),
        startup_lock: Mutex::new(()),
        token,
        port,
    });
    let capture = Arc::new(screen_capture::ScreenCaptureManager::default());
    let accessibility_monitor = Arc::new(accessibility::AccessibilityMonitor::default());
    let perception = Arc::new(Mutex::new(
        perception_controller::PerceptionController::default(),
    ));
    startup_diagnostic("BACKEND_STARTUP_DEFERRED_TO_FRONTEND_BOOTSTRAP=true");
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(manager)
        .manage(capture)
        .manage(accessibility_monitor)
        .manage(perception)
        .manage(Arc::new(human_input::HumanInputManager::default()))
        .invoke_handler(tauri::generate_handler![
            backend_config,
            transcribe_macos_audio,
            restore_external_api_credentials,
            human_input::begin_human_input,
            human_input::apply_human_input,
            human_input::end_human_input,
            save_export_file,
            set_web_credential,
            clear_web_credential,
            set_external_credential,
            open_external_url,
            assistant_notifications::assistant_sync_notifications,
            macos_calendar::assistant_open_calendar,
            assistant_media::assistant_player_fullscreen,
            open_local_file,
            read_dropped_file,
            start_screen_capture,
            stop_screen_capture,
            screen_capture_status,
            latest_screen_frame,
            latest_target_window_preview,
            pin_latest_grounding_frame,
            accessibility_permission_status,
            screen_capture_permission_status,
            request_screen_capture_permission,
            request_accessibility_permission,
            observe_focused_accessibility,
            list_running_accessibility_applications,
            list_installed_computer_use_applications,
            launch_computer_use_application,
            launch_computer_use_application_at_path,
            observe_accessibility_application,
            press_accessibility_disclosure,
            press_accessibility_button,
            enter_accessibility_text,
            press_accessibility_tab,
            press_accessibility_key,
            scroll_accessibility_area,
            click_accessibility_disclosure,
            pointer_event_permission_status,
            request_pointer_event_permission,
            activate_accessibility_find_shortcut,
            start_accessibility_monitoring,
            stop_accessibility_monitoring,
            accessibility_monitor_status,
            perception_status,
            signal_perception,
            recognize_image_text,
            crop_ocr_grounded_roi,
            track_grounded_screen_region,
            track_latest_grounded_screen_region,
            validate_grounded_pointer_target,
            click_vision_grounded_disclosure,
            click_vision_grounded_button
        ])
        .setup(|_| {
            if option_env!("OLCR_UI_ONLY_BUILD") == Some("models") {
                startup_diagnostic("UI_ONLY_BUILD_READY=true BACKEND_DISABLED=true");
                startup_diagnostic(&format!(
                    "UI_BUILD_ID={}",
                    env::var("OLCR_API_REPAIR_BUILD_ID").unwrap_or_default()
                ));
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error building OLCR desktop")
        .run(|app, event| {
            if matches!(
                event,
                tauri::RunEvent::Exit | tauri::RunEvent::ExitRequested { .. }
            ) {
                // Tauri may terminate without dropping managed Arc state. Stop
                // the owned backend explicitly instead of relying on Drop.
                if let Some(manager) = app.try_state::<Arc<BackendProcessManager>>() {
                    manager.stop();
                }
                if let Some(capture) = app.try_state::<Arc<screen_capture::ScreenCaptureManager>>()
                {
                    let _ = capture.stop();
                }
                if let Some(monitor) = app.try_state::<Arc<accessibility::AccessibilityMonitor>>() {
                    let _ = monitor.stop();
                }
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grounded_pointer_accepts_frames_within_shared_freshness_window() {
        // The seed is pinned before vision inference, so it may use the full
        // bounded 10-second capture tracking window. The tracked frame stays
        // within the short 2.5-second window before pointer input.
        assert!(validate_grounded_pointer_frame_freshness(90.0, 97.5, 100.0).is_ok());
        assert_eq!(
            validate_grounded_pointer_frame_freshness(89.9, 97.5, 100.0).unwrap_err(),
            "VISION_POINTER_TRACKED_FRAME_STALE"
        );
        assert!(validate_grounded_pointer_frame_freshness(95.1, 97.5, 100.0).is_ok());
        assert_eq!(
            validate_grounded_pointer_frame_freshness(96.9, 97.0, 100.0).unwrap_err(),
            "VISION_POINTER_TRACKED_FRAME_STALE"
        );
        assert_eq!(
            validate_grounded_pointer_frame_freshness(98.0, 101.1, 100.0).unwrap_err(),
            "VISION_POINTER_TRACKED_FRAME_STALE"
        );
    }

    #[test]
    fn isolated_cua_bundle_requires_its_run_specific_profile_on_relaunch() {
        let executable = Path::new(
            "/private/tmp/olcr-e2e-sample-20261002-target/debug/bundle/macos/OLCR.app/Contents/MacOS/olcr-desktop",
        );
        let expected = Path::new("/private/tmp/olcr-e2e-sample-20261002-profile");
        assert_eq!(
            expected_cua_profile_for_executable(executable).as_deref(),
            Some(expected)
        );
        assert_eq!(
            validate_isolated_cua_profile(executable, None),
            Err("OLCR_CUA_APP_SUPPORT_REQUIRED_FOR_ISOLATED_BUNDLE")
        );
        assert_eq!(
            validate_isolated_cua_profile(
                executable,
                Some(std::ffi::OsStr::new(
                    "/Users/test/Library/Application Support/OLCR"
                )),
            ),
            Err("OLCR_CUA_APP_SUPPORT_PATH_MISMATCH")
        );
        assert_eq!(
            validate_isolated_cua_profile(executable, Some(expected.as_os_str())),
            Ok(())
        );
    }

    #[test]
    fn packaged_bundle_keeps_the_existing_default_profile_behavior() {
        let executable = Path::new("/Applications/OLCR.app/Contents/MacOS/olcr-desktop");
        assert_eq!(expected_cua_profile_for_executable(executable), None);
        assert_eq!(validate_isolated_cua_profile(executable, None), Ok(()));
    }

    #[test]
    fn stopping_capture_releases_grounded_tracking_lease_before_next_session() {
        let capture = screen_capture::ScreenCaptureManager::default();
        let perception = Arc::new(Mutex::new(
            perception_controller::PerceptionController::default(),
        ));
        let active = perception.lock().unwrap().signal(
            "grounded_track_active",
            None,
            perception_controller::monotonic_now_ms(),
        );
        assert_eq!(active.state, perception_controller::PerceptionState::High);
        assert_eq!(active.compute_level_target, 2);

        let (status, stopped) = stop_capture_and_release_grounded_track(&capture, &perception)
            .expect("stopping capture should release its tracking lease");

        assert!(!status.running);
        assert_eq!(stopped.state, perception_controller::PerceptionState::Idle);
        assert_eq!(stopped.compute_level_target, 0);
        assert_eq!(stopped.reason, "stable_scene");
        let next_session_policy = perception
            .lock()
            .unwrap()
            .snapshot(perception_controller::monotonic_now_ms());
        assert_eq!(
            next_session_policy.state,
            perception_controller::PerceptionState::Idle
        );
        assert_eq!(next_session_policy.compute_level_target, 0);
    }

    #[test]
    fn single_instance_lock_rejects_a_second_desktop_and_releases_on_drop() {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = env::temp_dir().join(format!(
            "olcr-single-instance-test-{}-{nonce}.lock",
            std::process::id()
        ));
        let first =
            acquire_single_instance_lock(&path).expect("first instance should acquire lock");
        assert_eq!(
            "another_olcr_desktop_instance_is_running",
            acquire_single_instance_lock(&path).unwrap_err()
        );
        drop(first);
        let second = acquire_single_instance_lock(&path)
            .expect("kernel should release lock after owner exits");
        drop(second);
        let _ = fs::remove_file(path);
    }
    #[test]
    fn backend_process_manager_stops_and_reaps_its_child_on_drop() {
        let child = Command::new("sleep")
            .arg("30")
            .spawn()
            .expect("sleep fixture should start");
        let pid = child.id();
        let manager = BackendProcessManager {
            child: Mutex::new(Some(child)),
            startup_lock: Mutex::new(()),
            token: "test-token".into(),
            port: 0,
        };
        drop(manager);
        assert_eq!(
            unsafe { libc::kill(pid as i32, 0) },
            -1,
            "managed child must be gone after manager drop"
        );
    }
    #[test]
    fn process_identity_guard_rejects_other_desktop_and_backend_but_not_current_pid() {
        let snapshot = "100 1 /private/tmp/OLCR Acceptance R1.app/Contents/MacOS/olcr-desktop\n101 1 /opt/homebrew/bin/Python -m uvicorn olcr_api.app:app --host 127.0.0.1\n102 1 /opt/homebrew/bin/Python -m unrelated.app:app\n103 1 /private/tmp/OLCR Acceptance R2.app/Contents/MacOS/olcr-desktop\n";
        let found = parse_existing_olcr_processes(snapshot, 100).unwrap();
        assert_eq!(
            vec![101, 103],
            found.iter().map(|process| process.pid).collect::<Vec<_>>()
        );
        assert!(
            found
                .iter()
                .find(|process| process.pid == 101)
                .unwrap()
                .parent_pid
                == 1
        );
        assert!(
            found
                .iter()
                .find(|process| process.pid == 103)
                .unwrap()
                .desktop
        );
        assert!(parse_existing_olcr_processes("not-a-process-row\n", 1).is_err());
        assert!(parse_existing_olcr_processes("", 1).is_err());
    }
    fn fixture() -> (PathBuf, PathBuf) {
        let root = env::temp_dir().join(format!(
            "olcr-resolver-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let manifest = root.join("desktop/src-tauri");
        let backend = root.join("backend");
        fs::create_dir_all(&manifest).unwrap();
        fs::create_dir_all(backend.join("olcr_api")).unwrap();
        fs::create_dir_all(backend.join("olcr_cli")).unwrap();
        (root, manifest)
    }
    #[test]
    fn manifest_resolves_repository_backend() {
        let (root, manifest) = fixture();
        let (found, source) = resolve_backend_dir(&manifest, None).unwrap();
        assert_eq!(found, root.join("backend"));
        assert_eq!(source, "manifest");
        assert_ne!(found, root.join("desktop/backend"));
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn valid_override_wins() {
        let (root, manifest) = fixture();
        let override_dir = root.join("custom-backend");
        fs::create_dir_all(override_dir.join("olcr_api")).unwrap();
        fs::create_dir_all(override_dir.join("olcr_cli")).unwrap();
        let (found, source) = resolve_backend_dir(&manifest, Some(&override_dir)).unwrap();
        assert_eq!(found, override_dir);
        assert_eq!(source, "override");
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn invalid_override_fails_safely() {
        let (root, manifest) = fixture();
        let result = resolve_backend_dir(&manifest, Some(&root.join("missing")));
        assert_eq!(result.unwrap_err(), "backend_directory_invalid");
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn python_override_must_exist() {
        let (root, manifest) = fixture();
        let result = resolve_python(&manifest, Some(&root.join("missing-python")));
        assert_eq!(result.unwrap_err(), "python_runtime_invalid");
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn python_resolves_repository_virtualenv() {
        let (root, manifest) = fixture();
        let python = root.join(".venv/bin/python");
        fs::create_dir_all(python.parent().unwrap()).unwrap();
        fs::write(&python, b"runtime").unwrap();
        let (found, source) = resolve_python(&manifest, None).unwrap();
        assert_eq!(found, python);
        assert_eq!(source, "manifest_venv");
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn keychain_restore_is_enabled_unless_explicitly_skipped_at_launch() {
        assert!(!skip_keychain_restore_from(None));
        assert!(!skip_keychain_restore_from(Some("false")));
        assert!(!skip_keychain_restore_from(Some("1")));
        assert!(skip_keychain_restore_from(Some("true")));
    }
    #[test]
    fn credential_bundle_round_trips_only_supported_nonempty_entries() {
        let raw = r#"{"web:brave":"fake-web","OLCR_UCDP_API_TOKEN":"fake-user","__olcr_legacy_migration_v1":"true","unknown":"ignored","OLCR_ACLED_PASSWORD":""}"#;
        let parsed = parse_credential_bundle(raw).unwrap();
        assert_eq!(parsed.len(), 3);
        assert_eq!(
            parsed.get("web:brave").map(String::as_str),
            Some("fake-web")
        );
        assert_eq!(
            parsed.get("OLCR_UCDP_API_TOKEN").map(String::as_str),
            Some("fake-user")
        );
        assert!(!parsed.contains_key("unknown"));
        assert!(!parsed.contains_key("OLCR_ACLED_PASSWORD"));
        assert_eq!(
            parsed
                .get(CREDENTIAL_BUNDLE_MIGRATION_MARKER)
                .map(String::as_str),
            Some("true")
        );
        assert!(parse_credential_bundle("not-json").is_err());
    }
    #[test]
    fn credential_bundle_allowlist_supports_three_brain_accounts_with_stable_handles() {
        for provider in ["OPENAI", "GOOGLE", "ANTHROPIC", "NVIDIA_NIM", "HUGGINGFACE", "OPENAI_OAUTH"] {
            let first = format!("OLCR_BRAIN_{provider}");
            assert!(validate_external_credential_input(&first, "fixture-only").is_ok());
            for slot in [2, 3] {
                assert!(validate_external_credential_input(&format!("{first}_{slot}"), "fixture-only").is_ok());
            }
            assert!(validate_external_credential_input(&format!("{first}_4"), "fixture-only").is_err());
        }
        let restored = parse_credential_bundle(r#"{"OLCR_BRAIN_OPENAI_OAUTH_2":"fixture-two","OLCR_BRAIN_OPENAI_OAUTH_3":"fixture-three"}"#).unwrap();
        assert_eq!(restored.len(), 2);
    }
    #[test]
    fn keychain_migration_marker_is_internal_not_a_provider_credential() {
        assert!(is_supported_bundle_key(CREDENTIAL_BUNDLE_MIGRATION_MARKER));
        assert!(!EXTERNAL_CREDENTIAL_HANDLES
            .lines()
            .any(|handle| handle == CREDENTIAL_BUNDLE_MIGRATION_MARKER));
    }
    #[test]
    fn credential_bundle_allowlist_covers_web_and_canonical_external_handles_only() {
        for role in ["SEMANTIC_EMBEDDING", "TYPED_LOCAL_DECISIONS", "GENERAL_IMAGE",
                     "GUI_GROUNDING", "TRANSCRIPTION_JA", "TRANSCRIPTION_EN"] {
            for slot in ["A", "B"] {
                assert!(is_supported_bundle_key(&format!("OLCR_MODEL_{role}_{slot}")));
            }
        }
        assert!(is_supported_bundle_key("web:brave"));
        assert!(is_supported_bundle_key("web:tavily"));
        assert!(EXTERNAL_CREDENTIAL_HANDLES
            .lines()
            .filter(|handle| !handle.is_empty())
            .all(is_supported_bundle_key));
        assert!(!is_supported_bundle_key("web:unknown"));
        assert!(!is_supported_bundle_key("OLCR_UNREGISTERED_SECRET"));
    }
    #[test]
    fn credential_validation_accepts_oauth_bundles_larger_than_api_keys() {
        let oauth_bundle = "x".repeat(8 * 1024);
        assert!(validate_external_credential_input("OLCR_BRAIN_OPENAI_OAUTH", &oauth_bundle).is_ok());
        let oversized = "x".repeat(MAX_CREDENTIAL_VALUE_BYTES + 1);
        assert!(validate_external_credential_input("OLCR_BRAIN_OPENAI_OAUTH", &oversized).is_err());
        assert!(validate_external_credential_input(CREDENTIAL_BUNDLE_MIGRATION_MARKER, "false").is_err());
    }
    #[test]
    fn bounded_command_runner_sends_updates_over_stdin() {
        let fixture = br#"{"key":"web:brave","value":"fixture-only"}"#;
        let output = run_captured_command_with_input(
            &mut Command::new("/bin/cat"), Duration::from_secs(2), 128,
            "command_failed", "command_timeout", Some(fixture),
        ).unwrap();
        assert!(output.status.success());
        assert_eq!(output.stdout, fixture);
        assert!(output.stderr.is_empty());
    }
    #[test]
    fn bounded_command_runner_captures_limited_stdout_without_stderr() {
        let output = run_captured_command_with_timeout(
            Command::new("/usr/bin/printf").arg("{\"web:brave\":\"fake\"}"),
            Duration::from_secs(2),
            128,
            "command_failed",
            "command_timeout",
        )
        .unwrap();
        assert!(output.status.success());
        assert_eq!(output.stdout, br#"{"web:brave":"fake"}"#);
        assert!(output.stderr.is_empty());
    }
    #[test]
    fn bounded_command_runner_reads_at_most_one_byte_past_the_limit() {
        let output = run_captured_command_with_timeout(
            &mut Command::new("/usr/bin/yes"),
            Duration::from_secs(2),
            32,
            "command_failed",
            "command_timeout",
        )
        .unwrap();
        assert!(output.stdout.len() <= 33);
    }
    #[test]
    fn bounded_command_runner_kills_a_stalled_child() {
        let started = Instant::now();
        let error = run_captured_command_with_timeout(
            Command::new("/bin/sleep").arg("30"),
            Duration::from_millis(50),
            MAX_CREDENTIAL_BUNDLE_BYTES,
            "command_failed",
            "credential_bundle_bridge_timeout",
        )
        .unwrap_err();
        assert_eq!(error, "credential_bundle_bridge_timeout");
        assert!(started.elapsed() < Duration::from_secs(2));
    }
    #[test]
    fn backend_startup_timeout_configuration_is_bounded() {
        assert_eq!(backend_startup_timeout_from(Some("120")), Duration::from_secs(120));
        assert_eq!(backend_startup_timeout_from(Some("180")), Duration::from_secs(180));
        for invalid in [None, Some("garbage"), Some("4"), Some("301")] {
            assert_eq!(backend_startup_timeout_from(invalid), Duration::from_secs(180));
        }
    }
    #[test]
    fn backend_stdout_bursts_do_not_block_readiness() {
        use std::os::unix::fs::PermissionsExt;
        let (root, _) = fixture();
        let python = root.join("fake-python");
        fs::write(&python, r#"#!/usr/bin/env python3
import http.server, sys
sys.stdout.buffer.write(b'x' * 1024 * 1024)
sys.stdout.flush()
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'{}')
    def log_message(self, *args):
        pass
port = int(sys.argv[sys.argv.index('--port') + 1])
http.server.HTTPServer(('127.0.0.1', port), Handler).serve_forever()
"#).unwrap();
        fs::set_permissions(&python, fs::Permissions::from_mode(0o755)).unwrap();
        let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        let overrides = [
            ("OLCR_PYTHON", python.into_os_string()),
            ("OLCR_BACKEND_DIR", root.join("backend").into_os_string()),
            ("OLCR_BACKEND_STARTUP_TIMEOUT_SECONDS", "5".into()),
        ];
        let previous: Vec<_> = overrides.iter().map(|(key, _)| (*key, env::var_os(key))).collect();
        for (key, value) in &overrides { env::set_var(key, value); }
        let manager = BackendProcessManager {
            child: Mutex::new(None), startup_lock: Mutex::new(()), token: "fixture".into(), port,
        };
        // Call the backend boundary directly: this test never starts Tauri,
        // restores Keychain credentials, or interacts with an OLCR instance.
        let result = manager.start();
        for (key, value) in previous {
            match value { Some(value) => env::set_var(key, value), None => env::remove_var(key) }
        }
        drop(manager);
        let _ = fs::remove_dir_all(root);
        assert!(result.is_ok(), "stdout burst prevented backend readiness: {result:?}");
    }
    #[test]
    fn controlled_gui_build_persists_startup_diagnostics() {
        let (root, _) = fixture();
        let diagnostic_path = root.join("startup.log");
        append_startup_diagnostic(&diagnostic_path, "RUST_TEST_DIAGNOSTIC_WRITE=PASS");
        assert_eq!(fs::read_to_string(diagnostic_path).unwrap(), "RUST_TEST_DIAGNOSTIC_WRITE=PASS\n");
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn backend_config_uses_frontend_contract_keys() {
        let manager = BackendProcessManager {
            child: Mutex::new(Some(Command::new("sleep").arg("30").spawn().unwrap())),
            startup_lock: Mutex::new(()),
            token: "token".into(),
            port: 45678,
        };
        let value = serde_json::to_value(backend_config_for_manager(&manager).unwrap()).unwrap();
        assert_eq!(value["api_url"], "http://127.0.0.1:45678/api");
        assert_eq!(value["session_token"], "token");
        drop(manager);
    }
    #[test]
    fn backend_config_never_publishes_a_port_after_startup_failure() {
        let manager = BackendProcessManager {
            child: Mutex::new(None),
            startup_lock: Mutex::new(()),
            token: "token".into(),
            port: 45678,
        };
        assert!(
            matches!(backend_config_for_manager(&manager), Err(error) if error == "backend_process_unavailable")
        );
    }
    #[test]
    fn backend_config_rejects_a_child_that_exited_after_readiness() {
        let child = Command::new("true").spawn().unwrap();
        let manager = BackendProcessManager {
            child: Mutex::new(Some(child)),
            startup_lock: Mutex::new(()),
            token: "token".into(),
            port: 45678,
        };
        std::thread::sleep(Duration::from_millis(50));
        assert!(
            matches!(backend_config_for_manager(&manager), Err(error) if error == "backend_process_unavailable")
        );
        assert!(
            manager.child.lock().unwrap().is_none(),
            "exited child should be reaped from manager state"
        );
    }
}

#[cfg(test)]
mod working_folder_attachment_tests {
    #[test]
    fn read_dropped_folder_preserves_directory_as_working_files() {
        let unique = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
        let root = std::env::temp_dir().join(format!("olcr-folder-attachment-{unique}"));
        std::fs::create_dir(&root).unwrap();
        std::fs::write(root.join("first.txt"), "one").unwrap();
        std::fs::write(root.join("second.txt"), "two").unwrap();
        let attached = super::read_dropped_file(root.to_string_lossy().into_owned()).unwrap();
        assert_eq!(attached.kind.as_deref(), Some("folder"));
        assert_eq!(attached.directory_path.as_deref(), Some(root.canonicalize().unwrap().to_str().unwrap()));
        assert!(attached.content.is_empty());
        assert!(attached.bytes_base64.is_empty());
        assert!(root.join("first.txt").is_file());
        std::fs::remove_dir_all(root).unwrap();
    }
}
