import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).parent


def test_auth_callback_uses_febre_service():
    config = json.loads((ROOT / "configs" / "config_api.json").read_text(encoding="utf-8"))
    assert config["auth"] == "https://febre-system-auth.shardweb.app"


def test_oauth_url_has_correct_callback_and_state():
    from modules.cloud.oauth_panel import build_auth_url

    url = build_auth_url("123456789", guild_id="987654321", bot_id="111222333")
    query = parse_qs(urlparse(url).query)

    assert url.startswith("https://discord.com/api/oauth2/authorize?")
    assert query["redirect_uri"] == [
        "https://febre-system-auth.shardweb.app/api/auth/callback"
    ]
    assert query["state"] == ["111222333:987654321"]
    assert query["scope"] == ["identify email guilds.join"]

