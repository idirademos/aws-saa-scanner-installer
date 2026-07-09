#!/usr/bin/env bash
# Usage: ./test-pcloud-auth.sh [-v|--verbose]
# Tests the CyberArk Identity service user by generating an OAuth2 access token
set -euo pipefail

# -----------------------------------------------------------------------------
# Parse arguments
# -----------------------------------------------------------------------------
VERBOSE=false
if [[ "${1:-}" == "-v" || "${1:-}" == "--verbose" ]]; then
  VERBOSE=true
fi

# -----------------------------------------------------------------------------
# Configuration - Set these variables or pass as environment variables
# -----------------------------------------------------------------------------
CYBERARK_TENANT_ID="${CYBERARK_TENANT_ID:-}"
CYBERARK_USERNAME="${CYBERARK_USERNAME:-}"
CYBERARK_PASSWORD="${CYBERARK_PASSWORD:-}"

# -----------------------------------------------------------------------------
# Validate required inputs
# -----------------------------------------------------------------------------
if [[ -z "${CYBERARK_TENANT_ID}" ]]; then
  echo "ERROR: CYBERARK_TENANT_ID must be set (e.g., 'AAA1234' for the Identity tenant ID)" >&2
  exit 1
fi

if [[ -z "${CYBERARK_USERNAME}" ]]; then
  echo "ERROR: CYBERARK_USERNAME must be set" >&2
  exit 1
fi

if [[ -z "${CYBERARK_PASSWORD}" ]]; then
  echo "ERROR: CYBERARK_PASSWORD must be set" >&2
  exit 1
fi

# -----------------------------------------------------------------------------
# Construct the identity service URL
# -----------------------------------------------------------------------------
IDENTITY_URL="https://${CYBERARK_TENANT_ID}.id.cyberark.cloud"
TOKEN_ENDPOINT="${IDENTITY_URL}/oauth2/platformtoken"

echo "============================================================"
echo "Testing CyberArk Identity Service User Authentication"
echo "============================================================"
echo "Tenant ID (Identity): ${CYBERARK_TENANT_ID}"
echo "Username:             ${CYBERARK_USERNAME}"
echo "Endpoint:             ${TOKEN_ENDPOINT}"
echo ""

# -----------------------------------------------------------------------------
# Request OAuth2 token using client credentials grant
# -----------------------------------------------------------------------------
echo "Requesting access token..."
echo "Client ID: ${CYBERARK_USERNAME}"
echo ""

# Build curl command
CURL_CMD="curl -w \"\n%{http_code}\" -X POST \"${TOKEN_ENDPOINT}\" \
  -H \"Content-Type: application/x-www-form-urlencoded\" \
  -d \"grant_type=client_credentials\" \
  -d \"client_id=${CYBERARK_USERNAME}\" \
  -d \"client_secret=REDACTED\""

if [[ "${VERBOSE}" == "true" ]]; then
  echo "Curl command (password redacted):"
  echo "${CURL_CMD}"
  echo ""
  CURL_VERBOSE="-v"
else
  CURL_VERBOSE="-s"
fi

# Disable exit-on-error temporarily for curl command
set +e
if [[ "${VERBOSE}" == "true" ]]; then
  # In verbose mode, show diagnostics but don't mix with response
  curl -v -w "\n%{http_code}" -X POST "${TOKEN_ENDPOINT}" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=client_credentials" \
    -d "client_id=${CYBERARK_USERNAME}" \
    -d "client_secret=${CYBERARK_PASSWORD}" 2>&1 >&2
  # Now get the actual response without verbose output
  RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "${TOKEN_ENDPOINT}" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=client_credentials" \
    -d "client_id=${CYBERARK_USERNAME}" \
    -d "client_secret=${CYBERARK_PASSWORD}")
  CURL_EXIT_CODE=$?
else
  RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "${TOKEN_ENDPOINT}" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=client_credentials" \
    -d "client_id=${CYBERARK_USERNAME}" \
    -d "client_secret=${CYBERARK_PASSWORD}")
  CURL_EXIT_CODE=$?
fi
set -e

if [[ ${CURL_EXIT_CODE} -ne 0 ]]; then
  echo "ERROR: curl command failed with exit code ${CURL_EXIT_CODE}" >&2
  echo "Response: ${RESPONSE}" >&2
  exit 1
fi

# Parse response - handle edge case where response is empty
if [[ -z "${RESPONSE}" ]]; then
  echo "ERROR: Empty response from server" >&2
  exit 1
fi

HTTP_CODE=$(echo "${RESPONSE}" | tail -n1)
BODY=$(echo "${RESPONSE}" | sed '$d')

# Validate HTTP code is numeric
if ! [[ "${HTTP_CODE}" =~ ^[0-9]+$ ]]; then
  echo "ERROR: Invalid HTTP status code: ${HTTP_CODE}" >&2
  echo "Full response:" >&2
  echo "${RESPONSE}" >&2
  exit 1
fi

echo "HTTP Status: ${HTTP_CODE}"
echo "Response Body:"
echo "${BODY}"
echo ""

# -----------------------------------------------------------------------------
# Parse and display results
# -----------------------------------------------------------------------------
if [[ "${HTTP_CODE}" == "200" ]]; then
  echo "✅ SUCCESS - Token generated successfully!"
  echo ""

  # Extract token details using jq if available, otherwise show raw response
  if command -v jq &> /dev/null; then
    ACCESS_TOKEN=$(echo "${BODY}" | jq -r '.access_token // empty')
    TOKEN_TYPE=$(echo "${BODY}" | jq -r '.token_type // empty')
    EXPIRES_IN=$(echo "${BODY}" | jq -r '.expires_in // empty')
    SCOPE=$(echo "${BODY}" | jq -r '.scope // empty')

    echo "Token Type:  ${TOKEN_TYPE}"
    echo "Expires In:  ${EXPIRES_IN} seconds"
    echo "Scope:       ${SCOPE}"
    echo ""
    echo "Access Token (truncated):"
    echo "${ACCESS_TOKEN:0:50}..."
    echo ""
    echo "Full Response:"
    echo "${BODY}" | jq '.'
  else
    echo "Response Body:"
    echo "${BODY}"
    echo ""
    echo "Note: Install 'jq' for formatted JSON output"
  fi

  echo ""
  echo "============================================================"
  echo "Service user is configured correctly!"
  echo "============================================================"
  exit 0
else
  echo "❌ FAILED - Token generation failed"
  echo ""
  echo "Response Body:"
  echo "${BODY}"
  echo ""

  # Try to extract error details if available
  if command -v jq &> /dev/null; then
    ERROR=$(echo "${BODY}" | jq -r '.error // empty')
    ERROR_DESC=$(echo "${BODY}" | jq -r '.error_description // empty')

    if [[ -n "${ERROR}" ]]; then
      echo "Error:       ${ERROR}"
    fi
    if [[ -n "${ERROR_DESC}" ]]; then
      echo "Description: ${ERROR_DESC}"
    fi
  fi

  exit 1
fi
