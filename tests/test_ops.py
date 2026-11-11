import os
import shutil
import stat
import uuid
from pathlib import Path

import psycopg
import pytest
from kcd_core.contracts import MessageType
from kcd_core.keys import generate_keypair, load_keyring, load_signer
from kcd_core.migrate import MigrationError, apply_migrations, discover
from kcd_core.settings import Settings
from kcd_core.signing import sign_envelope, verify_envelope

from .conftest import MIGRATIONS, TEST_DSN, _pg_available, payload


class TestSettings:
    def test_dev_defaults_boot(self):
        Settings(env="dev").validate_for_env()

    @pytest.mark.parametrize("kw, msg", [
        (dict(env="prod", pager="log", bus_replicas=3), "reaches nobody"),
        (dict(env="prod", pager="webhook", pager_url="https://x", pager_secret="s", bus_replicas=3,
              dev_token="t", database_url="postgresql://u:p@h/db"), "DEV_TOKEN"),
        (dict(env="prod", pager="webhook", pager_url="https://x", pager_secret="s", bus_replicas=1,
              database_url="postgresql://u:p@h/db"), "REPLICAS"),
        (dict(env="dev", pager="webhook"), "PAGER_URL"),
        (dict(env="prod", pager="webhook", pager_url="https://x", pager_secret="s", bus_replicas=3), "password"),
    ])
    def test_unsafe_configs_refuse_to_start(self, kw, msg):
        with pytest.raises(SystemExit, match=msg):
            Settings(**kw).validate_for_env()


class TestKeys:
    def test_round_trip_and_permissions(self, tmp_path, desk):
        generate_keypair(tmp_path, "companion")
        signer = load_signer(tmp_path, "companion")
        env = sign_envelope(
            desk.envelope(MessageType.RED_FLAG_ESCALATION, "companion", "case_briefer",
                          payload(MessageType.RED_FLAG_ESCALATION), sign=False), signer)
        verify_envelope(env, load_keyring(tmp_path))
        if os.name == "posix":
            assert stat.S_IMODE((tmp_path / "companion.key").stat().st_mode) == 0o600
            (tmp_path / "companion.key").chmod(0o644)
            with pytest.raises(PermissionError):
                load_signer(tmp_path, "companion")

    def test_never_overwrites_silently(self, tmp_path):
        generate_keypair(tmp_path, "x")
        with pytest.raises(FileExistsError):
            generate_keypair(tmp_path, "x")


class TestMigrate:
    def test_bad_file_names_are_rejected(self, tmp_path):
        (tmp_path / "init.sql").write_text("select 1")
        with pytest.raises(MigrationError, match="NNN_name"):
            discover(tmp_path)

    def test_editing_an_applied_migration_is_an_error(self, tmp_path):
        if not _pg_available():
            pytest.skip("Postgres not reachable")
        schema = f"m_{uuid.uuid4().hex[:10]}"
        opts = f"-c search_path={schema}"
        with psycopg.connect(TEST_DSN, autocommit=True) as admin:
            admin.execute(f"CREATE SCHEMA {schema}")
        try:
            shutil.copy(MIGRATIONS / "001_initial.sql", tmp_path / "001_initial.sql")
            (tmp_path / "001_initial.sql").write_text("CREATE TABLE t (a int);")
            assert apply_migrations(TEST_DSN, tmp_path, options=opts) == ["001_initial"]
            assert apply_migrations(TEST_DSN, tmp_path, options=opts) == []
            (tmp_path / "001_initial.sql").write_text("CREATE TABLE t (a int, b int);")
            with pytest.raises(MigrationError, match="edited after"):
                apply_migrations(TEST_DSN, tmp_path, options=opts)
        finally:
            with psycopg.connect(TEST_DSN, autocommit=True) as admin:
                admin.execute(f"DROP SCHEMA {schema} CASCADE")


def test_path_import():
    assert Path(__file__).exists()
