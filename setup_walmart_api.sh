#!/usr/bin/env bash
# One-time Walmart Affiliate API (walmart.io) setup.
#  1. Generates an RSA key pair in a private temp folder.
#  2. Stores the PRIVATE key as GitHub secret WALMART_PRIVATE_KEY, then deletes it locally.
#  3. Prints the PUBLIC key for you to paste into walmart.io.
#  4. Asks for the Consumer ID walmart.io gives you and stores it as a secret.
set -euo pipefail
REPO="${1:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
TMP=$(mktemp -d); chmod 700 "$TMP"; trap 'rm -rf "$TMP"' EXIT
umask 077
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$TMP/priv.pem" 2>/dev/null
openssl rsa -in "$TMP/priv.pem" -pubout -out "$TMP/pub.pem" 2>/dev/null
gh secret set WALMART_PRIVATE_KEY -R "$REPO" < "$TMP/priv.pem"
echo "Private key saved as GitHub secret WALMART_PRIVATE_KEY on $REPO (local copy will be deleted)."
echo
echo "================ PUBLIC KEY: paste this single line into walmart.io ================"
grep -v -- '-----' "$TMP/pub.pem" | tr -d '\n'; echo
echo "===================================================================================="
command -v pbcopy >/dev/null && grep -v -- '-----' "$TMP/pub.pem" | tr -d '\n' | pbcopy && echo "(also copied to your clipboard)"
echo
echo "In walmart.io: Login > My Account > Applications > create an app (Affiliate API),"
echo "upload the public key above, then copy the Consumer ID it shows."
read -r -p "Consumer ID (looks like 1de0df0a-9de1-47b5-bb45-2dc8f26c5ab2): " CID
[[ "$CID" =~ ^[0-9a-fA-F-]{36}$ ]] || { echo "That doesn't look like a Consumer ID. Re-run the script."; exit 1; }
read -r -p "Key version shown by walmart.io [1]: " KV; KV="${KV:-1}"
printf '%s' "$CID" | gh secret set WALMART_CONSUMER_ID -R "$REPO"
printf '%s' "$KV"  | gh secret set WALMART_KEY_VERSION -R "$REPO"
echo "Saved WALMART_CONSUMER_ID and WALMART_KEY_VERSION. Done."
