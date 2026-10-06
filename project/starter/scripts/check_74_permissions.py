"""
Check that the lab account allows what stand-out 7.4 needs, using throwaway resources.

Creates and then deletes: a Cognito user pool, an IAM role for Lambda, a Lambda
function with a public Function URL (called once over HTTPS). Nothing is kept.

Usage (from project/starter, venv active, AWS credentials exported):
    python scripts/check_74_permissions.py
"""
import io
import json
import os
import sys
import time
import urllib.request
import uuid
import zipfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, ROOT)

import boto3    # noqa: E402
import config   # noqa: E402

REGION = config.AWS_REGION
SUFFIX = uuid.uuid4().hex[:6]
results = []


def step(name: str, func):
    """
    Run one check and record OK / DENIED.

    Args:
        name: Label printed in the summary
        func: Callable performing the AWS call; its return value is passed through

    Returns:
        The callable's result, or None if it failed
    """
    try:
        value = func()
        results.append((name, 'OK', ''))
        print(f"  OK      {name}")
        return value
    except Exception as exc:
        message = str(exc).split('\n')[0][:220]
        results.append((name, 'DENIED/ERROR', message))
        print(f"  FAILED  {name}: {message}")
        return None


def main() -> None:
    """Create, test and delete the throwaway resources, then print a summary."""
    cognito = boto3.client('cognito-idp', region_name=REGION)
    iam = boto3.client('iam')
    lam = boto3.client('lambda', region_name=REGION)
    pool_id, role_name, function_name = None, f"novamart-probe-role-{SUFFIX}", f"novamart-probe-{SUFFIX}"

    print("Cognito")
    pool = step('cognito-idp:CreateUserPool',
                lambda: cognito.create_user_pool(PoolName=f"novamart-probe-{SUFFIX}"))
    if pool:
        pool_id = pool['UserPool']['Id']
        step('cognito-idp:CreateUserPoolClient', lambda: cognito.create_user_pool_client(
            UserPoolId=pool_id, ClientName='probe', GenerateSecret=False,
            ExplicitAuthFlows=['ALLOW_USER_PASSWORD_AUTH', 'ALLOW_REFRESH_TOKEN_AUTH']))

    print("IAM")
    trust = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Principal': {
        'Service': 'lambda.amazonaws.com'}, 'Action': 'sts:AssumeRole'}]}
    role = step('iam:CreateRole (Lambda trust)', lambda: iam.create_role(
        RoleName=role_name, AssumeRolePolicyDocument=json.dumps(trust)))
    step('iam:PutRolePolicy', lambda: iam.put_role_policy(
        RoleName=role_name, PolicyName='probe', PolicyDocument=json.dumps({
            'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow',
                                                     'Action': 'logs:CreateLogGroup', 'Resource': '*'}]})))

    print("Lambda")
    if role:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('index.py', "def handler(event, context):\n    return {'statusCode': 200, 'body': 'ok'}\n")
        created = None
        for attempt in range(8):   # the new role needs a few seconds to propagate
            try:
                created = lam.create_function(
                    FunctionName=function_name, Runtime='python3.12', Role=role['Role']['Arn'],
                    Handler='index.handler', Code={'ZipFile': buffer.getvalue()}, Timeout=10)
                break
            except lam.exceptions.InvalidParameterValueException:
                time.sleep(5)
            except Exception as exc:
                created = exc
                break
        step('lambda:CreateFunction', lambda: created if not isinstance(created, Exception) and created
             else (_ for _ in ()).throw(created or RuntimeError('role never propagated')))
        if created and not isinstance(created, Exception):
            lam.get_waiter('function_active_v2').wait(FunctionName=function_name)
            url = step('lambda:CreateFunctionUrlConfig (AuthType NONE)', lambda: lam.create_function_url_config(
                FunctionName=function_name, AuthType='NONE',
                Cors={'AllowOrigins': ['*'], 'AllowMethods': ['POST'], 'AllowHeaders': ['*']}))
            step('lambda:AddPermission (public URL)', lambda: lam.add_permission(
                FunctionName=function_name, StatementId='url', Action='lambda:InvokeFunctionUrl',
                Principal='*', FunctionUrlAuthType='NONE'))
            step('lambda:AddPermission (InvokeFunction via URL)', lambda: lam.add_permission(
                FunctionName=function_name, StatementId='invoke', Action='lambda:InvokeFunction',
                Principal='*', InvokedViaFunctionUrl=True))
            if url:
                time.sleep(5)
                step('HTTPS call to the Function URL', lambda: urllib.request.urlopen(
                    urllib.request.Request(url['FunctionUrl'], data=b'{}', method='POST'), timeout=20).read())

    print("Cleanup")
    for name, func in [
        ('delete Lambda function', lambda: lam.delete_function(FunctionName=function_name)),
        ('delete IAM role policy', lambda: iam.delete_role_policy(RoleName=role_name, PolicyName='probe')),
        ('delete IAM role', lambda: iam.delete_role(RoleName=role_name)),
        ('delete user pool', lambda: cognito.delete_user_pool(UserPoolId=pool_id) if pool_id else None),
    ]:
        try:
            func()
            print(f"  removed  {name}")
        except Exception as exc:
            print(f"  skipped  {name}: {str(exc)[:120]}")

    failed = [r for r in results if r[1] != 'OK']
    print(f"\nSummary: {len(results) - len(failed)}/{len(results)} checks OK")
    if failed:
        print("Blocked:", ', '.join(r[0] for r in failed))


if __name__ == '__main__':
    main()
