#!/bin/bash

#-----------------------ethlambda setup----------------------

binary_path="$scriptDir/../ethlambda/target/release/ethlambda"

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

# Shadow sim-cost + fake-XMSS flags (only understood by a shadow-integration
# build, i.e. ghcr.io/lambdaclass/ethlambda:*-shadow). generate-shadow-yaml.sh
# exports these ETHLAMBDA_SHADOW_XMSS_* vars for ethlambda_* nodes.
shadow_cost_flags=""
if [ "${ETHLAMBDA_SHADOW_XMSS_FAKE:-}" == "true" ]; then
    shadow_cost_flags="${shadow_cost_flags} --shadow-xmss-fake"
fi
if [ -n "${ETHLAMBDA_SHADOW_XMSS_AGGREGATE_RATE:-}" ]; then
    shadow_cost_flags="${shadow_cost_flags} --shadow-xmss-aggregate-signatures-rate ${ETHLAMBDA_SHADOW_XMSS_AGGREGATE_RATE}"
fi
if [ -n "${ETHLAMBDA_SHADOW_XMSS_VERIFY_RATE:-}" ]; then
    shadow_cost_flags="${shadow_cost_flags} --shadow-xmss-verify-aggregated-signatures-rate ${ETHLAMBDA_SHADOW_XMSS_VERIFY_RATE}"
fi
if [ -n "${ETHLAMBDA_SHADOW_XMSS_MERGE_RATE:-}" ]; then
    shadow_cost_flags="${shadow_cost_flags} --shadow-xmss-merge-rate ${ETHLAMBDA_SHADOW_XMSS_MERGE_RATE}"
fi

# Command when running as binary
node_binary="$binary_path \
      --genesis $configDir/config.yaml \
      --validators $configDir/annotated_validators.yaml \
      --bootnodes $configDir/nodes.yaml \
      --validator-config $configDir/validator-config.yaml \
      --hash-sig-keys-dir $configDir/hash-sig-keys \
      --data-dir $dataDir/$item \
      --gossipsub-port $quicPort \
      --node-id $item \
      --node-key $configDir/$item.key \
      --http-address 0.0.0.0 \
      --api-port $apiPort \
      --metrics-port $metricsPort \
      $attestation_committee_flag \
      $aggregator_flag \
      $aggregate_subnet_ids_flag \
      $checkpoint_sync_flag \
      $shadow_cost_flags"

# Command when running as docker container
node_docker="ghcr.io/lambdaclass/ethlambda:devnet4 \
      --genesis /config/config.yaml \
      --validators /config/annotated_validators.yaml \
      --bootnodes /config/nodes.yaml \
      --validator-config /config/validator-config.yaml \
      --hash-sig-keys-dir /config/hash-sig-keys \
      --data-dir /data \
      --gossipsub-port $quicPort \
      --node-id $item \
      --node-key /config/$item.key \
      --http-address 0.0.0.0 \
      --api-port $apiPort \
      --metrics-port $metricsPort \
      $attestation_committee_flag \
      $aggregator_flag \
      $aggregate_subnet_ids_flag \
      $checkpoint_sync_flag \
      $shadow_cost_flags"

# docker-arm runner forces binary mode (Shadow launches native processes, not
# containers); the composite image supplies the shadow-integration binary.
node_setup="binary"
