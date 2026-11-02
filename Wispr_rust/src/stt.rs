use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use parking_lot::RwLock;
use whisper_rs::{FullParams, SamplingStrategy, WhisperContext, WhisperContextParameters};
use crate::hook::Language;

const MODEL_EN_URL: &str = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin";
const MODEL_RO_URL: &str = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin";

pub struct WhisperEngine {
    en_context: Arc<RwLock<Option<Arc<WhisperContext>>>>,
    ro_context: Arc<RwLock<Option<Arc<WhisperContext>>>>,
    model_en_path: PathBuf,
    model_ro_path: PathBuf,
}

impl WhisperEngine {
    pub fn new(model_en_path: PathBuf, model_ro_path: PathBuf) -> Self {
        let engine = Self {
            en_context: Arc::new(RwLock::new(None)),
            ro_context: Arc::new(RwLock::new(None)),
            model_en_path,
            model_ro_path,
        };

        // Try to load English model immediately or spawn background download
        let en_ctx_clone = engine.en_context.clone();
        let en_path_clone = engine.model_en_path.clone();

        std::thread::spawn(move || {
            if let Err(e) = Self::ensure_model_file(&en_path_clone, MODEL_EN_URL, "English (small.en)") {
                log::error!("Failed to prepare English Whisper model: {}", e);
                return;
            }

            match Self::load_context(&en_path_clone) {
                Ok(ctx) => {
                    log::info!("English Whisper model initialized successfully from {:?}", en_path_clone);
                    let mut guard = en_ctx_clone.write();
                    *guard = Some(Arc::new(ctx));
                }
                Err(e) => {
                    log::error!("Failed to load English Whisper model: {}", e);
                }
            }
        });

        engine
    }

    fn ensure_model_file(path: &Path, url: &str, model_name: &str) -> anyhow::Result<()> {
        if path.exists() {
            let metadata = fs::metadata(path)?;
            if metadata.len() > 10_000_000 {
                // Model exists and is >10MB
                return Ok(());
            }
        }

        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }

        log::info!("Downloading {} model from {} ...", model_name, url);

        let client = reqwest::blocking::Client::builder()
            .timeout(std::time::Duration::from_secs(600))
            .build()?;

        let mut resp = client.get(url).send()?;
        if !resp.status().is_success() {
            return Err(anyhow::anyhow!("Failed to download model: HTTP {}", resp.status()));
        }

        let total_size = resp.content_length().unwrap_or(0);
        log::info!("Model size: {} MB", total_size / (1024 * 1024));

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
            if total_size > 0 && downloaded % (20 * 1024 * 1024) < 65536 {
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
        log::info!("Downloaded {} model successfully to {:?}", model_name, path);
        Ok(())
    }

    fn load_context(path: &Path) -> anyhow::Result<WhisperContext> {
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

    pub fn ensure_en_loaded(&self) -> anyhow::Result<Arc<WhisperContext>> {
        {
            let guard = self.en_context.read();
            if let Some(ref ctx) = *guard {
                return Ok(ctx.clone());
            }
        }

        Self::ensure_model_file(&self.model_en_path, MODEL_EN_URL, "English (small.en)")?;
        let ctx = Arc::new(Self::load_context(&self.model_en_path)?);
        let mut guard = self.en_context.write();
        *guard = Some(ctx.clone());
        Ok(ctx)
    }

    pub fn ensure_ro_loaded(&self) -> anyhow::Result<Arc<WhisperContext>> {
        {
            let guard = self.ro_context.read();
            if let Some(ref ctx) = *guard {
                return Ok(ctx.clone());
            }
        }

        Self::ensure_model_file(&self.model_ro_path, MODEL_RO_URL, "Romanian (large-v3-turbo)")?;
        let ctx = Arc::new(Self::load_context(&self.model_ro_path)?);
        let mut guard = self.ro_context.write();
        *guard = Some(ctx.clone());
        Ok(ctx)
    }

    pub fn transcribe(&self, audio: &[f32], lang: Language) -> anyhow::Result<String> {
        if audio.is_empty() {
            return Ok(String::new());
        }

        log::info!("Starting Whisper transcription for {:?} ({} audio samples, {:.2}s)...", lang, audio.len(), audio.len() as f32 / 16000.0);

        let ctx = match lang {
            Language::English => self.ensure_en_loaded()?,
            Language::Romanian => self.ensure_ro_loaded()?,
        };

        let mut state = ctx.create_state()?;

        let mut params = FullParams::new(SamplingStrategy::Greedy { best_of: 1 });
        params.set_print_special(false);
        params.set_print_progress(false);
        params.set_print_realtime(false);
        params.set_print_timestamps(false);
        params.set_suppress_blank(true);
        params.set_suppress_nst(true);
        params.set_n_threads(num_cpus());

        match lang {
            Language::English => {
                params.set_language(Some("en"));
            }
            Language::Romanian => {
                params.set_language(Some("ro"));
            }
        }

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

        log::info!("Whisper transcription result: \"{}\"", result);
        Ok(result.trim().to_string())
    }
}

fn num_cpus() -> i32 {
    let count = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4);
    (count.min(6) as i32).max(1)
}
