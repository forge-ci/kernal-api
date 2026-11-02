//! A top-edge bar drawn through `kernal_api::gfx`: `Placement::Dock`.
//!
//! ```console
//! soldr cargo run --example gfx-dock --features gfx-egui
//! soldr cargo run --example gfx-dock --features gfx-egui -- --screenshot /tmp/dock.pam
//! ```
//!
//! The desktop reserves the bar's space, so maximised windows stop below it.
//! Where docks are unavailable (X11, GNOME, macOS, Windows today) the program
//! prints why and exits. `--screenshot PATH` writes the bar's own pixels as a
//! PAM image after a few frames and exits.

use std::io::Write as _;
use std::time::{SystemTime, UNIX_EPOCH};

use kernal_api::gfx::{self, App, Edge, Event, Frame, Placement, Support, WindowOptions};

const HEIGHT: f32 = 36.0;
/// Frames drawn before `--screenshot` asks for the bar's pixels.
const SETTLE: u32 = 5;

struct Bar {
    frames: u32,
    clicks: u32,
    out: Option<std::path::PathBuf>,
}

/// `HH:MM:SS` UTC, without a time-zone database.
fn clock() -> String {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |since| since.as_secs())
        % 86_400;
    format!(
        "{:02}:{:02}:{:02} UTC",
        secs / 3600,
        secs / 60 % 60,
        secs % 60
    )
}

impl App for Bar {
    fn frame(&mut self, frame: &mut Frame<'_>) {
        let captured = frame.events().iter().find_map(|event| match event {
            Event::Screenshot {
                width,
                height,
                rgba,
            } => Some((*width, *height, rgba.clone())),
            _ => None,
        });
        if let Some((width, height, rgba)) = captured {
            if let Some(path) = self.out.take() {
                write_pam(&path, width, height, &rgba).expect("write capture");
                println!("{} {width}x{height}", path.display());
                frame.close();
            }
        }
        let clicks = self.clicks;
        let mut clicked = false;
        frame.horizontal(|row| {
            row.label("talos");
            row.separator();
            row.label(&clock());
            clicked = row.button(&format!("clicked {clicks}"));
        });
        if clicked {
            self.clicks += 1;
        }
        self.frames += 1;
        if self.out.is_some() && self.frames == SETTLE {
            frame.request_screenshot();
        }
        // Simplest clock: redraw every frame the compositor allows.
        frame.request_repaint();
    }
}

fn write_pam(path: &std::path::Path, width: u32, height: u32, rgba: &[u8]) -> std::io::Result<()> {
    let mut file = std::io::BufWriter::new(std::fs::File::create(path)?);
    write!(
        file,
        "P7\nWIDTH {width}\nHEIGHT {height}\nDEPTH 4\nMAXVAL 255\nTUPLTYPE RGB_ALPHA\nENDHDR\n"
    )?;
    file.write_all(rgba)?;
    file.flush()
}

fn main() -> Result<(), gfx::GfxError> {
    let mut args = std::env::args().skip(1);
    let out = match (args.next().as_deref(), args.next()) {
        (Some("--screenshot"), Some(path)) => Some(path.into()),
        (None, _) => None,
        _ => {
            eprintln!("usage: gfx-dock [--screenshot OUT.pam]");
            std::process::exit(2);
        }
    };
    let caps = gfx::capabilities();
    println!("gfx dock support: {:?}", caps.dock);
    if caps.dock == Support::Unsupported {
        eprintln!("no dock here; run explains why:");
    }
    let options = WindowOptions::new("talos-bar").placement(Placement::Dock {
        edge: Edge::Top,
        thickness: HEIGHT,
    });
    gfx::run(
        options,
        Bar {
            frames: 0,
            clicks: 0,
            out,
        },
    )
}
