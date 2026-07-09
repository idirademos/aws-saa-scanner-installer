# pylint: disable=logging-fstring-interpolation, too-many-lines
"""
AWS Glue ETL Job for Bedrock Agent Discovery

This script performs comprehensive discovery of AWS Bedrock Agents across all regions
and uploads the results to CyberArk Discovery Service via S3 presigned URL.

Advantages over Lambda:
- No 15-minute timeout limit (can run for hours)
- More memory and compute resources available
- Better for large-scale discoveries
- Persistent logging to CloudWatch

Usage:
    Deploy as AWS Glue Python Shell job (Python 3.9)

Job Parameters:
    --secret_arn: AWS Secrets Manager secret ARN containing CyberArk credentials
    --aws_account_id: AWS Account ID
    --extra-py-files: S3 path to dependencies.zip containing required packages
"""

import os
# Priority fix: Ensure our packaged dependencies are loaded before system packages
# AWS Glue pre-installs boto3/botocore, but we need specific versions from dependencies.zip
import sys

# AWS Glue's behavior with --extra-py-files:
# 1. Downloads the zip file: s3://bucket/dependencies.zip → /tmp/glue-python-libs-XXX/dependencies.zip
# 2. Adds the temp directory to sys.path: /tmp/glue-python-libs-XXX
# 3. Does NOT extract the zip
#
# Why our extraction is necessary:
# - Python's zipimport can load .py code from zip files
# - But botocore needs to read data files (like endpoints.json) directly from filesystem
# - So we must extract the zip to make those data files accessible
DEPENDENCIES_ZIP_PATH = None
EXTRACT_DIR = None

for path in sys.path:  # pragma: no cover
    if os.path.isdir(path):
        # Check if dependencies.zip exists in this directory
        potential_zip = os.path.join(path, 'dependencies.zip')
        if os.path.exists(potential_zip):
            DEPENDENCIES_ZIP_PATH = potential_zip
            EXTRACT_DIR = os.path.join(path, 'dependencies_extracted')
            break

if DEPENDENCIES_ZIP_PATH and EXTRACT_DIR:  # pragma: no cover
    # Extract the zip if not already extracted
    if not os.path.exists(EXTRACT_DIR):
        import zipfile
        with zipfile.ZipFile(DEPENDENCIES_ZIP_PATH, 'r') as zip_ref:
            zip_ref.extractall(EXTRACT_DIR)

    # Add extracted directory to the front of sys.path
    sys.path.insert(0, EXTRACT_DIR)

import base64
import hashlib
import json
import logging
import time
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import quote

import boto3
import requests
# pylint: disable=import-error
from awsglue.utils import getResolvedOptions
from botocore.exceptions import ClientError

# Configure logging for Glue
# Glue requires specific logging setup to ensure logs appear in CloudWatch
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Create console handler with formatting
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# Suppress verbose boto3/botocore logging
logging.getLogger('boto3').setLevel(logging.WARNING)
logging.getLogger('botocore').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# Consts #

# Secret Fields
SECRET_USERNAME_KEY = 'username'
SECRET_PASSWORD_KEY = 'password'
SECRET_TENANT_NAME_KEY = 'tenant_name'

# Bedrock Client Names
BEDROCK_CLIENT_NAME = 'bedrock-agent'
BEDROCK_AGENTCORE_CLIENT_NAME = 'bedrock-agentcore-control'

AWS_REGIONS = [
    'us-east-1', 'us-east-2', 'us-west-1', 'us-west-2', 'af-south-1', 'ap-east-1', 'ap-east-2', 'ap-northeast-1', 'ap-northeast-2',
    'ap-northeast-3', 'ap-south-1', 'ap-south-2', 'ap-southeast-1', 'ap-southeast-2', 'ap-southeast-3', 'ap-southeast-4', 'ap-southeast-5',
    'ap-southeast-6', 'ap-southeast-7', 'eusc-de-east-1', 'us-gov-east-1', 'us-gov-west-1', 'ca-central-1', 'ca-west-1', 'eu-central-1',
    'eu-central-2', 'eu-north-1', 'eu-south-1', 'eu-south-2', 'eu-west-1', 'eu-west-2', 'eu-west-3', 'il-central-1', 'mx-central-1',
    'me-central-1', 'me-south-1', 'sa-east-1'
]

# Entity keys for organizing discoveries
STATUS_KEY = 'status'
ERROR_KEY = 'error'
DATA_KEY = 'data'
SUMMARY_KEY = 'summary'
DETAILS_KEY = 'details'
TAGS_KEY = 'tags'
ALIASES_KEY = 'aliases'
VERSIONS_KEY = 'versions'
ACTION_GROUPS_KEY = 'action_groups'

# Agent Version - controls SigV4 vs SigV2 behavior
# version >= v1.9.0 → SigV4 (requires file_size)
# version < v1.9.0 → SigV2 (no file_size needed)
# version == "development" → uses SigV4
AGENT_VERSION = os.environ.get('AGENT_VERSION', 'v1.9.0')
ENDPOINTS_KEY = 'endpoints'

# AWS boto3 client Keys
BEDROCK_AGENTS_KEY = 'agents'
BEDROCK_AGENTCORE_RUNTIMES_KEY = 'agent_runtimes'

# Rate limiter operation types
RATE_LIMITER_LIST = 'list'
RATE_LIMITER_GET = 'get'

# Conservative RPS limits (80% of AWS limits)
RATE_LIMITS = {
    RATE_LIMITER_LIST: 8,  # For all list_* operations
    RATE_LIMITER_GET: 12  # For all get_* operations
}


class DiscoveryStatus(str, Enum):
    PENDING = 'pending'
    SUCCESS = 'success'
    EMPTY = 'empty'
    ERROR = 'error'

    def __str__(self):
        return self.value


class DateTimeEncoder(json.JSONEncoder):
    """Custom JSON encoder that handles datetime objects"""

    def default(self, o):
        if isinstance(o, datetime):
            return o.isoformat()
        return super().default(o)


class CyberArkDiscoveryError(Exception):
    """Custom exception for CyberArk Discovery Service errors"""


class RateLimiter:
    """Token bucket rate limiter for API calls"""

    def __init__(self, operation: str, rps: int):
        self.operation = operation
        self.rps = rps
        self.min_interval = 1.0 / rps
        self.last_call = 0

    def wait(self):
        """Wait if necessary to respect rate limit"""
        now = time.time()
        elapsed = now - self.last_call
        if elapsed < self.min_interval:
            sleep_time = self.min_interval - elapsed
            time.sleep(sleep_time)
        self.last_call = time.time()


rate_limiters = {op: RateLimiter(op, rps) for op, rps in RATE_LIMITS.items()}


def get_secret(secret_arn: str) -> Dict[str, Any]:
    """
    Retrieve credentials and tenant info from AWS Secrets Manager

    Args:
        secret_name_or_arn: Secret name or full ARN

    Expected secret format:
    {
        "username": "service_account_username",
        "password": "service_account_password",
        "tenant_name": "customer_tenant_name"
    }
    """
    try:
        secrets_client = boto3.client('secretsmanager')
        logger.info('Retrieving secret')

        response = secrets_client.get_secret_value(SecretId=secret_arn)
        secret_data = json.loads(response['SecretString'])

        required_fields = [SECRET_USERNAME_KEY, SECRET_PASSWORD_KEY, SECRET_TENANT_NAME_KEY]

        if not all(field in secret_data for field in required_fields):
            raise CyberArkDiscoveryError('Missing required fields in secret')

        logger.info('Successfully retrieved secret')
        return secret_data

    except ClientError as e:
        logger.error(f'Failed to retrieve secret {secret_arn}: {e}')
        raise CyberArkDiscoveryError(f'Failed to retrieve secret: {e}') from e
    except json.JSONDecodeError as e:
        logger.error(f'Invalid JSON in secret {secret_arn}: {e}')
        raise CyberArkDiscoveryError(f'Invalid JSON in secret: {e}') from e
    except Exception as e:
        logger.error(f'An error occurred while retrieving secret {secret_arn}: {e}')
        raise CyberArkDiscoveryError(f'An error occurred while retrieving secret {secret_arn}: {e}') from e


def authenticate_cyberark(secret_data: Dict[str, str], identity_url: str, max_retries: int = 3) -> str:
    """
    Authenticate with CyberArk Identity IDP and obtain bearer token with retry mechanism

    Args:
        secret_data: Dictionary containing username and password
        identity_url: CyberArk Identity API URL
        max_retries: Maximum number of retry attempts (default: 3)

    Returns:
        Bearer token for API access
    """
    username = secret_data.get(SECRET_USERNAME_KEY)
    password = secret_data.get(SECRET_PASSWORD_KEY)

    # Construct the OAuth token endpoint URL
    token_url = f'{identity_url}/oauth2/platformtoken'
    userpass = f'{username}:{password}'.encode('utf-8')
    basic_auth = base64.b64encode(userpass).decode('utf-8')

    for attempt in range(1, max_retries + 1):
        try:
            if attempt == 1:
                logger.info('Authenticating with CyberArk Identity')
            else:
                logger.info(f'Retry attempt {attempt}/{max_retries} for CyberArk authentication')

            response = requests.post(token_url, data={'grant_type': 'client_credentials'}, headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'Authorization': f'Basic {basic_auth}',
            }, timeout=15)

            response.raise_for_status()
            token_data = response.json()
            bearer_token = token_data['access_token']

            logger.info('Successfully authenticated with CyberArk')
            return bearer_token

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            logger.error(f'Authentication error on attempt {attempt}/{max_retries}: {e}')
            if attempt >= max_retries:
                logger.error(f'Authentication failed after {max_retries} attempts due to error')
                raise CyberArkDiscoveryError(f'Authentication failed after {max_retries} error attempts: {e}') from e

            # Exponential backoff starting at 3 seconds: 3s, 6s, 12s, 24s, etc.
            backoff_time = 3 * (2**(attempt - 2))
            logger.info(f'Waiting {backoff_time} seconds before retry...')
            time.sleep(backoff_time)

        except Exception as e:
            logger.error(f'An error occurred while authenticating: {e}')
            raise CyberArkDiscoveryError(f'An error occurred while authenticating: {e}') from e

    raise CyberArkDiscoveryError(f'Authentication failed after {max_retries} attempts')  # pragma: no cover


def get_tenant_urls(tenant_name: str, max_retries: int = 3) -> Tuple[str, str]:
    """
    Get CyberArk Identity and Discovery URLs from CyberArk Platform Discovery Service API with retry mechanism

    Args:
        tenant_name: CyberArk tenant name
        max_retries: Maximum number of retry attempts (default: 3)

    Returns:
        Tuple of (identity_url, discovery_url)

    Raises:
        CyberArkDiscoveryError: If unable to retrieve URLs after retries
    """
    discovery_api_url = (f'https://platform-discovery.cyberark.cloud/api/public/tenant-discovery?'
                         f'bySubdomain={tenant_name}'
                         f'&selectedServices=identity_administration,discoverycontext')

    for attempt in range(1, max_retries + 1):
        try:
            if attempt == 1:
                logger.info('Retrieving CyberArk service URLs')
            else:
                logger.info(f'Retry attempt {attempt}/{max_retries} for retrieving CyberArk service URLs')

            response = requests.get(discovery_api_url, timeout=15)
            response.raise_for_status()
            response_json = response.json()

            # Extract required URLs
            identity_url, disco_url = None, None
            for service in response_json.get('services', []):
                service_name = service.get('service_name', '')
                service_url = service.get('endpoints')[0].get('api')
                if service_name == 'identity_administration':
                    logger.info(f'CyberArk Identity URL: {service.get("api", "")}')
                    identity_url = service_url
                elif service_name == 'discoverycontext':
                    disco_url = service_url

            if not identity_url:
                raise CyberArkDiscoveryError("Missing 'identity_administration.api' in response")

            if not disco_url:
                raise CyberArkDiscoveryError("Missing 'discoverycontext.api' in response")

            logger.info('Successfully retrieved CyberArk service URLs')

            return identity_url, disco_url

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            logger.error(f'Network error on attempt {attempt}/{max_retries}: {e}')
            if attempt >= max_retries:
                logger.error(f'Failed to retrieve CyberArk URLs after {max_retries} attempts')
                raise CyberArkDiscoveryError(f'Failed to retrieve CyberArk URLs after {max_retries} attempts: {e}') from e

            # Exponential backoff: 2s, 4s, 8s
            backoff_time = 2**attempt
            logger.info(f'Waiting {backoff_time} seconds before retry...')
            time.sleep(backoff_time)

        except Exception as e:
            logger.error(f'An error occurred while retrieving CyberArk URLs: {e}')
            raise CyberArkDiscoveryError(f'An error occurred while retrieving CyberArk URLs: {e}') from e

    raise CyberArkDiscoveryError(f'Failed to retrieve CyberArk URLs after {max_retries} attempts')  # pragma: no cover


def supports_sigv4(agent_version: str) -> bool:
    """
    Check if the agent version supports AWS SigV4

    Args:
        agent_version: Agent version string (e.g., 'v1.9.0', 'development')

    Returns:
        True if version supports SigV4, False for SigV2
    """
    if agent_version == 'development':
        return True

    try:
        version_str = agent_version.lstrip('v')
        version_parts = [int(x) for x in version_str.split('.')]

        if len(version_parts) >= 2:
            major, minor = version_parts[0], version_parts[1]
            return (major > 1) or (major == 1 and minor >= 9)

        return False
    except (ValueError, AttributeError):
        logger.warning(f'Unable to parse agent version: {agent_version}, defaulting to SigV2')
        return False


def calculate_checksum(data: str) -> Tuple[str, str]:
    """
    Calculate SHA256 checksum of JSON data in both hex and base64 formats

    Args:
        data: String data to checksum

    Returns:
        Tuple of (hex_checksum, base64_checksum)
    """
    try:
        sha256_hash = hashlib.sha256()
        data_bytes = data.encode('utf-8')
        sha256_hash.update(data_bytes)

        # Get checksum in both formats
        checksum_hex = sha256_hash.hexdigest()
        checksum_bytes = bytes.fromhex(checksum_hex)
        checksum_b64 = base64.b64encode(checksum_bytes).decode('utf-8')

        logger.info('Successfully calculated checksum')
        return checksum_hex, checksum_b64
    except Exception as e:
        logger.error(f'Error calculating checksum: {e}')
        raise CyberArkDiscoveryError(f'Error calculating checksum: {e}') from e


def decode_jwt_payload(token: str) -> Dict[str, Any]:
    """Decode JWT payload without verification to extract tenant_id and username"""
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return {}
        payload = parts[1]
        padded = payload + '=' * (4 - len(payload) % 4)
        decoded = base64.urlsafe_b64decode(padded)
        return json.loads(decoded)
    except Exception as e:
        logger.warning(f'Error decoding JWT: {e}')
        return {}


def build_tags_string(token: str, agent_version: str, identifier: str) -> str:
    """
    Build URI-encoded tags string for S3 upload (sorted alphabetically by key)

    Tags are constructed client-side from the JWT token payload and upload metadata.
    """
    token_payload = decode_jwt_payload(token)
    tenant_id = token_payload.get('tenant_id', '')
    username = token_payload.get('preferred_username', '')

    tags = {
        'agent_version': agent_version,
        'uploader_id': identifier,
        'vendor': 'aws',
        'upload_type': 'aws_snapshot',
        'tenant_id': tenant_id,
        'username': username
    }

    tags_string = '&'.join([f'{quote(k, safe="")}={quote(v, safe="")}' for k, v in sorted(tags.items())])
    logger.info(f'Built tags string: {tags_string}')
    return tags_string


def get_presigned_url(token: str, checksum: str, aws_account_id: str, disco_url: str, file_size: Optional[int] = None,
                      agent_version: str = AGENT_VERSION) -> str:
    """
    Get presigned URL from CyberArk Discovery Service API

    Args:
        token: CyberArk authentication token
        checksum: SHA256 checksum of the payload
        aws_account_id: AWS Account ID
        disco_url: CyberArk Discovery API URL
        file_size: Size of the file to be uploaded in bytes (required for SigV4)
        agent_version: Agent version string

    Returns:
        Presigned URL string
    """
    try:
        use_sigv4 = supports_sigv4(agent_version)
        logger.info(f'Requesting presigned URL from CyberArk Discovery Service (version: {agent_version}, SigV4: {use_sigv4})')

        if not disco_url:
            raise CyberArkDiscoveryError('Missing disco_url parameter')

        payload = {'agent_version': agent_version, 'identifier': aws_account_id, 'checksum_sha256': checksum}

        if use_sigv4:
            if file_size is None:
                raise CyberArkDiscoveryError('file_size is required for SigV4')
            payload['file_size'] = file_size

        api_url = f'{disco_url}/ingestions/aws/snapshot-links'

        headers = {
            'Authorization':
                f'Bearer {token}',
            'User-Agent':
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36'
        }

        response = requests.post(api_url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        response_json = response.json()

        if 'url' not in response_json:
            raise CyberArkDiscoveryError('No presigned URL in response')

        presigned_url = response_json['url']
        logger.info('Successfully obtained presigned URL')
        return presigned_url

    except Exception as e:
        logger.error(f'An error has occurred while getting presigned URL: {e}')
        raise CyberArkDiscoveryError(f'An error has occurred while getting presigned URL: {e}') from e


def upload_to_s3(presigned_url: str, payload: str, checksum_b64: str, use_sigv4: bool = False, tags_string: str = '') -> bool:
    """
    Upload discovery data to S3 using presigned URL

    Args:
        presigned_url: S3 presigned URL
        payload: JSON string to upload (already serialized)
        checksum_b64: Base64-encoded SHA256 checksum for S3 header
        use_sigv4: Whether to use SigV4 headers (encryption + tagging)
        tags_string: URI-encoded tags string (constructed client-side)

    Returns:
        True if upload successful, False otherwise
    """
    try:
        logger.info(f'Uploading discovery data to S3 (SigV4: {use_sigv4})')

        payload_bytes = payload.encode('utf-8')
        headers = {'x-amz-checksum-sha256': checksum_b64, 'Content-Length': str(len(payload_bytes))}

        if use_sigv4:
            headers['x-amz-server-side-encryption'] = 'AES256'
            headers['x-amz-tagging'] = tags_string

        response = requests.put(presigned_url, data=payload_bytes, headers=headers, timeout=60)
        response.raise_for_status()

        logger.info(f'Successfully uploaded {len(payload_bytes)} bytes to S3')
        return True

    except Exception as e:
        logger.error(f'Failed to upload to S3: {e}')

    return False


def upload_discovery_data(discovery_data: Dict[str, Any], token: str, aws_account_id: str, disco_url: str) -> bool:
    """
    Prepare and upload discovery data to CyberArk S3

    Args:
        discovery_data: Discovery data dictionary
        token: CyberArk authentication token
        aws_account_id: AWS Account ID
        disco_url: CyberArk Discovery API URL

    Returns:
        True if upload successful, False otherwise
    """
    payload_json: str = json.dumps(discovery_data, cls=DateTimeEncoder)
    payload_size: int = len(payload_json.encode('utf-8'))
    logger.info(f'Payload size: {payload_size:,} bytes ({payload_size/1024:.2f} KB)')

    checksum_hex, checksum_b64 = calculate_checksum(data=payload_json)

    use_sigv4 = supports_sigv4(AGENT_VERSION)

    presigned_url = get_presigned_url(token=token, checksum=checksum_hex, aws_account_id=aws_account_id, disco_url=disco_url,
                                      file_size=payload_size, agent_version=AGENT_VERSION)

    # Build tags client-side from JWT token (for SigV4)
    tags_string = build_tags_string(token=token, agent_version=AGENT_VERSION, identifier=aws_account_id) if use_sigv4 else ''

    upload_result: bool = upload_to_s3(presigned_url=presigned_url, payload=payload_json, checksum_b64=checksum_b64, use_sigv4=use_sigv4,
                                       tags_string=tags_string)

    return upload_result


def list_resources(client, operation_name: str, response_key: str, **kwargs) -> Union[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Generic method to list resources with pagination and rate limiting

    Handles both paginated list operations (returns list) and non-paginated operations (returns dict).

    Args:
        client: Boto3 client
        operation_name: Name of the list operation (e.g., 'list_agents', 'list_agent_aliases', 'list_tags_for_resource')
        response_key: Key in the response containing the resources (e.g., 'agentSummaries', 'tags')
        **kwargs: Additional parameters to pass to the operation

    Returns:
        List of resources (if paginated) or dict (if non-paginated like list_tags_for_resource)

    Raises:
        Exception: Re-raises any exception encountered during the operation
    """
    rate_limiters[RATE_LIMITER_LIST].wait()

    response_metadata_key = 'ResponseMetadata'
    # Check if the operation supports pagination
    if client.can_paginate(operation_name):
        # Use paginator for operations that support pagination
        paginator = client.get_paginator(operation_name)
        resources = []
        for page in paginator.paginate(**kwargs):
            page_data = page.get(response_key, [])
            # Handle both list and dict responses
            if isinstance(page_data, list):
                resources.extend(page_data)
            elif isinstance(page_data, dict):
                # For dict responses, return the dict directly
                page_data.pop(response_metadata_key, None)
                return page_data

        return resources
    # Operation doesn't support pagination (e.g., list_tags_for_resource)
    # Call the operation directly
    operation = getattr(client, operation_name)
    response = operation(**kwargs)
    result = response.get(response_key, {})
    result.pop(response_metadata_key, None)

    # Return the result as-is (could be list or dict)
    return result


def get_resource(client, operation_name: str, response_key: Optional[str], **kwargs) -> Optional[Dict[str, Any]]:
    """
    Generic method to get resource details with rate limiting

    Args:
        client: Boto3 client
        operation_name: Name of the get operation (e.g., 'get_agent')
        response_key: Key in the response containing the resource (e.g., 'agent')
        **kwargs: Parameters to pass to the operation

    Returns:
        Resource details dictionary, or None if error
    """
    try:
        rate_limiters[RATE_LIMITER_GET].wait()
        operation = getattr(client, operation_name)
        response = operation(**kwargs)
        response.pop('ResponseMetadata', None)
        # Extract the resource from response using the provided key
        # e.g., get_agent returns {'agent': {...}}
        return response.get(response_key, response)

    except Exception as e:
        logger.error(f'Error in {operation_name} with params {kwargs}: {e}')
        return None


def mask_environment_variables(runtime_details: Dict[str, Any]) -> Dict[str, Any]:
    """
    Mask sensitive environment variable values in agent runtime details

    Args:
        runtime_details: Dictionary containing agent runtime details

    Returns:
        Dictionary with environment variable values masked with '****'
    """
    if runtime_details and 'environmentVariables' in runtime_details:
        env_vars = runtime_details['environmentVariables']
        if env_vars and isinstance(env_vars, dict):
            # Create a new dict with masked values
            runtime_details['environmentVariables'] = {key: '****' for key in env_vars.keys()}
            logger.info(f'Masked {len(env_vars)} environment variables')

    return runtime_details


def discover_bedrock_agentcore_runtime_endpoint_details(client, runtime_id: str, endpoint_name: str,
                                                        endpoint_summary: Dict[str, Any]) -> Dict[str, Any]:
    """
    Discover complete details for a single agent runtime endpoint

    Args:
        client: Bedrock agentcore client
        runtime_id: Agent runtime ID
        endpoint_name: Agent runtime endpoint name
        endpoint_summary: Summary from list_agent_runtime_endpoints

    Returns:
        Dictionary containing all agent runtime endpoint information
    """
    endpoint_data = {SUMMARY_KEY: endpoint_summary, DETAILS_KEY: None, TAGS_KEY: {}}

    # Get full endpoint details (requires both runtime_id and endpoint_name)
    endpoint_details = get_resource(client=client, operation_name='get_agent_runtime_endpoint', response_key='agentRuntimeEndpoint',
                                    agentRuntimeId=runtime_id, endpointName=endpoint_name)

    if not endpoint_details:
        logger.warning(f'Failed to retrieve details for endpoint {endpoint_name} of runtime {runtime_id}')
    else:
        endpoint_data[DETAILS_KEY] = endpoint_details

        # Get tags using ARN from endpoint details
        endpoint_arn = endpoint_details.get('agentRuntimeEndpointArn')
        if endpoint_arn:
            # list_tags_for_resource returns a dict, handled by list_resources
            endpoint_data[TAGS_KEY] = list_resources(client=client, operation_name='list_tags_for_resource', response_key='tags',
                                                     resourceArn=endpoint_arn)

    return endpoint_data


# pylint: disable=too-many-locals
def discover_region_agentcore_runtimes(client, region: str) -> Dict[str, Any]:
    """
    Discover all agent runtimes in a region with full details

    Args:
        client: Bedrock agentcore client
        region: AWS region name

    Returns:
        Dictionary containing region status and all discovered agent runtimes
    """
    region_data: Dict[str, Any] = {STATUS_KEY: DiscoveryStatus.PENDING, ERROR_KEY: None, DATA_KEY: {}}

    try:
        # List all agent runtimes in the region
        runtime_summaries: List[Dict[str, Any]] = list_resources(client=client, operation_name='list_agent_runtimes',
                                                                 response_key='agentRuntimes', maxResults=100)

        total_versions = 0
        total_endpoints = 0

        # For each runtime, discover versions and endpoints
        for runtime_summary in runtime_summaries:
            runtime_id = runtime_summary.get('agentRuntimeId')

            runtime_data = {SUMMARY_KEY: runtime_summary, VERSIONS_KEY: {}, ENDPOINTS_KEY: {}}

            # Step 1: List all versions for this runtime
            version_summaries = list_resources(client=client, operation_name='list_agent_runtime_versions', response_key='agentRuntimes',
                                               agentRuntimeId=runtime_id, maxResults=100)

            # Step 2: For each version, get runtime details (requires runtime_id + version)
            for version_summary in version_summaries:
                version = version_summary.get('agentRuntimeVersion')
                if not version:
                    logger.warning(f'Failed to retrieve version for runtime {runtime_id}, continuing with summary data only')
                    continue

                runtime_details: Dict[str, Any] = get_resource(
                    client=client,
                    operation_name='get_agent_runtime',
                    response_key=None,  # get_agent_runtime returns a dict without a response key
                    agentRuntimeId=runtime_id,
                    agentRuntimeVersion=version)

                if not runtime_details:
                    logger.warning(
                        f'Failed to retrieve details for agent runtime {runtime_id} version {version}, continuing with summary data only')
                    continue

                # Mask sensitive environment variables if present
                runtime_details = mask_environment_variables(runtime_details=runtime_details)

                runtime_data['versions'][version] = {SUMMARY_KEY: version_summary, DETAILS_KEY: runtime_details}
                total_versions += 1

            # Step 3: List all endpoints for this runtime
            endpoint_summaries = list_resources(client=client, operation_name='list_agent_runtime_endpoints',
                                                response_key='runtimeEndpoints', agentRuntimeId=runtime_id, maxResults=100)

            # Step 4: For each endpoint, get endpoint details (requires runtime_id + endpoint_id)
            for endpoint_summary in endpoint_summaries:
                endpoint_id = endpoint_summary.get('name', endpoint_summary.get('id'))
                runtime_data['endpoints'][endpoint_id] = discover_bedrock_agentcore_runtime_endpoint_details(
                    client=client, runtime_id=runtime_id, endpoint_name=endpoint_id, endpoint_summary=endpoint_summary)
                total_endpoints += 1

            region_data[DATA_KEY][runtime_id] = runtime_data

        # Update status
        if runtime_summaries and len(runtime_summaries) > 0:
            region_data[STATUS_KEY] = DiscoveryStatus.SUCCESS
            logger.info(f'  {region}: Discovered {len(runtime_summaries)} runtimes, {total_versions} versions, {total_endpoints} endpoints')
        else:
            region_data[STATUS_KEY] = DiscoveryStatus.EMPTY
            logger.info(f'  {region}: No agent runtimes found')

    except Exception as e:
        region_data[STATUS_KEY] = DiscoveryStatus.ERROR
        region_data[ERROR_KEY] = str(e)
        logger.error(f'  {region}: Error during discovery - {e}')

    return region_data


def discover_bedrock_agent_details(client, agent_id: str, agent_summary: Dict[str, Any]) -> Dict[str, Any]:
    """
    Discover complete details for a single agent

    Args:
        client: Bedrock agent client
        agent_id: Agent ID
        agent_summary: Summary from list_agents

    Returns:
        Dictionary containing all agent information
    """
    agent_data = {SUMMARY_KEY: agent_summary, DETAILS_KEY: None, TAGS_KEY: {}, ALIASES_KEY: {}, VERSIONS_KEY: {}}

    # Get full agent details
    agent_details: Dict[str, Any] = get_resource(client=client, operation_name='get_agent', response_key='agent', agentId=agent_id)

    if not agent_details:
        logger.warning(f'Failed to retrieve details for agent {agent_id}, continuing with summary data only')
    else:
        agent_data[DETAILS_KEY] = agent_details

        # Get tags using ARN from details
        agent_arn = agent_details.get('agentArn')
        if not agent_arn:
            logger.warning(f'Failed to retrieve ARN for agent {agent_id}, continuing with summary data only')
        else:
            # list_tags_for_resource returns a dict, handled by list_resources
            agent_data[TAGS_KEY] = list_resources(client=client, operation_name='list_tags_for_resource', response_key='tags',
                                                  resourceArn=agent_arn)

    # Discover aliases
    alias_summaries = list_resources(client=client, operation_name='list_agent_aliases', response_key='agentAliasSummaries',
                                     agentId=agent_id, maxResults=1000)

    for alias_summary in alias_summaries:
        alias_id = alias_summary['agentAliasId']
        agent_data[ALIASES_KEY][alias_id] = alias_summary

    # Discover versions (includes action groups)
    version_summaries = list_resources(client=client, operation_name='list_agent_versions', response_key='agentVersionSummaries',
                                       agentId=agent_id, maxResults=1000)

    for version_summary in version_summaries:
        version = version_summary['agentVersion']
        agent_data[VERSIONS_KEY][version] = {SUMMARY_KEY: version_summary, ACTION_GROUPS_KEY: {}}

        # List action groups for this version
        action_group_summaries = list_resources(client=client, operation_name='list_agent_action_groups',
                                                response_key='actionGroupSummaries', agentId=agent_id, agentVersion=version,
                                                maxResults=1000)

        for ag_summary in action_group_summaries:
            action_group_id = ag_summary['actionGroupId']
            agent_data[VERSIONS_KEY][version][ACTION_GROUPS_KEY][action_group_id] = {SUMMARY_KEY: ag_summary}

    return agent_data


def discover_region_bedrock_agents(client, region: str) -> Dict[str, Any]:
    """
    Discover all agents in a region with full details

    Args:
        client: Bedrock agent client
        region: AWS region name

    Returns:
        Dictionary containing region status and all discovered agents
    """
    region_data: Dict[str, Any] = {STATUS_KEY: DiscoveryStatus.PENDING, ERROR_KEY: None, DATA_KEY: {}}

    try:
        # List all agents in the region
        agent_summaries: List[Dict[str, Any]] = list_resources(client=client, operation_name='list_agents', response_key='agentSummaries',
                                                               maxResults=1000)

        # Discover details for each agent
        for agent_summary in agent_summaries:
            agent_id = agent_summary['agentId']
            region_data[DATA_KEY][agent_id] = discover_bedrock_agent_details(client=client, agent_id=agent_id, agent_summary=agent_summary)

        # Update status
        if agent_summaries and len(agent_summaries) > 0:
            region_data[STATUS_KEY] = DiscoveryStatus.SUCCESS
            logger.info(f'  {region}: Discovered {len(agent_summaries)} agents with full details')
        else:
            region_data[STATUS_KEY] = DiscoveryStatus.EMPTY
            logger.info(f'  {region}: No agents found')

    except Exception as e:
        region_data[STATUS_KEY] = DiscoveryStatus.ERROR
        region_data[ERROR_KEY] = str(e)
        logger.error(f'  {region}: Error during discovery - {e}')

    return region_data


def log_discovery_summary(discovery_data: Dict[str, Any]) -> None:
    """
    Log regional summary statistics

    Expected structure: {discovery_type: {region: {entity_type: {status, error, data}}}}

    Args:
        discovery_data: Discovery data dictionary with nested structure containing regional results
    """
    logger.info('Discovery Summary:')
    # Process each discovery type using the keys from discovery_data
    for discovery_type in discovery_data.keys():
        type_data = discovery_data[discovery_type]

        logger.info('')
        logger.info(f'--- {discovery_type.upper()} ---')

        # Collect status information from all entity types in each region
        region_statuses = {}
        for region, region_data in type_data.items():
            # Get status from any entity type in the region
            for entity_data in region_data.values():
                if isinstance(entity_data, dict) and STATUS_KEY in entity_data:
                    region_statuses[region] = entity_data.get(STATUS_KEY)
                    break

        total_regions = len(region_statuses)
        non_empty_regions = sum(1 for status in region_statuses.values() if status == DiscoveryStatus.SUCCESS)
        empty_regions = sum(1 for status in region_statuses.values() if status == DiscoveryStatus.EMPTY)
        regions_error = sum(1 for status in region_statuses.values() if status == DiscoveryStatus.ERROR)

        logger.info(f'  - Regions scanned: {total_regions}')
        logger.info(f'  - Non-empty regions: {non_empty_regions}')
        logger.info(f'  - Empty regions: {empty_regions}')
        logger.info(f'  - Regions with errors: {regions_error}')


def log_bedrock_discovery(discovery_data: Dict[str, Any]) -> None:
    """
    Log detailed discovery results and statistics

    Args:
        discovery_data: Discovery data dictionary with structure: {region: {entity_type: {status, error, data}}}
    """

    # Count total resources discovered
    total_agents = sum(
        len(region_data[BEDROCK_AGENTS_KEY][DATA_KEY]) for region_data in discovery_data.values() if BEDROCK_AGENTS_KEY in region_data)

    agents_with_details = sum(1 for region_data in discovery_data.values() if BEDROCK_AGENTS_KEY in region_data
                              for agent_data in region_data[BEDROCK_AGENTS_KEY][DATA_KEY].values() if agent_data.get('details') is not None)

    total_versions = sum(
        len(agent_data['versions']) for region_data in discovery_data.values() if BEDROCK_AGENTS_KEY in region_data
        for agent_data in region_data[BEDROCK_AGENTS_KEY][DATA_KEY].values())

    total_action_groups = sum(
        len(version_data['action_groups']) for region_data in discovery_data.values() if BEDROCK_AGENTS_KEY in region_data
        for agent_data in region_data[BEDROCK_AGENTS_KEY][DATA_KEY].values()
        for version_data in agent_data['versions'].values())

    logger.info('Discovery Results:')
    logger.info(f'  - Total agents: {total_agents}')
    logger.info(f'  - Agents with full details: {agents_with_details}/{total_agents}')
    logger.info(f'  - Total versions: {total_versions}')
    logger.info(f'  - Total action groups: {total_action_groups}')

    # Final summary
    logger.info('BEDROCK DISCOVERY COMPLETE')


def log_bedrock_agentcore_discovery(discovery_data: Dict[str, Any]) -> None:
    """
    Log detailed discovery results and statistics for agentcore

    Args:
        discovery_data: Discovery data dictionary with structure: {region: {entity_type: {status, error, data}}}
    """

    # Count total resources discovered
    total_runtimes = sum(
        len(region_data[BEDROCK_AGENTCORE_RUNTIMES_KEY][DATA_KEY])
        for region_data in discovery_data.values()
        if BEDROCK_AGENTCORE_RUNTIMES_KEY in region_data)

    total_versions = sum(
        len(runtime_data['versions']) for region_data in discovery_data.values() if BEDROCK_AGENTCORE_RUNTIMES_KEY in region_data
        for runtime_data in region_data[BEDROCK_AGENTCORE_RUNTIMES_KEY][DATA_KEY].values())

    versions_with_details = sum(1 for region_data in discovery_data.values() if BEDROCK_AGENTCORE_RUNTIMES_KEY in region_data
                                for runtime_data in region_data[BEDROCK_AGENTCORE_RUNTIMES_KEY][DATA_KEY].values()
                                for version_data in runtime_data['versions'].values() if version_data.get('details') is not None)

    total_endpoints = sum(
        len(runtime_data['endpoints']) for region_data in discovery_data.values() if BEDROCK_AGENTCORE_RUNTIMES_KEY in region_data
        for runtime_data in region_data[BEDROCK_AGENTCORE_RUNTIMES_KEY][DATA_KEY].values())

    endpoints_with_details = sum(1 for region_data in discovery_data.values() if BEDROCK_AGENTCORE_RUNTIMES_KEY in region_data
                                 for runtime_data in region_data[BEDROCK_AGENTCORE_RUNTIMES_KEY][DATA_KEY].values()
                                 for endpoint_data in runtime_data['endpoints'].values() if endpoint_data.get('details') is not None)

    logger.info('Discovery Results:')
    logger.info(f'  - Total agent runtimes: {total_runtimes}')
    logger.info(f'  - Total versions: {total_versions}')
    logger.info(f'  - Versions with full details: {versions_with_details}/{total_versions}')
    logger.info(f'  - Total endpoints: {total_endpoints}')
    logger.info(f'  - Endpoints with full details: {endpoints_with_details}/{total_endpoints}')

    # Final summary
    logger.info('BEDROCK AGENTCORE DISCOVERY COMPLETE')


def run_bedrock_agentcore_discovery() -> Dict[str, Any]:
    """
    Single-phase discovery: Comprehensive agent runtime discovery with immediate enrichment

    Discovery process per region:
      1. List all agent runtimes (list_agent_runtimes)
      2. For each agent runtime:
         - List agent runtime versions (list_agent_runtime_versions)
           - For each version: Get agent runtime details (get_agent_runtime with runtime_id + version)
         - List agent runtime endpoints (list_agent_runtime_endpoints)
           - For each endpoint: Get endpoint details and tags (get_agent_runtime_endpoint + list_tags_for_resource)

    Returns:
        Discovery data dictionary with structure:
        {
            region: {
                status: 'success'|'empty'|'error',
                error: str|None,
                agent_runtimes: {
                    runtime_id: {
                        summary: {...},
                        versions: {
                            version: {
                                summary: {...},
                                details: {...}|None
                            }
                        },
                        endpoints: {
                            endpoint_name: {
                                summary: {...},
                                details: {...}|None,
                                tags: {...}
                            }
                        }
                    }
                }
            }
        }
    """
    bedrock_agentcore_runtimes_key = 'agent_runtimes'
    start_time = time.time()
    logger.info('Starting AWS Bedrock AgentCore Discovery')
    logger.info(f'  - Bedrock AgentCore regions: {len(AWS_REGIONS)}')

    # Data structure: {region: {status, error, agent_runtimes: {runtime_id: {summary, details, versions, endpoints}}}}
    discovery_data: Dict[str, Any] = {}

    # ==================== DISCOVERY: Complete AgentCore Runtime Catalog ====================
    for region in AWS_REGIONS:
        logger.info(f'Processing region: {region}')
        client = boto3.client(BEDROCK_AGENTCORE_CLIENT_NAME, region_name=region)

        # Initialize region if not exists and add entity data
        discovery_data.setdefault(region, {})
        # Discover all agent runtimes in this region using the helper method
        discovery_data[region][bedrock_agentcore_runtimes_key] = discover_region_agentcore_runtimes(client=client, region=region)

    # Log discovery results
    elapsed_time = time.time() - start_time
    logger.info(f'Bedrock AgentCore Discovery complete in {elapsed_time:.1f}s')
    log_bedrock_agentcore_discovery(discovery_data=discovery_data)

    return discovery_data


def run_bedrock_discovery() -> Dict[str, Any]:
    """
    Single-phase discovery: Comprehensive Bedrock Agent discovery with immediate enrichment

    For each region:
      1. List all agents (list_agents)
      2. For each agent:
         - Get full agent details (get_agent)
         - List agent tags (list_tags_for_resource)
         - List agent aliases (list_agent_aliases)
         - List agent versions (list_agent_versions)
         - For each version, list action groups (list_agent_action_groups)

    Returns:
        Discovery data dictionary with structure:
        {
            region: {
                status: 'success'|'empty'|'error',
                error: str|None,
                agents: {
                    agent_id: {
                        summary: {...},
                        details: {...}|None,
                        tags: {...},
                        aliases: {...},
                        versions: {...}
                    }
                }
            }
        }
    """
    bedrock_agents_key = 'agents'
    start_time = time.time()
    logger.info('Starting AWS Bedrock Discovery')
    logger.info(f'  - Bedrock regions: {len(AWS_REGIONS)}')

    # Data structure: {region: {status, error, agents: {agent_id: {summary, details, aliases, versions}}}}
    discovery_data: Dict[str, Any] = {}

    # ==================== DISCOVERY: Complete Agent Catalog ====================
    for region in AWS_REGIONS:
        logger.info(f'Processing region: {region}')
        client = boto3.client(BEDROCK_CLIENT_NAME, region_name=region)

        # Initialize region if not exists and add entity data
        discovery_data.setdefault(region, {})
        # Discover all agents in this region using the new helper method
        discovery_data[region][bedrock_agents_key] = discover_region_bedrock_agents(client=client, region=region)

    # Log discovery results
    elapsed_time = time.time() - start_time
    logger.info(f'Bedrock Discovery complete in {elapsed_time:.1f}s')
    log_bedrock_discovery(discovery_data=discovery_data)

    return discovery_data


def get_job_parameters() -> Tuple[str, str]:
    """
    Get and validate all AWS Glue job parameters upfront (fail-fast)

    Returns:
        Tuple of (secret_arn, aws_account_id)

    Raises:
        SystemExit: If required parameters are missing
    """
    secret_arn_key = 'secret_arn'
    aws_account_id_key = 'aws_account_id'

    logger.info('Loading job parameters')
    args = getResolvedOptions(sys.argv, [secret_arn_key, aws_account_id_key])

    secret_arn: str = args[secret_arn_key]
    aws_account_id: str = args[aws_account_id_key]

    logger.info('Job parameters successfully loaded')

    return secret_arn, aws_account_id


def main():
    """
    Main Glue job entry point
    """
    start_time = time.time()

    try:
        logger.info('Starting CyberArk AWS Discovery')

        # Get job parameters (fail-fast if missing)
        secret_arn, aws_account_id = get_job_parameters()

        # Step 1: Retrieve credentials from AWS Secrets Manager
        secret_data: Dict[str, Any] = get_secret(secret_arn=secret_arn)

        # Step 2: Get CyberArk service URLs from Platform Discovery Service
        tenant_name: str = secret_data.get(SECRET_TENANT_NAME_KEY)
        identity_url, disco_url = get_tenant_urls(tenant_name=tenant_name)

        # Step 3: Validate credentials by authenticating (fail fast if credentials are invalid)
        authenticate_cyberark(secret_data=secret_data, identity_url=identity_url)

        # Step 4: Run discoveries
        bedrock_data = run_bedrock_discovery()
        bedrock_agentcore_data = run_bedrock_agentcore_discovery()

        # Combine discoveries into structured format
        discovery_data = {'bedrock': bedrock_data, 'bedrock-agentcore': bedrock_agentcore_data}

        # Log regional summaries for both discoveries
        log_discovery_summary(discovery_data=discovery_data)

        # Step 5: Re-authenticate with CyberArk Identity (get fresh token for upload)
        bearer_token: str = authenticate_cyberark(secret_data=secret_data, identity_url=identity_url)

        # Step 6: Upload discovery data to CyberArk
        upload_success: bool = upload_discovery_data(discovery_data=discovery_data, token=bearer_token, aws_account_id=aws_account_id,
                                                     disco_url=disco_url)

        elapsed_time = time.time() - start_time

        if upload_success:
            logger.info(f'Discovery completed successfully in {elapsed_time:.2f}s')
            logger.info('JOB COMPLETED SUCCESSFULLY')
            return 0
        raise CyberArkDiscoveryError('Failed to upload data to S3')

    except Exception as e:
        elapsed_time = time.time() - start_time
        logger.error(f'An error occurred after {elapsed_time:.2f}s: {e}', exc_info=True)
        logger.error('JOB FAILED')
        return 1


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main())
