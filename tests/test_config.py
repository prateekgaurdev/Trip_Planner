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
