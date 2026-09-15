"""Static policy for the Garage alternate storage profile (Wave 5 /
T27-garage-alt-profile).

`docker_compose/hardened_media_m8/docker-compose.garage.yml` overrides
`storage`, `storage-config` and `storage-init` (SeaweedFS) with Garage 2.x
equivalents, plus two new one-shots: `storage-tools` (seeds a shell for the
scratch-based Garage image) and `storage-cors`. These tests parse the YAML
directly — no running Docker required — and check the same static shape
`test_compose_image_pins.py`/`test_compose_storage_policy.py` check for the
base file: image pins, no host ports, hardening left to the base file
(nothing re-declared that would collide with it), and the credential-shape
guard rules the entrypoint enforces.

`T30-close-deferred-flags` closed the two flags `T27` recorded as follow-on
and this suite now guards both: `garage.toml` is rendered from
`garage/garage.toml.template` by `storage-config` with `s3_api.s3_region`
substituted from `S3_REGION` (Garage checks the SigV4 scope on every request —
measured live: the matching region is a 200, any other is a 400
`AuthorizationHeaderMalformed`), and
`docker-compose.garage.production.yml` moves `GARAGE_RPC_SECRET` onto a Docker
secret through `GARAGE_RPC_SECRET_FILE`.

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
_PROD_OVERLAY = _STACK / "docker-compose.garage.production.yml"
_TOML_TEMPLATE = _STACK / "garage" / "garage.toml.template"
_ENV_EXAMPLES = (
    "media.env.example",
    "media.env.production.example",
    "worker.env.example",
    "worker.env.production.example",
)

_GARAGE_IMAGE = "dxflrs/garage:v2.3.0"
_AWSCLI_IMAGE = "amazon/aws-cli:2.36.40"
_BUSYBOX_IMAGE = "busybox:1.37.0-musl"
_RPC_SECRET_FILE = "/run/secrets/garage_rpc_secret"


class _ComposeSafeLoader(yaml.SafeLoader):
    """`yaml.safe_load` plus the Compose Spec's merge-control tags.

    `docker-compose.garage.yml` uses `!reset` / `!override` (Compose Spec's
    own YAML extensions, understood by the `docker compose` CLI, not standard
    YAML): `!reset []` clears a list the base file populated, `!override`
    replaces it with the list that follows. (`!reset` followed by a list does
    NOT keep the list — the merged value is null; measured under T30, which
    is why the mounts below are `!override`.) For these static, single-file
    assertions the tagged node's own value is exactly what we want to read —
    no cross-file merge is being simulated here.
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


@pytest.fixture(scope="module")
def prod_overlay() -> dict:
    return _load(_PROD_OVERLAY)


def _raw_tagged_lines(path: Path, key: str) -> list[str]:
    """Lines of *path* that declare `<key>: !<tag>` — the tag is what the
    YAML loader normalises away, so it is asserted on the raw text."""
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith(f"{key}: !")
    ]


class TestGarageProfileShape:
    def test_exactly_the_expected_services_are_overridden_or_added(self, overlay: dict):
        assert set(overlay) == {
            "storage-config",
            "storage",
            "storage-tools",
            "storage-init",
            "storage-cors",
            "media_worker",
            "media_service",
            "media_service_worker",
        }

    def test_config_file_ships_next_to_the_overlay_and_has_no_secret(self):
        assert _TOML_TEMPLATE.is_file()
        assert not (_STACK / "garage" / "garage.toml").exists(), (
            "garage/garage.toml is rendered into garage/config/ at boot — a "
            "tracked copy next to the template would be the static-region "
            "file T30 retired"
        )
        text = _TOML_TEMPLATE.read_text(encoding="utf-8")
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
        assert overlay["storage-tools"]["image"] == _BUSYBOX_IMAGE
        for svc in ("storage", "storage-tools", "storage-init", "storage-cors"):
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
            "/etc/garage/garage.toml",
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
        assert "garage/config/*" in ignored, (
            "the rendered garage.toml is regenerated every boot and must not "
            "be committed"
        )
        assert "garage/garage.toml.template" not in ignored

    def test_env_example_declares_the_rpc_secret_placeholder(self):
        env_example = (_STACK / ".env.example").read_text(encoding="utf-8")
        assert "GARAGE_RPC_SECRET=changethis" in env_example

    def test_base_stack_is_untouched_by_the_overlay_existing(self, base: dict):
        # The overlay must be additive-only from the base file's own point of
        # view: SeaweedFS stays the default until `-f docker-compose.garage.yml`
        # is passed explicitly.
        assert base["storage"]["image"].startswith("chrislusf/seaweedfs:")
        assert base["storage-init"]["image"].startswith("amazon/aws-cli:")


class TestGarageRegionTemplating:
    """T30 item 4: `s3_api.s3_region` is rendered from `S3_REGION` at boot."""

    def test_template_carries_the_placeholder_not_a_static_region(self):
        text = _TOML_TEMPLATE.read_text(encoding="utf-8")
        assert 's3_region = "@S3_REGION@"' in text
        assert 's3_region = "eu-west-1"' not in text, (
            "a static region silently breaks every SigV4 signature the moment "
            "media.env changes S3_REGION — it must come from S3_REGION"
        )

    def test_storage_config_renders_the_template_from_s3_region(self, overlay: dict):
        block = overlay["storage-config"]
        script = block["entrypoint"][2]
        assert "@S3_REGION@" in script and "S3_REGION" in script
        assert "S3_REGION is empty" in script, "an empty region must fail closed"
        assert "*[!a-z0-9-]*" in script, "region charset must be guarded before sed"
        mounts = block["volumes"]
        assert "./garage/config:/config" in mounts
        assert "./garage/garage.toml.template:/garage.toml.template:ro" in mounts

    def test_replacing_mount_lists_use_override_not_reset(self):
        tagged = _raw_tagged_lines(_OVERLAY, "volumes")
        assert tagged, "the profile must replace the SeaweedFS mounts explicitly"
        assert all(line.startswith("volumes: !override") for line in tagged), (
            "mount lists that replace the base file's must use `!override` — "
            "`!reset` followed by a list merges to null (measured)"
        )

    def test_storage_and_storage_init_mount_the_rendered_directory(self, overlay: dict):
        # A directory mount, not a file mount: Docker would create a directory
        # named garage.toml at a file-mount path that does not exist yet.
        assert "./garage/config:/etc/garage:ro" in overlay["storage"]["volumes"]
        assert "./garage/config:/etc/garage:ro" in overlay["storage-init"]["volumes"]
        assert "/etc/garage/garage.toml" in overlay["storage-init"]["entrypoint"][2]
        assert overlay["storage"]["healthcheck"]["test"][2:4] == [
            "-c",
            "/etc/garage/garage.toml",
        ]

    def test_env_examples_all_pin_the_same_region(self):
        # The apps and the rendered Garage config read the SAME variable; the
        # examples must agree with each other so a copied stack is consistent.
        regions = set()
        for name in _ENV_EXAMPLES:
            for line in (_STACK / name).read_text(encoding="utf-8").splitlines():
                if line.startswith("S3_REGION="):
                    regions.add(line.split("=", 1)[1].strip())
        assert len(regions) == 1, regions


class TestGarageBootstrapHasAShell:
    """The upstream Garage image is `FROM scratch` (`/garage` is its only
    file). A shell-scripted storage-init only runs on it with a shell mounted
    in — which storage-tools seeds from busybox into a named volume."""

    def test_storage_tools_seeds_a_named_volume_from_busybox(self, overlay: dict):
        block = overlay["storage-tools"]
        assert block["command"] == ["true"]
        assert block["volumes"] == ["garage_tools:/bin"]
        assert block.get("restart") == "no"
        assert block.get("networks") == ["data_net"]
        top = _load(_OVERLAY)
        assert "garage_tools" in top.get("volumes", {})

    def test_storage_init_runs_through_the_seeded_shell(self, overlay: dict):
        block = overlay["storage-init"]
        assert block["entrypoint"][0] == "/tools/bin/sh"
        assert "garage_tools:/tools/bin:ro" in block["volumes"]
        assert "export PATH=/tools/bin" in block["entrypoint"][2]
        assert block["depends_on"]["storage-tools"]["condition"] == (
            "service_completed_successfully"
        )

    def test_storage_init_never_enables_bucket_website(self, overlay: dict):
        # `garage bucket website --allow` publishes a bucket for anonymous
        # read through the web endpoint — FORBIDDEN_OPERATIONS territory.
        script = overlay["storage-init"]["entrypoint"][2]
        assert "bucket website --allow" not in script

    def test_storage_cors_loads_the_env_file_that_holds_the_origin(self, overlay: dict):
        # S3_CORS_ALLOW_ORIGIN lives in .env (same as for the SeaweedFS
        # storage-init); without it storage-cors exits 1 on every boot.
        assert overlay["storage-cors"]["env_file"] == ["./.env", "./media.env"]


class TestGarageProductionOverlay:
    """T30 item 3: `GARAGE_RPC_SECRET` has Docker-secret `_FILE` wiring."""

    _RPC_CONSUMERS = ("storage-config", "storage", "storage-init")

    def test_declares_the_rpc_secret_as_a_file_source(self, prod_overlay: dict):
        secret = prod_overlay["secrets"]["garage_rpc_secret"]
        assert secret == {"file": "./secrets/garage_rpc_secret.txt"}

    @pytest.mark.parametrize("service", _RPC_CONSUMERS)
    def test_every_garage_rpc_consumer_gets_the_secret_and_the_file_var(
        self, prod_overlay: dict, service: str
    ):
        block = prod_overlay["services"][service]
        assert "garage_rpc_secret" in block["secrets"]
        assert block["environment"]["GARAGE_RPC_SECRET_FILE"] == _RPC_SECRET_FILE

    def test_no_application_container_mounts_the_rpc_secret(self, prod_overlay: dict):
        for name, block in prod_overlay["services"].items():
            if name in self._RPC_CONSUMERS:
                continue
            assert "garage_rpc_secret" not in (block.get("secrets") or []), name

    def test_garage_binary_services_drop_dotenv(self, prod_overlay: dict):
        # Garage refuses to start when GARAGE_RPC_SECRET (even an empty line
        # from .env) and GARAGE_RPC_SECRET_FILE are both present — measured:
        # "only one of `rpc_secret` and `rpc_secret_file` can be set".
        services = prod_overlay["services"]
        assert services["storage"]["env_file"] == []
        assert services["storage-init"]["env_file"] == ["./media.env.production"]
        tagged = _raw_tagged_lines(_PROD_OVERLAY, "env_file")
        assert tagged and all(
            line.startswith("env_file: !override") for line in tagged
        ), "env_file lists here must REPLACE the base/profile lists, not merge"

    def test_garage_only_bootstrap_steps_get_the_media_rw_secret(
        self, prod_overlay: dict
    ):
        # docker-compose.production.yml wires storage-init for SeaweedFS,
        # where it is admin-only; under Garage it imports media-rw and
        # storage-cors (unknown to the shared overlay) signs with it.
        for svc in ("storage-init", "storage-cors"):
            block = prod_overlay["services"][svc]
            assert {"s3_access_key", "s3_secret_key"} <= set(block["secrets"])
            assert (
                block["environment"]["S3_ACCESS_KEY_FILE"]
                == "/run/secrets/s3_access_key"
            )
            assert (
                block["environment"]["S3_SECRET_KEY_FILE"]
                == "/run/secrets/s3_secret_key"
            )

    def test_env_example_points_production_at_the_secret_file(self):
        env_example = (_STACK / ".env.example").read_text(encoding="utf-8")
        assert "garage_rpc_secret.txt" in env_example
        assert "docker-compose.garage.production.yml" in env_example
