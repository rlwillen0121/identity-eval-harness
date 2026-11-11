"""Keep published, tracked text free of workstation paths and private infrastructure."""

import ipaddress
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 5 * 1024 * 1024
HOME = re.compile(r"/(?:Users|home)/([^/\s\"'`]+)(?:/|(?=[\s\"'`]|$))")
PLACEHOLDERS = {"user", "username", "alice", "bob", "example", "youruser", "yourname"}
IP = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
PRIVATE_HOST = re.compile(
    r"(?<![\w.-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
    r"(?:local|ts\.net|tailscale\.net)(?![\w.-])",
    re.IGNORECASE,
)


def violations(text: str) -> list[str]:
    found = []
    for line_no, line in enumerate(text.splitlines(), 1):
        for match in HOME.finditer(line):
            name = match[1]
            placeholder = name.lower() in PLACEHOLDERS or (
                name.startswith("<") and name.endswith(">")
            )
            if not placeholder:
                found.append(f"{line_no}: absolute home path")
        for match in IP.finditer(line):
            try:
                address = ipaddress.ip_address(match[0])
            except ValueError:
                continue
            if any(address in network for network in PRIVATE_NETWORKS):
                found.append(f"{line_no}: private IP")
        if PRIVATE_HOST.search(line):
            found.append(f"{line_no}: private hostname")
        if "CPA_" in line or "CLIProxy" in line:
            found.append(f"{line_no}: private proxy name")
    return found


def tracked_violations(root: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("repository hygiene requires a Git checkout")
    found = []
    for name in result.stdout.decode().split("\0"):
        if not name or name.startswith("tests/"):
            continue
        path = root / name
        # Scan a symlink's tracked target text, without reading anything outside the repo.
        if path.is_symlink():
            data = str(path.readlink()).encode("utf-8")
        else:
            # Deleted tracked files can be pending in this shared worktree.
            if not path.is_file() or path.stat().st_size > MAX_BYTES:
                continue
            data = path.read_bytes()
        if b"\0" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        found.extend(f"{name}:{item}" for item in violations(text))
    return found


def test_tracked_text_is_machine_neutral():
    assert not (found := tracked_violations(ROOT)), "\n".join(found)


@pytest.mark.parametrize(
    "text",
    [
        "/Users/someone/project/file",
        "/home/someone/project",
        "10.2.3.4",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.2.3",
        "https://machine.local:8443",
        "host.tail123.ts.net",
        "host.tailscale.net",
        "CPA_CLIENT_API_KEY",
        "CLIProxy",
    ],
)
def test_hygiene_detects_private_details(text):
    assert violations(text)


@pytest.mark.parametrize(
    "text",
    [
        "/Users/<name>/project",
        "/home/user/project",
        "/Users/alice/project",
        "~/.local/bin/muse",
        "172.15.0.1",
        "172.32.0.1",
        "127.0.0.1",
        "https://example.com",
        "LOCAL_PROXY_API_KEY",
        "not.localhost",
        "1.2.3.999",
    ],
)
def test_hygiene_accepts_placeholders_and_public_details(text):
    assert not violations(text)


def test_hygiene_skips_non_git_checkout(tmp_path):
    with pytest.raises(pytest.skip.Exception, match="Git checkout"):
        tracked_violations(tmp_path)


def test_hygiene_scans_only_tracked_text_and_symlink_targets(tmp_path, monkeypatch):
    private = b"/Users/someone/project"
    (tmp_path / "published.txt").write_bytes(private)
    (tmp_path / "untracked.txt").write_bytes(private)
    (tmp_path / "binary.bin").write_bytes(private + b"\0")
    (tmp_path / "large.txt").write_bytes(private + b" " * MAX_BYTES)
    (tmp_path / "link").symlink_to("/Users/someone/project")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "fixture.txt").write_bytes(private)
    names = b"published.txt\0binary.bin\0large.txt\0link\0tests/fixture.txt\0deleted.txt\0"

    def git_files(argv, **kwargs):
        assert argv == ["git", "ls-files", "-z"]
        assert kwargs["cwd"] == tmp_path
        return subprocess.CompletedProcess(argv, 0, stdout=names)

    monkeypatch.setattr(subprocess, "run", git_files)
    assert tracked_violations(tmp_path) == [
        "published.txt:1: absolute home path",
        "link:1: absolute home path",
    ]


def test_no_edit_agents_require_shell_approval():
    agents = json.loads((ROOT / "opencode.json").read_text())["agent"]
    required = {
        "orchestrate",
        "review",
        "bench-operator",
        "lane-rules",
        "lane-security",
        "lane-oracle",
        "lane-stats",
    }
    assert required <= agents.keys()
    for name, agent in agents.items():
        permission = agent["permission"]
        if permission["edit"] != "deny":
            continue
        bash = permission["bash"]
        assert bash["*"] == "ask"
        assert bash["git status*"] == "allow"
        assert bash["cat *"] == "allow"
        if name == "bench-operator":
            assert bash["uv run identitybench v41 run*"] == "allow"
            assert bash["uv run identitybench v41 report*"] == "allow"
        else:
            assert "uv run identitybench v41 run*" not in bash
        if name == "review" or name.startswith("lane-"):
            for command in ("rm *", "git push*", "git commit*", "git reset*", "git checkout*"):
                assert bash[command] == "deny"
