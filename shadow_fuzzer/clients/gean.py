from __future__ import annotations

import re
from typing import Any

from .base import ClientParser, parse_block_size_bytes

_SOURCE = "gean_text"

# gean logs ISO-8601 timestamps, e.g. 2000-01-01T00:01:04.812Z (frac is ms).
_GEAN_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T(\d{2}):(\d{2}):(\d{2})\.(\d{3})")


class GeanParser(ClientParser):
    name = "gean"
    attestation_id_warning = (
        "gean text parser uses (slot, validator_id), not a cryptographic attestation ID"
    )

    def match_host(self, host_name: str) -> bool:
        return host_name.startswith("gean")

    def parse_timestamp(self, line: str, default_ts_ms: float) -> float:
        m = _GEAN_TS_RE.search(line)
        if m:
            h, mi, s, ms = m.groups()
            return (int(h) * 3600 + int(mi) * 60 + int(s)) * 1000 + int(ms)
        return default_ts_ms

    def parse_line(self, line: str, ts_ms: float) -> list[dict[str, Any]]:
        # Aggregators log each gossiped attestation they verify (non-aggregators
        # do not verify individual attestations, so receive events come from them).
        m = re.search(r"attestation verified: validator=(\d+)\s+slot=(\d+)", line)
        if m:
            return [{
                "_kind": "receive_attestation",
                "ts": ts_ms / 1000,
                "slot": int(m.group(2)),
                "validator_id": int(m.group(1)),
                "source": _SOURCE,
            }]

        m = re.search(
            r"published attestation to network slot=(\d+)\s+validator=(\d+)", line
        )
        if m:
            return [{
                "_kind": "publish_attestation",
                "ts": ts_ms / 1000,
                "slot": int(m.group(1)),
                "validator_id": int(m.group(2)),
                "source": _SOURCE,
            }]

        # Proposer publishing its own block to the block gossip topic. Scope to
        # the /block/ topic so attestation/aggregation publishes don't match.
        m = re.search(
            r"publish gossip topic=\S*/block/\S*\s+slot=(\d+)\s+proposer=(\d+)", line
        )
        if m:
            return [_block_event("publish_block", m, line, ts_ms)]

        m = re.search(r"received block slot=(\d+)\s+proposer=(\d+)", line)
        if m:
            return [_block_event("receive_block", m, line, ts_ms)]

        return []


def _block_event(kind: str, m: re.Match[str], line: str, ts_ms: float) -> dict[str, Any]:
    evt: dict[str, Any] = {
        "_kind": kind,
        "ts": ts_ms / 1000,
        "slot": int(m.group(1)),
        "proposer": int(m.group(2)),
        "source": _SOURCE,
    }
    size_bytes = parse_block_size_bytes(line)
    if size_bytes is None:
        sm = re.search(r"compressed_len=(\d+)", line)
        if sm:
            size_bytes = int(sm.group(1))
    if size_bytes is not None:
        evt["size_bytes"] = size_bytes
    return evt
