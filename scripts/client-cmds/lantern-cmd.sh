#!/bin/bash

#-----------------------lantern setup----------------------
LANTERN_IMAGE="${LANTERN_IMAGE:-bitminemavan/lantern:v0.0.5-shadow}"
lantern_binary="${LANTERN_BINARY:-lantern}"

devnet_flag=""
if [ -n "$devnet" ]; then
        devnet_flag="--devnet $devnet"
fi

# Set aggregator flag based on isAggregator value
aggregator_flag=""
if [ "$isAggregator" == "true" ]; then
    aggregator_flag="--is-aggregator"
fi

# In multi-subnet deployments, an aggregator must subscribe to every subnet's
# attestation topics so it can aggregate votes from all committees. The caller
# (spin-node.sh / ansible roles) exports aggregateSubnetIds as a CSV of the
# full subnet id set for the network.
aggregate_subnet_ids_flag=""
if [ "$isAggregator" == "true" ] && [ -n "${aggregateSubnetIds:-}" ] && [[ "$aggregateSubnetIds" == *,* ]]; then
    aggregate_subnet_ids_flag="--aggregate-subnet-ids $aggregateSubnetIds"
fi

# Set attestation committee count flag if explicitly configured
attestation_committee_flag=""
if [ -n "$attestationCommitteeCount" ]; then
    attestation_committee_flag="--attestation-committee-count $attestationCommitteeCount"
fi

# Set checkpoint sync URL when restarting with checkpoint sync
checkpoint_sync_flag=""
if [ -n "${checkpoint_sync_url:-}" ]; then
    checkpoint_sync_flag="--checkpoint-sync-url $checkpoint_sync_url"
fi

# Set HTTP port (default to 5055 if not specified in validator-config.yaml)
if [ -z "$httpPort" ]; then
    httpPort="5055"
fi

shadow_cost_flags=""
if [ -n "${LANTERN_SHADOW_XMSS_AGGREGATE_RATE:-}" ]; then
    shadow_cost_flags="${shadow_cost_flags} --shadow-xmss-aggregate-signatures-rate ${LANTERN_SHADOW_XMSS_AGGREGATE_RATE}"
fi
if [ -n "${LANTERN_SHADOW_XMSS_VERIFY_RATE:-}" ]; then
    shadow_cost_flags="${shadow_cost_flags} --shadow-xmss-verify-aggregated-signatures-rate ${LANTERN_SHADOW_XMSS_VERIFY_RATE}"
fi
if [ -n "${LANTERN_SHADOW_XMSS_MERGE_RATE:-}" ]; then
    shadow_cost_flags="${shadow_cost_flags} --shadow-xmss-merge-rate ${LANTERN_SHADOW_XMSS_MERGE_RATE}"
fi

# Lantern's repo: https://github.com/Pier-Two/lantern
#
# Flags match the v0.0.5 CLI (see /usr/local/bin/lantern-entrypoint.sh in the
# image). Notably v0.0.5 takes a single --validator_config DIR that holds both
# annotated_validators.yaml and validator-config.yaml (the genesis dir), and
# discovers peers via --bootnodes pointing at nodes.yaml.
#
# NOTE: do not prefix node_binary with `/usr/bin/env VAR=... `. generate-shadow-yaml.sh
# replaces only the first whitespace-delimited token with the resolved binary
# path, so a prefix would be left in the args. OPENSSL_ia32cap is an x86-only
# knob and a no-op on arm64 anyway.
node_binary="$lantern_binary \
        --data-dir $dataDir/$item \
        --genesis-config $configDir/config.yaml \
        --nodes-path $configDir/nodes.yaml \
        --genesis-state $configDir/genesis.ssz \
        --validator_config $configDir \
        --bootnodes $configDir/nodes.yaml \
        $devnet_flag \
        --node-id $item --node-key-path $configDir/$privKeyPath \
        --listen-address /ip4/0.0.0.0/udp/$quicPort/quic-v1 \
        --metrics-port $metricsPort \
        --http-port $apiPort \
        --log-level info \
        --hash-sig-key-dir $configDir/hash-sig-keys \
        $attestation_committee_flag \
        $aggregator_flag \
        $aggregate_subnet_ids_flag \
        $checkpoint_sync_flag \
        $shadow_cost_flags"

node_docker="$LANTERN_IMAGE --data-dir /data \
        --genesis-config /config/config.yaml \
        --nodes-path /config/nodes.yaml \
        --genesis-state /config/genesis.ssz \
        --validator_config /config \
        --bootnodes /config/nodes.yaml \
        $devnet_flag \
        --node-id $item --node-key-path /config/$privKeyPath \
        --listen-address /ip4/0.0.0.0/udp/$quicPort/quic-v1 \
        --metrics-port $metricsPort \
        --http-port $apiPort \
        --log-level info \
        --hash-sig-key-dir /config/hash-sig-keys \
        $attestation_committee_flag \
        $aggregator_flag \
        $aggregate_subnet_ids_flag \
        $checkpoint_sync_flag \
        $shadow_cost_flags"

# choose either binary or docker
node_setup="binary"
