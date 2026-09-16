"""Static policy for the data-migration overlay and its digest script (T25).

`docker_compose/hardened_media_m8/docker-compose.migration.yml` adds the
frozen MinIO and an rclone one-shot for the object-bytes migration
(`DATA_MIGRATION_RUNBOOK.md`). It is an overlay a plain `up` never loads,
but it sits next to the hardened stack, so the invariants that stack proves
must hold for it too: no host port on either backend (S1), nothing outside
`data_net`, the `migration` profile on both services, the frozen image pinned
to the exact tag the fleet ran before the swap, and the credentials it needs
taken from the same env files as everything else — never inlined.

`verify_migration_digests.py` is exercised here as well (imported by path;
`docker_compose/` is outside the package), on the same matched / mismatch /
missing / uncovered cases the runbook's §5.3 relies on.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

_STACK = Path(__file__).parent.parent / "docker_compose" / "hardened_media_m8"
_OVERLAY = _STACK / "docker-compose.migration.yml"
_BASE = _STACK / "docker-compose.yml"
_SCRIPT = _STACK / "verify_migration_digests.py"
_RUNBOOK = _STACK / "DATA_MIGRATION_RUNBOOK.md"

#: The MinIO tag every stack in this fleet pinned before Wave 3 (`main`'s
#: `hardened_media_m8/docker-compose.yml`, `T2-baseline-minio`).
_FROZEN_MINIO = "quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z.hotfix.7aa24e772"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def overlay() -> dict:
    return _load(_OVERLAY)["services"]


class TestMigrationOverlayShape:
    def test_runbook_and_script_ship_next_to_the_overlay(self):
        assert _RUNBOOK.is_file() and _SCRIPT.is_file()

    def test_exactly_the_two_migration_services(self, overlay: dict):
        assert set(overlay) == {"minio-frozen", "rclone"}

    @pytest.mark.parametrize("service", ["minio-frozen", "rclone"])
    def test_behind_the_migration_profile(self, overlay: dict, service: str):
        assert overlay[service].get("profiles") == ["migration"], (
            f"{service}: must be behind the `migration` profile so a plain "
            "`docker compose up` never starts it"
        )

    @pytest.mark.parametrize("service", ["minio-frozen", "rclone"])
    def test_no_host_ports_and_data_net_only(self, overlay: dict, service: str):
        block = overlay[service]
        assert "ports" not in block, f"{service}: S1 — no host port, ever"
        assert block.get("networks") == ["data_net"], (
            f"{service}: internal data_net only, got {block.get('networks')}"
        )

    @pytest.mark.parametrize("service", ["minio-frozen", "rclone"])
    def test_one_shot_never_restarts(self, overlay: dict, service: str):
        assert overlay[service].get("restart") == "no"

    def test_frozen_minio_is_the_pre_migration_pin_on_its_old_volume(
        self, overlay: dict
    ):
        block = overlay["minio-frozen"]
        assert block["image"] == _FROZEN_MINIO
        assert "./minio/data:/data" in block["volumes"]
        assert block["command"] == "server /data", "no console address, no extras"
        assert block["environment"]["MINIO_BROWSER"] == "off"

    def test_frozen_minio_root_pair_is_required_from_env_not_inlined(
        self, overlay: dict
    ):
        env = overlay["minio-frozen"]["environment"]
        for key in ("MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD"):
            value = env[key]
            assert value.startswith("${" + key + ":?"), (
                f"{key}: must be a required (`:?`) interpolation from .env — "
                f"got {value!r}"
            )

    def test_rclone_is_pinned_and_reads_the_stack_env_files(self, overlay: dict):
        block = overlay["rclone"]
        image = block["image"]
        assert image.startswith("rclone/rclone:") and ":latest" not in image
        assert image.split(":", 1)[1].count(".") == 2, "exact x.y.z tag"
        assert block["env_file"] == ["./.env", "./media.env"]

    def test_rclone_remotes_point_at_the_two_backends_path_style(self, overlay: dict):
        env = overlay["rclone"]["environment"]
        assert env["RCLONE_CONFIG_SRC_ENDPOINT"] == "http://minio-frozen:9000"
        assert env["RCLONE_CONFIG_DST_ENDPOINT"] == "http://storage:8333"
        assert env["RCLONE_CONFIG_SRC_PROVIDER"] == "Minio"
        assert env["RCLONE_CONFIG_DST_PROVIDER"] == "SeaweedFS"
        assert env["RCLONE_CONFIG_SRC_FORCE_PATH_STYLE"] == "true"
        assert env["RCLONE_CONFIG_DST_FORCE_PATH_STYLE"] == "true"
        assert env["RCLONE_CONFIG"] == "/dev/null", "never a config file"

    def test_rclone_credentials_come_from_the_wrapper_not_the_environment(
        self, overlay: dict
    ):
        block = overlay["rclone"]
        for key in block["environment"]:
            assert "ACCESS_KEY" not in key and "SECRET" not in key, (
                f"{key}: credentials are picked (value or *_FILE) inside the "
                "entrypoint, never declared in `environment:`"
            )
        script = block["entrypoint"][2]
        assert "pick()" in script
        for name in (
            "MINIO_ROOT_USER",
            "MINIO_ROOT_PASSWORD",
            "S3_ROOT_USER",
            "S3_ROOT_PASSWORD",
        ):
            assert f"$${{{name}_FILE:-}}" in script, (
                f"{name}: the *_FILE (Docker secret) form must be honoured, "
                "as storage-config does"
            )
        assert 'exec rclone "$$@"' in script

    def test_rclone_waits_for_both_backends_to_be_healthy(self, overlay: dict):
        deps = overlay["rclone"]["depends_on"]
        assert deps["minio-frozen"]["condition"] == "service_healthy"
        assert deps["storage"]["condition"] == "service_healthy"

    def test_report_directory_is_gitignored(self):
        ignored = (_STACK / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert "migration-reports/" in ignored
        assert "minio/data/*" in ignored

    def test_base_stack_still_has_no_minio_service(self):
        # The overlay is the *only* place MinIO reappears; the swap itself
        # must not regress by someone re-adding it to the base file.
        assert "minio" not in _load(_BASE)["services"]


# ---------------------------------------------------------------------------
# verify_migration_digests.py
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def digests_module():
    spec = importlib.util.spec_from_file_location("verify_migration_digests", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_SHA_A = "a" * 64
_SHA_B = "b" * 64


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class TestVerifyMigrationDigests:
    def test_classifies_every_case_and_exits_one_on_loss(
        self, digests_module, tmp_path: Path, capsys
    ):
        digests = _write(
            tmp_path / "digests.csv",
            "storage_bucket,object_key,sha256\n"
            f"private-media,u/1/original/ok.png,{_SHA_A}\n"
            f"private-media,u/2/original/bad.png,{_SHA_A}\n"
            "private-media,u/3/original/nodigest.png,\n"
            f"private-media,u/4/original/gone.png,{_SHA_A}\n"
            f"public-media,u/5/original/nobucket.png,{_SHA_A}\n",
        )
        hashsum = _write(
            tmp_path / "sha256-private-media.txt",
            f"{_SHA_A}  u/1/original/ok.png\n"
            f"{_SHA_B}  u/2/original/bad.png\n"
            f"{_SHA_B}  u/3/original/nodigest.png\n",
        )
        missing_out = tmp_path / "missing.txt"
        rc = digests_module.main(
            [
                "--digests",
                str(digests),
                "--hashsum",
                f"private-media={hashsum}",
                "--missing-out",
                str(missing_out),
            ]
        )
        out = capsys.readouterr().out
        assert rc == 1
        assert "rows=5 matched=1 mismatch=1 missing=2 uncovered=1" in out
        assert f"mismatch  private-media/u/2/original/bad.png  db={_SHA_A}" in out
        assert missing_out.read_text(encoding="utf-8").splitlines() == [
            "private-media/u/4/original/gone.png",
            "public-media/u/5/original/nobucket.png",
        ]

    def test_listing_mode_is_presence_only_and_diffs_against_hashsum_mode(
        self, digests_module, tmp_path: Path, capsys
    ):
        digests = _write(
            tmp_path / "digests.csv",
            "storage_bucket,object_key,sha256\n"
            f"private-media,u/1/original/ok.png,{_SHA_A}\n"
            f"private-media,u/4/original/gone.png,{_SHA_A}\n",
        )
        listing = _write(
            tmp_path / "lsf-src.txt", "u/1/original/ok.png\nu/9/original/extra.png\n"
        )
        src_missing = tmp_path / "missing-src.txt"
        rc = digests_module.main(
            [
                "--digests",
                str(digests),
                "--listing",
                f"private-media={listing}",
                "--missing-out",
                str(src_missing),
            ]
        )
        out = capsys.readouterr().out
        assert rc == 1  # the baseline itself is non-empty
        assert "rows=2 matched=0 mismatch=0 missing=1 uncovered=1" in out
        # destination run: same key still missing, the other one hashed
        hashsum = _write(tmp_path / "sha256.txt", f"{_SHA_A}  u/1/original/ok.png\n")
        dst_missing = tmp_path / "missing-dst.txt"
        digests_module.main(
            [
                "--digests",
                str(digests),
                "--hashsum",
                f"private-media={hashsum}",
                "--missing-out",
                str(dst_missing),
            ]
        )
        assert src_missing.read_text() == dst_missing.read_text()

    def test_clean_run_exits_zero(self, digests_module, tmp_path: Path, capsys):
        digests = _write(
            tmp_path / "digests.csv",
            f"storage_bucket,object_key,sha256\nb,k,{_SHA_A.upper()}\n",
        )
        hashsum = _write(tmp_path / "h.txt", f"{_SHA_A}  k\n")
        rc = digests_module.main(
            ["--digests", str(digests), "--hashsum", f"b={hashsum}"]
        )
        assert rc == 0
        assert (
            "rows=1 matched=1 mismatch=0 missing=0 uncovered=0"
            in capsys.readouterr().out
        )

    def test_refuses_a_malformed_hashsum_line_and_a_wrong_header(
        self, digests_module, tmp_path: Path
    ):
        bad_hash = _write(tmp_path / "h.txt", "not-a-digest  k\n")
        with pytest.raises(SystemExit, match="not an rclone hashsum line"):
            digests_module.load_hashsums(bad_hash)
        bad_csv = _write(tmp_path / "d.csv", "bucket,key,sha\nb,k,\n")
        with pytest.raises(SystemExit, match="expected header"):
            digests_module.load_digest_rows(bad_csv)

    def test_a_bucket_given_twice_is_an_error(self, digests_module, tmp_path: Path):
        digests = _write(tmp_path / "d.csv", "storage_bucket,object_key,sha256\n")
        listing = _write(tmp_path / "l.txt", "")
        with pytest.raises(SystemExit):
            digests_module.main(
                [
                    "--digests",
                    str(digests),
                    "--listing",
                    f"b={listing}",
                    "--hashsum",
                    f"b={listing}",
                ]
            )
