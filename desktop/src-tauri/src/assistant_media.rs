// Restrict fullscreen changes to the window that invoked the command.
#[tauri::command]
pub fn assistant_player_fullscreen(window: tauri::Window, enabled: Option<bool>) -> Result<bool, String> {
    let previous = window.is_fullscreen().map_err(|error| error.to_string())?;
    if let Some(enabled) = enabled {
        if previous != enabled {
            window.set_fullscreen(enabled).map_err(|error| error.to_string())?;
        }
    }
    Ok(previous)
}
