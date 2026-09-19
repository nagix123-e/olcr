fn main() {
  // A direct latest-source `cargo build` embeds frontend/dist.  Watch its
  // manifest so the Desktop binary cannot silently retain a previous UI that
  // falls back to a relative /health request after the frontend was rebuilt.
  println!("cargo:rerun-if-changed=../../frontend/dist/index.html");
  println!("cargo:rerun-if-changed=tauri.conf.json");
  tauri_build::build()
}
