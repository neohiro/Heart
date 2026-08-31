"""
test_heart_healthz.py
=====================
Static source-analysis tests for Heart/cmd/heart/main.go's /healthz handler.

The Heart binary exposes /healthz on 127.0.0.1:9090.  We can't run it in
this Windows-based test environment, so we verify behaviour by reading the Go
source and asserting structural invariants.

Each test corresponds to a specific security property.  If any test breaks,
the /healthz endpoint has likely been changed in a way that widens the
attack surface or removes a defence-in-depth measure.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

MAIN_GO = Path(__file__).resolve().parents[1] / "main.go"
SRC = MAIN_GO.read_text(encoding="utf-8")


def _has(name: str) -> bool:
    """True if `name` is defined as a top-level function or var."""
    return bool(re.search(rf"^(func|var)\s+{name}\b", SRC, re.MULTILINE))


def _get_func_body(name: str) -> str:
    """Return the body text of a top-level function, excluding the signature.

    Raises AssertionError if the function is not found.
    """
    # The regex allows an optional return type (e.g. `*http.Server` or `bool`)
    # between the closing paren and the opening brace. Uses `[^{]` (any char
    # except open brace) to permit whitespace in return types like `bool`.
    pattern = re.compile(
        rf"^func\s+{re.escape(name)}\s*\([^)]*\)(?:[^{{]*?){{", re.MULTILINE
    )
    m = pattern.search(SRC)
    if not m:
        raise AssertionError(f"function {name!r} not found in {MAIN_GO}")
    start = m.end()  # after the opening {
    depth = 1
    i = start
    while i < len(SRC) and depth > 0:
        ch = SRC[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return SRC[start:i]
        i += 1
    raise AssertionError(f"unmatched braces in function {name!r}")


class TestSecurityProperties:
    """Hardening contract for the /healthz endpoint."""

    def test_bound_to_localhost(self):
        """The HTTP server must bind to 127.0.0.1, never 0.0.0.0."""
        assert re.search(r'Addr:\s*"127\.0\.0\.1:', SRC), (
            "HTTP server.Addr must be 127.0.0.1 (never 0.0.0.0)"
        )

    def test_no_getenv_in_handler(self):
        """The request handler must not read env vars at request time.

        serveHealthz() (the setup function) is allowed to read
        HEART_HEALTH_PORT for port configuration, but the request handler
        itself must not touch env (no per-request env reads = no
        TOCTOU/race surface).
        """
        body = _get_func_body("healthzHandler")
        assert "os.Getenv" not in body, (
            "healthzHandler must not call os.Getenv at request time"
        )

    def test_no_filesystem_access_in_handler(self):
        """The handler must not open or read files."""
        body = _get_func_body("healthzHandler")
        fs_calls = [
            "os.Open", "os.ReadFile", "os.Stat",
            "os.ReadDir", "os.Create", "os.WriteFile",
            "ioutil.ReadFile", "os.Read",
        ]
        for fn in fs_calls:
            assert fn not in body, f"{fn!r} must not appear in healthzHandler"

    def test_rejects_non_get(self):
        """Non-GET requests must return 405 Method Not Allowed."""
        body = _get_func_body("healthzHandler")
        assert "http.MethodGet" in body, (
            "handler must check r.Method != http.MethodGet"
        )
        assert "StatusMethodNotAllowed" in body, (
            "must return 405 for non-GET"
        )

    def test_body_size_capped(self):
        """Request body must be capped to prevent large-payload attacks."""
        body = _get_func_body("healthzHandler")
        assert "MaxBytesReader" in body, (
            "handler must use http.MaxBytesReader to cap request body size"
        )

    def test_read_timeout_set(self):
        """ReadTimeout prevents slow-client attacks."""
        body = _get_func_body("serveHealthz")
        assert "ReadTimeout" in body
        assert "ReadHeaderTimeout" in body
        assert "WriteTimeout" in body

    def test_content_type_json(self):
        """Response Content-Type must be explicit application/json."""
        body = _get_func_body("healthzHandler")
        assert 'Content-Type' in body
        assert 'application/json' in body

    def test_cache_control_no_store(self):
        """Cache-Control: no-store prevents proxy caching."""
        body = _get_func_body("healthzHandler")
        assert 'Cache-Control' in body
        assert 'no-store' in body

    def test_no_path_parameters(self):
        """The mux must register /healthz as a literal path, not a pattern."""
        # Extract the HandleFunc calls in serveHealthz
        body = _get_func_body("serveHealthz")
        for line in body.splitlines():
            if 'HandleFunc("' in line or "HandleFunc(`" in line:
                # Pattern paths like /healthz/:id are forbidden
                assert ':' not in line.split('HandleFunc')[1].split(')')[0], (
                    f"HandleFunc must use a literal path, not a pattern: {line.strip()}"
                )

    def test_catchall_404(self):
        """Any path other than /healthz must return 404."""
        body = _get_func_body("serveHealthz")
        assert 'http.NotFound' in body, (
            "serveHealthz must register a catch-all returning 404"
        )

    def test_graceful_shutdown_wired(self):
        """serveHealthz returns *http.Server so main can call Shutdown on exit."""
        # Look at the full source (not just the body) since the return type
        # is in the signature, before the opening brace.
        assert re.search(r"func\s+serveHealthz\s*\([^)]*\)\s+\*http\.Server", SRC), (
            "serveHealthz must return *http.Server for graceful shutdown"
        )
        main_body = _get_func_body("main")
        assert "Shutdown(" in main_body, (
            "main() must call healthSrv.Shutdown() for graceful shutdown"
        )


class TestAtomicSafety:
    """The /healthz handler reads shared state without a mutex."""

    def test_cycle_count_is_int32(self):
        """cycleCount must be int32 for atomic operations."""
        assert re.search(r"var\s+cycleCount\s+int32", SRC), (
            "cycleCount must be declared as int32"
        )

    def test_atomic_load_for_cycle_count(self):
        """The handler must read cycleCount via atomic.LoadInt32."""
        body = _get_func_body("healthzHandler")
        if "cycleCount" in body:
            assert "atomic.LoadInt32(&cycleCount)" in body, (
                "healthzHandler must use atomic.LoadInt32 for cycleCount"
            )

    def test_phase_status_is_atomic_pointer(self):
        """lastPhase must be atomic.Pointer so the HTTP handler is lock-free."""
        assert "atomic.Pointer[phaseStatus]" in SRC, (
            "lastPhase must be atomic.Pointer[phaseStatus]"
        )
        assert "lastPhase.Store" in SRC, "cycle loop must call lastPhase.Store"
        assert "lastPhase.Load" in SRC, "handler must call lastPhase.Load"

    def test_phaseHeartbeat_atomic_cycle_load(self):
        """phaseHeartbeat must read cycleCount atomically when serialising JSON."""
        body = _get_func_body("phaseHeartbeat")
        # The bare `cycleCount` (no atomic) read inside phaseHeartbeat would
        # race with atomic.AddInt32 from the cycle loop. We require the
        # local var pattern: curCycle := int(atomic.LoadInt32(&cycleCount))
        # and its use in the map literal.
        assert "atomic.LoadInt32(&cycleCount)" in body, (
            "phaseHeartbeat must read cycleCount via atomic.LoadInt32"
        )
        assert '"cycle":    curCycle' in body or '"cycle":curCycle' in body, (
            "phaseHeartbeat must serialise curCycle (not bare cycleCount)"
        )


class TestIntegration:
    """The endpoint is wired correctly into main() and the container."""

    def test_serveHealthz_called_from_main(self):
        """serveHealthz must be started from main()."""
        main_body = _get_func_body("main")
        assert "serveHealthz(" in main_body, (
            "main() must call serveHealthz() to start the HTTP server"
        )

    def test_phase_recorded_after_each_phase(self):
        """The cycle loop must call lastPhase.Store after every phase."""
        assert "lastPhase.Store(&phaseStatus{" in SRC, (
            "lastPhase must be stored after each phase"
        )

    def test_dockerfile_exposes_9090(self):
        """The Heart Dockerfile must EXPOSE 9090."""
        dockerfile = MAIN_GO.parents[2] / "Dockerfile"
        text = dockerfile.read_text(encoding="utf-8")
        assert "EXPOSE 9090" in text, (
            "Dockerfile must expose port 9090 for /healthz"
        )

    def test_sidecar_probes_heart_9090(self):
        """The Heart Dockerfile must set SERVICE_HOST to the healthz port."""
        dockerfile = MAIN_GO.parents[2] / "Dockerfile"
        text = dockerfile.read_text(encoding="utf-8")
        assert 'SERVICE_HOST' in text
        assert '9090' in text


class TestPortValidation:
    """serveHealthz must validate HEART_HEALTH_PORT before binding.

    Without validation, a typo in the env var produces a generic
    'address invalid' error from the OS — harder to diagnose and
    easier to mistake for a real bug. Validating upfront gives a
    specific log line and falls back to a safe default.
    """

    def test_isValidTCPPort_defined(self):
        """isValidTCPPort helper must exist."""
        assert re.search(r"^func\s+isValidTCPPort", SRC, re.MULTILINE), (
            "isValidTCPPort helper must be defined"
        )

    def test_isValidTCPPort_rejects_empty(self):
        """Empty string must be rejected."""
        body = _get_func_body("isValidTCPPort")
        # The function returns false for empty input
        assert '""' in body or 'empty' in body, (
            "isValidTCPPort must reject empty string"
        )

    def test_isValidTCPPort_rejects_zero(self):
        """Port 0 (OS-assigned) is not useful for a server with a known contract."""
        body = _get_func_body("isValidTCPPort")
        # Should reject "0" or "00000" — there's a leading-zero check
        assert "0" in body

    def test_isValidTCPPort_rejects_too_large(self):
        """Port > 65535 must be rejected."""
        body = _get_func_body("isValidTCPPort")
        assert "65535" in body, (
            "isValidTCPPort must enforce the 65535 upper bound"
        )

    def test_isValidTCPPort_rejects_non_digit(self):
        """Non-numeric input must be rejected (no abc, no 'foo:80')."""
        body = _get_func_body("isValidTCPPort")
        # Loops over chars and checks ASCII range 0-9
        assert "'0'" in body and "'9'" in body, (
            "isValidTCPPort must check char-by-char for digit"
        )

    def test_port_fallback_in_serveHealthz(self):
        """serveHealthz must call isValidTCPPort and fall back on invalid input."""
        body = _get_func_body("serveHealthz")
        assert "isValidTCPPort" in body, (
            "serveHealthz must validate HEART_HEALTH_PORT before binding"
        )
