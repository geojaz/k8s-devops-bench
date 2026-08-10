# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for devops_bench.core.errors."""

import pytest

from devops_bench.core.errors import (
    AlreadyRegisteredError,
    ConfigError,
    DevOpsBenchError,
    InvalidKeyError,
    MissingDependencyError,
    NotRegisteredError,
    RegistryError,
    SubprocessError,
    redact,
)


@pytest.mark.parametrize(
    "error_cls",
    [
        ConfigError,
        RegistryError,
        AlreadyRegisteredError,
        InvalidKeyError,
        NotRegisteredError,
        MissingDependencyError,
        SubprocessError,
    ],
)
def test_all_errors_derive_from_base(error_cls):
    assert issubclass(error_cls, DevOpsBenchError)


def test_registry_errors_share_registry_base():
    assert issubclass(AlreadyRegisteredError, RegistryError)
    assert issubclass(InvalidKeyError, RegistryError)
    assert issubclass(NotRegisteredError, RegistryError)


def test_invalid_key_error_message_and_fields() -> None:
    err = InvalidKeyError("agents", "Gemini", "agent keys must be lowercase")
    assert err.registry_name == "agents"
    assert err.key == "Gemini"
    assert err.reason == "agent keys must be lowercase"
    assert "Gemini" in str(err)
    assert "agents" in str(err)
    assert "agent keys must be lowercase" in str(err)


def test_already_registered_error_message_and_fields():
    err = AlreadyRegisteredError("agents", "gemini")
    assert err.registry_name == "agents"
    assert err.key == "gemini"
    assert "gemini" in str(err)
    assert "agents" in str(err)


def test_not_registered_error_lists_available_sorted():
    err = NotRegisteredError("agents", "missing", available=["beta", "alpha"])
    assert err.available == ("beta", "alpha")
    message = str(err)
    assert "missing" in message
    assert message.index("alpha") < message.index("beta")


def test_not_registered_error_without_available():
    err = NotRegisteredError("agents", "missing")
    assert "available" not in str(err)


def test_missing_dependency_error_hints_install_command():
    err = MissingDependencyError("The GCP deployer", "gcp")
    assert err.feature == "The GCP deployer"
    assert err.extra == "gcp"
    assert "pip install devops-bench[gcp]" in str(err)


def test_subprocess_error_captures_streams():
    err = SubprocessError(["kubectl", "get", "pods"], returncode=1, stdout="out", stderr="boom")
    assert err.cmd == ["kubectl", "get", "pods"]
    assert err.returncode == 1
    assert err.stdout == "out"
    assert err.stderr == "boom"
    rendered = str(err)
    assert "kubectl get pods" in rendered
    assert "exit code 1" in rendered
    assert "boom" in rendered


def test_subprocess_error_redacts_secret_shaped_value_in_command():
    """A `docker run ... -e GEMINI_API_KEY=<value> ...` failure must never put
    the value in the exception's own message: this is the exact leak this
    harness put into results.json on a real run's failure path."""
    secret = "SENTINEL-NOT-A-REAL-KEY-0000"
    cmd = ["docker", "run", "--rm", "-e", f"GEMINI_API_KEY={secret}", "image"]

    err = SubprocessError(cmd, returncode=1, stdout="", stderr="")

    rendered = str(err)
    assert secret not in rendered
    assert "GEMINI_API_KEY=" in rendered  # the name survives; only the value is masked
    # cmd itself stays intact for programmatic use (e.g. a caller that wants
    # to re-run it); only the string form is scrubbed.
    assert err.cmd == cmd


def test_subprocess_error_redacts_secret_shaped_value_in_stderr():
    """stderr can echo the failing invocation (or another secret) just as
    easily as argv does, so it needs the same treatment."""
    secret = "SENTINEL-NOT-A-REAL-KEY-0000"

    err = SubprocessError(
        ["some-tool"], returncode=1, stdout="", stderr=f"auth failed for GOOGLE_API_KEY={secret}"
    )

    rendered = str(err)
    assert secret not in rendered
    assert "GOOGLE_API_KEY=" in rendered


def test_subprocess_error_does_not_redact_benign_values():
    """Redaction must not eat unrelated command text, or debugging a real
    failure from results.json becomes impossible."""
    err = SubprocessError(
        ["kubectl", "get", "pods", "-n", "bench-system"], returncode=1, stdout="", stderr=""
    )
    rendered = str(err)
    assert "kubectl get pods -n bench-system" in rendered


def test_redact_masks_secret_shaped_env_assignment():
    secret = "SENTINEL-NOT-A-REAL-KEY-0000"
    assert secret not in redact(f"GEMINI_API_KEY={secret}")
    assert "GEMINI_API_KEY=" in redact(f"GEMINI_API_KEY={secret}")


def test_redact_preserves_non_secret_assignments():
    assert redact("BENIGN_VAR=hello world").startswith("BENIGN_VAR=hello")
