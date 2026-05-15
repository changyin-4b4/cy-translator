import requests

CHAT_ENDPOINT = "/v1/chat/completions"
TIMEOUT_SECONDS = 120


def translate(base_url: str, api_key: str, model: str, system_prompt: str, user_text: str) -> str:
    """Send a translation request. Returns the assistant's reply text."""
    url = base_url.rstrip("/") + CHAT_ENDPOINT
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"请将以下内容翻译为中文，使用 Markdown 格式输出，只输出翻译结果，不要解释、不要额外说明：\n\n{user_text}"},
        ],
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT_SECONDS)
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices", [])
    if not choices:
        raise ValueError("LLM 返回结果中没有 choices")
    message = choices[0].get("message", {})
    content = message.get("content", "")
    if not content:
        raise ValueError("LLM 返回的消息内容为空")
    return content
