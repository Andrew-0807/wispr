use std::collections::HashMap;
use std::fs;
use std::path::PathBuf;
use std::sync::RwLock;
use regex::{Regex, RegexBuilder};
use serde::{Deserialize, Serialize};

pub const SAMPLE_RATE: u32 = 16_000;
#[allow(dead_code)]
pub const CHANNELS: u16 = 1;

pub const CLEANUP_SYSTEM_PROMPT: &str = "You are a voice transcription cleanup assistant. \
The user will give you raw speech-to-text output. \
Your job: remove filler words (um, uh, like, you know, etc.) and fix punctuation/capitalization. \
CRITICAL: preserve the user's exact words, phrasing, and meaning. \
Do NOT rephrase, reword, restructure, or 'improve' the text. \
Only remove filler words and add punctuation — change nothing else. \
Return ONLY the cleaned text. No explanations, no quotes, no preamble.";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum CleanupMode {
    Llm,
    Punct,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ProviderType {
    Gemini,
    OpenRouter,
    LmStudio,
    Ollama,
}

impl ProviderType {
    pub fn from_str(s: &str) -> Option<Self> {
        match s.to_lowercase().trim() {
            "gemini" => Some(Self::Gemini),
            "openrouter" => Some(Self::OpenRouter),
            "lmstudio" => Some(Self::LmStudio),
            "ollama" => Some(Self::Ollama),
            _ => None,
        }
    }

    #[allow(dead_code)]
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Gemini => "gemini",
            Self::OpenRouter => "openrouter",
            Self::LmStudio => "lmstudio",
            Self::Ollama => "ollama",
        }
    }
}

#[derive(Debug, Clone)]
pub struct AppConfig {
    pub provider: ProviderType,
    pub fallback_provider: Option<ProviderType>,
    pub cleanup_mode: CleanupMode,

    pub gemini_api_key: String,
    pub gemini_model: String,

    pub openrouter_api_key: String,
    pub openrouter_model: String,

    pub lmstudio_url: String,
    pub lmstudio_model: String,
    pub lmstudio_api_key: String,

    pub ollama_url: String,
    pub ollama_model: String,

    pub model_en_path: PathBuf,
    pub tokens_en_path: PathBuf,
    pub model_ro_path: PathBuf,
}

impl AppConfig {
    pub fn load() -> Self {
        let _ = dotenvy::dotenv();
        if let Ok(exe_path) = std::env::current_exe() {
            if let Some(exe_dir) = exe_path.parent() {
                let _ = dotenvy::from_path(exe_dir.join(".env"));
            }
        }

        let provider_str = std::env::var("LLM_PROVIDER").unwrap_or_else(|_| "gemini".to_string());
        let provider = ProviderType::from_str(&provider_str).unwrap_or(ProviderType::Gemini);

        let fallback_str = std::env::var("LLM_FALLBACK").unwrap_or_default();
        let fallback_provider = if fallback_str.is_empty() {
            None
        } else {
            ProviderType::from_str(&fallback_str)
        };

        let cleanup_mode_str = std::env::var("CLEANUP_MODE").unwrap_or_else(|_| "punct".to_string());
        let cleanup_mode = if cleanup_mode_str.eq_ignore_ascii_case("llm") {
            CleanupMode::Llm
        } else {
            CleanupMode::Punct
        };

        let gemini_api_key = std::env::var("GEMINI_API_KEY").unwrap_or_default();
        let gemini_model = std::env::var("GEMINI_MODEL").unwrap_or_else(|_| "gemini-2.0-flash".to_string());

        let openrouter_api_key = std::env::var("OPENROUTER_API_KEY").unwrap_or_default();
        let openrouter_model = std::env::var("OPENROUTER_MODEL")
            .unwrap_or_else(|_| "nvidia/nemotron-3-super-120b-a12b:free".to_string());

        let lmstudio_url = std::env::var("LMSTUDIO_URL").unwrap_or_else(|_| "http://localhost:1234/v1".to_string());
        let lmstudio_model = std::env::var("LMSTUDIO_MODEL").unwrap_or_else(|_| "qwen2.5-1.5b-instruct".to_string());
        let lmstudio_api_key = std::env::var("LMSTUDIO_API_KEY").unwrap_or_default();

        let ollama_url = std::env::var("OLLAMA_URL").unwrap_or_else(|_| "http://localhost:11434/v1".to_string());
        let ollama_model = std::env::var("OLLAMA_MODEL").unwrap_or_else(|_| "qwen2.5:1.5b".to_string());

        let model_en_path = find_model_path("sense-voice.int8.onnx");
        let tokens_en_path = find_model_path("sense-voice-tokens.txt");
        let model_ro_path = find_model_path("ggml-large-v3-turbo.bin");

        Self {
            provider,
            fallback_provider,
            cleanup_mode,
            gemini_api_key,
            gemini_model,
            openrouter_api_key,
            openrouter_model,
            lmstudio_url,
            lmstudio_model,
            lmstudio_api_key,
            ollama_url,
            ollama_model,
            model_en_path,
            tokens_en_path,
            model_ro_path,
        }
    }
}

fn find_model_path(filename: &str) -> PathBuf {
    let candidates = [
        PathBuf::from(filename),
        PathBuf::from("models").join(filename),
        PathBuf::from("..").join("models").join(filename),
        PathBuf::from("..").join("wispr_py").join("models").join(filename),
    ];

    for c in &candidates {
        if c.exists() {
            return c.clone();
        }
    }

    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let p1 = dir.join(filename);
            if p1.exists() {
                return p1;
            }
            let p2 = dir.join("models").join(filename);
            if p2.exists() {
                return p2;
            }
        }
    }

    PathBuf::from("models").join(filename)
}

struct WordRule {
    pattern: Regex,
    replacement: String,
}

pub struct CustomDictionary {
    rules: RwLock<Vec<WordRule>>,
}

impl CustomDictionary {
    pub fn new() -> Self {
        let dict = Self {
            rules: RwLock::new(Vec::new()),
        };
        dict.reload();
        dict
    }

    pub fn reload(&self) {
        let mut words_map: HashMap<String, String> = HashMap::new();

        let possible_paths = [
            PathBuf::from("words.json"),
            PathBuf::from("..").join("wispr_py").join("words.json"),
        ];

        for path in &possible_paths {
            if path.exists() {
                if let Ok(content) = fs::read_to_string(path) {
                    if let Ok(parsed) = serde_json::from_str::<HashMap<String, String>>(&content) {
                        words_map = parsed;
                        break;
                    }
                }
            }
        }

        let mut entries: Vec<(String, String)> = words_map.into_iter().collect();
        entries.sort_by_key(|b| std::cmp::Reverse(b.0.len()));

        let mut compiled = Vec::new();
        for (k, v) in entries {
            let escaped = regex::escape(k.trim());
            let pattern_str = format!(r"(?i)\b{}\b", escaped);
            if let Ok(re) = RegexBuilder::new(&pattern_str).build() {
                compiled.push(WordRule {
                    pattern: re,
                    replacement: v,
                });
            }
        }

        if let Ok(mut guard) = self.rules.write() {
            *guard = compiled;
        }
    }

    pub fn apply(&self, text: &str) -> String {
        let guard = match self.rules.read() {
            Ok(g) => g,
            Err(_) => return text.to_string(),
        };

        let mut result = text.to_string();
        for rule in guard.iter() {
            result = rule.pattern.replace_all(&result, rule.replacement.as_str()).to_string();
        }
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_provider_parsing() {
        assert_eq!(ProviderType::from_str("gemini"), Some(ProviderType::Gemini));
        assert_eq!(ProviderType::from_str("openrouter"), Some(ProviderType::OpenRouter));
        assert_eq!(ProviderType::from_str("LMSTUDIO"), Some(ProviderType::LmStudio));
        assert_eq!(ProviderType::from_str("ollama"), Some(ProviderType::Ollama));
        assert_eq!(ProviderType::from_str("unknown"), None);
    }

    #[test]
    fn test_custom_dictionary_replacement() {
        let dict = CustomDictionary::new();
        // Insert custom rule directly for testing
        {
            let mut guard = dict.rules.write().unwrap();
            guard.push(WordRule {
                pattern: RegexBuilder::new(r"(?i)\bcloud code\b").build().unwrap(),
                replacement: "Claude Code".to_string(),
            });
            guard.push(WordRule {
                pattern: RegexBuilder::new(r"(?i)\bcloud\b").build().unwrap(),
                replacement: "Claude".to_string(),
            });
        }

        let input = "I am using cloud code and cloud for programming";
        let output = dict.apply(input);
        assert_eq!(output, "I am using Claude Code and Claude for programming");
    }
}
