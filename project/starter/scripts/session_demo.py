"""
Persistent session demo (stand-out 7.3): turn 1, simulated process restart, turn 2.

The orchestrator is built with a session_id, so its conversation is stored in DynamoDB
(DynamoDBSessionRepository + Strands RepositorySessionManager). It is then deleted and
rebuilt from scratch with the same session_id, which simulates a restart: turn 2 refers
to "the order I mentioned earlier", which only the restored history can resolve.

Usage (from project/starter, venv active, AWS credentials exported, table created):
    python scripts/session_demo.py 2>&1 | tee evidence/standout/session-demo.txt
"""
import os
import sys
import uuid

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, os.path.join(ROOT, 'src'))
sys.path.insert(0, ROOT)

from boto3.dynamodb.conditions import Key   # noqa: E402
import agent_orchestrator as ao             # noqa: E402

CUSTOMER_ID = 'CUST-001'
TURN_1 = 'Hi, my name is Alice. What is the status of my order ORD-27176?'
TURN_2 = 'Can you remind me which order I asked about earlier, and its product name?'


def _ask(orchestrator, session_id: str, text: str) -> str:
    """
    Send one customer message and return the final reply stored by the CommunicationAgent.

    Args:
        orchestrator: OrchestratorAgent built with the demo session_id
        session_id:   Demo session identifier
        text:         Customer message

    Returns:
        The customer-facing reply (WorkflowState column communication_agent)
    """
    orchestrator(f"[Session ID: {session_id}] [Customer ID: {CUSTOMER_ID}] {text}")
    state = ao._read_workflow_state(session_id) or {}
    return state.get('communication_agent', '(no reply stored)')


def main() -> None:
    """Run turn 1, rebuild the orchestrator (restart), run turn 2 and show the stored items."""
    session_id = f"demo-{uuid.uuid4().hex[:8]}"
    workers = (ao.build_inventory_agent(), ao.build_refund_agent(),
               ao.build_policy_agent(), ao.build_communication_agent())

    print(f"\n=== TURN 1 (session {session_id}) ===\nCustomer: {TURN_1}")
    first = ao.build_orchestrator_agent(*workers, session_id=session_id)
    print(f"\nREPLY 1:\n{_ask(first, session_id, TURN_1)}")

    del first   # simulate a process restart: nothing is kept in memory
    print("\n=== PROCESS RESTART: orchestrator rebuilt from DynamoDB ===")
    second = ao.build_orchestrator_agent(*workers, session_id=session_id)
    print(f"Messages restored into the new orchestrator: {len(second.messages)}")

    print(f"\n=== TURN 2 ===\nCustomer: {TURN_2}")
    print(f"\nREPLY 2:\n{_ask(second, session_id, TURN_2)}")

    items = ao.dynamodb.Table(ao.SESSIONS_TABLE).query(
        KeyConditionExpression=Key('pk').eq(session_id))['Items']
    print(f"\nItems stored in {ao.SESSIONS_TABLE} for this session: {len(items)}")
    for item in sorted(items, key=lambda i: i['sk'])[:12]:
        print(f"  {item['sk']}")


if __name__ == '__main__':
    main()
