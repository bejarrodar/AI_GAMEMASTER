# LLM Setup

Use the **LLM Management** page in Streamlit to configure providers, models, and generation behavior.

## Provider Selection

- `ollama`: local or remote Ollama endpoint.
- `openai`: OpenAI or OpenAI-compatible endpoint.
- `stub`: fallback/testing provider.

## Ollama

- Set `AIGM_OLLAMA_URL`.
- Set default model `AIGM_OLLAMA_MODEL`.
- Optional task overrides:
  - `AIGM_OLLAMA_MODEL_NARRATION`
  - `AIGM_OLLAMA_MODEL_INTENT`
  - `AIGM_OLLAMA_MODEL_REVIEW`
- Use UI actions to:
  - test endpoint
  - list models
  - pull model
  - delete model

## OpenAI / Compatible

- Set `AIGM_OPENAI_API_KEY`.
- Optional `AIGM_OPENAI_BASE_URL` for compatible services.
- Set default model `AIGM_OPENAI_MODEL`.
- Optional task overrides:
  - `AIGM_OPENAI_MODEL_NARRATION`
  - `AIGM_OPENAI_MODEL_INTENT`
  - `AIGM_OPENAI_MODEL_REVIEW`

## JSON and Runtime Settings

- `AIGM_LLM_JSON_MODE_STRICT=true|false`
- `AIGM_OLLAMA_TIMEOUT_S`
- `AIGM_OPENAI_TIMEOUT_S`
- `AIGM_OLLAMA_GEN_TEMPERATURE`
- `AIGM_OLLAMA_JSON_TEMPERATURE`
- `AIGM_OLLAMA_GEN_NUM_PREDICT`
- `AIGM_OLLAMA_JSON_NUM_PREDICT`

## Apply Changes

- Click **Save LLM settings** in Streamlit.
- Restart stack services to apply the new environment values.
