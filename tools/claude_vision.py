"""Small helper for structured Claude vision calls, shared by the image agent and the video scanner.

Same setup as app/server.py: key from ~/.openclaw/openclaw.json (env.vars.ANTHROPIC_API_KEY),
claude-opus-5-5, JSON-schema output, server-side refusal fallback.
"""
import base64, io, json, os

from PIL import Image

MODEL = "claude-opus-5-5"
OPENCLAW_CONFIG = os.path.expanduser("~/.openclaw/openclaw.json")


class ClaudeError(Exception):
    pass


def api_key():
    try:
        with open(OPENCLAW_CONFIG) as f:
            env = json.load(f)["env"]
        key = env.get("vars", {}).get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_API_KEY")
        if key:
            return key
    except Exception:
        pass
    return os.environ.get("ANTHROPIC_API_KEY", "")


_client = None


def client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=api_key(), max_retries=3)
    return _client


def image_block(img, max_px=1568, quality=85):
    """PIL image (or path) -> base64 image content block."""
    if not isinstance(img, Image.Image):
        img = Image.open(img)
    img = img.convert("RGB")
    if max(img.size) > max_px:
        img = img.copy()
        img.thumbnail((max_px, max_px), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(buf.getvalue()).decode()}}


def ask_json(content, schema, effort="medium", max_tokens=16000):
    """One user turn (list of content blocks) -> dict matching `schema`. Raises ClaudeError."""
    import anthropic
    try:
        with client().beta.messages.stream(
            model=MODEL,
            max_tokens=max_tokens,
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default",
                        "output_config": {"effort": effort,
                                          "format": {"type": "json_schema", "schema": schema}}},
            messages=[{"role": "user", "content": content}],
        ) as stream:
            resp = stream.get_final_message()
    except anthropic.APIStatusError as e:
        raise ClaudeError(f"API {e.status_code}: {e.message}")
    except anthropic.APIConnectionError as e:
        raise ClaudeError(f"connection error: {e}")
    if resp.stop_reason == "refusal":
        raise ClaudeError("the model declined this request")
    if resp.stop_reason == "max_tokens":
        raise ClaudeError("answer was cut off (max_tokens)")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if text is None:
        raise ClaudeError(f"no answer ({resp.stop_reason})")
    return json.loads(text)
