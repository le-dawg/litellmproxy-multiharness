import yaml
from pathlib import Path


def _verify_config_dict(config: dict):
    litellm_settings = config.get("litellm_settings", {})
    assert litellm_settings.get("expose_router_debug_in_errors") is False, (
        "expose_router_debug_in_errors must be false to prevent cosmetic fallback errors"
    )
    assert litellm_settings.get("drop_params") is True, "drop_params must remain true"
    assert litellm_settings.get("request_timeout") == 20, "request_timeout must be 20 to prevent unbounded hangs"
    assert litellm_settings.get("use_chat_completions_url_for_anthropic_messages") is True, (
        "use_chat_completions_url_for_anthropic_messages must be True for clean Antigravity bridge adaptation"
    )

    model_list = config.get("model_list", [])
    flash_entry = next((m for m in model_list if m.get("model_name") == "antigravity-flash"), None)
    assert flash_entry is not None, "antigravity-flash model must be configured in model_list"
    assert flash_entry.get("litellm_params", {}).get("use_chat_completions_api") is True, (
        "antigravity-flash must have use_chat_completions_api set to True"
    )

    flash_low_entry = next((m for m in model_list if m.get("model_name") == "gemini-3.8-flash-low"), None)
    assert flash_low_entry is not None, "gemini-3.8-flash-low must be configured in model_list"
    assert flash_low_entry.get("litellm_params", {}).get("use_chat_completions_api") is True

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


def _get_repo_config() -> dict:
    repo_config = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
    assert repo_config.is_file(), f"Config file missing at {repo_config}"
    with open(repo_config, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_antigravity_pool_definitions():
    config = _get_repo_config()
    model_list = config.get("model_list", [])
    model_map = {m["model_name"]: m.get("litellm_params", {}) for m in model_list if "model_name" in m}
    for model_name in [
        "antigravity-flash-low",
        "antigravity-flash-medium",
        "antigravity-flash-high",
        "antigravity-opus-4.6",
    ]:
        assert model_name in model_map, f"{model_name} missing from model_list"
        assert model_map[model_name].get("api_base") == "http://127.0.0.1:8788/v1", (
            f"{model_name} api_base must be http://127.0.0.1:8788/v1"
        )


def test_default_client_fallbacks():
    config = _get_repo_config()
    model_list = config.get("model_list", [])
    model_map = {m["model_name"]: m.get("litellm_params", {}) for m in model_list if "model_name" in m}
    for model_name in ["claude-gpt-5", "claude-*", "gpt-*", "*"]:
        assert model_name in model_map, f"{model_name} missing from model_list"
        assert model_map[model_name].get("api_base") == "http://127.0.0.1:8788/v1", (
            f"{model_name} api_base must be http://127.0.0.1:8788/v1"
        )


def test_inactive_azure_models_preserved():
    config = _get_repo_config()
    model_list = config.get("model_list", [])
    model_map = {m["model_name"]: m.get("litellm_params", {}) for m in model_list if "model_name" in m}
    for model_name in ["inactive-azure-gpt-5.4", "inactive-azure-claude-gpt-5"]:
        assert model_name in model_map, f"{model_name} missing from model_list"
        params = model_map[model_name]
        assert "azure" in params.get("model", "") or "azure" in params.get("api_base", ""), (
            f"{model_name} should preserve azure configuration"
        )


def test_remote_antigravity_pool_definitions():
    config = _get_repo_config()
    model_list = config.get("model_list", [])
    model_map = {m["model_name"]: m.get("litellm_params", {}) for m in model_list if "model_name" in m}
    for model_name in [
        "antigravity-remote-flash-low",
        "antigravity-remote-flash-medium",
        "antigravity-remote-flash-high",
        "antigravity-remote-opus-4.6",
    ]:
        assert model_name in model_map, f"{model_name} missing from model_list"
        assert model_map[model_name].get("api_base") == "http://10.147.17.130:8788/v1", (
            f"{model_name} api_base must be http://10.147.17.130:8788/v1"
        )
        assert model_map[model_name].get("api_key") == "sk-antigravity-local", (
            f"{model_name} api_key must be sk-antigravity-local"
        )


def test_router_fallbacks_to_remote_counterparts():
    config = _get_repo_config()
    router_settings = config.get("router_settings", {})
    fallbacks = router_settings.get("fallbacks", [])
    assert isinstance(fallbacks, list), "router_settings.fallbacks must be a list"

    fallback_map = {}
    for item in fallbacks:
        if isinstance(item, dict):
            fallback_map.update(item)

    expected_fallbacks = {
        "antigravity-flash-low": "antigravity-remote-flash-low",
        "antigravity-flash-medium": "antigravity-remote-flash-medium",
        "antigravity-flash-high": "antigravity-remote-flash-high",
        "antigravity-opus-4.6": "antigravity-remote-opus-4.6",
    }
    for primary, remote in expected_fallbacks.items():
        assert primary in fallback_map, f"{primary} missing from router_settings.fallbacks"
        fallback_targets = fallback_map[primary]
        if isinstance(fallback_targets, str):
            fallback_targets = [fallback_targets]
        assert remote in fallback_targets, (
            f"{remote} not in fallback targets for {primary}: {fallback_targets}"
        )


