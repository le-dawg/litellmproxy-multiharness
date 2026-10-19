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

