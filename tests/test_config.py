from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from habitica_tasks_sync.config import ConfigError, load_config


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_minimal_config(tmp_path: Path):
    cfg = _write(
        tmp_path,
        """
        sync_interval_seconds: 60
        user_agent:
          uuid: 12345678-1234-1234-1234-123456789abc
          app_name: my-app
        pairs:
          - name: alice
            habitica:
              user_id: 11111111-2222-3333-4444-555555555555
              api_token: 99999999-2222-3333-4444-555555555555
            google:
              credentials_file: /tmp/cred.json
              token_file: /tmp/alice.json
              tasklist_title: Habitica
        """,
    )
    config = load_config(cfg)
    assert len(config.pairs) == 1
    assert config.pairs[0].name == "alice"
    assert config.user_agent_app == "my-app"
    assert config.sync_interval_seconds == 60


def test_invalid_interval_too_low(tmp_path: Path):
    cfg = _write(
        tmp_path,
        """
        sync_interval_seconds: 5
        user_agent:
          uuid: 12345678-1234-1234-1234-123456789abc
        pairs:
          - name: alice
            habitica: {user_id: 11111111-2222-3333-4444-555555555555, api_token: 99999999-2222-3333-4444-555555555555}
            google: {credentials_file: /tmp/c.json, token_file: /tmp/t.json}
        """,
    )
    with pytest.raises(ConfigError, match="sync_interval_seconds"):
        load_config(cfg)


def test_missing_user_agent_uuid(tmp_path: Path):
    cfg = _write(
        tmp_path,
        """
        pairs:
          - name: alice
            habitica: {user_id: 11111111-2222-3333-4444-555555555555, api_token: 99999999-2222-3333-4444-555555555555}
            google: {credentials_file: /tmp/c.json, token_file: /tmp/t.json}
        """,
    )
    with pytest.raises(ConfigError, match="user_agent.uuid"):
        load_config(cfg)


def test_invalid_habitica_uuid(tmp_path: Path):
    cfg = _write(
        tmp_path,
        """
        user_agent: {uuid: 12345678-1234-1234-1234-123456789abc}
        pairs:
          - name: alice
            habitica: {user_id: not-a-uuid, api_token: 99999999-2222-3333-4444-555555555555}
            google: {credentials_file: /tmp/c.json, token_file: /tmp/t.json}
        """,
    )
    with pytest.raises(ConfigError, match="user_id"):
        load_config(cfg)


def test_env_var_interpolation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ALICE_HABITICA_USER_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setenv("ALICE_HABITICA_API_TOKEN", "99999999-2222-3333-4444-555555555555")
    cfg = _write(
        tmp_path,
        """
        user_agent: {uuid: 12345678-1234-1234-1234-123456789abc}
        pairs:
          - name: alice
            habitica:
              user_id: ${ALICE_HABITICA_USER_ID}
              api_token: ${ALICE_HABITICA_API_TOKEN}
            google:
              credentials_file: /tmp/c.json
              token_file: /tmp/t.json
        """,
    )
    config = load_config(cfg)
    assert config.pairs[0].habitica.user_id == "11111111-2222-3333-4444-555555555555"


def test_env_var_default(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("UNSET_VAR", raising=False)
    cfg = _write(
        tmp_path,
        """
        user_agent:
          uuid: 12345678-1234-1234-1234-123456789abc
          app_name: "${UNSET_VAR:-fallback}"
        pairs:
          - name: alice
            habitica: {user_id: 11111111-2222-3333-4444-555555555555, api_token: 99999999-2222-3333-4444-555555555555}
            google: {credentials_file: /tmp/c.json, token_file: /tmp/t.json}
        """,
    )
    config = load_config(cfg)
    assert config.user_agent_app == "fallback"


def test_duplicate_pair_names(tmp_path: Path):
    cfg = _write(
        tmp_path,
        """
        user_agent: {uuid: 12345678-1234-1234-1234-123456789abc}
        pairs:
          - name: alice
            habitica: {user_id: 11111111-2222-3333-4444-555555555555, api_token: 99999999-2222-3333-4444-555555555555}
            google: {credentials_file: /tmp/c.json, token_file: /tmp/t.json}
          - name: alice
            habitica: {user_id: 22222222-2222-3333-4444-555555555555, api_token: 88888888-2222-3333-4444-555555555555}
            google: {credentials_file: /tmp/c.json, token_file: /tmp/b.json}
        """,
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(cfg)


def test_non_numeric_interval(tmp_path: Path):
    cfg = _write(
        tmp_path,
        """
        sync_interval_seconds: hello
        user_agent: {uuid: 12345678-1234-1234-1234-123456789abc}
        pairs:
          - name: alice
            habitica: {user_id: 11111111-2222-3333-4444-555555555555, api_token: 99999999-2222-3333-4444-555555555555}
            google: {credentials_file: /tmp/c.json, token_file: /tmp/t.json}
        """,
    )
    with pytest.raises(ConfigError, match="sync_interval_seconds"):
        load_config(cfg)
