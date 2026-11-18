"""Keep published, tracked text free of workstation paths and private infrastructure."""

import hashlib
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
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "100.64.0.0/10", "172.16.0.0/12", "192.168.0.0/16")
)
PRIVATE_HOST = re.compile(
    r"(?<![\w.-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
    r"(?:local|lan|internal|home\.arpa|ts\.net|tailscale\.net)(?![\w.-])",
    re.IGNORECASE,
)
POLICY_ALLOWLIST_PATH = "reports/v41-skill-orch-v2/allowlist.py"
HOME_METADATA_BASELINE_PATH = Path(__file__).parent / "fixtures" / "repo_hygiene_home_metadata.json"
HOME_METADATA_BASELINE_SCHEMA = "identitybench-repo-hygiene-home-metadata-v1"
HOME_METADATA_BASELINE_KIND = "absolute-home-metadata"
REVIEWED_HOME_METADATA_COUNT = 46

_POLICY_IPV4_CIDRS = {
    "10.0.0.0/8",
    "100.64.0.0/10",
    "172.16.0.0/12",
    "192.168.0.0/16",
}
_POLICY_PRIVATE_IP_LINE = re.compile(r'^\s*ipaddress\.IPv4Network\("([^\"]+)"\),\s*$')
_POLICY_HOST_LINES = {
    '"metadata.google.internal",',
    '"metadata.internal",',
    '"instance-data.ec2.internal",',
    'if host in {"localhost", "host.docker.internal"}:',
}
_POLICY_PROXY_LINES = {
    '"CPA_OPENAI_BASE_URL",',
    '"CPA_OPENAI_API_KEY",',
    '"CPA_CLIENT_API_KEY",',
}
_DOCUMENTED_PROXY_NAMES = {
    "reports/v5/M5.0-Core-draft5-completion-20261115.md": {
        "CPA_BASE_URL",
        "CPA_API_KEY",
    },
    "reports/v5/spec-draft5/external-review/model-metadata.json": {
        "CPA_CLIENT_API_KEY",
    },
    "reports/v5/spec-draft5/external-review/revision-model-metadata.json": {
        "CPA_CLIENT_API_KEY",
    },
}
_PROXY_ENV_NAME = re.compile(r"CPA_[A-Z0-9_]+")
_APPROVED_CPA_ENV_DOC_LINE = (
    "- **Ryan CPA (`ryan-codex/gpt-6-luna`):** Requires `CPA_BASE_URL` and `CPA_API_KEY` "
    "set in the environment (`experiments/local/v5_core_cpa_ryanware_template.yaml`). "
    "Never inspect, log, or echo keys or endpoint URLs, and never route through another "
    "user's CPA prefix."
)


def load_home_metadata_baseline(
    path: Path = HOME_METADATA_BASELINE_PATH,
) -> dict[str, tuple[str, str]]:
    """Load exact path/hash approvals for reviewed immutable proof metadata."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != HOME_METADATA_BASELINE_SCHEMA:
        raise ValueError("unsupported home-metadata baseline schema")
    entries = payload.get("entries")
    if not isinstance(entries, list) or len(entries) != REVIEWED_HOME_METADATA_COUNT:
        raise ValueError("home-metadata baseline must contain exactly 46 reviewed files")

    baseline: dict[str, tuple[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "kind"}:
            raise ValueError("invalid home-metadata baseline entry")
        name = entry["path"]
        digest = entry["sha256"]
        kind = entry["kind"]
        relpath = Path(name) if isinstance(name, str) else Path("/")
        if (
            not isinstance(name, str)
            or relpath.is_absolute()
            or ".." in relpath.parts
            or not name.startswith("reports/")
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or kind != HOME_METADATA_BASELINE_KIND
            or name in baseline
        ):
            raise ValueError("invalid home-metadata baseline entry")
        baseline[name] = (digest, kind)
    return baseline


def _recognized_policy_finding(path: str | None, line: str, kind: str) -> bool:
    """Recognize only the exact security-policy literals reviewed for this scanner."""
    if path != POLICY_ALLOWLIST_PATH:
        return False
    if kind == "private IP":
        match = _POLICY_PRIVATE_IP_LINE.fullmatch(line)
        return match is not None and match.group(1) in _POLICY_IPV4_CIDRS
    if kind == "private hostname":
        return line.strip() in _POLICY_HOST_LINES
    return False


def _recognized_documented_proxy_name(path: str | None, line: str) -> bool:
    """Allow known environment-name documentation without allowing proxy values."""
    if path == POLICY_ALLOWLIST_PATH:
        # These are individual quoted environment-name declarations in policy data.
        return line.strip() in _POLICY_PROXY_LINES
    if path in _DOCUMENTED_PROXY_NAMES:
        names = set(_PROXY_ENV_NAME.findall(line))
        if not names or not names <= _DOCUMENTED_PROXY_NAMES[path]:
            return False
        if path.endswith("model-metadata.json") or path.endswith("revision-model-metadata.json"):
            return (
                re.fullmatch(
                    r'\s*"credential_source": "existing_process_CPA_CLIENT_API_KEY"[,]?\s*',
                    line,
                )
                is not None
            )
        return line == _APPROVED_CPA_ENV_DOC_LINE
    return False


def violations(text: str, *, path: str | None = None) -> list[str]:
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
                if _recognized_policy_finding(path, line, "private IP"):
                    continue
                found.append(f"{line_no}: private IP")
        # This enum member is code, not an internal DNS name. Keep DNS case-insensitive.
        if any(match[0] != "ExitCode.INTERNAL" for match in PRIVATE_HOST.finditer(line)) and not (
            _recognized_policy_finding(path, line, "private hostname")
        ):
            found.append(f"{line_no}: private hostname")
        if (
            "CPA_" in line and not _recognized_documented_proxy_name(path, line)
        ) or "CLIProxy" in line:
            found.append(f"{line_no}: private proxy name")
    return found


def tracked_violations(
    root: Path,
    *,
    home_metadata_baseline: dict[str, tuple[str, str]] | None = None,
) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("repository hygiene requires a Git checkout")
    baseline = (
        load_home_metadata_baseline() if home_metadata_baseline is None else home_metadata_baseline
    )
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
        digest = hashlib.sha256(data).hexdigest()
        approval = baseline.get(name)
        for item in violations(text, path=name):
            _, kind = item.split(": ", 1)
            if (
                kind == "absolute home path"
                and approval is not None
                and approval == (digest, HOME_METADATA_BASELINE_KIND)
            ):
                continue
            found.append(f"{name}:{item}")
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
        "100.64.0.0",
        "100.127.255.255",
        "https://machine.local:8443",
        "https://machine.lan:8443",
        "machine.internal",
        "MACHINE.INTERNAL",
        "machine.home.arpa",
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
        "100.63.255.255",
        "100.128.0.0",
        "127.0.0.1",
        "https://example.com",
        "LOCAL_PROXY_API_KEY",
        "not.localhost",
        "ExitCode.INTERNAL",
        "1.2.3.999",
    ],
)
def test_hygiene_accepts_placeholders_and_public_details(text):
    assert not violations(text)


def test_new_non_fixture_private_details_are_detected():
    new_home_path = "/" + "Users" + "/" + "new-person" + "/project"
    new_private_ip = ".".join(("10", "42", "7", "9"))
    new_private_hostname = "service" + ".private" + ".lan"
    new_proxy_name = "C" + "PA_CLIENT_API_KEY"

    assert violations(new_home_path)
    assert violations(new_private_ip)
    assert violations(new_private_hostname)
    assert violations(new_proxy_name)


def test_reviewed_security_policy_constants_are_narrowly_recognized():
    policy_path = "reports/v41-skill-orch-v2/allowlist.py"
    policy_lines = (ROOT / policy_path).read_text(encoding="utf-8").splitlines()
    for line_no in (113, 114, 115, 116, 135, 136, 137, 319, 48, 79, 80, 81):
        line = policy_lines[line_no - 1]
        assert not violations(line, path=policy_path), f"recognized policy line {line_no}"

    # The same network literal is still detected outside its exact policy context.
    assert violations(policy_lines[112], path="reports/other-policy.py") == ["1: private IP"]

    documented_paths = {
        "reports/v5/M5.0-Core-draft5-completion-20261115.md": (180,),
        "reports/v5/spec-draft5/external-review/model-metadata.json": (4,),
        "reports/v5/spec-draft5/external-review/revision-model-metadata.json": (20,),
    }
    for name, line_numbers in documented_paths.items():
        lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
        for line_no in line_numbers:
            line = lines[line_no - 1]
            assert not violations(line, path=name)
            if name.endswith("M5.0-Core-draft5-completion-20261115.md"):
                for appended_value in (
                    " CPA_API_KEY=synthetic-sensitive-value",
                    " CPA_BASE_URL=https://synthetic-gateway.example/v1",
                    " CPA_BASE_URL=https://service.private.lan/v1",
                ):
                    assert "private proxy name" in " ".join(
                        violations(line + appended_value, path=name)
                    )


def test_home_metadata_baseline_is_exactly_typed_and_bounded():
    baseline = load_home_metadata_baseline()
    assert len(baseline) == REVIEWED_HOME_METADATA_COUNT
    assert all(name.startswith("reports/") for name in baseline)
    assert all(kind == HOME_METADATA_BASELINE_KIND for _, kind in baseline.values())


def test_home_metadata_baseline_is_hash_bound_and_suppresses_only_home_paths(tmp_path, monkeypatch):
    home_path = "/" + "Users" + "/" + "reviewed" + "/proof"
    private_ip = ".".join(("10", "23", "5", "7"))
    private_hostname = "service" + ".private" + ".lan"
    proxy_name = "C" + "PA_CLIENT_API_KEY"
    original = f"{home_path}\n{private_ip}\n{private_hostname}\n{proxy_name}\n".encode()
    proof = tmp_path / "proof.txt"
    proof.write_bytes(original)

    def git_files(argv, **kwargs):
        assert argv == ["git", "ls-files", "-z"]
        assert kwargs["cwd"] == tmp_path
        return subprocess.CompletedProcess(argv, 0, stdout=b"proof.txt\0")

    monkeypatch.setattr(subprocess, "run", git_files)
    baseline = {
        "proof.txt": (
            hashlib.sha256(original).hexdigest(),
            HOME_METADATA_BASELINE_KIND,
        )
    }
    assert tracked_violations(tmp_path, home_metadata_baseline=baseline) == [
        "proof.txt:2: private IP",
        "proof.txt:3: private hostname",
        "proof.txt:4: private proxy name",
    ]

    proof.write_bytes(original + b"changed bytes\n")
    changed = tracked_violations(tmp_path, home_metadata_baseline=baseline)
    assert "proof.txt:1: absolute home path" in changed

    other = tmp_path / "other.txt"
    other.write_bytes(original)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=b"proof.txt\0other.txt\0"
        ),
    )
    other_findings = tracked_violations(tmp_path, home_metadata_baseline=baseline)
    assert "other.txt:1: absolute home path" in other_findings


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
        "lane-blind-conformance",
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


def test_implement_cannot_run_live_evaluations():
    agents = json.loads((ROOT / "opencode.json").read_text())["agent"]
    bash = agents["implement"]["permission"]["bash"]
    assert bash["uv run identitybench v41 run*"] == "deny"
    assert bash["*run_skill_orchestration*"] == "deny"
    assert (
        agents["lane-blind-conformance"]["permission"]["bash"]["uv run pytest tests/conformance*"]
        == "allow"
    )
