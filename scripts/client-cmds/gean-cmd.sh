#!/bin/bash

#-----------------------gean setup----------------------
binary_path="$scriptDir/../gean/bin/gean"

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

# Shadow simulator: model XMSS prover cost as virtual-time sleeps (sig/s rates).
# generate-shadow-yaml.sh exports GEAN_SHADOW_XMSS_* from the run's aggregation
# rates; unset means no delay (real deployments are unaffected). gean has no
# separate recursive-merge step, so recursive_aggregation_rate has no gean knob.
shadow_cost_flags=""
if [ -n "${GEAN_SHADOW_XMSS_AGGREGATE_RATE:-}" ]; then
    shadow_cost_flags="${shadow_cost_flags} --shadow-xmss-aggregate-signatures-rate ${GEAN_SHADOW_XMSS_AGGREGATE_RATE}"
fi
if [ -n "${GEAN_SHADOW_XMSS_VERIFY_AGGREGATED_RATE:-}" ]; then
    shadow_cost_flags="${shadow_cost_flags} --shadow-xmss-verify-aggregated-signatures-rate ${GEAN_SHADOW_XMSS_VERIFY_AGGREGATED_RATE}"
fi
if [ -n "${GEAN_SHADOW_XMSS_VERIFY_RATE:-}" ]; then
    shadow_cost_flags="${shadow_cost_flags} --shadow-xmss-verify-signature-rate ${GEAN_SHADOW_XMSS_VERIFY_RATE}"
fi

# Command when running as binary
node_binary="$binary_path \
      --custom-network-config-dir $configDir \
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
node_docker="ghcr.io/geanlabs/gean:devnet5 \
      --custom-network-config-dir /config \
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

node_setup="binary"