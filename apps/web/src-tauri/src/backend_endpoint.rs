//! The one place the desktop shell learns where its backend lives.
//!
//! `TARS_BACKEND_URL` overrides the default, and every Rust caller, the
//! spawned Python child (via `BACKEND_PORT`) and the webviews (via an
//! injected global) all derive from this value, so the address cannot differ
//! between the thing being supervised and the things talking to it. Only a
//! loopback http address is accepted: this shell supervises a local process
//! and sends it a shutdown secret, so it must never be pointed elsewhere.
use std::sync::OnceLock;

pub const DEFAULT_BASE_URL: &str = "http://127.0.0.1:8000";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Endpoint {
    pub base_url: String,
    pub port: u16,
}

/// Pure so it is directly testable: `None` for anything but loopback http.
pub fn parse(raw: &str) -> Option<Endpoint> {
    let trimmed = raw.trim().trim_end_matches('/');
    let authority = trimmed.strip_prefix("http://")?;
    let (host, port) = authority.rsplit_once(':')?;
    if host != "127.0.0.1" && host != "localhost" {
        return None;
    }
    let port: u16 = port.parse().ok()?;
    if port == 0 {
        return None;
    }
    Some(Endpoint { base_url: format!("http://{host}:{port}"), port })
}

fn resolved() -> &'static Endpoint {
    static ENDPOINT: OnceLock<Endpoint> = OnceLock::new();
    ENDPOINT.get_or_init(|| {
        let default = || parse(DEFAULT_BASE_URL).expect("default endpoint is valid");
        match std::env::var("TARS_BACKEND_URL") {
            Ok(raw) if !raw.trim().is_empty() => parse(&raw).unwrap_or_else(|| {
                eprintln!("[TARS][backend] ignoring TARS_BACKEND_URL={raw:?}: only http://127.0.0.1:<port> is accepted");
                default()
            }),
            _ => default(),
        }
    })
}

pub fn base_url() -> &'static str {
    &resolved().base_url
}

pub fn port() -> u16 {
    resolved().port
}

pub fn url(path: &str) -> String {
    format!("{}{path}", base_url())
}

/// Installs the address before any page script runs, so every webview uses
/// the same backend the supervisor manages.
pub fn init_script() -> String {
    format!("window.__TARS_BACKEND_URL__ = {:?};", base_url())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_loopback_and_normalises_a_trailing_slash() {
        let parsed = parse("http://127.0.0.1:8123/").unwrap();
        assert_eq!(parsed.base_url, "http://127.0.0.1:8123");
        assert_eq!(parsed.port, 8123);
        assert_eq!(parse("http://localhost:9000").unwrap().port, 9000);
    }

    #[test]
    fn refuses_anything_that_is_not_loopback_http() {
        for bad in [
            "https://127.0.0.1:8000",
            "http://example.com:8000",
            "http://192.168.1.5:8000",
            "http://127.0.0.1",
            "http://127.0.0.1:0",
            "http://127.0.0.1:notaport",
            "127.0.0.1:8000",
            "",
        ] {
            assert_eq!(parse(bad), None, "{bad:?} must be refused");
        }
    }

    #[test]
    fn the_default_parses_to_the_historical_port() {
        assert_eq!(parse(DEFAULT_BASE_URL).unwrap().port, 8000);
    }
}
