//! Microphone capture as mono 16 kHz `f32` chunks, without exposing the audio backend.
//!
//! ALSA (which PipeWire and PulseAudio serve) on Linux, WASAPI on Windows, CoreAudio on macOS.
//! The device runs on a dedicated thread because some backends' streams are not `Send`.

use std::sync::mpsc as std_mpsc;
use std::thread::JoinHandle;
use std::time::Duration;

use cpal::traits::{DeviceTrait as _, HostTrait as _, StreamTrait as _};
use tokio::sync::mpsc;

/// Output rate: what speech models such as Whisper consume.
pub const SAMPLE_RATE: u32 = 16_000;
/// Chunks buffered before the oldest are dropped (about 10 s at typical callback sizes).
const BUFFERED_CHUNKS: usize = 512;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AudioError {
    /// No input device is present (common on CI runners and servers).
    NoDevice,
    /// The OS refused access (macOS microphone permission, Windows privacy setting).
    Denied,
    /// The device exists but could not be opened or started.
    Unavailable,
}

/// Mix interleaved frames down to one channel.
pub(crate) fn downmix(interleaved: &[f32], channels: usize, out: &mut Vec<f32>) {
    let channels = channels.max(1);
    out.extend(
        interleaved
            .chunks_exact(channels)
            .map(|frame| frame.iter().sum::<f32>() / channels as f32),
    );
}

/// Streaming linear resampler that carries its phase across chunks.
pub(crate) struct Resampler {
    step: f64,
    pos: f64,
    prev: f32,
}

impl Resampler {
    pub(crate) fn new(from_rate: u32, to_rate: u32) -> Self {
        Self {
            step: f64::from(from_rate) / f64::from(to_rate),
            pos: 1.0,
            prev: 0.0,
        }
    }

    pub(crate) fn push(&mut self, input: &[f32], out: &mut Vec<f32>) {
        if input.is_empty() {
            return;
        }
        // Index 0 is the previous chunk's last sample, so interpolation spans chunk borders.
        let sample = |i: usize| if i == 0 { self.prev } else { input[i - 1] };
        let last = input.len() as f64;
        let mut t = self.pos;
        while t <= last {
            let i = t.floor() as usize;
            let frac = (t - i as f64) as f32;
            let a = sample(i);
            let b = if i as f64 >= last { a } else { sample(i + 1) };
            out.push(a + (b - a) * frac);
            t += self.step;
        }
        self.pos = t - last;
        self.prev = input[input.len() - 1];
    }
}

/// A running capture from the default input device. Dropping it stops the device.
pub struct Capture {
    chunks: mpsc::Receiver<Vec<f32>>,
    stop: Option<std_mpsc::Sender<()>>,
    thread: Option<JoinHandle<()>>,
    device_rate: u32,
}

impl Capture {
    /// Open and start the default input device.
    pub async fn open_default() -> Result<Self, AudioError> {
        let (chunk_tx, chunks) = mpsc::channel(BUFFERED_CHUNKS);
        let (ready_tx, ready_rx) = std_mpsc::sync_channel(1);
        let (stop, stop_rx) = std_mpsc::channel::<()>();
        let thread = std::thread::Builder::new()
            .name("kernal-api-audio".into())
            .spawn(move || run_device(chunk_tx, ready_tx, stop_rx))
            .map_err(|_| AudioError::Unavailable)?;
        let device_rate = tokio::task::spawn_blocking(move || ready_rx.recv())
            .await
            .map_err(|_| AudioError::Unavailable)?
            .map_err(|_| AudioError::Unavailable)??;
        Ok(Self {
            chunks,
            stop: Some(stop),
            thread: Some(thread),
            device_rate,
        })
    }

    /// The device's native rate, before resampling to [`SAMPLE_RATE`].
    pub fn device_rate(&self) -> u32 {
        self.device_rate
    }

    /// Take one buffered chunk without waiting.
    pub fn try_chunk(&mut self) -> Option<Vec<f32>> {
        self.chunks.try_recv().ok()
    }

    /// Wait up to `budget` for the next chunk; `None` on timeout or when the device stopped.
    pub async fn next_chunk_within(&mut self, budget: Duration) -> Option<Vec<f32>> {
        tokio::time::timeout(budget, self.chunks.recv())
            .await
            .ok()
            .flatten()
    }
}

impl Drop for Capture {
    fn drop(&mut self) {
        drop(self.stop.take());
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

fn open_error(err: &dyn std::fmt::Display) -> AudioError {
    let text = err.to_string().to_ascii_lowercase();
    if text.contains("permission") || text.contains("denied") || text.contains("access") {
        AudioError::Denied
    } else {
        AudioError::Unavailable
    }
}

fn run_device(
    chunks: mpsc::Sender<Vec<f32>>,
    ready: std_mpsc::SyncSender<Result<u32, AudioError>>,
    stop: std_mpsc::Receiver<()>,
) {
    let started = (|| {
        let device = cpal::default_host()
            .default_input_device()
            .ok_or(AudioError::NoDevice)?;
        let config = device.default_input_config().map_err(|e| open_error(&e))?;
        let rate = config.sample_rate();
        let channels = usize::from(config.channels());
        let mut resampler = Resampler::new(rate, SAMPLE_RATE);
        let mut mono = Vec::new();
        let stream = device
            .build_input_stream(
                config.config(),
                move |data: &[f32], _: &cpal::InputCallbackInfo| {
                    mono.clear();
                    downmix(data, channels, &mut mono);
                    let mut out = Vec::with_capacity(mono.len());
                    resampler.push(&mono, &mut out);
                    let _ = chunks.try_send(out);
                },
                |_err| {},
                None,
            )
            .map_err(|e| open_error(&e))?;
        stream.play().map_err(|e| open_error(&e))?;
        Ok((stream, rate))
    })();
    match started {
        Ok((stream, rate)) => {
            let _ = ready.send(Ok(rate));
            // Blocks until `Capture` drops its sender; the stream lives on this thread.
            let _ = stop.recv();
            drop(stream);
        }
        Err(err) => {
            let _ = ready.send(Err(err));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn downmix_averages_each_frame() {
        let mut out = Vec::new();
        downmix(&[1.0, 0.0, 0.5, 0.5, -1.0, 1.0], 2, &mut out);
        assert_eq!(out, [0.5, 0.5, 0.0]);
    }

    #[test]
    fn resampling_48k_to_16k_keeps_duration_across_chunks() {
        let mut r = Resampler::new(48_000, SAMPLE_RATE);
        let mut out = Vec::new();
        for _ in 0..10 {
            r.push(&[0.25; 480], &mut out);
        }
        // 4800 input samples at 48 kHz = 0.1 s = 1600 samples at 16 kHz.
        assert!((1599..=1601).contains(&out.len()), "{}", out.len());
        assert!(out[10..].iter().all(|s| (s - 0.25).abs() < 1e-6));
    }

    #[test]
    fn resampling_interpolates_a_ramp() {
        let mut r = Resampler::new(2, 4);
        let mut out = Vec::new();
        r.push(&[0.0, 1.0], &mut out);
        r.push(&[2.0], &mut out);
        assert_eq!(out, [0.0, 0.5, 1.0, 1.5, 2.0]);
    }

    #[tokio::test]
    async fn open_default_reports_a_typed_outcome() {
        // CI runners have no microphone; either outcome must arrive without hanging or panicking.
        match Capture::open_default().await {
            Ok(mut capture) => {
                assert!(capture.device_rate() > 0);
                let _ = capture.next_chunk_within(Duration::from_secs(2)).await;
            }
            Err(err) => assert!(matches!(
                err,
                AudioError::NoDevice | AudioError::Unavailable | AudioError::Denied
            )),
        }
    }
}
