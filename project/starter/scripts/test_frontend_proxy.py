"""
End-to-end test of the web front-end path (stand-out 7.4):
Cognito sign-in -> proxy Lambda (Function URL) -> AgentCore Runtime -> agents.

Checks:
  1. No token                         -> 401
  2. Forged token                     -> 401
  3. Alice (CUST-001), and the body tries to impersonate CUST-002 -> answered as CUST-001
  4. Bob (CUST-002) asks about Alice's order ORD-27176          -> no access to it
  5. Bob asks for CUST-001's e-mail and orders                  -> refused

Usage (from project/starter, after setup_cognito.py and deploy_frontend_proxy.py):
    python scripts/test_frontend_proxy.py 2>&1 | tee evidence/standout/frontend-e2e-test.txt
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
import boto3   # noqa: E402

COGNITO = json.load(open(os.path.join(ROOT, 'frontend', 'cognito.json')))
API_URL = re.search(r'"apiUrl":\s*"([^"]+)"', open(os.path.join(ROOT, 'frontend', 'config.js')).read()).group(1)
USERS = {line.split()[0]: line.split()[1]
         for line in open(os.path.expanduser('~/.novamart-demo-users')) if line.strip()}
cognito = boto3.client('cognito-idp', region_name=COGNITO['region'])


def sign_in(email: str) -> str:
    """
    Sign a demo user in with USER_PASSWORD_AUTH, as the web page does.

    Args:
        email: Demo user e-mail

    Returns:
        The Cognito access token
    """
    username = next(u for u in USERS if u.replace('+web@', '@') == email)   # alias-tolerant
    result = cognito.initiate_auth(ClientId=COGNITO['clientId'], AuthFlow='USER_PASSWORD_AUTH',
                                   AuthParameters={'USERNAME': username, 'PASSWORD': USERS[username]})
    return result['AuthenticationResult']['AccessToken']


def call(message: str, token: str = None, extra: dict = None) -> tuple:
    """
    POST one chat message to the proxy, as the web page does.

    Args:
        message: Customer message
        token:   Access token (None to test the unauthenticated case)
        extra:   Extra body fields (used to try to impersonate another customer)

    Returns:
        Tuple (HTTP status, JSON body, elapsed seconds)
    """
    body = {'message': message, 'session_id': uuid.uuid4().hex, **(extra or {})}
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f"Bearer {token}"
    request = urllib.request.Request(API_URL, data=json.dumps(body).encode(), headers=headers, method='POST')
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=150) as response:
            return response.status, json.loads(response.read()), time.time() - started
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or '{}'), time.time() - started


def report(n: int, title: str, status: int, body: dict, elapsed: float) -> None:
    """Print one test result."""
    print(f"\n[{n}] {title}\n    HTTP {status} in {elapsed:.1f}s | customer_id={body.get('customer_id', '-')}")
    text = body.get('reply') or body.get('error', '')
    print('    ' + text[:500].replace('\n', '\n    ') + ('...' if len(text) > 500 else ''))


def main() -> None:
    """Run the five checks."""
    print(f"Proxy: {API_URL}")
    report(1, 'No token', *call('What is the status of my order ORD-27176?'))
    report(2, 'Forged token', *call('What is the status of my order ORD-27176?', token='abc.def.ghi'))
    alice, bob = sign_in('alice@example.com'), sign_in('bob@example.com')
    report(3, 'Alice signed in, body claims customer_id CUST-002',
           *call('What is the status of my order ORD-27176?', alice, {'customer_id': 'CUST-002'}))
    report(4, "Bob signed in, asks about Alice's order ORD-27176",
           *call('What is the status of my order ORD-27176?', bob))
    report(5, "Bob signed in, asks for CUST-001's e-mail and orders",
           *call('What is the email address and order history of customer CUST-001?', bob))


if __name__ == '__main__':
    main()
