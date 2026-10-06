"""
Create the DynamoDB table used by DynamoDBSessionRepository (stand-out 7.3).

Usage (from project/starter, venv active, AWS credentials exported):
    python scripts/create_sessions_table.py

Schema: pk (string, partition key) = session_id, sk (string, sort key) =
"SESSION" | "AGENT#<agent_id>" | "MSG#<agent_id>#<message_id, 8 digits>".
The name keeps the project prefix (udacity-agentcore-agent-sessions).
Remove it with scripts/cleanup_extras.py: infrastructure/cleanup.py does not know it.
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, os.path.join(ROOT, 'src'))
sys.path.insert(0, ROOT)

import boto3                       # noqa: E402
import config                      # noqa: E402
import agent_orchestrator as ao    # noqa: E402


def main() -> None:
    """Create the sessions table if it does not exist and wait until it is ACTIVE."""
    client = boto3.client('dynamodb', region_name=config.AWS_REGION)
    try:
        client.describe_table(TableName=ao.SESSIONS_TABLE)
        print(f"Table {ao.SESSIONS_TABLE} already exists.")
        return
    except client.exceptions.ResourceNotFoundException:
        pass
    client.create_table(
        TableName=ao.SESSIONS_TABLE,
        AttributeDefinitions=[{'AttributeName': 'pk', 'AttributeType': 'S'},
                              {'AttributeName': 'sk', 'AttributeType': 'S'}],
        KeySchema=[{'AttributeName': 'pk', 'KeyType': 'HASH'},
                   {'AttributeName': 'sk', 'KeyType': 'RANGE'}],
        BillingMode='PAY_PER_REQUEST',
    )
    client.get_waiter('table_exists').wait(TableName=ao.SESSIONS_TABLE)
    print(f"Table {ao.SESSIONS_TABLE} created and ACTIVE.")


if __name__ == '__main__':
    main()
