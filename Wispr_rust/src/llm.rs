use std::time::Duration;
use reqwest::Client;
use serde_json::json;
use crate::config::{AppConfig, CleanupMode, ProviderType, CLEANUP_SYSTEM_PROMPT};

pub struct LlmEngine {
    client: Client,
    config: AppConfig,
}

impl LlmEngine {
    pub fn new(config: AppConfig) -> Self {
        let client = Client::builder()
            .timeout(Duration::from_millis(2500))
            .build()
            .unwrap_or_else(|_| Client::new());

        Self { client, config }
    }

    pub fn update_config(&mut self, config: AppConfig) {
        self.config = config;
    }

    pub async fn cleanup_text(&self, raw: &str) -> String {
        let trimmed = raw.trim();
        if trimmed.is_empty() {
            return String::new();
        }

        if self.config.cleanup_mode == CleanupMode::Punct {
            return local_punct(trimmed);
        }

        // Try primary provider
        match self.call_provider(self.config.provider, trimmed).await {
            Ok(cleaned) if !cleaned.trim().is_empty() => return cleaned.trim().to_string(),
            Err(e) => {
                log::warn!("Primary LLM ({:?}) failed: {}", self.config.provider, e);
            }
            _ => {}
        }

        // Try fallback provider if configured
        if let Some(fallback) = self.config.fallback_provider {
            match self.call_provider(fallback, trimmed).await {
                Ok(cleaned) if !cleaned.trim().is_empty() => return cleaned.trim().to_string(),
                Err(e) => {
                    log::warn!("Fallback LLM ({:?}) failed: {}", fallback, e);
                }
                _ => {}
            }
        }

        // Final graceful fallback: local punctuation
        local_punct(trimmed)
    }

    async fn call_provider(&self, provider: ProviderType, text: &str) -> anyhow::Result<String> {
        match provider {
            ProviderType::Gemini => self.call_gemini(text).await,
            ProviderType::OpenRouter => self.call_openrouter(text).await,
            ProviderType::LmStudio => self.call_lmstudio(text).await,
            ProviderType::Ollama => self.call_ollama(text).await,
        }
    }

    async fn call_gemini(&self, text: &str) -> anyhow::Result<String> {
        if self.config.gemini_api_key.is_empty() {
            return Err(anyhow::anyhow!("GEMINI_API_KEY is not set"));
        }

        let url = format!(
            "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent?key={}",
            self.config.gemini_model, self.config.gemini_api_key
        );

        let payload = json!({
            "system_instruction": {
                "parts": [{ "text": CLEANUP_SYSTEM_PROMPT }]
            },
            "contents": [{
                "parts": [{ "text": text }]
            }],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 2048
            }
        });

        let resp = self.client.post(&url).json(&payload).send().await?;
        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            return Err(anyhow::anyhow!("Gemini error {}: {}", status, body));
        }

        let body: serde_json::Value = resp.json().await?;
        if let Some(candidate) = body["candidates"].as_array().and_then(|a| a.first()) {
            if let Some(text_content) = candidate["content"]["parts"][0]["text"].as_str() {
                return Ok(text_content.to_string());
            }
        }

        Err(anyhow::anyhow!("Invalid response from Gemini API"))
    }

    async fn call_openrouter(&self, text: &str) -> anyhow::Result<String> {
        if self.config.openrouter_api_key.is_empty() {
            return Err(anyhow::anyhow!("OPENROUTER_API_KEY is not set"));
        }

        let url = "https://openrouter.ai/api/v1/chat/completions";
        let payload = json!({
            "model": self.config.openrouter_model,
            "messages": [
                { "role": "system", "content": CLEANUP_SYSTEM_PROMPT },
                { "role": "user", "content": text }
            ],
            "temperature": 0.1
        });

        let resp = self
            .client
            .post(url)
            .header("Authorization", format!("Bearer {}", self.config.openrouter_api_key))
            .header("HTTP-Referer", "https://github.com/wispr/wispr-rust")
            .header("X-Title", "Wispr Rust Dictation")
            .json(&payload)
            .send()
            .await?;

        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            return Err(anyhow::anyhow!("OpenRouter error {}: {}", status, body));
        }

        let body: serde_json::Value = resp.json().await?;
        if let Some(choice) = body["choices"].as_array().and_then(|a| a.first()) {
            if let Some(content) = choice["message"]["content"].as_str() {
                return Ok(content.to_string());
            }
        }

        Err(anyhow::anyhow!("Invalid response from OpenRouter API"))
    }

    async fn call_lmstudio(&self, text: &str) -> anyhow::Result<String> {
        let base = self.config.lmstudio_url.trim_end_matches('/');
        let url = format!("{}/chat/completions", base);

        let payload = json!({
            "model": self.config.lmstudio_model,
            "messages": [
                { "role": "system", "content": CLEANUP_SYSTEM_PROMPT },
                { "role": "user", "content": text }
            ],
            "temperature": 0.1
        });

        let mut req = self.client.post(&url).json(&payload);
        if !self.config.lmstudio_api_key.is_empty() {
            req = req.header("Authorization", format!("Bearer {}", self.config.lmstudio_api_key));
        }

        let resp = req.send().await?;
        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            return Err(anyhow::anyhow!("LM Studio error {}: {}", status, body));
        }

        let body: serde_json::Value = resp.json().await?;
        if let Some(choice) = body["choices"].as_array().and_then(|a| a.first()) {
            if let Some(content) = choice["message"]["content"].as_str() {
                return Ok(content.to_string());
            }
        }

        Err(anyhow::anyhow!("Invalid response from LM Studio"))
    }

    async fn call_ollama(&self, text: &str) -> anyhow::Result<String> {
        let base = self.config.ollama_url.trim_end_matches('/');
        let url = format!("{}/chat/completions", base);

        let payload = json!({
            "model": self.config.ollama_model,
            "messages": [
                { "role": "system", "content": CLEANUP_SYSTEM_PROMPT },
                { "role": "user", "content": text }
            ],
            "temperature": 0.1
        });

        let resp = self.client.post(&url).json(&payload).send().await?;
        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            return Err(anyhow::anyhow!("Ollama error {}: {}", status, body));
        }

        let body: serde_json::Value = resp.json().await?;
        if let Some(choice) = body["choices"].as_array().and_then(|a| a.first()) {
            if let Some(content) = choice["message"]["content"].as_str() {
                return Ok(content.to_string());
            }
        }

        Err(anyhow::anyhow!("Invalid response from Ollama"))
    }
}

pub fn local_punct(text: &str) -> String {
    let text = text.trim();
    if text.is_empty() {
        return String::new();
    }

    let mut chars: Vec<char> = text.chars().collect();
    if let Some(first) = chars.first_mut() {
        *first = first.to_uppercase().next().unwrap_or(*first);
    }

    let mut res: String = chars.into_iter().collect();
    if let Some(last) = res.chars().last() {
        if !matches!(last, '.' | '!' | '?' | ',' | ';' | ':') {
            res.push('.');
        }
    }

    res
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_local_punct() {
        assert_eq!(local_punct("hello world"), "Hello world.");
        assert_eq!(local_punct("already punctuated."), "Already punctuated.");
        assert_eq!(local_punct("what is this?"), "What is this?");
        assert_eq!(local_punct(""), "");
    }
}
