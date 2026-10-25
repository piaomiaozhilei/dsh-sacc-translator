mod ocr;
mod desktop;
use serde::{Deserialize, Serialize};
use std::io::{self, Write};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Segment {
    pub id: String,
    pub text: String,
    pub left: i32,
    pub top: i32,
    pub width: i32,
    pub height: i32,
    pub confidence: f32,
    #[serde(default)]
    pub translation: String,
}
pub fn emit(value: serde_json::Value) {
    let mut out = io::stdout().lock();
    let _ = writeln!(out, "{}", value);
    let _ = out.flush();
}
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let result = if args.get(1).map(String::as_str) == Some("--ocr") {
        ocr::worker(&args)
    } else if args.get(1).map(String::as_str) == Some("--benchmark") {
        ocr::benchmark(&args)
    } else {
        desktop::run()
    };
    if let Err(error) = result {
        emit(serde_json::json!({"type":"error","error":error.to_string()}));
        std::process::exit(1);
    }
}

