"""
Create the Cognito user pool, app client and demo customers for the web front end (stand-out 7.4).

Each Cognito user carries custom:customer_id (e.g. CUST-001). The attribute is read-only for the
app client, so users cannot change which customer they are; the proxy Lambda reads it from the
validated token. Re-running the script reuses the existing pool and users.

Usage (from project/starter, venv active, AWS credentials exported):
    python scripts/setup_cognito.py

Outputs:
    frontend/cognito.json      pool id, app client id, region (public identifiers, committed)
    ~/.novamart-demo-users     demo e-mails and passwords (outside the repository, never committed)
Notes: the lab account denies cognito-idp:DeleteUserPool (the pool cannot be deleted) and does not
allow cognito-idp:AdminSetUserPassword (demo users set their password through the
NEW_PASSWORD_REQUIRED sign-in challenge instead).
"""
import json
import os
import secrets
import string
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, ROOT)

import boto3    # noqa: E402
import config   # noqa: E402

POOL_NAME = 'novamart-customers'
CLIENT_NAME = 'novamart-web'
DEMO_USERS = [   # (email, display name, customer id) - matches the seeded customers
    ('alice@example.com', 'Alice Johnson', 'CUST-001'),
    ('bob@example.com',   'Bob Smith',     'CUST-002'),
]
cognito = boto3.client('cognito-idp', region_name=config.AWS_REGION)


def _password() -> str:
    """Return a random password matching the pool policy (upper, lower, digit, symbol)."""
    alphabet = string.ascii_letters + string.digits
    core = ''.join(secrets.choice(alphabet) for _ in range(14))
    return f"Nm{core}7!"


def _find_pool() -> str | None:
    """Return the id of the existing NovaMart pool, if any."""
    paginator = cognito.get_paginator('list_user_pools')
    for page in paginator.paginate(MaxResults=50):
        for pool in page['UserPools']:
            if pool['Name'] == POOL_NAME:
                return pool['Id']
    return None


PASSWORDS_FILE = os.path.expanduser('~/.novamart-demo-users')


def _stored_passwords() -> dict:
    """Return {email: password} from the local demo-users file, if it exists."""
    if not os.path.exists(PASSWORDS_FILE):
        return {}
    return {line.split()[0]: line.split()[1] for line in open(PASSWORDS_FILE) if line.strip()}


def _can_sign_in(client_id: str, email: str, password: str) -> bool:
    """Tell whether the user can sign in with this password (no challenge pending)."""
    try:
        result = cognito.initiate_auth(ClientId=client_id, AuthFlow='USER_PASSWORD_AUTH',
                                       AuthParameters={'USERNAME': email, 'PASSWORD': password})
        return 'AuthenticationResult' in result
    except cognito.exceptions.ClientError:
        return False


def _create_demo_user(pool_id: str, client_id: str, email: str, name: str, customer_id: str) -> tuple:
    """
    (Re)create a demo user and give it a permanent password.

    The lab account does not allow cognito-idp:AdminSetUserPassword, so the user is created
    with a known temporary password and then completes the NEW_PASSWORD_REQUIRED challenge
    through the public sign-in API, exactly as a real user would on first sign-in.

    Args:
        pool_id:     User pool id
        client_id:   App client id (USER_PASSWORD_AUTH enabled)
        email:       Demo user e-mail (username)
        name:        Display name
        customer_id: Customer bound to the user (custom:customer_id)

    Returns:
        Tuple (username actually used, permanent password)
    """
    try:
        cognito.admin_delete_user(UserPoolId=pool_id, Username=email)   # restart from a clean user
    except cognito.exceptions.UserNotFoundException:
        pass
    except cognito.exceptions.ClientError as exc:
        if exc.response['Error']['Code'] != 'AccessDeniedException':
            raise
        # The user exists with an unknown password and cannot be deleted here: use an alias
        email = email.replace('@', '+web@')
        print(f"  (existing user cannot be reset in this account; using {email} instead)")
    temporary, permanent = _password(), _password()
    cognito.admin_create_user(
        UserPoolId=pool_id, Username=email, MessageAction='SUPPRESS', TemporaryPassword=temporary,
        UserAttributes=[{'Name': 'email', 'Value': email},
                        {'Name': 'email_verified', 'Value': 'true'},
                        {'Name': 'name', 'Value': name},
                        {'Name': 'custom:customer_id', 'Value': customer_id}])
    challenge = cognito.initiate_auth(ClientId=client_id, AuthFlow='USER_PASSWORD_AUTH',
                                      AuthParameters={'USERNAME': email, 'PASSWORD': temporary})
    if challenge.get('ChallengeName') != 'NEW_PASSWORD_REQUIRED':
        raise RuntimeError(f"Unexpected sign-in state for {email}: {challenge.get('ChallengeName')}")
    cognito.respond_to_auth_challenge(
        ClientId=client_id, ChallengeName='NEW_PASSWORD_REQUIRED', Session=challenge['Session'],
        ChallengeResponses={'USERNAME': email, 'NEW_PASSWORD': permanent})
    print(f"User created: {email} -> {customer_id}")
    return email, permanent


def main() -> None:
    """Create (or reuse) the pool, the app client and the demo users, then write the configs."""
    pool_id = _find_pool()
    if pool_id:
        print(f"User pool {POOL_NAME} already exists: {pool_id}")
    else:
        pool_id = cognito.create_user_pool(
            PoolName=POOL_NAME,
            UsernameAttributes=['email'],
            Policies={'PasswordPolicy': {'MinimumLength': 12, 'RequireUppercase': True,
                                         'RequireLowercase': True, 'RequireNumbers': True,
                                         'RequireSymbols': False}},
            AdminCreateUserConfig={'AllowAdminCreateUserOnly': True},   # no self sign-up
            Schema=[{'Name': 'customer_id', 'AttributeDataType': 'String', 'Mutable': True,
                     'StringAttributeConstraints': {'MinLength': '8', 'MaxLength': '16'}}],
        )['UserPool']['Id']
        print(f"User pool created: {pool_id}")

    clients = cognito.list_user_pool_clients(UserPoolId=pool_id, MaxResults=60)['UserPoolClients']
    client_id = next((c['ClientId'] for c in clients if c['ClientName'] == CLIENT_NAME), None)
    if client_id:
        print(f"App client {CLIENT_NAME} already exists: {client_id}")
    else:
        client_id = cognito.create_user_pool_client(
            UserPoolId=pool_id, ClientName=CLIENT_NAME, GenerateSecret=False,
            ExplicitAuthFlows=['ALLOW_USER_PASSWORD_AUTH', 'ALLOW_REFRESH_TOKEN_AUTH'],
            ReadAttributes=['email', 'name', 'custom:customer_id'],
            WriteAttributes=['name'],                 # users cannot change custom:customer_id
            AccessTokenValidity=1, IdTokenValidity=1, RefreshTokenValidity=1,
            TokenValidityUnits={'AccessToken': 'hours', 'IdToken': 'hours', 'RefreshToken': 'days'},
            PreventUserExistenceErrors='ENABLED',
        )['UserPoolClient']['ClientId']
        print(f"App client created: {client_id}")

    # Write the public config first, so the proxy can be deployed even if a user step fails
    os.makedirs(os.path.join(ROOT, 'frontend'), exist_ok=True)
    with open(os.path.join(ROOT, 'frontend', 'cognito.json'), 'w') as fh:
        json.dump({'region': config.AWS_REGION, 'userPoolId': pool_id, 'clientId': client_id}, fh, indent=2)

    stored = _stored_passwords()
    credentials = []
    for email, name, customer_id in DEMO_USERS:
        known = next(((e, p) for e, p in stored.items() if e.replace('+web@', '@') == email), None)
        if known and _can_sign_in(client_id, *known):
            email, password = known
            print(f"User ready: {email} -> {customer_id} (existing password kept)")
        else:
            email, password = _create_demo_user(pool_id, client_id, email, name, customer_id)
        credentials.append(f"{email}  {password}  ({customer_id})")

    with open(PASSWORDS_FILE, 'w') as fh:
        fh.write('\n'.join(credentials) + '\n')
    os.chmod(PASSWORDS_FILE, 0o600)
    print(f"\nWrote frontend/cognito.json and {PASSWORDS_FILE} (demo passwords, outside the repo):")
    print('\n'.join(f"  {line}" for line in credentials))


if __name__ == '__main__':
    main()
