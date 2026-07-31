#!/bin/sh
# Point a deployment at one network or the other, then restart it.
#
#   sh deploy/switch_network.sh base
#   sh deploy/switch_network.sh base-sepolia
#
# Every value moves together on purpose. The whole failure mode of a network
# switch is changing three of them and missing the fourth: the mainnet USDC
# contract signs under the EIP-712 domain "USD Coin" where the testnet one uses
# "USDC", and a deployment that changes chain and contract but keeps the old
# domain rejects every buyer signature while looking perfectly healthy.
#
# These values are duplicated from KNOWN_NETWORKS in scripts/verify_deployment.py,
# and tests/test_deployment_config.py fails if the two ever disagree.
#
# Run scripts/verify_deployment.py --network <the same network> afterwards. It
# reads the chain, contract and domain back off /healthz and refuses a
# deployment whose three do not agree.
set -eu
cd /opt/appgen

set_var() {
    if grep -q "^$1=" .env.deploy; then
        # `|` as the delimiter: no value here contains one, while "USD Coin"
        # contains a space and the addresses would collide with `/`.
        sed -i "s|^$1=.*|$1=$2|" .env.deploy
    else
        printf '%s=%s\n' "$1" "$2" >> .env.deploy
    fi
}

case "${1:-}" in
  base)
    set_var X402_TOKEN_CONTRACT 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
    set_var X402_CHAIN_ID 8453
    set_var X402_NETWORK base
    set_var "X402_DOMAIN_NAME" "USD Coin"
    ;;
  base-sepolia)
    set_var X402_TOKEN_CONTRACT 0x036CbD53842c5426634e7929541eC2318f3dCF7e
    set_var X402_CHAIN_ID 84532
    set_var X402_NETWORK base-sepolia
    set_var "X402_DOMAIN_NAME" "USDC"
    ;;
  *)
    echo "usage: $0 base|base-sepolia" >&2
    exit 2
    ;;
esac

grep -E '^X402_(TOKEN_CONTRACT|CHAIN_ID|NETWORK|DOMAIN_NAME|PRICE_ATOMIC|PAY_TO)=' .env.deploy

# No --build: the image is unchanged, only the environment it runs with. Compose
# recreates the container by itself because the environment differs.
timeout 600 docker compose --env-file .env.deploy up -d 2>&1 | tail -3
