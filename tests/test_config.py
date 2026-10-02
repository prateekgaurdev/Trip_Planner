"""Config tests for Vercel/serverless checkpoint path."""
import os

from app.config import Settings, get_settings, prepare_checkpoint_path


def test_vercel_forces_tmp_checkpoint(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("CHECKPOINT_DB", "checkpoints.sqlite")
    s = Settings()
    assert s.checkpoint_db.startswith("/tmp/")


def test_prepare_checkpoint_path_creates_parent(tmp_path, monkeypatch):
    db = tmp_path / "nested" / "ckpt.sqlite"
    prepare_checkpoint_path(str(db))
    assert db.parent.is_dir()


def test_masked_key_hides_secrets():
    s = Settings(google_api_key="AIzaSyA1234567890abcdef", serpapi_key="")
    masked = s.masked_key("google_api_key")
    assert masked.startswith("AIz")
    assert masked.endswith("cdef")
    assert "1234567890" not in masked

    empty_masked = s.masked_key("serpapi_key")
    assert empty_masked == "UNSET"

def test_carto_api_key_loaded():
    s = Settings(carto_api_key="jwt_token_from_carto")
    assert s.carto_api_key == "jwt_token_from_carto"
    masked = s.masked_key("carto_api_key")
    assert "jwt_token_from_carto" not in masked
    assert masked.startswith("jwt")
    assert masked.endswith("arto")


def test_langsmith_config_sync(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_test_key_123")
    monkeypatch.setenv("LANGSMITH_PROJECT", "test-project")
    s = Settings()
    assert os.environ.get("LANGCHAIN_TRACING_V2") == "true"
    assert os.environ.get("LANGCHAIN_API_KEY") == "lsv2_pt_test_key_123"
    assert os.environ.get("LANGCHAIN_PROJECT") == "test-project"
