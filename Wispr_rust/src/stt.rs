use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::{Arc, OnceLock};
use parking_lot::RwLock;
use regex::Regex;
use sherpa_onnx::{OfflineRecognizer, OfflineRecognizerConfig, OfflineSenseVoiceModelConfig};
use whisper_rs::{FullParams, SamplingStrategy, WhisperContext, WhisperContextParameters};
use crate::hook::Language;

const SENSE_VOICE_MODEL_URL: &str =
    "https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/model.int8.onnx";
const SENSE_VOICE_TOKENS_URL: &str =
    "https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/tokens.txt";
const MODEL_RO_URL: &str =
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin";

pub struct WhisperEngine {
    sense_voice_recognizer: Arc<RwLock<Option<Arc<OfflineRecognizer>>>>,
    ro_context: Arc<RwLock<Option<Arc<WhisperContext>>>>,
    model_en_path: PathBuf,
    tokens_en_path: PathBuf,
    model_ro_path: PathBuf,
}

impl WhisperEngine {
    pub fn new(model_en_path: PathBuf, tokens_en_path: PathBuf, model_ro_path: PathBuf) -> Self {
        let engine = Self {
            sense_voice_recognizer: Arc::new(RwLock::new(None)),
            ro_context: Arc::new(RwLock::new(None)),
            model_en_path,
            tokens_en_path,
            model_ro_path,
        };

        // Try to load SenseVoice model immediately or spawn background download
        let en_recognizer_clone = engine.sense_voice_recognizer.clone();
        let model_path_clone = engine.model_en_path.clone();
        let tokens_path_clone = engine.tokens_en_path.clone();

        std::thread::spawn(move || {
            if let Err(e) = Self::ensure_model_file(&model_path_clone, SENSE_VOICE_MODEL_URL, "SenseVoiceSmall (int8.onnx)") {
                log::error!("Failed to prepare SenseVoice model file: {}", e);
                return;
            }

            if let Err(e) = Self::ensure_model_file(&tokens_path_clone, SENSE_VOICE_TOKENS_URL, "SenseVoice tokens (tokens.txt)") {
                log::error!("Failed to prepare SenseVoice tokens file: {}", e);
                return;
            }

            match Self::load_sense_voice(&model_path_clone, &tokens_path_clone) {
                Ok(recognizer) => {
                    log::info!("SenseVoiceSmall initialized successfully from {:?}", model_path_clone);
                    let mut guard = en_recognizer_clone.write();
                    *guard = Some(Arc::new(recognizer));
                }
                Err(e) => {
                    log::error!("Failed to initialize SenseVoiceSmall recognizer: {}", e);
                }
            }
        });

        engine
    }

    fn ensure_model_file(path: &Path, url: &str, model_name: &str) -> anyhow::Result<()> {
        if path.exists() {
            let metadata = fs::metadata(path)?;
            if metadata.len() > 100_000 {
                // File exists and is >100KB
                return Ok(());
            }
        }

        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }

        log::info!("Downloading {} from {} ...", model_name, url);

        let client = reqwest::blocking::Client::builder()
            .timeout(std::time::Duration::from_secs(600))
            .user_agent("wispr/0.1.0")
            .build()?;

        let mut resp = client.get(url).send()?;
        if !resp.status().is_success() {
            return Err(anyhow::anyhow!("Failed to download {}: HTTP {}", model_name, resp.status()));
        }

        let total_size = resp.content_length().unwrap_or(0);
        log::info!("File size: {} MB", total_size / (1024 * 1024));

        let tmp_path = path.with_extension("tmp");
        let mut file = File::create(&tmp_path)?;

        let mut downloaded: u64 = 0;
        let mut buffer = [0u8; 65536];

        use std::io::Read;
        loop {
            let bytes_read = resp.read(&mut buffer)?;
            if bytes_read == 0 {
                break;
            }
            file.write_all(&buffer[..bytes_read])?;
            downloaded += bytes_read as u64;
            if total_size > 0 && downloaded % (10 * 1024 * 1024) < 65536 {
                log::info!(
                    "Downloading {}: {} / {} MB ({}%)",
                    model_name,
                    downloaded / (1024 * 1024),
                    total_size / (1024 * 1024),
                    (downloaded * 100) / total_size
                );
            }
        }

        file.flush()?;
        drop(file);

        fs::rename(&tmp_path, path)?;
        log::info!("Downloaded {} successfully to {:?}", model_name, path);
        Ok(())
    }

    fn load_sense_voice(model_path: &Path, tokens_path: &Path) -> anyhow::Result<OfflineRecognizer> {
        let mut config = OfflineRecognizerConfig::default();
        config.model_config.sense_voice = OfflineSenseVoiceModelConfig {
            model: Some(model_path.to_str().ok_or_else(|| anyhow::anyhow!("Invalid model path"))?.to_string()),
            language: Some("en".to_string()),
            use_itn: true,
        };
        config.model_config.tokens = Some(tokens_path.to_str().ok_or_else(|| anyhow::anyhow!("Invalid tokens path"))?.to_string());
        config.model_config.num_threads = num_cpus();
        config.model_config.provider = Some("cpu".to_string());
        config.model_config.debug = false;

        OfflineRecognizer::create(&config)
            .ok_or_else(|| anyhow::anyhow!("Failed to create sherpa-onnx SenseVoice recognizer"))
    }

    fn load_whisper_context(path: &Path) -> anyhow::Result<WhisperContext> {
        let path_str = path.to_str().ok_or_else(|| anyhow::anyhow!("Invalid path"))?;
        let params = WhisperContextParameters::default();
        let ctx = WhisperContext::new_with_params(path_str, params)?;
        Ok(ctx)
    }

    pub fn has_ro_loaded(&self) -> bool {
        self.ro_context.read().is_some()
    }

    pub fn unload_ro(&self) {
        let mut guard = self.ro_context.write();
        *guard = None;
        log::info!("Unloaded Romanian Whisper model to free memory.");
    }

    pub fn ensure_en_loaded(&self) -> anyhow::Result<Arc<OfflineRecognizer>> {
        {
            let guard = self.sense_voice_recognizer.read();
            if let Some(ref recognizer) = *guard {
                return Ok(recognizer.clone());
            }
        }

        Self::ensure_model_file(&self.model_en_path, SENSE_VOICE_MODEL_URL, "SenseVoiceSmall (int8.onnx)")?;
        Self::ensure_model_file(&self.tokens_en_path, SENSE_VOICE_TOKENS_URL, "SenseVoice tokens (tokens.txt)")?;
        let recognizer = Arc::new(Self::load_sense_voice(&self.model_en_path, &self.tokens_en_path)?);
        let mut guard = self.sense_voice_recognizer.write();
        *guard = Some(recognizer.clone());
        Ok(recognizer)
    }

    pub fn ensure_ro_loaded(&self) -> anyhow::Result<Arc<WhisperContext>> {
        {
            let guard = self.ro_context.read();
            if let Some(ref ctx) = *guard {
                return Ok(ctx.clone());
            }
        }

        Self::ensure_model_file(&self.model_ro_path, MODEL_RO_URL, "Romanian (large-v3-turbo)")?;
        let ctx = Arc::new(Self::load_whisper_context(&self.model_ro_path)?);
        let mut guard = self.ro_context.write();
        *guard = Some(ctx.clone());
        Ok(ctx)
    }

    pub fn transcribe(&self, audio: &[f32], lang: Language) -> anyhow::Result<String> {
        if audio.is_empty() {
            return Ok(String::new());
        }

        match lang {
            Language::English => {
                log::info!(
                    "Starting SenseVoiceSmall transcription ({} audio samples, {:.2}s)...",
                    audio.len(),
                    audio.len() as f32 / 16000.0
                );

                let recognizer = self.ensure_en_loaded()?;
                let stream = recognizer.create_stream();
                stream.accept_waveform(16000, audio);
                recognizer.decode(&stream);

                let raw_text = stream.get_result().map(|r| r.text).unwrap_or_default();

                // Strip SenseVoice control/metadata tags: <|en|>, <|NEUTRAL|>, <|Speech|>, <|withitn|>
                static TAG_RE: OnceLock<Regex> = OnceLock::new();
                let tag_re = TAG_RE.get_or_init(|| Regex::new(r"<\|[^|]+\|>").unwrap());
                let cleaned = tag_re.replace_all(&raw_text, "").trim().to_string();

                log::info!("SenseVoice transcription result: \"{}\"", cleaned);
                Ok(cleaned)
            }
            Language::Romanian => {
                log::info!(
                    "Starting Whisper transcription for Romanian ({} audio samples, {:.2}s)...",
                    audio.len(),
                    audio.len() as f32 / 16000.0
                );

                let ctx = self.ensure_ro_loaded()?;
                let mut state = ctx.create_state()?;

                let mut params = FullParams::new(SamplingStrategy::Greedy { best_of: 1 });
                params.set_print_special(false);
                params.set_print_progress(false);
                params.set_print_realtime(false);
                params.set_print_timestamps(false);
                params.set_suppress_blank(true);
                params.set_suppress_nst(true);
                params.set_n_threads(num_cpus());
                params.set_language(Some("ro"));

                state.full(params, audio)?;

                let mut result = String::new();
                for segment in state.as_iter() {
                    if let Ok(segment_text) = segment.to_str_lossy() {
                        if !result.is_empty() {
                            result.push(' ');
                        }
                        result.push_str(segment_text.trim());
                    }
                }

                log::info!("Whisper Romanian transcription result: \"{}\"", result);
                Ok(result.trim().to_string())
            }
        }
    }
}

fn num_cpus() -> i32 {
    let count = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4);
    (count.min(6) as i32).max(1)
}
