import yaml
from pathlib import Path


def _verify_config_dict(config: dict):
    litellm_settings = config.get("litellm_settings", {})
    assert litellm_settings.get("expose_router_debug_in_errors") is False, (
        "expose_router_debug_in_errors must be false to prevent cosmetic fallback errors"
    )
    assert litellm_settings.get("drop_params") is True, "drop_params must remain true"

    router_settings = config.get("router_settings", {})
    assert router_settings.get("num_retries", 0) >= 3, "num_retries must be >= 3"
    assert router_settings.get("retry_after", 0) >= 3, "retry_after must be >= 3 seconds"


def test_repo_config_template_hardening():
    repo_config = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
    assert repo_config.is_file(), f"Config file missing at {repo_config}"
    with open(repo_config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    _verify_config_dict(config)


def test_installed_litellm_config_hardening():
    user_config = Path.home() / ".litellm" / "config.yaml"
    if user_config.exists():
        with open(user_config, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        _verify_config_dict(config)
