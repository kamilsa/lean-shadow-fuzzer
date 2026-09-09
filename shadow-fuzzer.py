#!/usr/bin/env python3
"""Shadow Fuzzer — randomized reproducible Shadow simulation sweeps.

Reads a template config.toml with optional {min, max} range values, generates
per-run concrete configs with deterministic sampling, and runs Shadow
simulations either locally or inside a Docker ARM container.

Usage:
  uv run shadow-fuzzer.py [config.toml]
  uv run shadow-fuzzer.py --dry-run config.example.docker-arm.toml
  uv run shadow-fuzzer.py --clean-output config.toml
  uv run shadow-fuzzer.py --serve config.toml
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import random
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

import yaml

FUZZER_ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = FUZZER_ROOT / "scripts"
SHADOW_EPOCH = 946684800
SHADOW_GENESIS_TIME = 946684860
HASH_SIG_KEY_CACHE_DIR = "_hash-sig-key-cache"

DEFAULT_REGION_WEIGHTS = {
    "us-east": 0.30,
    "us-west": 0.15,
    "europe": 0.25,
    "asia": 0.20,
    "sa": 0.05,
    "africa": 0.05,
}

DEFAULT_BANDWIDTH_WEIGHTS = {
    "1 Gbit": 0.05,
    "100 Mbit": 0.20,
    "50 Mbit": 0.75,
}

DOCKER_ARM_CLIENT_ROOT = "/opt/shadow-fuzzer/clients"


def _resolve_value(raw: Any, rng: random.Random) -> Any:
    if isinstance(raw, dict) and "min" in raw and "max" in raw:
        lo = raw["min"]
        hi = raw["max"]
        if isinstance(lo, int) and isinstance(hi, int):
            return rng.randint(lo, hi)
        return lo + rng.random() * (hi - lo)
    return raw


def _resolve_weight_table(
    raw: dict[str, Any], rng: random.Random
) -> dict[str, float]:
    resolved: dict[str, float] = {}
    for key, val in raw.items():
        resolved[key] = float(_resolve_value(val, rng))
    total = sum(resolved.values())
    if total > 0:
        for k in resolved:
            resolved[k] /= total
    return resolved


def _sample_clients(
    client_weights: dict[str, float], total_nodes: int, rng: random.Random
) -> tuple[list[str], dict[str, int]]:
    names = list(client_weights.keys())
    probs = [client_weights[n] for n in names]
    sampled = rng.choices(names, weights=probs, k=total_nodes)
    counts: dict[str, int] = {}
    for c in sampled:
        counts[c] = counts.get(c, 0) + 1
    return sampled, counts


def _docker_stage_name(client: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in client.lower())
    return f"client_{safe}"


def _docker_client_executable_path(client: str, executable: str) -> str:
    executable_name = Path(executable).name
    return f"{DOCKER_ARM_CLIENT_ROOT}/{client}/{executable_name}"


def _resolve_image_executable_path(image: str, executable: str) -> str:
    inspect = subprocess.run(
        ["docker", "image", "inspect", image],
        check=False,
        capture_output=True,
        text=True,
    )
    if inspect.returncode != 0:
        subprocess.run(
            ["docker", "pull", "--platform", "linux/arm64", image],
            check=True,
        )
        inspect = subprocess.run(
            ["docker", "image", "inspect", image],
            check=True,
            capture_output=True,
            text=True,
        )
    image_info = json.loads(inspect.stdout)[0]
    entrypoint = image_info.get("Config", {}).get("Entrypoint") or []
    if entrypoint:
        first = entrypoint[0]
        if first.startswith("/") and Path(first).name == Path(executable).name:
            return first

    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/arm64",
            "--entrypoint",
            "/bin/sh",
            image,
            "-lc",
            f"command -v {shlex.quote(executable)}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(
            f"failed to resolve executable '{executable}' in image '{image}'. "
            "Set executable_path_in_image explicitly in config."
        )
    return result.stdout.strip().splitlines()[-1]


def _prepare_docker_arm_runtime(
    template: dict[str, Any], output_dir: Path, dry_run: bool
) -> dict[str, Any] | None:
    runner = template["fuzzer"].get("runner", "local")
    if runner != "docker-arm":
        return None

    docker_arm_raw = template.get("docker_arm", {})
    client_images_raw = template.get("client_images", {})
    clients = template.get("clients", {})

    docker_arm = {
        "shadow_image": docker_arm_raw.get("shadow_image", "kamilsa/shadow-arm:latest"),
        "image_name": docker_arm_raw.get("image_name", "lean-shadow-fuzzer:local"),
        "rebuild": bool(docker_arm_raw.get("rebuild", True)),
    }

    client_images: dict[str, dict[str, Any]] = {}
    client_runtime: dict[str, dict[str, str]] = {}
    for client in clients:
        raw = client_images_raw.get(client, {})
        executable = str(raw.get("executable", client))
        source_path = raw.get("executable_path_in_image")
        if source_path is None and not dry_run and docker_arm["rebuild"]:
            source_path = _resolve_image_executable_path(raw["image"], executable)

        dest_path = _docker_client_executable_path(client, executable)
        client_images[client] = {
            "image": raw["image"],
            "executable": executable,
            "executable_path_in_image": source_path,
            "runtime_path": dest_path,
        }
        client_runtime[client] = {"path": dest_path}

    if not dry_run and docker_arm["rebuild"]:
        _build_docker_arm_image(output_dir, docker_arm, client_images)
    elif dry_run:
        print("[dry-run] Skipping Docker ARM composite image build")

    return {
        "docker_arm": docker_arm,
        "client_images": client_images,
        "client_runtime": client_runtime,
    }


def _build_docker_arm_image(
    output_dir: Path,
    docker_arm: dict[str, Any],
    client_images: dict[str, dict[str, Any]],
) -> None:
    build_dir = output_dir / "_docker-build"
    build_dir.mkdir(parents=True, exist_ok=True)
    dockerfile = build_dir / "Dockerfile"

    lines = [f"FROM --platform=linux/arm64 {docker_arm['shadow_image']} AS shadow_base"]
    for client, cfg in client_images.items():
        lines.append(f"FROM --platform=linux/arm64 {cfg['image']} AS {_docker_stage_name(client)}")

    lines.append("FROM shadow_base")
    for client, cfg in client_images.items():
        source_path = cfg.get("executable_path_in_image")
        if not source_path:
            raise RuntimeError(f"client '{client}' has no resolved executable_path_in_image")
        if " " in source_path or " " in cfg["runtime_path"]:
            raise RuntimeError(f"client '{client}' executable paths must not contain spaces")
        dest_dir = str(Path(cfg["runtime_path"]).parent)
        lines.append(f"RUN mkdir -p {dest_dir}")
        lines.append(
            f"COPY --from={_docker_stage_name(client)} {source_path} {cfg['runtime_path']}"
        )
        lines.append(f"RUN chmod +x {cfg['runtime_path']}")

    dockerfile.write_text("\n".join(lines) + "\n")
    subprocess.run(
        [
            "docker",
            "build",
            "--platform",
            "linux/arm64",
            "-t",
            docker_arm["image_name"],
            str(build_dir),
        ],
        check=True,
    )


def _node_ip(index: int) -> str:
    """Map a 0-based host index to a distinct 100.0.x.y address.

    A flat 100.0.0.N scheme runs out at 254 nodes, so walk /24 blocks and skip
    the .0 and .255 host numbers within each one.
    """
    block, host = divmod(index, 254)
    return f"100.0.{block + 1}.{host + 1}"


def _generate_privkey(rng: random.Random) -> str:
    return secrets.token_hex(32)


def _write_validator_config(
    run_dir: Path,
    client_list: list[str],
    total_subnets: int,
    aggregators_per_subnet: int,
    rng: random.Random,
    active_epoch: int = 18,
) -> None:
    genesis_dir = run_dir / "genesis"
    genesis_dir.mkdir(parents=True, exist_ok=True)

    validators: list[dict[str, Any]] = []
    host_index = 0
    client_indices: dict[str, int] = {}

    for client in client_list:
        idx = client_indices.get(client, 0)
        client_indices[client] = idx + 1
        name = f"{client}_{idx}"
        validators.append(
            {
                "name": name,
                "privkey": _generate_privkey(rng),
                "enrFields": {
                    "ip": _node_ip(host_index),
                    "quic": 9001 + host_index,
                },
                "metricsPort": 8081 + host_index,
                "apiPort": 5052,
                "isAggregator": False,
                "count": 1,
            }
        )
        host_index += 1

    subnet_buckets: dict[int, list[int]] = {}
    for i, _ in enumerate(validators):
        subnet = i % total_subnets
        subnet_buckets.setdefault(subnet, []).append(i)

    for subnet in range(total_subnets):
        candidates = subnet_buckets.get(subnet, [])
        n_select = min(aggregators_per_subnet, len(candidates))
        selected = rng.sample(candidates, n_select) if n_select else []
        for idx in selected:
            validators[idx]["isAggregator"] = True

    config = {
        "shuffle": "roundrobin",
        "deployment_mode": "local",
        "config": {
            "activeEpoch": active_epoch,
            "keyType": "hash-sig",
            "attestation_committee_count": total_subnets,
        },
        "validators": validators,
    }

    vc_path = genesis_dir / "validator-config.yaml"
    with open(vc_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    print(f"  Wrote {vc_path}")



def _resolve_config(template: dict[str, Any], run_index: int) -> dict[str, Any]:
    template_seed = template["fuzzer"]["seed"]
    seed = template_seed + run_index
    rng = random.Random(seed)

    fuzzer_raw = template["fuzzer"]
    simulation_raw = template.get("simulation", {})
    clients_raw = template.get("clients", {})
    network_raw = template.get("network", {})

    duration_secs = _resolve_value(fuzzer_raw["duration_secs"], rng)
    runner = _resolve_value(fuzzer_raw["runner"], rng)

    total_nodes = int(_resolve_value(simulation_raw["total_nodes"], rng))
    total_subnets = int(_resolve_value(simulation_raw["total_subnets"], rng))
    aggregators_per_subnet = int(
        _resolve_value(simulation_raw.get("aggregators_per_subnet", 1), rng)
    )
    sig_agg_rate = float(
        _resolve_value(
            simulation_raw.get("signatures_aggregation_rate", 22.704), rng
        )
    )
    rec_agg_rate = float(
        _resolve_value(
            simulation_raw.get("recursive_aggregation_rate", 0.0), rng
        )
    )
    # Wire size of every fake stub proof (--shadow-xmss-fake-proof-size).
    # Default mirrors the client's compiled-in 32 KiB; the ByteList512KiB SSZ
    # cap bounds it at 524288.
    fake_proof_size = int(
        _resolve_value(simulation_raw.get("fake_proof_size_bytes", 32768), rng)
    )

    client_weights = _resolve_weight_table(clients_raw, rng)
    client_list, node_counts = _sample_clients(client_weights, total_nodes, rng)

    region_weights = _resolve_weight_table(
        network_raw.get("regions", DEFAULT_REGION_WEIGHTS), rng
    )
    bandwidth_weights = _resolve_weight_table(
        network_raw.get("bandwidths", DEFAULT_BANDWIDTH_WEIGHTS), rng
    )
    jitter_ratio = float(
        _resolve_value(network_raw.get("latency_jitter_ratio", 0.3), rng)
    )
    packet_loss = float(_resolve_value(network_raw.get("packet_loss", 0.0), rng))
    # Multiplier on the region latency matrix (1.0 = the hardcoded geography).
    latency_scale = float(_resolve_value(network_raw.get("latency_scale", 1.0), rng))

    fuzzer_section: dict[str, Any] = {
        "run_index": run_index,
        "template_seed": template_seed,
        "seed": seed,
        "duration_secs": duration_secs,
        "output_dir": fuzzer_raw["output_dir"],
        "runner": runner,
        "base_genesis_dir": fuzzer_raw.get(
            "base_genesis_dir", "templates/genesis"
        ),
        "render_notebooks": fuzzer_raw.get("render_notebooks", False),
    }

    simulation_section: dict[str, Any] = {
        "total_nodes": total_nodes,
        "total_subnets": total_subnets,
        "aggregators_per_subnet": aggregators_per_subnet,
        "signatures_aggregation_rate": sig_agg_rate,
        "recursive_aggregation_rate": rec_agg_rate,
        "fake_proof_size_bytes": fake_proof_size,
    }

    # Recorded per run so sweep analysis can correlate outcomes against the
    # sampled network conditions. Region/bandwidth *assignments* land in the run
    # dir as regions.json / bandwidths.json; these are the sampled weights.
    network_section: dict[str, Any] = {
        "latency_jitter_ratio": jitter_ratio,
        "packet_loss": packet_loss,
        "regions": {k: round(v, 4) for k, v in region_weights.items()},
        "bandwidths": {k: round(v, 4) for k, v in bandwidth_weights.items()},
    }

    resolved: dict[str, Any] = {
        "fuzzer": fuzzer_section,
        "simulation": simulation_section,
        "network": network_section,
        "clients": {k: round(v, 4) for k, v in client_weights.items()},
        "node_counts": node_counts,
    }

    resolved["_internal"] = {
        "client_list": client_list,
        "region_weights": region_weights,
        "bandwidth_weights": bandwidth_weights,
        "jitter_ratio": jitter_ratio,
        "packet_loss": packet_loss,
        "latency_scale": latency_scale,
        "rng_state": rng,
    }

    return resolved


def _validate_template(template: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    fz = template.get("fuzzer", {})
    if not isinstance(fz, dict):
        errors.append("[fuzzer] section missing or not a table")
        return errors

    runner = fz.get("runner", "local")
    if runner not in ("local", "docker-arm"):
        errors.append(f"runner must be 'local' or 'docker-arm', got '{runner}'")

    clients = template.get("clients", {})
    if not clients:
        errors.append("[clients] section missing or empty")

    if runner == "docker-arm":
        docker_arm = template.get("docker_arm", {})
        if not isinstance(docker_arm, dict):
            errors.append("[docker_arm] section missing or not a table")
        else:
            if not docker_arm.get("shadow_image"):
                errors.append("[docker_arm].shadow_image is required for docker-arm runner")
            if not docker_arm.get("image_name"):
                errors.append("[docker_arm].image_name is required for docker-arm runner")

        client_images = template.get("client_images", {})
        if not isinstance(client_images, dict):
            errors.append("[client_images] section missing or not a table")
        else:
            for client_name in clients:
                image_cfg = client_images.get(client_name)
                if not isinstance(image_cfg, dict):
                    errors.append(
                        f"[client_images.{client_name}] is required for docker-arm runner"
                    )
                    continue
                if not image_cfg.get("image"):
                    errors.append(f"[client_images.{client_name}].image is required")
                explicit_path = image_cfg.get("executable_path_in_image")
                if explicit_path is not None and not str(explicit_path).startswith("/"):
                    errors.append(
                        f"[client_images.{client_name}].executable_path_in_image must be absolute"
                    )

    SCRIPT_DIR = SCRIPTS_DIR
    for client_name in clients:
        cmd = SCRIPT_DIR / "client-cmds" / f"{client_name}-cmd.sh"
        if not cmd.is_file():
            errors.append(
                f"client '{client_name}' has no client-cmds/{client_name}-cmd.sh"
            )

    return errors


def _hash_sig_cache_key(genesis_dir: Path) -> str:
    validator_config = genesis_dir / "validator-config.yaml"
    with open(validator_config) as f:
        config = yaml.safe_load(f) or {}
    validators = config.get("validators", [])
    validator_count = sum(int(v.get("count", 1)) for v in validators)
    active_epoch = int(config.get("config", {}).get("activeEpoch", 0))
    return f"validators-{validator_count}-active-{active_epoch}"


def _restore_hash_sig_key_cache(genesis_dir: Path, cache_root: Path | None) -> Path | None:
    if cache_root is None:
        return None
    cache_dir = cache_root / _hash_sig_cache_key(genesis_dir)
    if not cache_dir.is_dir():
        return cache_dir
    target = genesis_dir / "hash-sig-keys"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(cache_dir, target)
    print(f"  Reusing cached hash-sig keys: {cache_dir}")
    return cache_dir


def _store_hash_sig_key_cache(genesis_dir: Path, cache_dir: Path | None) -> None:
    if cache_dir is None or cache_dir.exists():
        return
    source = genesis_dir / "hash-sig-keys"
    if not source.is_dir():
        return
    cache_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = cache_dir.with_name(f".{cache_dir.name}.tmp")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    shutil.copytree(source, tmp_dir)
    os.replace(tmp_dir, cache_dir)
    print(f"  Cached hash-sig keys: {cache_dir}")


def _run_genesis(
    run_dir: Path,
    base_genesis_dir: str,
    key_cache_root: Path | None = None,
) -> None:
    run_dir = run_dir.resolve()
    script = SCRIPTS_DIR / "generate-genesis.sh"
    genesis_dir = run_dir / "genesis"
    cache_dir = _restore_hash_sig_key_cache(genesis_dir, key_cache_root)
    subprocess.run(
        [
            str(script),
            str(genesis_dir),
            "--genesis-time",
            str(SHADOW_GENESIS_TIME),
        ],
        check=True,
        env=os.environ | {"DOCKER_USE_SUDO": os.environ.get("DOCKER_USE_SUDO", "false")},
    )
    _store_hash_sig_key_cache(genesis_dir, cache_dir)


def _run_topology(run_dir: Path, resolved: dict[str, Any]) -> None:
    run_dir = run_dir.resolve()
    internal = resolved["_internal"]
    region_weights = internal["region_weights"]
    bandwidth_weights = internal["bandwidth_weights"]
    jitter_ratio = internal["jitter_ratio"]
    seed = resolved["fuzzer"]["seed"]
    total_nodes = resolved["simulation"]["total_nodes"]

    script = FUZZER_ROOT / "shadow_fuzzer" / "generate_shadow_topology.py"
    cmd: list[str] = [
        sys.executable,
        str(script),
        str(total_nodes),
        str(run_dir),
        "--seed",
        str(seed),
        "--jitter",
        str(jitter_ratio),
        "--region-weights",
        json.dumps(region_weights),
        "--bandwidth-weights",
        json.dumps(bandwidth_weights),
        "--packet-loss",
        str(internal["packet_loss"]),
        "--latency-scale",
        str(internal["latency_scale"]),
    ]
    subprocess.run(cmd, check=True)


def _run_shadow_yaml(run_dir: Path, resolved: dict[str, Any]) -> None:
    run_dir = run_dir.resolve()
    script = SCRIPTS_DIR / "generate-shadow-yaml.sh"
    genesis_dir = run_dir / "genesis"
    seed = resolved["fuzzer"]["seed"]
    duration = resolved["fuzzer"]["duration_secs"]
    stop_time = f"{duration}s"
    shadow_yaml = run_dir / "shadow.yaml"
    topology_gml = run_dir / "topology.gml"
    bandwidths_json = run_dir / "bandwidths.json"
    metadata_json = run_dir / "run-metadata.json"

    cmd: list[str] = [
        "bash",
        str(script),
        str(genesis_dir),
        "--project-root",
        str(FUZZER_ROOT),
        "--stop-time",
        stop_time,
        "--output",
        str(shadow_yaml),
        "--seed",
        str(seed),
        "--shadow-data-dir",
        str(run_dir / "shadow.data"),
    ]

    if topology_gml.is_file() and bandwidths_json.is_file():
        cmd += [
            "--topology-gml",
            str(topology_gml),
            "--bandwidths-json",
            str(bandwidths_json),
        ]

    if "client_runtime" in resolved and metadata_json.is_file():
        cmd += ["--client-runtime-json", str(metadata_json)]

    subprocess.run(cmd, check=True)


def _run_shadow(run_dir: Path, resolved: dict[str, Any], dry_run: bool = False) -> None:
    run_dir = run_dir.resolve()
    shadow_yaml = run_dir / "shadow.yaml"
    shadow_data = run_dir / "shadow.data"
    runner = resolved["fuzzer"]["runner"]

    if shadow_data.exists():
        shutil.rmtree(shadow_data)

    if dry_run:
        print(f"  [dry-run] Would clean {shadow_data}")
        print(f"  [dry-run] Would run: shadow {shadow_yaml}")
        return

    if runner == "local":
        subprocess.run(
            ["shadow", "-d", str(shadow_data), "--parallelism", "16", "--progress", "true", str(shadow_yaml)],
            check=True,
        )
    elif runner == "docker-arm":
        project_root = FUZZER_ROOT.resolve()
        docker_image = resolved.get("docker_arm", {}).get(
            "image_name", "kamilsa/shadow-arm:latest"
        )
        # Remove any stale container with the same name from a previous run
        subprocess.run(
            ["docker", "rm", "-f", "shadow-sim-container"],
            check=False,
            capture_output=True,
        )
        subprocess.run(
            [
                "docker", "run", "--rm",
                "--name", "shadow-sim-container",
                "--platform", "linux/arm64",
                "--security-opt", "seccomp=unconfined",
                "--shm-size", "4g",
                "-v", f"{project_root}:{project_root}",
                "-v", f"{run_dir}:{run_dir}",
                "-w", str(project_root),
                "--entrypoint", "/bin/bash",
                docker_image,
                "-c", f"shadow -d {shadow_data} --progress true {shadow_yaml}",
            ],
            check=True,
        )


def _run_stats(run_dir: Path, metadata_path: Path) -> None:
    run_dir = run_dir.resolve()
    script = FUZZER_ROOT / "shadow_fuzzer" / "stats_shadow.py"
    subprocess.run(
        [sys.executable, str(script), str(run_dir), "--metadata-json", str(metadata_path)],
        check=True,
    )


def _render_notebooks(run_dir: Path) -> None:
    """Execute and render analysis notebooks for a completed run."""
    script = FUZZER_ROOT / "scripts" / "render_notebooks.py"
    if not script.is_file():
        print("  SKIP: render_notebooks.py not found")
        return
    subprocess.run(
        ["uv", "run", "python3", str(script), "--run-dir", str(run_dir.resolve())],
        check=False,
    )
    # Rebuild the static observatory site so new runs appear immediately
    site_dir = FUZZER_ROOT / "site"
    if (site_dir / "package.json").is_file():
        print("  Rebuilding observatory site...")
        subprocess.run(
            ["npm", "run", "build"],
            cwd=str(site_dir),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _write_stats_snapshot(
    run_dir: Path,
    metadata: dict[str, Any],
    extra_warnings: list[str] | None = None,
    *,
    status: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    from shadow_fuzzer import stats_shadow

    stats = stats_shadow.collect_stats(str(run_dir), metadata)
    warnings = list(dict.fromkeys([
        *stats.get("warnings", []),
        *(extra_warnings or []),
    ]))
    stats["warnings"] = warnings
    if status is not None:
        stats["status"] = status
    if error is not None:
        stats["error"] = error
    (run_dir / "stats.json").write_text(json.dumps(stats, indent=2))
    print(f"  Wrote {run_dir / 'stats.json'}")
    return stats


def _shadow_failure_message(exc: subprocess.CalledProcessError) -> str:
    command = exc.cmd
    if isinstance(command, (list, tuple)):
        command_text = shlex.join(str(part) for part in command)
    else:
        command_text = str(command)
    return f"Shadow exited with status {exc.returncode}: {command_text}"


def _generate_run_id(run_index: int) -> str:
    try:
        import coolname

        return coolname.generate_slug(3)
    except ImportError:
        return f"run-{run_index:04d}"


def _validate_resolved(resolved: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    sim = resolved["simulation"]
    total_nodes = sim["total_nodes"]
    total_subnets = sim["total_subnets"]
    agg_per_subnet = sim["aggregators_per_subnet"]

    if total_subnets > total_nodes:
        warnings.append(
            f"total_subnets ({total_subnets}) > total_nodes ({total_nodes}); "
            "some subnets may have no nodes"
        )

    node_counts = resolved.get("node_counts", {})
    for subnet in range(total_subnets):
        nodes_in_subnet = sum(
            1
            for i in range(total_nodes)
            if i % total_subnets == subnet
        )
        if nodes_in_subnet < agg_per_subnet:
            warnings.append(
                f"subnet {subnet}: only {nodes_in_subnet} nodes available, "
                f"requested {agg_per_subnet} aggregators"
            )

    sampled_client_count = sum(node_counts.values())
    if sampled_client_count != total_nodes:
        warnings.append(
            f"sampled client count ({sampled_client_count}) != total_nodes ({total_nodes})"
        )

    return warnings


def _clean_observatory_renders() -> None:
    """Remove rendered notebook HTML and manifest from site/rendered/."""
    rendered_dir = FUZZER_ROOT / "site" / "rendered"
    if not rendered_dir.is_dir():
        return
    removed_any = False
    for path in rendered_dir.iterdir():
        if path.name == ".gitkeep":
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        removed_any = True
    if removed_any:
        print("Cleaned observatory rendered notebooks.")


def _clean_output_dir(output_dir: Path) -> list[Path]:
    removed: list[Path] = []
    output_dir.mkdir(parents=True, exist_ok=True)
    for path in output_dir.iterdir():
        if path.name == HASH_SIG_KEY_CACHE_DIR:
            continue

        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        removed.append(path)
    return removed


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run randomized Shadow fuzzer sweeps")
    parser.add_argument(
        "config",
        nargs="?",
        default="config.example.docker-arm.toml",
        help="Path to the fuzzer TOML config",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate run artifacts and metadata without running Shadow",
    )
    parser.add_argument(
        "--clean-output",
        action="store_true",
        help=(
            "Remove previous run files from output_dir before starting, including "
            "run directories and observatory rendered notebooks, "
            "but keep cached hash-sig keys."
        ),
    )
    parser.add_argument(
        "--run-index",
        type=int,
        default=None,
        help="Run only a single run index (0-based) instead of the full sweep",
    )
    parser.add_argument(
        "--serve",
        "-s",
        action="store_true",
        help="Start the observatory site (astro dev) alongside the fuzzer. "
        "Auto-enables render_notebooks so completed runs appear on the site.",
    )
    parser.add_argument(
        "--serve-only",
        action="store_true",
        help="Start the observatory site without running any fuzzer simulations. "
        "Previous run data will be available in the browser.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Observatory site subprocess management (--serve)
# ---------------------------------------------------------------------------

_site_process: subprocess.Popen[bytes] | None = None


def _start_observatory_site() -> None:
    """Build and serve the observatory site in production mode."""
    global _site_process
    site_dir = FUZZER_ROOT / "site"

    if not (site_dir / "node_modules").is_dir():
        print("Observatory site dependencies not installed. Running npm install...")
        subprocess.run(
            ["npm", "install"],
            cwd=str(site_dir),
            check=True,
        )

    print("Building observatory site...")
    subprocess.run(
        ["npm", "run", "build"],
        cwd=str(site_dir),
        check=True,
    )

    print("Starting observatory site (astro preview)...")
    _site_process = subprocess.Popen(
        ["npm", "run", "preview", "--", "--host", "0.0.0.0"],
        cwd=str(site_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,  # create new process group for clean kill
    )
    print("Observatory site: http://0.0.0.0:4321")
    print()


def _stop_observatory_site() -> None:
    """Kill the astro dev server subprocess (if running)."""
    global _site_process
    if _site_process is None:
        return
    try:
        # Kill entire process group so npm + astro both die
        os.killpg(os.getpgid(_site_process.pid), signal.SIGTERM)
        _site_process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(os.getpgid(_site_process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    _site_process = None


def _handle_shutdown_signal(signum: int, frame: Any) -> None:
    """Forward SIGINT/SIGTERM to the site subprocess, then re-raise."""
    print("\nShutting down observatory site...")
    _stop_observatory_site()
    # Re-raise the signal to trigger the default handler (exit)
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def main() -> None:
    args = _parse_args()
    dry_run = args.dry_run
    config_path_abs = Path(args.config)
    if not config_path_abs.is_absolute():
        config_path_abs = Path.cwd() / config_path_abs

    if not config_path_abs.is_file():
        print(f"ERROR: config file not found: {config_path_abs}", file=sys.stderr)
        sys.exit(1)

    with open(config_path_abs, "rb") as f:
        template = tomllib.load(f)

    errors = _validate_template(template)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    fuzzer = template["fuzzer"]
    max_runs = _resolve_value(fuzzer["max_runs"], random.Random(0))
    if dry_run:
        max_runs = 1
    output_dir = Path(fuzzer.get("output_dir", "fuzzer-output"))
    if not output_dir.is_absolute():
        output_dir = config_path_abs.parent / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Fuzzer: max_runs={max_runs}, output_dir={output_dir}")
    print(f"Config: {config_path_abs}")
    print()

    # --serve / --serve-only: auto-enable notebook rendering and start astro dev server
    if args.serve or args.serve_only:
        fuzzer["render_notebooks"] = True
        atexit.register(_stop_observatory_site)
        signal.signal(signal.SIGINT, _handle_shutdown_signal)
        signal.signal(signal.SIGTERM, _handle_shutdown_signal)
        _start_observatory_site()

    # --serve-only: just serve the site, no fuzzer runs
    if args.serve_only:
        print("Serve-only mode: no new runs will be executed.")
        print("View previous runs at http://0.0.0.0:4321")
        print("Press Ctrl+C to stop.")
        # Block until interrupted; cleanup is handled by atexit/signal handlers
        try:
            signal.pause()
        except KeyboardInterrupt:
            pass
        return

    if args.clean_output:
        removed = _clean_output_dir(output_dir)
        if removed:
            print("Cleaned previous output files:")
            for path in removed:
                print(f"  Removed {path}")
        else:
            print("Output directory already clean.")
        # Also clean observatory rendered notebooks
        _clean_observatory_renders()
        print()

    docker_runtime = None
    if fuzzer.get("runner") == "docker-arm":
        print("Preparing Docker ARM runtime...")
        docker_runtime = _prepare_docker_arm_runtime(template, output_dir, dry_run)
        print()
    hash_sig_key_cache_root = output_dir / HASH_SIG_KEY_CACHE_DIR
    failed_runs = 0

    run_indices: list[int]
    if args.run_index is not None:
        run_indices = [args.run_index]
    else:
        run_indices = list(range(int(max_runs)))

    for run_index in run_indices:
        if args.run_index is not None:
            print(f"--- Run {run_index + 1} (index {run_index}) ---")
        else:
            print(f"--- Run {run_index + 1}/{max_runs} ---")

        resolved = _resolve_config(template, run_index)
        if docker_runtime:
            resolved.update(docker_runtime)
        warnings = _validate_resolved(resolved)
        for w in warnings:
            print(f"  WARNING: {w}")

        run_id = _generate_run_id(run_index)
        run_dir = output_dir / run_id
        while run_dir.exists():
            run_id = _generate_run_id(run_index + 1000)
            run_dir = output_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)

        print(f"  Run ID: {run_id}")
        print(f"  Seed: {resolved['fuzzer']['seed']}")
        print(f"  Nodes: {resolved['simulation']['total_nodes']}")
        print(f"  Subnets: {resolved['simulation']['total_subnets']}")
        print(f"  Duration: {resolved['fuzzer']['duration_secs']}s")
        print(f"  Node counts: {resolved['node_counts']}")

        internal = resolved.pop("_internal", {})
        metadata = {
            "run_id": run_id,
            "run_index": run_index,
            "fuzzer": resolved["fuzzer"],
            "simulation": resolved["simulation"],
            "network": resolved.get("network", {}),
            "clients": resolved.get("clients", {}),
            "node_counts": resolved.get("node_counts", {}),
        }
        for key in ("docker_arm", "client_images", "client_runtime"):
            if key in resolved:
                metadata[key] = resolved[key]

        try:
            print("  Generating validator config...")
            _write_validator_config(
                run_dir,
                internal["client_list"],
                resolved["simulation"]["total_subnets"],
                resolved["simulation"]["aggregators_per_subnet"],
                internal["rng_state"],
                # Number of epochs of the key's 2^32 lifetime to materialise.
                # Drives hash-sig keygen time and secret-key size; the public key
                # (52 B) and signature (2536 B) are fixed by the compile-time
                # scheme, so lowering it does not change anything on the wire.
                int(fuzzer.get("active_epoch", 18)),
            )

            print("  Writing run metadata...")
            metadata_path = run_dir / "run-metadata.json"
            metadata_path.write_text(json.dumps(metadata, indent=2))
            print(f"  Wrote {metadata_path}")

            generate_genesis = not dry_run

            if generate_genesis:
                print("  Generating genesis...")
                _run_genesis(
                    run_dir,
                    str(fuzzer.get("base_genesis_dir", "templates/genesis")),
                    key_cache_root=hash_sig_key_cache_root,
                )

            print("  Generating topology...")
            _run_topology(run_dir, {**resolved, "_internal": internal})

            print("  Generating shadow.yaml...")
            _run_shadow_yaml(run_dir, {**resolved, "_internal": internal})

            print("  Running Shadow...")
            shadow_error = None
            try:
                _run_shadow(run_dir, {**resolved, "_internal": internal}, dry_run=dry_run)
            except subprocess.CalledProcessError as exc:
                shadow_error = _shadow_failure_message(exc)
                print(f"  ERROR: {shadow_error}")

            if not dry_run:
                print("  Collecting stats...")
                try:
                    _run_stats(run_dir, metadata_path)
                except subprocess.CalledProcessError:
                    if shadow_error is None:
                        raise
                    print("  Stats command failed; writing snapshot from partial logs.")
                    _write_stats_snapshot(
                        run_dir,
                        metadata,
                        [shadow_error],
                        status="error",
                        error=shadow_error,
                    )
            else:
                print("  [dry-run] Writing metadata-only stats.json")
                _write_stats_snapshot(run_dir, metadata, ["dry-run"])

            stats = json.loads((run_dir / "stats.json").read_text())
            if shadow_error is not None:
                stats["status"] = "error"
                stats["error"] = shadow_error
                stats["warnings"] = list(
                    dict.fromkeys([*stats.get("warnings", []), shadow_error])
                )
                (run_dir / "stats.json").write_text(json.dumps(stats, indent=2))
            final_status = "error" if shadow_error else "complete"

            # Render analysis notebooks if enabled and run succeeded
            if not dry_run and final_status != "error" and resolved.get("fuzzer", {}).get("render_notebooks"):
                try:
                    print("  Rendering analysis notebooks...")
                    _render_notebooks(run_dir)
                except Exception as exc:
                    print(f"  WARNING: notebook rendering failed: {exc}")

            if shadow_error is not None:
                failed_runs += 1
                print(f"  Recorded failed Shadow run → {run_dir}")
            else:
                print(f"  Done → {run_dir}")
            print()
        except Exception:
            raise

    if failed_runs:
        print(f"Finished {max_runs} run(s) with {failed_runs} Shadow error(s).")
        _stop_observatory_site()
        sys.exit(1)
    else:
        print(f"All {max_runs} runs complete.")
        _stop_observatory_site()


if __name__ == "__main__":
    main()
