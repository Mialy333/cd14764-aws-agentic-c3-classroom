"""
NovaMart front-end proxy (stand-out 7.4) - AWS Lambda behind a Function URL.

Flow:  browser (Cognito sign-in) --Bearer access token--> this Lambda --SigV4--> AgentCore Runtime

Why a Lambda proxy: API Gateway stops waiting after 30 s, while one multi-agent request takes
20 to 50 s; a Lambda Function URL waits up to the function timeout. The runtime keeps its IAM
(SigV4) authorization, so the course tests and the CLI keep working unchanged.

Security:
  - The access token is validated server-side by Cognito (GetUser). The issuer (user pool) and
    the app client are then checked from the token claims.
  - The customer_id comes ONLY from the user's Cognito attribute custom:customer_id, never from
    the request body: a user cannot act as another customer by editing the page.

Environment variables: RUNTIME_ARN, USER_POOL_ID, APP_CLIENT_ID (AWS_REGION is set by Lambda).
"""
import base64
import json
import os
import re
import uuid

import boto3
from botocore.exceptions import ClientError, ParamValidationError

REGION = os.environ.get('AWS_REGION', 'us-east-1')
RUNTIME_ARN = os.environ['RUNTIME_ARN']
USER_POOL_ID = os.environ['USER_POOL_ID']
APP_CLIENT_ID = os.environ['APP_CLIENT_ID']
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"

cognito = boto3.client('cognito-idp', region_name=REGION)
agentcore = boto3.client('bedrock-agentcore', region_name=REGION)


def _response(status: int, body: dict) -> dict:
    """
    Build a Function URL response (CORS headers are added by the Function URL configuration).

    Args:
        status: HTTP status code
        body:   JSON-serializable body

    Returns:
        Lambda Function URL response dict
    """
    return {'statusCode': status, 'headers': {'Content-Type': 'application/json'},
            'body': json.dumps(body)}


def _claims(token: str) -> dict:
    """
    Decode the payload of a JWT WITHOUT checking its signature.

    Only called after Cognito GetUser has accepted the token, so the signature and expiry
    are already validated server-side.

    Args:
        token: Cognito access token

    Returns:
        The token claims
    """
    payload = token.split('.')[1]
    return json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))


def _authenticated_customer(event: dict) -> tuple:
    """
    Validate the bearer token and return the customer bound to the signed-in user.

    Args:
        event: Lambda Function URL event

    Returns:
        Tuple (customer_id, user display name)

    Raises:
        PermissionError: missing, invalid or foreign token, or user without customer_id
    """
    headers = {k.lower(): v for k, v in (event.get('headers') or {}).items()}
    auth = headers.get('authorization', '')
    if not auth.lower().startswith('bearer '):
        raise PermissionError('Missing bearer token')
    token = auth[7:].strip()
    try:
        user = cognito.get_user(AccessToken=token)       # validates signature, expiry, revocation
    except (ClientError, ParamValidationError) as exc:     # malformed, expired or revoked token
        raise PermissionError('Invalid or expired token') from exc
    try:
        claims = _claims(token)
    except (IndexError, ValueError) as exc:
        raise PermissionError('Invalid token') from exc
    if claims.get('iss') != ISSUER or claims.get('client_id') != APP_CLIENT_ID:
        raise PermissionError('Token was not issued for this application')
    attributes = {a['Name']: a['Value'] for a in user['UserAttributes']}
    customer_id = attributes.get('custom:customer_id')
    if not customer_id:
        raise PermissionError('No customer account is linked to this user')
    return customer_id, attributes.get('name') or attributes.get('email', '')


def lambda_handler(event, context):
    """
    Forward one chat message of the signed-in customer to the AgentCore Runtime.

    Request body:  {"message": "<text>", "session_id": "<32 hex chars, optional>"}
    Response body: {"reply": "...", "customer_id": "...", "session_id": "...", "trace_id": "..."}

    Args:
        event:   Lambda Function URL event
        context: Lambda context

    Returns:
        HTTP response: 200 with the reply, 400 bad request, 401 unauthenticated, 502 runtime error
    """
    if event.get('requestContext', {}).get('http', {}).get('method') == 'OPTIONS':
        return _response(204, {})
    try:
        customer_id, name = _authenticated_customer(event)
    except PermissionError as exc:
        return _response(401, {'error': str(exc)})

    try:
        body = json.loads(event.get('body') or '{}')
        if event.get('isBase64Encoded'):
            body = json.loads(base64.b64decode(event['body']))
    except ValueError:
        return _response(400, {'error': 'Body must be JSON'})
    message = (body.get('message') or '').strip()
    if not message or len(message) > 2000:
        return _response(400, {'error': 'Message must be between 1 and 2000 characters'})
    session_id = body.get('session_id') or uuid.uuid4().hex
    if not re.fullmatch(r'[0-9a-f]{32}', session_id):
        return _response(400, {'error': 'Invalid session_id'})
    # Any customer_id sent by the browser is ignored on purpose: identity comes from the token.

    try:
        result = agentcore.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            runtimeSessionId=f"novamart-web-{session_id}",    # >= 33 characters, stable per chat
            contentType='application/json',
            accept='application/json',
            payload=json.dumps({'prompt': message, 'customer_id': customer_id,
                                'session_id': session_id[:8]}),
        )
        raw = result['response'].read()
        data = json.loads(raw) if raw else {}
    except Exception as exc:
        print(f"Runtime invocation failed: {exc}")
        return _response(502, {'error': 'The support agents are unavailable. Please try again.'})

    return _response(200, {
        'reply':       data.get('result', ''),
        'customer_id': customer_id,
        'name':        name,
        'session_id':  session_id,
        'trace_id':    data.get('trace_id'),
    })
