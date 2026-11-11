#[cfg(target_os = "macos")]
fn emit_debug_swift_runtime_rpaths() {
    use std::{env, fs, path::PathBuf, process::Command};

    if env::var("PROFILE").as_deref() != Ok("debug") {
        return;
    }
    println!("cargo:rerun-if-env-changed=DEVELOPER_DIR");

    let developer_dir = env::var_os("DEVELOPER_DIR")
        .map(PathBuf::from)
        .filter(|path| path.is_dir())
        .or_else(|| {
            Command::new("xcode-select")
                .arg("-p")
                .output()
                .ok()
                .filter(|output| output.status.success())
                .map(|output| PathBuf::from(String::from_utf8_lossy(&output.stdout).trim()))
                .filter(|path| path.is_dir())
        });
    let Some(developer_dir) = developer_dir else {
        return;
    };

    let swift_lib = developer_dir.join("Toolchains/XcodeDefault.xctoolchain/usr/lib");
    let direct_runtime = swift_lib.join("swift/macosx");
    if direct_runtime.join("libswift_Concurrency.dylib").is_file() {
        println!("cargo:rustc-link-arg=-Wl,-rpath,{}", direct_runtime.display());
    }
    let Ok(entries) = fs::read_dir(&swift_lib) else {
        return;
    };
    for entry in entries.flatten() {
        let name = entry.file_name();
        if !name.to_string_lossy().starts_with("swift-") {
            continue;
        }
        let runtime = entry.path().join("macosx");
        if runtime.join("libswift_Concurrency.dylib").is_file() {
            println!("cargo:rustc-link-arg=-Wl,-rpath,{}", runtime.display());
        }
    }
}

fn main() {
    println!("cargo:rerun-if-env-changed=OLCR_API_REPAIR_BUILD_ID");
    // A direct latest-source `cargo build` embeds frontend/dist.  Watch its
    // manifest so the Desktop binary cannot silently retain a previous UI that
    // falls back to a relative /health request after the frontend was rebuilt.
    println!("cargo:rerun-if-changed=../../frontend/dist/index.html");
    println!("cargo:rerun-if-changed=tauri.conf.json");
    println!("cargo:rerun-if-changed=Info.plist");
    #[cfg(target_os = "macos")]
    emit_debug_swift_runtime_rpaths();
    tauri_build::build()
}
