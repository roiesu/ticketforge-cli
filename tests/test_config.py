from tforge import config
from tforge.config import Config


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "config.json"
    config.save(Config(base_url="https://tf.example", username="alice"), path)

    assert config.load(path) == Config(base_url="https://tf.example", username="alice")
    assert "password" not in path.read_text()


def test_missing_or_corrupt_config_means_not_configured(tmp_path):
    path = tmp_path / "config.json"
    assert config.load(path) is None

    path.write_text("{not json")
    assert config.load(path) is None


def test_config_dir_can_be_overridden(tmp_path, monkeypatch):
    monkeypatch.setenv("TICKETFORGE_CONFIG_DIR", str(tmp_path))
    assert config.config_path() == tmp_path / "config.json"
