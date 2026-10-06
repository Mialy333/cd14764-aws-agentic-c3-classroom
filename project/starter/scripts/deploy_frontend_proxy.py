"""
Deploy the front-end proxy Lambda and its public Function URL (stand-out 7.4).

Creates (or updates): IAM role novamart-frontend-proxy-role, Lambda novamart-frontend-proxy
(Python 3.12, code from frontend_proxy/ + a recent boto3 that knows bedrock-agentcore),
a Function URL (AuthType NONE: authentication is the Cognito token checked in the code)
with CORS, then writes frontend/config.js for the web page.

Usage (from project/starter, venv active, AWS credentials exported, after setup_cognito.py):
    python scripts/deploy_frontend_proxy.py
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, ROOT)

import boto3    # noqa: E402
import config   # noqa: E402

FUNCTION_NAME = 'novamart-frontend-proxy'
ROLE_NAME = 'novamart-frontend-proxy-role'
REGION = config.AWS_REGION
iam = boto3.client('iam')
lam = boto3.client('lambda', region_name=REGION)


def _package() -> bytes:
    """
    Build the deployment zip: lambda_function.py plus boto3 installed for Lambda.

    Returns:
        The zip archive bytes
    """
    with tempfile.TemporaryDirectory() as build:
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--quiet', '--target', build,
                        'boto3>=1.42'], check=True)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.write(os.path.join(ROOT, 'frontend_proxy', 'lambda_function.py'), 'lambda_function.py')
            for folder, _, files in os.walk(build):
                for name in files:
                    if name.endswith('.pyc'):
                        continue
                    path = os.path.join(folder, name)
                    archive.write(path, os.path.relpath(path, build))
        return buffer.getvalue()


def _role_arn() -> str:
    """Create (or reuse) the Lambda execution role and return its ARN."""
    trust = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Principal': {
        'Service': 'lambda.amazonaws.com'}, 'Action': 'sts:AssumeRole'}]}
    try:
        arn = iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(trust),
                              Description='NovaMart web front-end proxy')['Role']['Arn']
        print(f"Role created: {ROLE_NAME}")
    except iam.exceptions.EntityAlreadyExistsException:
        arn = iam.get_role(RoleName=ROLE_NAME)['Role']['Arn']
        print(f"Role already exists: {ROLE_NAME}")
    runtime_arn = config.AGENTCORE_RUNTIME_ARN
    policy = {'Version': '2012-10-17', 'Statement': [
        {'Effect': 'Allow', 'Action': ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
         'Resource': '*'},
        {'Effect': 'Allow', 'Action': 'bedrock-agentcore:InvokeAgentRuntime',
         'Resource': [runtime_arn, f"{runtime_arn}/*"]},
    ]}
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName='novamart-frontend-proxy',
                        PolicyDocument=json.dumps(policy))
    return arn


def main() -> None:
    """Deploy or update the function and its URL, then write frontend/config.js."""
    if not config.AGENTCORE_RUNTIME_ARN:
        sys.exit("AGENTCORE_RUNTIME_ARN missing from .env.")
    cognito_cfg = json.load(open(os.path.join(ROOT, 'frontend', 'cognito.json')))
    env = {'Variables': {'RUNTIME_ARN': config.AGENTCORE_RUNTIME_ARN,
                         'USER_POOL_ID': cognito_cfg['userPoolId'],
                         'APP_CLIENT_ID': cognito_cfg['clientId']}}
    role_arn = _role_arn()
    print("Packaging the function (lambda_function.py + boto3)...")
    code = _package()

    try:
        lam.get_function(FunctionName=FUNCTION_NAME)
        lam.update_function_code(FunctionName=FUNCTION_NAME, ZipFile=code)
        lam.get_waiter('function_updated_v2').wait(FunctionName=FUNCTION_NAME)
        lam.update_function_configuration(FunctionName=FUNCTION_NAME, Environment=env, Timeout=120)
        lam.get_waiter('function_updated_v2').wait(FunctionName=FUNCTION_NAME)
        print(f"Function updated: {FUNCTION_NAME}")
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(10):    # a new role takes a few seconds to propagate
            try:
                lam.create_function(FunctionName=FUNCTION_NAME, Runtime='python3.12', Role=role_arn,
                                    Handler='lambda_function.lambda_handler', Code={'ZipFile': code},
                                    Timeout=120, MemorySize=256, Environment=env,
                                    Description='NovaMart web front-end proxy to AgentCore Runtime')
                break
            except lam.exceptions.InvalidParameterValueException:
                time.sleep(5)
        lam.get_waiter('function_active_v2').wait(FunctionName=FUNCTION_NAME)
        print(f"Function created: {FUNCTION_NAME}")

    cors = {'AllowOrigins': ['*'], 'AllowMethods': ['POST'],
            'AllowHeaders': ['authorization', 'content-type'], 'MaxAge': 3600}
    try:
        url = lam.create_function_url_config(FunctionName=FUNCTION_NAME, AuthType='NONE',
                                             Cors=cors)['FunctionUrl']
        lam.add_permission(FunctionName=FUNCTION_NAME, StatementId='public-url',
                           Action='lambda:InvokeFunctionUrl', Principal='*', FunctionUrlAuthType='NONE')
        lam.add_permission(FunctionName=FUNCTION_NAME, StatementId='public-invoke',
                           Action='lambda:InvokeFunction', Principal='*', InvokedViaFunctionUrl=True)
    except lam.exceptions.ResourceConflictException:
        url = lam.update_function_url_config(FunctionName=FUNCTION_NAME, AuthType='NONE',
                                             Cors=cors)['FunctionUrl']
    print(f"Function URL: {url}")

    with open(os.path.join(ROOT, 'frontend', 'config.js'), 'w') as fh:
        fh.write("// Generated by scripts/deploy_frontend_proxy.py - public identifiers only, no secret.\n")
        fh.write("window.NOVAMART_CONFIG = " + json.dumps({
            'region': cognito_cfg['region'], 'userPoolId': cognito_cfg['userPoolId'],
            'clientId': cognito_cfg['clientId'], 'apiUrl': url}, indent=2) + ";\n")
    print("Wrote frontend/config.js")


if __name__ == '__main__':
    main()
