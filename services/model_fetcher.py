import requests

MODELS_ENDPOINT = "/v1/models"
TIMEOUT_SECONDS = 10


def fetch_models(base_url: str, api_key: str) -> list[str]:
    """Fetch available model IDs from /v1/models. Raises on failure."""
    url = base_url.rstrip("/") + MODELS_ENDPOINT
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    resp = requests.get(url, headers=headers, timeout=TIMEOUT_SECONDS)
    resp.raise_for_status()
    data = resp.json()
    models = []
    for item in data.get("data", []):
        model_id = item.get("id", "")
        if model_id:
            models.append(model_id)
    if not models:
        raise ValueError("模型列表为空")
    return sorted(models)
