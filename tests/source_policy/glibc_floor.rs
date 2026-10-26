//! fbuild links this crate into manylinux_2_17 binaries and wheels (#395).
//!
//! A libc wrapper newer than glibc 2.17 breaks fbuild's release link, or its
//! wheel at import time on an older distro, even when the kernel call itself
//! is older. Call these through `libc::syscall(libc::SYS_…)` with a runtime
//! fallback instead (manylinux2014's kernel floor is Linux 3.10). The
//! link-time `objdump -T` check that #395 also asks for belongs in CI.

use std::path::Path;

use super::facade_policy::rust_sources;

/// Wrappers newer than glibc 2.17, with the release that added each.
const NEWER_THAN_FLOOR: &[(&str, &str)] = &[
    ("memfd_create", "2.27"),
    ("getrandom", "2.25"),
    ("copy_file_range", "2.27"),
    ("close_range", "2.34"),
    ("pidfd_open", "2.36"),
    ("pidfd_send_signal", "2.36"),
    ("pidfd_getfd", "2.36"),
    ("statx", "2.28"),
    ("renameat2", "2.28"),
    ("gettid", "2.30"),
];

#[test]
fn no_libc_wrapper_newer_than_the_glibc_2_17_floor() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut found = Vec::new();
    for path in rust_sources(&root) {
        let text = std::fs::read_to_string(&path).expect("read source");
        for (line_number, line) in text.lines().enumerate() {
            if line.trim_start().starts_with("//") {
                continue;
            }
            for (name, glibc) in NEWER_THAN_FLOOR {
                let token = format!("libc::{name}");
                let calls_wrapper = line.match_indices(&token).any(|(at, _)| {
                    !line[at + token.len()..]
                        .starts_with(|next: char| next.is_ascii_alphanumeric() || next == '_')
                });
                if calls_wrapper {
                    found.push(format!(
                        "{}:{}: libc::{name} needs glibc {glibc}",
                        path.display(),
                        line_number + 1
                    ));
                }
            }
        }
    }
    assert!(
        found.is_empty(),
        "use libc::syscall(libc::SYS_…) with a fallback instead:\n{}",
        found.join("\n")
    );
}
