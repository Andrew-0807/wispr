use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::Arc;
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{SampleFormat, Stream};
use parking_lot::Mutex;

const TARGET_SAMPLE_RATE: u32 = 16_000;
const MAX_RECORDING_SAMPLES: usize = TARGET_SAMPLE_RATE as usize * 60 * 30; // 30 mins cap

#[derive(Clone)]
pub struct RmsMeter {
    rms_level_bits: Arc<AtomicU32>,
}

impl RmsMeter {
    pub fn get_level(&self) -> f32 {
        let bits = self.rms_level_bits.load(Ordering::Relaxed);
        f32::from_bits(bits)
    }
}

pub struct AudioEngine {
    _stream: Stream,
    is_recording: Arc<AtomicBool>,
    audio_buffer: Arc<Mutex<Vec<f32>>>,
    rms_meter: RmsMeter,
}

// Safety: The cpal stream on Windows holds WASAPI COM interfaces.
// We keep AudioEngine on the main thread and share safe atomic/mutex handles.
unsafe impl Send for AudioEngine {}
unsafe impl Sync for AudioEngine {}

impl AudioEngine {
    pub fn new() -> anyhow::Result<Self> {
        let host = cpal::default_host();
        let device = host
            .default_input_device()
            .ok_or_else(|| anyhow::anyhow!("No default audio input device found"))?;

        let device_name = device.name().unwrap_or_else(|_| "Unknown".to_string());
        log::info!("Using audio input device: {}", device_name);

        let default_config = device.default_input_config()?;
        let sample_format = default_config.sample_format();
        let sample_rate = default_config.sample_rate().0;
        let channels = default_config.channels();

        log::info!(
            "Input format: {:?}, sample rate: {} Hz, channels: {}",
            sample_format,
            sample_rate,
            channels
        );

        let is_recording = Arc::new(AtomicBool::new(false));
        let audio_buffer = Arc::new(Mutex::new(Vec::with_capacity(TARGET_SAMPLE_RATE as usize * 10)));
        let rms_level_bits = Arc::new(AtomicU32::new(0));
        let rms_meter = RmsMeter {
            rms_level_bits: rms_level_bits.clone(),
        };

        let is_rec_clone = is_recording.clone();
        let buf_clone = audio_buffer.clone();
        let rms_clone = rms_level_bits.clone();

        let mut resampler_state = ResamplerState::new(sample_rate, channels);

        let err_fn = |err| {
            log::error!("Audio stream error: {}", err);
        };

        let stream = match sample_format {
            SampleFormat::F32 => device.build_input_stream(
                &default_config.into(),
                move |data: &[f32], _| {
                    process_audio(data, &mut resampler_state, &is_rec_clone, &buf_clone, &rms_clone);
                },
                err_fn,
                None,
            )?,
            SampleFormat::I16 => device.build_input_stream(
                &default_config.into(),
                move |data: &[i16], _| {
                    let f32_data: Vec<f32> = data.iter().map(|&s| s as f32 / i16::MAX as f32).collect();
                    process_audio(&f32_data, &mut resampler_state, &is_rec_clone, &buf_clone, &rms_clone);
                },
                err_fn,
                None,
            )?,
            SampleFormat::U16 => device.build_input_stream(
                &default_config.into(),
                move |data: &[u16], _| {
                    let f32_data: Vec<f32> = data
                        .iter()
                        .map(|&s| (s as f32 - u16::MAX as f32 / 2.0) / (u16::MAX as f32 / 2.0))
                        .collect();
                    process_audio(&f32_data, &mut resampler_state, &is_rec_clone, &buf_clone, &rms_clone);
                },
                err_fn,
                None,
            )?,
            _ => return Err(anyhow::anyhow!("Unsupported audio sample format: {:?}", sample_format)),
        };

        stream.play()?;

        Ok(Self {
            _stream: stream,
            is_recording,
            audio_buffer,
            rms_meter,
        })
    }

    pub fn rms_meter(&self) -> RmsMeter {
        self.rms_meter.clone()
    }

    pub fn start_recording(&self) {
        let mut buf = self.audio_buffer.lock();
        buf.clear();
        self.is_recording.store(true, Ordering::SeqCst);
    }

    pub fn stop_recording(&self) -> Vec<f32> {
        self.is_recording.store(false, Ordering::SeqCst);
        let mut buf = self.audio_buffer.lock();
        let samples = buf.clone();
        buf.clear();
        samples
    }

    #[allow(dead_code)]
    pub fn get_rms_level(&self) -> f32 {
        self.rms_meter.get_level()
    }
}

fn process_audio(
    input_samples: &[f32],
    state: &mut ResamplerState,
    is_recording: &Arc<AtomicBool>,
    buffer: &Arc<Mutex<Vec<f32>>>,
    rms_atomic: &Arc<AtomicU32>,
) {
    if input_samples.is_empty() {
        return;
    }

    // Convert multi-channel to mono
    let mono: Vec<f32> = if state.channels > 1 {
        let ch = state.channels as usize;
        input_samples
            .chunks_exact(ch)
            .map(|frame| frame.iter().sum::<f32>() / ch as f32)
            .collect()
    } else {
        input_samples.to_vec()
    };

    // Calculate RMS volume level
    let sum_sq: f32 = mono.iter().map(|&s| s * s).sum();
    let rms = (sum_sq / mono.len() as f32).sqrt();
    rms_atomic.store(rms.to_bits(), Ordering::Relaxed);

    // Resample to 16,000 Hz if needed
    let resampled = if state.input_rate == TARGET_SAMPLE_RATE {
        mono
    } else {
        state.resample(&mono)
    };

    // If actively recording, append to buffer
    if is_recording.load(Ordering::SeqCst) {
        let mut buf = buffer.lock();
        if buf.len() < MAX_RECORDING_SAMPLES {
            buf.extend_from_slice(&resampled);
        }
    }
}

struct ResamplerState {
    input_rate: u32,
    channels: u16,
    resample_pos: f64,
}

impl ResamplerState {
    fn new(input_rate: u32, channels: u16) -> Self {
        Self {
            input_rate,
            channels,
            resample_pos: 0.0,
        }
    }

    fn resample(&mut self, input: &[f32]) -> Vec<f32> {
        if input.is_empty() {
            return Vec::new();
        }

        let ratio = self.input_rate as f64 / TARGET_SAMPLE_RATE as f64;
        let estimated_len = ((input.len() as f64 - self.resample_pos) / ratio).max(0.0) as usize + 2;
        let mut output = Vec::with_capacity(estimated_len);

        while self.resample_pos < input.len() as f64 {
            let idx = self.resample_pos.floor() as usize;
            let frac = (self.resample_pos - idx as f64) as f32;

            if idx + 1 < input.len() {
                let sample = input[idx] * (1.0 - frac) + input[idx + 1] * frac;
                output.push(sample);
            } else if idx < input.len() {
                output.push(input[idx]);
            } else {
                break;
            }

            self.resample_pos += ratio;
        }

        self.resample_pos -= input.len() as f64;
        output
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_resampler_identity() {
        let mut resampler = ResamplerState::new(16000, 1);
        let input = vec![0.1, 0.2, 0.3, 0.4];
        let output = resampler.resample(&input);
        assert_eq!(input, output);
    }

    #[test]
    fn test_resampler_downsample() {
        let mut resampler = ResamplerState::new(48000, 1);
        // 48kHz to 16kHz is 3:1 ratio
        let input = vec![0.0; 480];
        let output = resampler.resample(&input);
        assert_eq!(output.len(), 160);
    }
}
