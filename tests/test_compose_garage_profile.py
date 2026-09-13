"""Static policy for the Garage alternate storage profile (Wave 5 /
T27-garage-alt-profile).

`docker_compose/hardened_media_m8/docker-compose.garage.yml` overrides
`storage`, `storage-config` and `storage-init` (SeaweedFS) with Garage 2.x
equivalents, plus a new `storage-cors` one-shot. These tests parse the YAML
directly — no running Docker required — and check the same static shape
`test_compose_image_pins.py`/`test_compose_storage_policy.py` check for the
base file: image pins, no host ports, hardening left to the base file
(nothing re-declared that would collide with it), and the credential-shape
guard rules the entrypoint enforces.

A live merge is exercised manually (`docker compose -f docker-compose.yml -f
docker-compose.garage.yml config`), not here — this suite is deliberately
docker-free like its siblings.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_STACK = Path(__file__).parent.parent / "docker_compose" / "hardened_media_m8"
_OVERLAY = _STACK / "docker-compose.garage.yml"
_BASE = _STACK / "docker-compose.yml"
_TOML = _STACK / "garage" / "garage.toml"

_GARAGE_IMAGE = "dxflrs/garage:v2.3.0"
_AWSCLI_IMAGE = "amazon/aws-cli:2.36.40"


class _ComposeSafeLoader(yaml.SafeLoader):
    """`yaml.safe_load` plus the Compose Spec's merge-control tags.

    `docker-compose.garage.yml` uses `!reset` (Compose Spec's own YAML
    extension, understood by the `docker compose` CLI, not standard YAML) to
    clear a list a base file already populated before setting a new one. For
    these static, single-file assertions the tagged node's own value is
    exactly what we want to read — no cross-file merge is being simulated
    here.
    """


def _construct_reset(loader: yaml.SafeLoader, node: yaml.Node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_ComposeSafeLoader.add_constructor("!reset", _construct_reset)
_ComposeSafeLoader.add_constructor("!override", _construct_reset)


def _load(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeSafeLoader)


@pytest.fixture(scope="module")
def overlay() -> dict:
    return _load(_OVERLAY)["services"]


@pytest.fixture(scope="module")
def base() -> dict:
    return _load(_BASE)["services"]


class TestGarageProfileShape:
    def test_exactly_the_expected_services_are_overridden_or_added(self, overlay: dict):
        assert set(overlay) == {
            "storage-config",
            "storage",
            "storage-init",
            "storage-cors",
            "media_worker",
            "media_service",
            "media_service_worker",
        }

    def test_config_file_ships_next_to_the_overlay_and_has_no_secret(self):
        assert _TOML.is_file()
        text = _TOML.read_text(encoding="utf-8")
        assert "rpc_secret" not in text, (
            "garage.toml must carry no secret — GARAGE_RPC_SECRET is supplied "
            "at runtime via env, not baked into a tracked file"
        )
        assert 'rpc_bind_addr = "127.0.0.1:3901"' in text, (
            "RPC must stay loopback-bound (S2) — the same posture SeaweedFS's "
            "master/volume/filer/webdav use"
        )
        assert 'api_bind_addr = "0.0.0.0:8333"' in text, (
            "S3 gateway must keep port 8333 so nothing downstream of the "
            "storage boundary needs to change when switching backends"
        )

    def test_storage_and_bootstrap_images_are_pinned(self, overlay: dict):
        assert overlay["storage"]["image"] == _GARAGE_IMAGE
        assert overlay["storage-init"]["image"] == _GARAGE_IMAGE
        assert overlay["storage-cors"]["image"] == _AWSCLI_IMAGE
        for svc in ("storage", "storage-init", "storage-cors"):
            img = overlay[svc]["image"]
            assert ":" in img and not img.endswith(":latest"), (
                f"{svc}: image {img!r} must be pinned, never :latest"
            )

    def test_storage_container_name_and_command_point_at_the_config_file(
        self, overlay: dict, base: dict
    ):
        storage = overlay["storage"]
        # container_name is deliberately left for the base file to supply
        # (unchanged from SeaweedFS's "storage") — asserted on the base file
        # instead of repeated here.
        assert base["storage"]["container_name"] == "storage"
        assert storage["command"] == [
            "/garage",
            "-c",
            "/etc/garage.toml",
            "server",
        ], (
            "no ENTRYPOINT on the upstream image — command must name the full binary path"
        )

    def test_storage_hardening_flags_are_not_redeclared(self, overlay: dict):
        # These are unchanged from the base SeaweedFS service and must be left
        # for it to supply: list-type fields merge additively across `-f`
        # files, so repeating an identical entry trips `docker compose
        # config`'s duplicate-item check (confirmed empirically while writing
        # this file).
        storage = overlay["storage"]
        for key in (
            "user",
            "security_opt",
            "cap_drop",
            "read_only",
            "tmpfs",
            "deploy",
            "restart",
            "networks",
        ):
            assert key not in storage, (
                f"storage (garage profile): {key!r} must not be redeclared — "
                "it merges additively with the base file and duplicates"
            )

    def test_storage_no_host_ports(self, overlay: dict):
        assert "ports" not in overlay["storage"], "S1 — no host port, ever"

    def test_storage_init_shares_storage_network_namespace(self, overlay: dict):
        block = overlay["storage-init"]
        assert block.get("network_mode") == "service:storage", (
            "storage-init must reach the loopback-bound RPC port by sharing "
            "storage's network namespace, not a Docker-socket exec or an "
            "open admin port"
        )
        assert block.get("networks") == [], (
            "networks must be explicitly reset to empty — network_mode and "
            "networks are mutually exclusive on the same service"
        )

    def test_storage_cors_is_a_plain_data_net_one_shot(self, overlay: dict):
        block = overlay["storage-cors"]
        assert "ports" not in block
        assert block.get("networks") == ["data_net"]
        assert block.get("restart") == "no"
        assert block["depends_on"]["storage-init"]["condition"] == (
            "service_completed_successfully"
        )

    def test_consumers_gain_storage_cors_as_a_dependency(self, overlay: dict):
        for svc in ("media_worker", "media_service", "media_service_worker"):
            block = overlay[svc]
            assert block == {
                "depends_on": {
                    "storage-cors": {"condition": "service_completed_successfully"}
                }
            }, (
                f"{svc}: must ADD storage-cors to depends_on and touch nothing "
                "else — depends_on maps merge by key, so this must not "
                "re-list any base dependency"
            )

    def test_media_rw_is_granted_owner_not_just_read_write(self, overlay: dict):
        script = overlay["storage-init"]["entrypoint"][2]
        assert "bucket allow --read --write --owner --key media-rw" in script, (
            "Garage only allows PutBucketCors to a key holding the bucket's "
            "Owner permission — RW alone is not enough (verified live "
            "against v2.3.0)"
        )

    def test_storage_init_is_idempotent_on_layout_bucket_and_key(self, overlay: dict):
        script = overlay["storage-init"]["entrypoint"][2]
        assert "NO ROLE ASSIGNED" in script, (
            "layout assign/apply must be skip-if-already-applied"
        )
        assert "key info media-rw" in script, (
            "key import must be skip-if-already-exists"
        )
        assert "bucket info" in script, "bucket create must be skip-if-already-exists"

    def test_storage_config_rejects_placeholder_and_short_secret(self, overlay: dict):
        script = overlay["storage-config"]["entrypoint"][2]
        assert "changethis" in script
        assert "GARAGE_RPC_SECRET must be at least 64 hex chars" in script
        assert "GARAGE_RPC_SECRET_FILE" in script, (
            "must honour the Docker-secret _FILE indirection, as every other "
            "one-shot in this stack does"
        )

    def test_storage_cors_rejects_wildcard_origin(self, overlay: dict):
        script = overlay["storage-cors"]["entrypoint"][2]
        assert "S3_CORS_ALLOW_ORIGIN must not contain" in script

    def test_data_dir_is_gitignored_config_file_is_not(self):
        ignored = (_STACK / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert "garage/data/*" in ignored
        assert "garage/garage.toml" not in ignored

    def test_env_example_declares_the_rpc_secret_placeholder(self):
        env_example = (_STACK / ".env.example").read_text(encoding="utf-8")
        assert "GARAGE_RPC_SECRET=changethis" in env_example

    def test_base_stack_is_untouched_by_the_overlay_existing(self, base: dict):
        # The overlay must be additive-only from the base file's own point of
        # view: SeaweedFS stays the default until `-f docker-compose.garage.yml`
        # is passed explicitly.
        assert base["storage"]["image"].startswith("chrislusf/seaweedfs:")
        assert base["storage-init"]["image"].startswith("amazon/aws-cli:")
