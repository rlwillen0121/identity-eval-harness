"""Published configs cannot embed a user's home directory."""

import json

import pytest

from identitybench.runner.config import ExperimentConfig, load_config


def config(path, kind="muse_cli", key="binary"):
    return {
        "name": "test",
        "dataset": "datasets/x",
        "tasks": ["requests"],
        "models": [{"provider": kind, "model": "test", "extra": {key: path}}],
    }


@pytest.mark.parametrize(
    "path", ["/Users/someone/bin/muse", "/home/someone/muse", "~/muse", "~other/muse"]
)
@pytest.mark.parametrize(("kind", "key"), [("muse_cli", "binary"), ("replay", "path")])
def test_loaded_home_paths_require_local_config(tmp_path, path, kind, key):
    file = tmp_path / "experiment.yaml"
    file.write_text(json.dumps(config(path, kind, key)))
    with pytest.raises(ValueError, match="experiments/local/"):
        load_config(file)
    local = tmp_path / "experiments" / "local" / "nested" / "experiment.yaml"
    local.parent.mkdir(parents=True)
    local.write_text(file.read_text())
    assert load_config(local).models[0].extra[key] == path


@pytest.mark.parametrize("path", ["muse", "/usr/local/bin/muse", "/opt/bin/muse"])
def test_portable_paths_are_accepted(tmp_path, path):
    file = tmp_path / "experiment.yaml"
    file.write_text(json.dumps(config(path)))
    assert load_config(file).models[0].extra["binary"] == path


def test_programmatic_home_paths_still_warn():
    with pytest.warns(UserWarning, match="home directory"):
        ExperimentConfig.model_validate(config("/home/someone/muse"))


def test_local_symlink_cannot_exempt_published_config(tmp_path):
    published = tmp_path / "published.yaml"
    published.write_text(json.dumps(config("~/muse")))
    local = tmp_path / "experiments" / "local"
    local.mkdir(parents=True)
    link = local / "linked.yaml"
    link.symlink_to(published)
    with pytest.raises(ValueError, match="experiments/local/"):
        load_config(link)
