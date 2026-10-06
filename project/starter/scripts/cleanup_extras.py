"""
Remove the resources created by the stand-out extras, which infrastructure/cleanup.py
does not know about. Run it BEFORE infrastructure/cleanup.py, only after the reviewer
has validated the project.

Usage (from project/starter):
    python scripts/cleanup_extras.py          # dry run: list what would be deleted
    python scripts/cleanup_extras.py --yes    # delete

Note: the lab account denies cognito-idp:DeleteUserPool, so the Cognito user pools
(novamart-customers and the permission probe) are listed but cannot be deleted from here.
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, ROOT)

import boto3    # noqa: E402
import config   # noqa: E402

REGION = config.AWS_REGION
DASHBOARD_NAME = 'NovaMart-Agents'
SESSIONS_TABLE = f"{config.PROJECT_NAME}-agent-sessions"
PROXY_FUNCTION = 'novamart-frontend-proxy'
PROXY_ROLE = 'novamart-frontend-proxy-role'


def _attempt(label: str, func) -> None:
    """Run one deletion and print its outcome without stopping the cleanup."""
    try:
        func()
        print(f"  deleted  {label}")
    except Exception as exc:
        print(f"  skipped  {label}: {str(exc)[:140]}")


def main() -> None:
    """Delete (or list, in dry-run mode) the stand-out resources."""
    apply = '--yes' in sys.argv
    targets = [
        (f"CloudWatch dashboard {DASHBOARD_NAME}", lambda: boto3.client('cloudwatch', region_name=REGION)
         .delete_dashboards(DashboardNames=[DASHBOARD_NAME])),
        (f"DynamoDB table {SESSIONS_TABLE}", lambda: boto3.client('dynamodb', region_name=REGION)
         .delete_table(TableName=SESSIONS_TABLE)),
        (f"Lambda function {PROXY_FUNCTION} (and its Function URL)", lambda: boto3.client(
            'lambda', region_name=REGION).delete_function(FunctionName=PROXY_FUNCTION)),
        (f"IAM role {PROXY_ROLE}", lambda: (
            boto3.client('iam').delete_role_policy(RoleName=PROXY_ROLE, PolicyName='novamart-frontend-proxy'),
            boto3.client('iam').delete_role(RoleName=PROXY_ROLE))),
    ]
    print(f"{'Deleting' if apply else 'Dry run - would delete'}:")
    for label, func in targets:
        if apply:
            _attempt(label, func)
        else:
            print(f"  {label}")
    print("  Cognito user pools: not deletable in the lab account (cognito-idp:DeleteUserPool denied)")
    if not apply:
        print("Re-run with --yes to delete. Custom metrics expire on their own.")


if __name__ == '__main__':
    main()
