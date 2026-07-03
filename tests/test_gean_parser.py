from shadow_fuzzer.clients import get_parser_for_host
from shadow_fuzzer.clients.gean import GeanParser


def test_gean_parser_is_registered_for_gean_hosts():
    assert get_parser_for_host("gean-2").name == "gean"


def test_gean_parser_extracts_receive_attestation():
    line = (
        "2000-01-01T00:01:04.812Z  INFO   [signature]   "
        "attestation verified: validator=0 slot=3"
    )

    parser = GeanParser()
    events = parser.parse_line(line, parser.parse_timestamp(line, 0))

    assert events == [{
        "_kind": "receive_attestation",
        "ts": 64.812,
        "slot": 3,
        "validator_id": 0,
        "source": "gean_text",
    }]


def test_gean_parser_extracts_publish_attestation():
    line = (
        "2000-01-01T00:01:00.801Z  INFO   [chain]   "
        "published attestation to network slot=0 validator=2"
    )

    parser = GeanParser()
    events = parser.parse_line(line, parser.parse_timestamp(line, 0))

    assert events == [{
        "_kind": "publish_attestation",
        "ts": 60.801,
        "slot": 0,
        "validator_id": 2,
        "source": "gean_text",
    }]


def test_gean_parser_extracts_publish_block_from_block_topic():
    line = (
        "2000-01-01T00:01:08.242Z  INFO   [network]   "
        "publish gossip topic=/leanconsensus/12345678/block/ssz_snappy "
        "slot=2 proposer=2"
    )

    parser = GeanParser()
    events = parser.parse_line(line, parser.parse_timestamp(line, 0))

    assert events == [{
        "_kind": "publish_block",
        "ts": 68.242,
        "slot": 2,
        "proposer": 2,
        "source": "gean_text",
    }]


def test_gean_parser_extracts_receive_block():
    line = (
        "2000-01-01T00:01:04.434Z  INFO   [chain]   "
        "received block slot=1 proposer=1 "
        "block_root=0xac81ff837f8f94869ca460fb385658ecc4003635175c7c93f56a928dc7299028"
    )

    parser = GeanParser()
    events = parser.parse_line(line, parser.parse_timestamp(line, 0))

    assert events == [{
        "_kind": "receive_block",
        "ts": 64.434,
        "slot": 1,
        "proposer": 1,
        "source": "gean_text",
    }]


def test_gean_parser_ignores_unrelated_lines():
    parser = GeanParser()
    assert parser.parse_line("2000-01-01T00:00:00.000Z  INFO   [node]   started", 0) == []
