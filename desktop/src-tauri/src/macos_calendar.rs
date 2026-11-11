#[tauri::command]
pub async fn assistant_open_calendar(target: String) -> Result<(), String> {
    let args = match target.as_str() {
        "calendar" => vec!["-a", "Calendar"],
        "privacy" => {
            vec!["x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars"]
        }
        _ => return Err("CALENDAR_TARGET_INVALID".into()),
    };
    tauri::async_runtime::spawn_blocking(move || {
        std::process::Command::new("/usr/bin/open")
            .args(args)
            .status()
            .map_err(|_| "CALENDAR_OPEN_FAILED".to_string())
            .and_then(|status| {
                if status.success() {
                    Ok(())
                } else {
                    Err("CALENDAR_OPEN_FAILED".into())
                }
            })
    })
    .await
    .map_err(|_| "CALENDAR_OPEN_FAILED".to_string())?
}
