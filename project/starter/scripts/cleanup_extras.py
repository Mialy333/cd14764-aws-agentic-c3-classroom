"""
Remove the resources created by the stand-out extras, which infrastructure/cleanup.py
does not know about. Run it BEFORE infrastructure/cleanup.py, only after the reviewer
has validated the project.

Usage (from project/starter):
    python scripts/cleanup_extras.py          # dry run: list what would be deleted
    python scripts/cleanup_extras.py --yes    # delete
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, ROOT)

import boto3    # noqa: E402
import config   # noqa: E402

DASHBOARD_NAME = 'NovaMart-Agents'
SESSIONS_TABLE = f"{config.PROJECT_NAME}-agent-sessions"


def main() -> None:
    """Delete (or list, in dry-run mode) the stand-out resources."""
    apply = '--yes' in sys.argv
    print(f"{'Deleting' if apply else 'Dry run - would delete'}:")
    print(f"  CloudWatch dashboard {DASHBOARD_NAME}")
    print(f"  DynamoDB table {SESSIONS_TABLE}")
    if not apply:
        print("Re-run with --yes to delete. Custom metrics expire on their own.")
        return
    try:
        boto3.client('cloudwatch', region_name=config.AWS_REGION).delete_dashboards(
            DashboardNames=[DASHBOARD_NAME])
        print("  dashboard deleted")
    except Exception as exc:
        print(f"  dashboard: {exc}")
    try:
        boto3.client('dynamodb', region_name=config.AWS_REGION).delete_table(TableName=SESSIONS_TABLE)
        print("  sessions table deleted")
    except Exception as exc:
        print(f"  sessions table: {exc}")


if __name__ == '__main__':
    main()
