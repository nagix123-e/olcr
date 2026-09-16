use rand::{distributions::Alphanumeric, Rng};
use serde::Serialize;
use std::io::{BufRead, BufReader, Read, Write};
use std::{env, net::TcpStream, path::{Path, PathBuf}, process::{Child, Command, Stdio}, sync::Mutex, time::Duration};
#[cfg(test)] use std::fs;

struct BackendProcessManager { child: Mutex<Option<Child>>, token: String, port: u16 }
const KEYCHAIN_SERVICE: &str = "local.olcr.desktop.web-search";
fn keychain_entry(provider: &str) -> Result<keyring::Entry, String> {
  if !matches!(provider, "brave" | "tavily") { return Err("unsupported web provider".into()); }
  keyring::Entry::new(KEYCHAIN_SERVICE, provider).map_err(|e| e.to_string())
}
#[tauri::command]
fn set_web_credential(provider: String, key: String) -> Result<(), String> {
  if key.trim().is_empty() { return Err("API key cannot be empty".into()); }
  keychain_entry(&provider)?.set_password(&key).map_err(|e| e.to_string())
}
#[tauri::command]
fn clear_web_credential(provider: String) -> Result<(), String> {
  match keychain_entry(&provider)?.delete_credential() { Ok(()) => Ok(()), Err(keyring::Error::NoEntry) => Ok(()), Err(e) => Err(e.to_string()) }
}
fn restore_web_credentials() {
  for provider in ["brave", "tavily"] {
    if let Ok(value) = keychain_entry(provider).and_then(|e| e.get_password().map_err(|err| err.to_string())) {
      let name = if provider == "brave" { "OLCR_WEB_BRAVE_API_KEY" } else { "OLCR_WEB_TAVILY_API_KEY" };
      std::env::set_var(name, value);
    }
  }
}
#[tauri::command]
fn open_external_url(url: String) -> Result<(), String> {
  let local_path = url.starts_with("/Users/") || url.starts_with("/private/") || url.starts_with("/tmp/") || url.starts_with("/var/") || url.starts_with("/Volumes/");
  let valid = (url.starts_with("https://") || url.starts_with("http://") || url.starts_with("file://") || local_path)
    && !url.chars().any(|c| c.is_control() || c == '\n' || c == '\r');
  if !valid { return Err("external_url_scheme_not_allowed".into()); }
  std::process::Command::new("open").arg(&url).status()
    .map_err(|e| format!("open_external_url_failed: {e}"))
    .and_then(|status| if status.success() { Ok(()) } else { Err(format!("open_external_url_failed: {status}")) })
}
#[derive(Serialize)] #[serde(rename_all = "camelCase")] struct DroppedFile { name: String, mime_type: String, content: String, data_url: Option<String> }
fn base64_encode(bytes: &[u8]) -> String {
  const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  let mut output=String::with_capacity((bytes.len()+2)/3*4);
  for chunk in bytes.chunks(3) {
    let a=chunk[0]; let b=*chunk.get(1).unwrap_or(&0); let c=*chunk.get(2).unwrap_or(&0);
    output.push(TABLE[(a >> 2) as usize] as char); output.push(TABLE[(((a & 3) << 4) | (b >> 4)) as usize] as char);
    output.push(if chunk.len()>1 { TABLE[(((b & 15) << 2) | (c >> 6)) as usize] as char } else { '=' });
    output.push(if chunk.len()>2 { TABLE[(c & 63) as usize] as char } else { '=' });
  }
  output
}
#[tauri::command]
fn read_dropped_file(path: String) -> Result<DroppedFile, String> {
  let candidate=PathBuf::from(&path); let metadata=std::fs::metadata(&candidate).map_err(|_| "dropped_file_unavailable".to_string())?;
  if !metadata.is_file() { return Err("dropped_path_is_not_a_file".into()); }
  if metadata.len()==0 { return Err("dropped_file_empty".into()); }
  if metadata.len()>5_000_000 { return Err("dropped_file_too_large".into()); }
  let name=candidate.file_name().and_then(|value| value.to_str()).ok_or_else(|| "dropped_file_name_invalid".to_string())?.to_string();
  let extension=candidate.extension().and_then(|value| value.to_str()).unwrap_or("").to_ascii_lowercase();
  let mime_type=match extension.as_str() { "png"=>"image/png", "jpg"|"jpeg"=>"image/jpeg", "webp"=>"image/webp", "gif"=>"image/gif", _=>"text/plain" }.to_string();
  let bytes=std::fs::read(&candidate).map_err(|_| "dropped_file_unreadable".to_string())?;
  if mime_type.starts_with("image/") { return Ok(DroppedFile{name,mime_type: mime_type.clone(),content:String::new(),data_url:Some(format!("data:{mime_type};base64,{}",base64_encode(&bytes)))}); }
  let content=String::from_utf8(bytes).map_err(|_| "dropped_file_is_not_text".to_string())?;
  Ok(DroppedFile{name,mime_type,content,data_url:None})
}
#[derive(Serialize)] struct BackendConfig { api_url: String, session_token: String }

fn authenticated_ready(port: u16, token: &str) -> bool {
  let Ok(mut stream) = TcpStream::connect(("127.0.0.1", port)) else { return false };
  let _ = stream.set_read_timeout(Some(Duration::from_secs(1)));
  let _ = stream.set_write_timeout(Some(Duration::from_secs(1)));
  let request = format!("GET /api/projects HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-OLCR-Session: {token}\r\nConnection: close\r\n\r\n");
  if stream.write_all(request.as_bytes()).is_err() { return false }
  let mut response = [0; 128];
  match stream.read(&mut response) { Ok(n) => response[..n].starts_with(b"HTTP/1.1 200 "), Err(_) => false }
}

fn valid_backend_dir(path: &Path) -> bool {
  path.is_dir() && path.join("olcr_api").is_dir() && path.join("olcr_cli").is_dir()
}

fn resolve_backend_dir(manifest_dir: &Path, override_dir: Option<&Path>) -> Result<(PathBuf, &'static str), String> {
  if let Some(path) = override_dir {
    if valid_backend_dir(path) { return Ok((path.to_path_buf(), "override")); }
    return Err("backend_directory_invalid".into());
  }
  let repository_root = manifest_dir
    .parent().and_then(Path::parent)
    .ok_or_else(|| "backend_directory_missing".to_string())?;
  let candidate = repository_root.join("backend");
  if valid_backend_dir(&candidate) { Ok((candidate, "manifest")) }
  else { Err("backend_directory_missing".into()) }
}

impl BackendProcessManager {
  fn start(&self) -> Result<(), String> {
    if self.child.lock().map_err(|_| "backend manager unavailable")?.is_some() { return Ok(()) }
    // Development uses an explicit runtime. Never guess a Python that may lack OLCR.
    let python=std::env::var("OLCR_PYTHON").map_err(|_| "Set OLCR_PYTHON to the OLCR virtual-environment Python before launching the desktop app.")?;
    let manifest_dir=PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let override_dir=env::var_os("OLCR_BACKEND_DIR").map(PathBuf::from);
    let (backend_dir, resolution_source)=resolve_backend_dir(&manifest_dir, override_dir.as_deref())?;
    eprintln!("backend_resolution_source={resolution_source}");
    eprintln!("backend_directory_valid=true");
    eprintln!("python_resolution_source=OLCR_PYTHON");
    let mut child=Command::new(python).args(["-m","uvicorn","olcr_api.app:app","--host","127.0.0.1","--port",&self.port.to_string(),"--log-level","warning"])
      .current_dir(&backend_dir).env("OLCR_GUI_SESSION_TOKEN",&self.token).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().map_err(|e|format!("Could not start OLCR backend: {e}"))?;
    if let Some(stderr) = child.stderr.take() {
      std::thread::spawn(move || for line in BufReader::new(stderr).lines().flatten() {
        eprintln!("OLCR_BACKEND_STDERR {line}");
      });
    }
    eprintln!("backend_spawn_started=true");
    eprintln!("backend_spawn_pid={}", child.id());
    let mut ready=false;
    for _ in 0..75 { std::thread::sleep(Duration::from_millis(200)); if let Some(status)=child.try_wait().map_err(|e|e.to_string())? { return Err(format!("OLCR backend exited during startup: {status}")) } if authenticated_ready(self.port,&self.token) { ready=true; break } }
    if !ready { let _=child.kill(); let _=child.wait(); return Err("backend_api_ready_timeout".into()) }
    eprintln!("backend_api_ready=true");
    *self.child.lock().map_err(|_| "backend manager unavailable")?=Some(child); Ok(())
  }
  fn stop(&self) { if let Ok(mut slot)=self.child.lock() { if let Some(mut child)=slot.take() { let _=child.kill(); let _=child.wait(); } } }
}
impl Drop for BackendProcessManager { fn drop(&mut self) { self.stop() } }
#[tauri::command] fn backend_config(state: tauri::State<BackendProcessManager>) -> BackendConfig { BackendConfig{api_url:format!("http://127.0.0.1:{}/api",state.port),session_token:state.token.clone()} }
fn main(){restore_web_credentials();let token:String=rand::thread_rng().sample_iter(&Alphanumeric).take(48).map(char::from).collect();let listener=std::net::TcpListener::bind("127.0.0.1:0").expect("loopback allocation failed");let port=listener.local_addr().unwrap().port();drop(listener);let manager=BackendProcessManager{child:Mutex::new(None),token,port};if let Err(error)=manager.start(){eprintln!("OLCR_BACKEND_STARTUP_FAILED category=backend_process error={error}");}tauri::Builder::default().plugin(tauri_plugin_dialog::init()).plugin(tauri_plugin_opener::init()).manage(manager).invoke_handler(tauri::generate_handler![backend_config,set_web_credential,clear_web_credential,open_external_url,read_dropped_file]).run(tauri::generate_context!()).expect("error running OLCR desktop");}

#[cfg(test)]
mod tests {
  use super::*;
  fn fixture() -> (PathBuf, PathBuf) {
    let root=env::temp_dir().join(format!("olcr-resolver-{}-{:?}", std::process::id(), std::thread::current().id()));
    let manifest=root.join("desktop/src-tauri"); let backend=root.join("backend");
    fs::create_dir_all(&manifest).unwrap(); fs::create_dir_all(backend.join("olcr_api")).unwrap(); fs::create_dir_all(backend.join("olcr_cli")).unwrap(); (root,manifest)
  }
  #[test] fn manifest_resolves_repository_backend() { let (root,manifest)=fixture(); let (found,source)=resolve_backend_dir(&manifest,None).unwrap(); assert_eq!(found,root.join("backend")); assert_eq!(source,"manifest"); assert_ne!(found,root.join("desktop/backend")); let _=fs::remove_dir_all(root); }
  #[test] fn valid_override_wins() { let (root,manifest)=fixture(); let override_dir=root.join("custom-backend"); fs::create_dir_all(override_dir.join("olcr_api")).unwrap(); fs::create_dir_all(override_dir.join("olcr_cli")).unwrap(); let (found,source)=resolve_backend_dir(&manifest,Some(&override_dir)).unwrap(); assert_eq!(found,override_dir); assert_eq!(source,"override"); let _=fs::remove_dir_all(root); }
  #[test] fn invalid_override_fails_safely() { let (root,manifest)=fixture(); let result=resolve_backend_dir(&manifest,Some(&root.join("missing"))); assert_eq!(result.unwrap_err(),"backend_directory_invalid"); let _=fs::remove_dir_all(root); }
}
