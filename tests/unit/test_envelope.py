from __future__ import annotations

import pytest
from pydantic import ValidationError

from mini_claude.core.bus.envelope import (
    PARSE_ERROR,
    JsonRpcRequest,
    JsonRpcSuccess,
    make_error,
)


def test_request_roundtrip() -> None:
    req = JsonRpcRequest(id="1", method="core.ping", params={"client": "test"})
    req2 = JsonRpcRequest.model_validate_json(req.model_dump_json())
    assert req2.id == "1"
    assert req2.method == "core.ping"
    assert req2.params == {"client": "test"}


def test_request_default_params() -> None:
    req = JsonRpcRequest(id="1", method="x")
    assert req.params == {}


def test_request_missing_id_raises() -> None:
    with pytest.raises(ValidationError):
        JsonRpcRequest.model_validate({"jsonrpc": "2.0", "method": "x"})


def test_request_wrong_version_raises() -> None:
    with pytest.raises(ValidationError):
        JsonRpcRequest.model_validate({"jsonrpc": "1.0", "id": "1", "method": "x"})


def test_success_roundtrip() -> None:
    resp = JsonRpcSuccess(id="1", result={"key": "value"})
    resp2 = JsonRpcSuccess.model_validate_json(resp.model_dump_json())
    assert resp2.id == "1"
    assert resp2.result == {"key": "value"}


def test_make_error_sets_code() -> None:
    err = make_error("1", PARSE_ERROR, "Parse error")
    assert err.error.code == PARSE_ERROR
    assert err.id == "1"
    assert err.error.data is None


def test_make_error_null_id() -> None:
    err = make_error(None, PARSE_ERROR, "bad json")
    assert err.id is None
