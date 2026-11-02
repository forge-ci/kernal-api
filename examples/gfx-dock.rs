//! A top-edge bar drawn through `kernal_api::gfx`: `Placement::Dock`.
//!
//! ```console
//! soldr cargo run --example gfx-dock --features gfx-egui
//! soldr cargo run --example gfx-dock --features gfx-egui -- --screenshot /tmp/dock.pam
//! soldr cargo run --example gfx-dock --features gfx-egui -- --fit center
//! ```
//!
//! The desktop reserves the bar's space, so maximised windows stop below it.
//! Where docks are unavailable (X11, GNOME, macOS, Windows today) the program
//! prints why and exits. `--screenshot PATH` writes the bar's own pixels as a
//! PAM image after a few frames and exits. `--fit start|center|end` makes the
//! bar only as long as its content (measured with `Frame::text_width`) at
//! that end of the edge; the desktop still reserves its full thickness.

use std::io::Write as _;
use std::time::{SystemTime, UNIX_EPOCH};

use kernal_api::gfx::{
    self, App, DockAlign, DockLength, Edge, Event, Frame, Placement, Support, WindowOptions,
};

const HEIGHT: f32 = 36.0;
/// Frames drawn before `--screenshot` asks for the bar's pixels.
const SETTLE: u32 = 5;

struct Bar {
    frames: u32,
    clicks: u32,
    out: Option<std::path::PathBuf>,
    /// `--fit`: the length last asked for, so it is only asked again on change.
    fit: Option<f32>,
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
        let mut used = 0.0;
        frame.horizontal(|row| {
            let full = row.available_width();
            row.label("talos");
            row.separator();
            row.label(&clock());
            clicked = row.button(&format!("clicked {clicks}"));
            used = full - row.available_width();
        });
        if let Some(asked) = self.fit {
            // Content plus the frame's margin on both sides, rounded up so
            // the last widget never wraps; ignores sub-pixel jitter.
            let wanted = (used + 16.0).ceil();
            if (wanted - asked).abs() >= 1.0 {
                if let Err(err) = frame.set_dock_length(DockLength::Fixed(wanted)) {
                    eprintln!("fit: {err}");
                }
                self.fit = Some(wanted);
                println!("fit: dock length {wanted}");
            }
        }
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
    let (mut out, mut align) = (None, None);
    while let Some(arg) = args.next() {
        match (arg.as_str(), args.next().as_deref()) {
            ("--screenshot", Some(path)) => out = Some(path.into()),
            ("--fit", Some("start")) => align = Some(DockAlign::Start),
            ("--fit", Some("center")) => align = Some(DockAlign::Center),
            ("--fit", Some("end")) => align = Some(DockAlign::End),
            _ => {
                eprintln!("usage: gfx-dock [--screenshot OUT.pam] [--fit start|center|end]");
                std::process::exit(2);
            }
        }
    }
    let caps = gfx::capabilities();
    println!("gfx dock support: {:?}", caps.dock);
    if caps.dock == Support::Unsupported {
        eprintln!("no dock here; run explains why:");
    }
    let options = WindowOptions::new("talos-bar")
        .placement(Placement::Dock {
            edge: Edge::Top,
            thickness: HEIGHT,
        })
        .dock_align(align.unwrap_or_default());
    gfx::run(
        options,
        Bar {
            frames: 0,
            clicks: 0,
            out,
            fit: align.map(|_| 0.0),
        },
    )
}
