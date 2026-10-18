"""Regression tests for the Category-1 connector sandbox hardening (str-subclass getattr
bypass, subprocess resource limits, and optional macOS OS-level containment)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile

import pytest
from tests.v3.test_v3_architecture import V3_DEV_DIR

import identitybench.v3.sandbox as sandbox_mod
from identitybench.v3.generator import load_v3_episodes
from identitybench.v3.oracle import build_reference_connector_code
from identitybench.v3.sandbox import (
    DEFAULT_MEMORY_LIMIT_BYTES,
    SANDBOX_CPU_LIMIT_S,
    SANDBOX_FSIZE_LIMIT_BYTES,
    _build_launch_argv,
    _make_child_preexec,
    build_macos_sandbox_profile,
    execute_connector_in_sandbox,
    os_sandbox_available,
)
from identitybench.v3.schemas import CategoryId, ConnectorTargetSpec

_STR_SUBCLASS_PRELUDE = """
cnt = [0]
def eqf(self, other):
    cnt[0] += 1
    return cnt[0] > 1
S = type('S', (str,), {
    'startswith': lambda self, p: False,
    '__hash__': lambda self: hash(str(self)),
    '__eq__': eqf,
})
"""

_ALWAYS_EQUAL_PRELUDE = """
S = type('S', (str,), {
    'startswith': lambda self, p: False,
    '__hash__': lambda self: hash(str(self)),
    '__eq__': lambda self, other: True,
})
"""


def _connector_target() -> ConnectorTargetSpec:
    episodes = load_v3_episodes(V3_DEV_DIR)
    conn_ep = next(ep for ep in episodes if ep.category == CategoryId.connectors)
    assert conn_ep.world_state.connector_target is not None
    return conn_ep.world_state.connector_target


def _assert_forbidden(code: str) -> None:
    target = _connector_target()
    res = execute_connector_in_sandbox(code, target, timeout_s=5.0)
    assert res.passed is False
    assert res.ast_safe is True, res.violations
    assert res.violations == ("CONNECTOR_RUNTIME_ERROR",), (res.violations, res.error)
    assert res.error is not None
    assert "Forbidden attribute access" in res.error, res.error
    assert "GOT" not in res.error, res.error


# --------------------------------------------------------------------------------------
# Fix 1: str-subclass name-filter bypass in _safe_getattr / _SafeModuleProxy.__getattr__
# --------------------------------------------------------------------------------------


def test_str_subclass_cannot_bypass_safe_getattr_name_filter() -> None:
    """The verified exploit (overridden startswith/__eq__/__hash__) must be rejected."""
    code = (
        _STR_SUBCLASS_PRELUDE
        + """
def sync_identities(api_client, state_cursor=None):
    cnt[0] = 0
    c = getattr(api_client, S('__class__'))
    raise RuntimeError("GOT " + repr(c))
"""
    )
    _assert_forbidden(code)


def test_str_subclass_cannot_reach_module_proxy_internals() -> None:
    """Same trick against the module proxy's private `_mod` slot and a sys traversal."""
    for prelude in (_STR_SUBCLASS_PRELUDE, _ALWAYS_EQUAL_PRELUDE):
        code = (
            "import json\n"
            + prelude
            + """
def sync_identities(api_client, state_cursor=None):
    m = getattr(json, S('_mod'))
    raise RuntimeError("GOT " + repr(m))
"""
        )
        _assert_forbidden(code)
    code = (
        "import dataclasses\n"
        + _ALWAYS_EQUAL_PRELUDE
        + """
def sync_identities(api_client, state_cursor=None):
    s = getattr(dataclasses, S('sys'))
    raise RuntimeError("GOT " + repr(s))
"""
    )
    _assert_forbidden(code)


def test_str_subclass_dict_and_default_argument_variants_rejected() -> None:
    """`__dict__` with a default argument and `__class__` via a str subclass both fail."""
    code = (
        _ALWAYS_EQUAL_PRELUDE
        + """
def sync_identities(api_client, state_cursor=None):
    d = getattr(api_client, S('__dict__'), None)
    raise RuntimeError("GOT " + repr(d))
"""
    )
    _assert_forbidden(code)


def test_plain_str_getattr_on_public_attribute_still_works() -> None:
    """Legitimate `getattr(api_client, "required_schema_fields", ())` keeps passing."""
    target = _connector_target()
    code = build_reference_connector_code(target.target_id).replace(
        "tuple(api_client.required_schema_fields)",
        'tuple(getattr(api_client, "required_schema_fields", ()))',
    )
    assert code != build_reference_connector_code(target.target_id)
    res = execute_connector_in_sandbox(code, target)
    assert res.passed is True
    assert res.score == 1.0


# --------------------------------------------------------------------------------------
# Fix 2: resource limits on the sandbox subprocess
# --------------------------------------------------------------------------------------


def test_runaway_allocation_is_killed_not_succeeding() -> None:
    """`[0] * 200_000_000` (~1.6 GB) must not complete under the 512 MiB default cap."""
    target = _connector_target()
    code = (
        "def sync_identities(api_client, state_cursor=None):\n"
        "    x = [0] * 200_000_000\n"
        "    raise RuntimeError('ALLOCATED ' + str(len(x)))\n"
    )
    res = execute_connector_in_sandbox(code, target, timeout_s=20.0)
    assert res.passed is False
    assert res.timed_out is False
    assert res.error is not None
    assert "ALLOCATED" not in res.error, res.error
    assert res.violations[0] in {"SUBPROCESS_FAILURE", "CONNECTOR_RUNTIME_ERROR"}, res.violations
    assert "SANDBOX_MEMORY_LIMIT" in res.violations or "MemoryError" in res.error, (
        res.violations,
        res.error,
    )
    assert res.score == 0.0


def test_reference_connector_passes_with_resource_limits_on() -> None:
    target = _connector_target()
    ref_code = build_reference_connector_code(target.target_id)
    res = execute_connector_in_sandbox(
        ref_code, target, memory_limit_bytes=DEFAULT_MEMORY_LIMIT_BYTES, os_sandbox=True
    )
    assert res.passed is True
    assert res.score == 1.0
    assert res.violations == ()
    assert res.os_sandbox_active is os_sandbox_available()

    plain = execute_connector_in_sandbox(ref_code, target, os_sandbox=False)
    assert plain.passed is True and plain.os_sandbox_active is False


def test_memory_limit_keyword_is_honoured() -> None:
    """The same touched allocation (~96 MiB of list slots, every slot written) is killed
    under a 48 MiB cap but completes under a 1 GiB cap, proving the keyword is live.
    (`bytearray` is not a sandbox builtin, so an earlier version of this test was vacuous.)"""
    target = _connector_target()
    code = (
        "def sync_identities(api_client, state_cursor=None):\n"
        "    x = [0] * 12_000_000\n"
        "    # Hold the allocation and busy-spin (no `time` in the sandbox) so the 10 ms\n"
        "    # footprint poller is guaranteed a window before the connector returns.\n"
        "    checksum = 0\n"
        "    for _ in range(12):\n"
        "        checksum += sum(x)\n"
        "    raise RuntimeError('ALLOCATED ' + str(len(x)) + ' ' + str(checksum))\n"
    )
    small = execute_connector_in_sandbox(code, target, memory_limit_bytes=48 * 1024 * 1024)
    assert small.passed is False
    assert small.error is not None
    assert "ALLOCATED" not in small.error, small.error
    assert "SANDBOX_MEMORY_LIMIT" in small.violations or "MemoryError" in small.error, (
        small.violations,
        small.error,
    )

    large = execute_connector_in_sandbox(code, target, memory_limit_bytes=1024 * 1024 * 1024)
    assert large.violations == ("CONNECTOR_RUNTIME_ERROR",), (large.violations, large.error)
    assert large.error is not None and "ALLOCATED 12000000" in large.error, large.error


@pytest.mark.skipif(sys.platform == "win32", reason="rlimits are POSIX-only")
def test_child_preexec_applies_rlimits_where_platform_supports_them() -> None:
    preexec = _make_child_preexec(DEFAULT_MEMORY_LIMIT_BYTES)
    assert preexec is not None
    probe = (
        "import resource, json\n"
        "print(json.dumps({"
        "'fsize': resource.getrlimit(resource.RLIMIT_FSIZE),"
        "'cpu': resource.getrlimit(resource.RLIMIT_CPU),"
        "'nproc': resource.getrlimit(resource.RLIMIT_NPROC),"
        "'as': resource.getrlimit(resource.RLIMIT_AS),"
        "}))\n"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-S", "-c", probe],
        capture_output=True,
        text=True,
        timeout=30,
        preexec_fn=preexec,
        check=True,
    )
    limits = json.loads(out.stdout)
    assert limits["fsize"][0] == SANDBOX_FSIZE_LIMIT_BYTES
    assert limits["cpu"][0] == SANDBOX_CPU_LIMIT_S
    assert limits["nproc"][0] <= sandbox_mod.SANDBOX_NPROC_LIMIT
    if sys.platform.startswith("linux"):
        assert limits["as"][0] == DEFAULT_MEMORY_LIMIT_BYTES


def test_stdout_scan_is_capped() -> None:
    assert sandbox_mod._MAX_STDOUT_SCAN_CHARS <= 8 * 1024 * 1024


# --------------------------------------------------------------------------------------
# Fix 3: optional OS-level containment (macOS sandbox-exec) with graceful fallback
# --------------------------------------------------------------------------------------

_DARWIN_SANDBOX = sys.platform == "darwin" and os.access("/usr/bin/sandbox-exec", os.X_OK)


def test_launch_argv_respects_keyword_and_env_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as work_dir:
        argv_off, sandboxed_off = _build_launch_argv("pass", work_dir, os_sandbox=False)
        assert sandboxed_off is False
        assert argv_off[:3] == [sys.executable, "-I", "-S"]

        monkeypatch.setenv("IDENTITYBENCH_OS_SANDBOX", "0")
        assert os_sandbox_available() is False
        argv_env, sandboxed_env = _build_launch_argv("pass", work_dir, os_sandbox=True)
        assert sandboxed_env is False
        assert argv_env == argv_off

        monkeypatch.delenv("IDENTITYBENCH_OS_SANDBOX")
        argv_on, sandboxed_on = _build_launch_argv("pass", work_dir, os_sandbox=True)
        if _DARWIN_SANDBOX:
            assert sandboxed_on is True
            assert argv_on[0] == "/usr/bin/sandbox-exec"
            assert argv_on[1] == "-p"
            assert "(deny default)" in argv_on[2]
            assert "(deny network*)" in argv_on[2]
            assert "(allow sysctl-read)" not in argv_on[2]
            assert "(allow file-read-metadata)" not in argv_on[2]
            assert '(sysctl-name "hw.ncpu")' in argv_on[2]
            assert "(deny process-info*)" in argv_on[2]
            assert "procargs" not in argv_on[2]
            assert argv_on[3:6] == [sys.executable, "-I", "-S"]
        else:
            assert sandboxed_on is False


@pytest.mark.skipif(not _DARWIN_SANDBOX, reason="macOS sandbox-exec not available")
def test_macos_profile_allows_python_but_denies_network_and_foreign_io() -> None:
    """The profile is verified with a plain interpreter (socket is not importable inside
    the harness), asserting the connection attempt raises while benign work succeeds."""
    with tempfile.TemporaryDirectory(prefix="ib_v3_sandbox_") as work_dir:
        profile = build_macos_sandbox_profile(work_dir)
        base = ["/usr/bin/sandbox-exec", "-p", profile, sys.executable, "-I", "-S", "-c"]

        ok = subprocess.run(
            [
                *base,
                "import json, hashlib, re, datetime, urllib.parse; "
                "print(hashlib.sha256(json.dumps({'a': 1}).encode()).hexdigest()[:8])",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=work_dir,
        )
        assert ok.returncode == 0, ok.stderr
        digest = ok.stdout.strip()
        assert len(digest) == 8 and all(ch in "0123456789abcdef" for ch in digest)

        net = subprocess.run(
            [
                *base,
                "import socket; socket.create_connection(('1.1.1.1', 53), timeout=2); "
                "print('CONNECTED')",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=work_dir,
        )
        assert net.returncode != 0
        assert "CONNECTED" not in net.stdout
        assert "PermissionError" in net.stderr or "Operation not permitted" in net.stderr

        home = os.path.expanduser("~")
        foreign = subprocess.run(
            [
                *base,
                f"open({home!r} + '/ib_sandbox_probe_should_not_exist.txt', 'w').write('x'); "
                "print('WROTE')",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=work_dir,
        )
        assert foreign.returncode != 0
        assert "WROTE" not in foreign.stdout
        assert not os.path.exists(os.path.join(home, "ib_sandbox_probe_should_not_exist.txt"))

        site_packages = next(
            (
                os.path.join(prefix, "lib", f"python{sys.version_info[0]}.{sys.version_info[1]}")
                + "/site-packages"
                for prefix in (sys.prefix, sys.base_prefix)
                if os.path.isdir(
                    os.path.join(
                        prefix,
                        "lib",
                        f"python{sys.version_info[0]}.{sys.version_info[1]}",
                        "site-packages",
                    )
                )
            ),
            None,
        )
        if site_packages is not None:
            listing = subprocess.run(
                [*base, f"import os; print(len(os.listdir({site_packages!r})))"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=work_dir,
            )
            assert listing.returncode != 0, listing.stdout
            assert (
                "PermissionError" in listing.stderr or "Operation not permitted" in listing.stderr
            )


@pytest.mark.skipif(not _DARWIN_SANDBOX, reason="macOS sandbox-exec not available")
def test_reference_connector_passes_under_os_sandbox() -> None:
    target = _connector_target()
    ref_code = build_reference_connector_code(target.target_id)
    res = execute_connector_in_sandbox(ref_code, target, os_sandbox=True)
    assert res.passed is True
    assert res.score == 1.0
    assert res.os_sandbox_active is True

    loop_code = (
        "def sync_identities(api_client, state_cursor=None):\n    while True:\n        pass\n"
    )
    timeout_res = execute_connector_in_sandbox(loop_code, target, timeout_s=0.6, os_sandbox=True)
    assert timeout_res.timed_out is True
    assert timeout_res.os_sandbox_active is True


@pytest.mark.skipif(not _DARWIN_SANDBOX, reason="macOS sandbox-exec not available")
def test_os_sandbox_launch_failure_falls_back_and_warns_every_time(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    target = _connector_target()
    ref_code = build_reference_connector_code(target.target_id)
    monkeypatch.setattr(
        sandbox_mod, "build_macos_sandbox_profile", lambda _work_dir: "(version 1) (bogus"
    )
    with caplog.at_level(logging.WARNING, logger="identitybench.v3.sandbox"):
        first = execute_connector_in_sandbox(ref_code, target, os_sandbox=True)
        second = execute_connector_in_sandbox(ref_code, target, os_sandbox=True)
    assert first.passed is True and first.score == 1.0
    assert second.passed is True and second.score == 1.0
    assert first.os_sandbox_active is False
    assert second.os_sandbox_active is False
    fallback_warnings = [
        r for r in caplog.records if "OS-level sandbox launch failed" in r.getMessage()
    ]
    assert len(fallback_warnings) == 2
    assert all(r.levelno == logging.WARNING for r in fallback_warnings)


# Raw MIB form (CTL_KERN=1, KERN_PROCARGS2=49): this bypasses the sysctl *name* allowlist and
# is only stopped by `(deny process-info*)`. Prints DENIED on EPERM/ENOENT, or RC 0 + bytes.
_PROCARGS2_CHILD = r"""
import ctypes, errno, os, sys
libc = ctypes.CDLL(None, use_errno=True)
mib = (ctypes.c_int * 3)(1, 49, os.getppid())
size = ctypes.c_size_t(0)
if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
    err = ctypes.get_errno()
    print("DENIED" if err in (errno.EPERM, errno.ENOENT) else "ERRNO " + str(err))
    raise SystemExit(0)
buf = ctypes.create_string_buffer(size.value)
rc = libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0)
if rc != 0:
    err = ctypes.get_errno()
    print("DENIED" if err in (errno.EPERM, errno.ENOENT) else "ERRNO " + str(err))
    raise SystemExit(0)
print("RC 0", size.value)
sys.stdout.flush()
sys.stdout.buffer.write(buf.raw)
"""

# `import ctypes` calls os.uname(), which needs these three keys; they are added to the TEST
# copy of the profile only, so the sysctl is actually attempted inside the sandbox.
_UNAME_SYSCTL_KEYS = (
    ' (sysctl-name "kern.ostype") (sysctl-name "kern.hostname") (sysctl-name "hw.machine")'
)

_PROCARGS2_PARENT = r"""
import subprocess, sys
proc = subprocess.run(sys.argv[1:], capture_output=True, timeout=60)
sys.stdout.buffer.write(proc.stdout)
sys.stdout.buffer.write(b"\n--STDERR--\n" + proc.stderr)
"""


@pytest.mark.skipif(not _DARWIN_SANDBOX, reason="macOS sandbox-exec not available")
def test_macos_profile_blocks_kern_procargs2_of_parent() -> None:
    """A sandboxed child must not read the parent's exec-time environment via the
    `kern.procargs2` sysctl (an unrestricted `(allow sysctl-read)` permitted this). The
    marker lives in the env of an intermediate parent, whose sandboxed child (plain
    interpreter + ctypes, outside the Python-level sandbox) queries procargs2 for its ppid."""
    marker = "IB_SANDBOX_PROCARGS_MARKER_" + os.urandom(6).hex()
    parent_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "IB_PROBE_SECRET": marker}
    with tempfile.TemporaryDirectory(prefix="ib_v3_sandbox_") as work_dir:
        production_profile = build_macos_sandbox_profile(work_dir)
        anchor = '(sysctl-name-prefix "hw.optional"))'
        assert production_profile.count(anchor) == 1
        profile = production_profile.replace(anchor, anchor[:-1] + _UNAME_SYSCTL_KEYS + ")")
        assert "(deny process-info*)" in profile

        uname_ok = subprocess.run(
            [
                "/usr/bin/sandbox-exec",
                "-p",
                profile,
                sys.executable,
                "-I",
                "-S",
                "-c",
                "import ctypes; print('CTYPES_OK')",
            ],
            capture_output=True,
            text=True,
            cwd=work_dir,
            timeout=60,
        )
        assert uname_ok.stdout.strip() == "CTYPES_OK", uname_ok.stderr[-300:]

        sandboxed_child = [
            "/usr/bin/sandbox-exec",
            "-p",
            profile,
            sys.executable,
            "-I",
            "-S",
            "-c",
            _PROCARGS2_CHILD,
        ]
        plain_child = [sys.executable, "-I", "-S", "-c", _PROCARGS2_CHILD]

        control = subprocess.run(
            [sys.executable, "-I", "-S", "-c", _PROCARGS2_PARENT, *plain_child],
            capture_output=True,
            env=parent_env,
            cwd=work_dir,
            timeout=120,
        )
        assert b"RC 0" in control.stdout, (control.stdout[:200], control.stderr[:200])
        assert marker.encode() in control.stdout, (
            "positive control failed: unsandboxed child could not read parent procargs2",
            control.stdout[:200],
            control.stderr[:200],
        )

        sandboxed = subprocess.run(
            [sys.executable, "-I", "-S", "-c", _PROCARGS2_PARENT, *sandboxed_child],
            capture_output=True,
            env=parent_env,
            cwd=work_dir,
            timeout=120,
        )
        assert b"DENIED" in sandboxed.stdout, (sandboxed.stdout[:300], sandboxed.stderr[-300:])
        assert b"RC 0" not in sandboxed.stdout, sandboxed.stdout[:300]
        assert marker.encode() not in sandboxed.stdout, sandboxed.stdout[:300]
        assert b"IB_PROBE_SECRET" not in sandboxed.stdout, sandboxed.stdout[:300]
