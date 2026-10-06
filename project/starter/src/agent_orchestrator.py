"""
agent_orchestrator.py
=====================
Enterprise Multi-Agent Customer Support System
Built with Strands Agents SDK + Amazon Bedrock AgentCore

Architecture implemented:

  Customer Request
        │
  OrchestratorAgent  (Claude Haiku 4.5 - fast routing, manages WorkflowState)
        │
   ┌────┼────────────────────┬────────────────────────┐
   │    │                    │                        │
InventoryAgent   PolicyAgent   RefundAgent  CommunicationAgent
(DynamoDB)    (Multi-Agent RAG)  (DynamoDB)   (composes response)
                    │
         ┌──────────┼──────────┐
    ReturnsPolicyRetriever  ShippingPolicyRetriever  WarrantyPolicyRetriever
        (KB: returns)           (KB: shipping)           (KB: warranty)
         └──────────── all run in PARALLEL ────────────┘

Shared state flows through DynamoDB WorkflowStateTable.
OrchestratorAgent creates state at start, each routing tool reads and
updates it after the worker responds.

Commands:
  python src/agent_orchestrator.py test            # 3 scenarios, local run, traced to X-Ray
  python src/agent_orchestrator.py chat            # interactive terminal chat
  python src/agent_orchestrator.py deploy          # Tasks 3-6 deployment pipeline (uses the AgentCore CLI)
  python src/agent_orchestrator.py invoke "<msg>"  # call the deployed AgentCore Runtime
  python src/agent_orchestrator.py serve           # HTTP server (what AgentCore Runtime runs)

Deployment uses the AgentCore CLI (`agentcore`, npm package @aws/agentcore,
https://github.com/aws/agentcore-cli) through the pre-written helper
src/agentcore_cli.py - see the README for the prerequisites (Node.js 20+, uv).
"""

import boto3
import json
import time
import os
import sys
import uuid
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

# Ensure the parent directory is on sys.path so config.py and
# bedrock_kb_retrieval.py are importable regardless of where this
# script is invoked from (e.g. python src/agent_orchestrator.py)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Strands Agents SDK - see: https://github.com/strands-agents/sdk-python
from strands import Agent
from strands.models import BedrockModel
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

import config
from bedrock_kb_retrieval import retrieve_from_knowledge_base, format_kb_results

# Configure logging for debugging
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────
# OUTPUT UTILITIES
# ─────────────────────────────────────────────────────
# Terminal trace UI, ANSI colour constants, and agent metadata
# are defined in agent_utils.py - keeping this file focused on
# agent architecture.
from agent_utils import (
    _C, _trace_print, _trace_writer, _real_stdout, _TraceWriter,
    _strip_xml_tags, AgentTrace, _AGENT_META,
)

# ─────────────────────────────────────────────────────
# OBSERVABILITY
# ─────────────────────────────────────────────────────
# `tool` is the Strands @tool decorator wrapped so that every tool call is
# recorded as an X-Ray subsegment (the orchestrator's route_to_* tools become
# the worker-agent nodes on the X-Ray Service Map) and logged at INFO level.
# Use it exactly like `strands.tool`:  @tool  above each tool function.
from agent_observability import (
    tool, tracer, setup_logging, flush_logs, print_trace_hint,
    apply_observability_config, wait_for_runtime_ready,
)


# ─────────────────────────────────────────────────────
# AWS CLIENTS
# ─────────────────────────────────────────────────────
bedrock_agent_client = boto3.client('bedrock-agent', region_name=config.AWS_REGION)
bedrock_runtime      = boto3.client('bedrock-runtime', region_name=config.AWS_REGION)
agentcore_client     = boto3.client('bedrock-agentcore', region_name=config.AWS_REGION)
agentcore_control    = boto3.client('bedrock-agentcore-control', region_name=config.AWS_REGION)
dynamodb             = boto3.resource('dynamodb', region_name=config.AWS_REGION)
logs_client          = boto3.client('logs', region_name=config.AWS_REGION)


# ═══════════════════════════════════════════════════════
#  WORKFLOW STATE - SHARED DynamoDB STATE OBJECT
#
#  WorkflowState stores the accumulated context for one customer session:
#    - What the InventoryAgent found (order status, eligibility, customer tier)
#    - What the PolicyAgent found (relevant policy text)
#    - What the RefundAgent decided (approval/denial, reference number)
#    - The CommunicationAgent's final draft
#
#  The `version` field enables optimistic locking: every write is a
#  conditional DynamoDB update that fails if someone else updated first.
#  If the condition fails, the update is retried after a fresh read.
# ═══════════════════════════════════════════════════════

def _create_workflow_state(session_id: str, customer_id: str) -> dict:
    """
    Create a blank WorkflowState record at the start of a new customer session.

    Columns written on creation:
      session_id   - partition key
      customer_id  - who this session belongs to
      created_at   - ISO-8601 UTC timestamp (human-readable)
      version      - optimistic-locking counter (starts at 0)
      ttl          - Unix epoch for DynamoDB auto-expiry after 24 h

    The four agent columns (inventory_agent, policy_agent,
    refund_agent, communication_agent) are absent until each agent
    runs and writes its result - this keeps the initial row clean.
    """
    state = {
        'session_id':  session_id,
        'customer_id': customer_id,
        'created_at':  time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'version':     0,
        'ttl':         int(time.time()) + (24 * 3600),
    }
    table = dynamodb.Table(config.WORKFLOW_STATE_TABLE)
    table.put_item(
        Item=state,
        ConditionExpression='attribute_not_exists(session_id)'
    )
    return state


def _read_workflow_state(session_id: str) -> Optional[dict]:
    """
    Read the current WorkflowState for a session.
    """
    table = dynamodb.Table(config.WORKFLOW_STATE_TABLE)
    response = table.get_item(Key={'session_id': session_id})
    return response.get('Item')


# Trace singleton - created after _read_workflow_state so AgentTrace.summary()
# can read DynamoDB WorkflowState. The read_state_fn avoids a circular import.
trace = AgentTrace(read_state_fn=_read_workflow_state)


def _update_workflow_state(session_id: str, updates: dict,
                           expected_version: int, max_retries: int = 3) -> dict:
    """
    Update WorkflowState with optimistic locking.
    """
    from boto3.dynamodb.conditions import Attr

    table = dynamodb.Table(config.WORKFLOW_STATE_TABLE)

    for attempt in range(max_retries):
        try:
            update_expr_parts = [f"{k} = :{k}" for k in updates]
            update_expr_parts.append("version = :new_version")
            update_expr = "SET " + ", ".join(update_expr_parts)

            expr_values = {f":{k}": v for k, v in updates.items()}
            expr_values[':new_version']      = expected_version + 1
            expr_values[':expected_version'] = expected_version

            table.update_item(
                Key={'session_id': session_id},
                UpdateExpression=update_expr,
                ConditionExpression='version = :expected_version',
                ExpressionAttributeValues=expr_values
            )
            return _read_workflow_state(session_id)

        except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
            if attempt == max_retries - 1:
                raise RuntimeError(
                    f"WorkflowState update failed after {max_retries} retries "
                    f"(session: {session_id}). Too many concurrent writes."
                )
            logger.warning(
                f"WorkflowState version conflict on attempt {attempt+1}, retrying..."
            )
            current = _read_workflow_state(session_id)
            if current:
                expected_version = int(current['version'])
            time.sleep(0.1 * (attempt + 1))

    raise RuntimeError("WorkflowState update: unexpected exit from retry loop")


# ═══════════════════════════════════════════════════════
#  TASK 2 - MULTI-AGENT ORCHESTRATION
# ═══════════════════════════════════════════════════════


# ───────────────────────────────────────────────────────
#  2.A - INVENTORY AGENT
# ───────────────────────────────────────────────────────

def _to_json_safe(item: dict) -> dict:
    """
    Convert a DynamoDB item into a JSON-serializable dict.

    DynamoDB returns numbers as Decimal (e.g. total_orders), which json.dumps
    cannot serialize: they are converted to strings with default=str.

    Args:
        item: Raw DynamoDB item (may contain Decimal values)

    Returns:
        Copy of the item containing only native JSON types
    """
    return json.loads(json.dumps(item, default=str))


def _days_since(date_str: str) -> Optional[int]:
    """
    Compute the number of days elapsed since a YYYY-MM-DD date (UTC).

    Args:
        date_str: Date in YYYY-MM-DD format (e.g. order_date)

    Returns:
        Whole days elapsed, or None if the date is missing or invalid
    """
    from datetime import datetime, timezone
    try:
        start = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) - start).days


from strands.types.tools import ToolContext   # noqa: E402  (used by the scoped tools below)


def _check_customer_scope(tool_context: ToolContext, customer_id: str) -> Optional[dict]:
    """
    Enforce customer isolation inside a DynamoDB tool.

    The routing tools bind the session's customer to the worker agent
    (agent.state["session_customer_id"]). A tool may then only read or change
    that customer's data, whatever customer_id the model passes. When no
    customer is bound (direct calls from tests or scripts), access is allowed.

    Args:
        tool_context: Strands tool context, gives access to the calling agent
        customer_id:  Customer requested by the model

    Returns:
        None if access is allowed, otherwise an access-denied dict for the model
    """
    bound = tool_context.agent.state.get('session_customer_id')
    if bound and customer_id != bound:
        logger.warning("Blocked cross-customer access: session %s requested %s", bound, customer_id)
        return {
            'found': False,
            'access_denied': True,
            'message': (f"Access denied: this session belongs to customer {bound}. "
                        f"Data of customer {customer_id} cannot be accessed."),
        }
    return None


def build_inventory_agent() -> Agent:
    """
    Build the Inventory Agent.

    Gathers order and customer facts from DynamoDB. Does NOT make decisions -
    only retrieves data for the OrchestratorAgent to share with downstream agents.
    """

    # Worker model (Sonnet 4.5); low temperature: facts, not creativity
    model = BedrockModel(
        model_id=config.WORKER_MODEL_ID,
        temperature=0.1,
        region_name=config.AWS_REGION,
    )

    # System prompt: fact gatherer, never an eligibility decision
    system_prompt = """You are the InventoryAgent of NovaMart customer support.
Your ONLY job is to gather accurate facts from NovaMart's order and customer databases.

Tools:
- check_order_status(customer_id, order_id): one order (status, product, dates, price, days_since_order)
- get_customer_tier(customer_id): customer profile, including tier (Standard or Premium)
- list_customer_orders(customer_id): every order of a customer

Rules:
1. Always use the tools; never guess or invent data.
2. For a request about a specific order, call check_order_status AND get_customer_tier,
   so downstream agents have both the order facts and the customer tier.
3. If no order ID is given, call list_customer_orders.
4. For account questions ("what is my tier?", "am I premium?"), call get_customer_tier.
5. Report facts only. NEVER decide or state whether a return or refund is eligible,
   approved or denied - that is the RefundAgent's job.
6. If a record is not found, say so explicitly.
7. You only serve the customer of the current session. If a tool returns "access_denied",
   report that the data belongs to another customer and cannot be shared.

Answer with a concise, structured fact sheet: customer (id, name, tier), then each order
(order_id, product, category, status, order_date, estimated_delivery, days_since_order,
price, quantity)."""

    # NOTE: the Orders table has a COMPOSITE key (customer_id = partition key,
    # order_id = sort key), so a get_item needs BOTH values. That is why this
    # tool takes customer_id as well as order_id.
    @tool(context=True)
    def check_order_status(customer_id: str, order_id: str, tool_context: ToolContext) -> dict:
        """
        Look up one order in DynamoDB and report its status, product, dates
        and amount. Reports facts only - it does NOT decide return eligibility.

        Args:
            customer_id: The customer's unique identifier (e.g. CUST-001)
            order_id: The order identifier (e.g. ORD-27176)
            tool_context: Injected by Strands (not provided by the model); used to
                          enforce the session's customer scope

        Returns:
            Order record (order_id, status, product_name, order_date, price, ...)
            plus days_since_order, a not-found message, or an access-denied message
        """
        denied = _check_customer_scope(tool_context, customer_id)
        if denied:
            return denied
        table = dynamodb.Table(config.ORDERS_TABLE)
        try:
            # get_item needs the full composite key: customer_id + order_id
            response = table.get_item(Key={'customer_id': customer_id, 'order_id': order_id})
        except ClientError as exc:
            return {'found': False, 'error': f"DynamoDB error: {exc.response['Error']['Message']}"}

        item = response.get('Item')
        if not item:
            return {
                'found': False,
                'customer_id': customer_id,
                'order_id': order_id,
                'message': f"No order {order_id} found for customer {customer_id}.",
            }
        order = _to_json_safe(item)
        # Computed fact (not a decision): order age in days
        order['days_since_order'] = _days_since(order.get('order_date'))
        order['found'] = True
        return order

    @tool(context=True)
    def get_customer_tier(customer_id: str, tool_context: ToolContext) -> dict:
        """
        Retrieve a customer's tier (Standard or Premium) from DynamoDB.
        Standard customers have a 30-day return window; Premium customers have 60 days.

        Args:
            customer_id: The customer's unique identifier
            tool_context: Injected by Strands (not provided by the model); used to
                          enforce the session's customer scope

        Returns:
            Customer profile including tier and account details, a not-found
            message, or an access-denied message
        """
        denied = _check_customer_scope(tool_context, customer_id)
        if denied:
            return denied
        table = dynamodb.Table(config.CUSTOMERS_TABLE)
        try:
            response = table.get_item(Key={'customer_id': customer_id})
        except ClientError as exc:
            return {'found': False, 'error': f"DynamoDB error: {exc.response['Error']['Message']}"}

        item = response.get('Item')
        if not item:
            return {
                'found': False,
                'customer_id': customer_id,
                'message': f"No customer {customer_id} found.",
            }
        customer = _to_json_safe(item)
        customer['found'] = True
        return customer

    @tool(context=True)
    def list_customer_orders(customer_id: str, tool_context: ToolContext) -> dict:
        """
        Retrieve all orders for a customer from DynamoDB.

        Args:
            customer_id: The customer's unique identifier
            tool_context: Injected by Strands (not provided by the model); used to
                          enforce the session's customer scope

        Returns:
            Dict with customer_id, order_count and the list of orders (order_id,
            product_name, status, order_date, days_since_order, price, quantity),
            or an access-denied message
        """
        denied = _check_customer_scope(tool_context, customer_id)
        if denied:
            return denied
        table = dynamodb.Table(config.ORDERS_TABLE)
        try:
            # Query on the partition key: every order of the customer
            response = table.query(KeyConditionExpression=Key('customer_id').eq(customer_id))
        except ClientError as exc:
            return {'customer_id': customer_id, 'error': f"DynamoDB error: {exc.response['Error']['Message']}"}

        orders = []
        for item in response.get('Items', []):
            order = _to_json_safe(item)
            order['days_since_order'] = _days_since(order.get('order_date'))
            orders.append(order)
        orders.sort(key=lambda o: o.get('order_date', ''), reverse=True)
        return {'customer_id': customer_id, 'order_count': len(orders), 'orders': orders}

    return Agent(
        name='InventoryAgent',
        model=model,
        system_prompt=system_prompt,
        tools=[check_order_status, get_customer_tier, list_customer_orders],
    )


# ───────────────────────────────────────────────────────
#  2.B - REFUND AGENT
# ───────────────────────────────────────────────────────

def build_refund_agent() -> Agent:
    """
    Build the Refund Agent.

    Makes return/refund eligibility decisions based on order facts from
    WorkflowState and applies the correct policy window per customer tier.
    """

    # Return windows per tier (days since order_date): Standard 30, Premium 60
    return_windows = {'Standard': 30, 'Premium': 60}

    # Worker model (Sonnet 4.5); low temperature: reproducible decisions
    model = BedrockModel(
        model_id=config.WORKER_MODEL_ID,
        temperature=0.1,
        region_name=config.AWS_REGION,
    )

    # System prompt: step-by-step decision procedure with explicit windows
    system_prompt = """You are the RefundAgent of NovaMart customer support.
You decide whether a return/refund request is eligible, based ONLY on facts
gathered by the InventoryAgent, and you process eligible returns.

Return windows (counted from the order date, using days_since_order):
- Standard tier: 30 days
- Premium tier: 60 days

Decision process - follow it in order:
1. ALWAYS call get_inventory_context(session_id) FIRST to read the InventoryAgent findings.
   If no inventory facts are available, do not guess: say the order facts are missing.
2. Identify the customer tier, the order status and days_since_order.
3. Eligibility rules:
   - the order status must be "delivered" (shipped, processing or cancelled orders cannot be returned);
   - days_since_order must be <= 30 for Standard customers, <= 60 for Premium customers.
4. If eligible AND the customer explicitly asks to return the order or get a refund, call
   initiate_refund(customer_id, order_id, reason) with the customer's reason
   (use "Customer requested return" if none was given).
5. If not eligible, do NOT call initiate_refund. If the customer only asks about the order
   status or history (no return/refund requested), do NOT call initiate_refund either: just
   state whether the order would be eligible for a return and until which day count.

Answer with a short decision summary: decision (APPROVED or DENIED), customer tier, applicable
window, days_since_order, order status, the reason for the decision and, if approved, the
return_reference and next steps returned by initiate_refund. Never invent a return reference."""

    @tool
    def get_inventory_context(session_id: str) -> dict:
        """
        Read the WorkflowState to access facts gathered by the InventoryAgent.

        Args:
            session_id: The current session identifier

        Returns:
            Dict with session_id, customer_id and the inventory_agent findings,
            or a message saying the facts are not available yet
        """
        state = _read_workflow_state(session_id)
        if not state:
            return {'session_id': session_id, 'found': False,
                    'message': f"No WorkflowState found for session {session_id}."}
        inventory = state.get('inventory_agent')
        if not inventory:
            return {'session_id': session_id, 'found': False,
                    'message': 'InventoryAgent has not run yet for this session.'}
        return {
            'session_id':      session_id,
            'customer_id':     state.get('customer_id'),
            'found':           True,
            'inventory_agent': inventory,
        }

    @tool(context=True)
    def initiate_refund(customer_id: str, order_id: str, reason: str,
                        tool_context: ToolContext) -> dict:
        """
        Initiate a return by updating the order record in DynamoDB.

        Re-checks the eligibility rules in code (status delivered, 30-day window for
        Standard, 60-day window for Premium) before writing, so a model error can never
        approve an ineligible return. Idempotent: a second call on the same order
        returns the existing return reference.

        Args:
            customer_id: The customer's unique identifier
            order_id: The order to return
            reason: Customer-provided reason for the return
            tool_context: Injected by Strands (not provided by the model); used to
                          enforce the session's customer scope

        Returns:
            Confirmation dict with return_reference number and instructions,
            or a refusal dict with the reason
        """
        denied = _check_customer_scope(tool_context, customer_id)
        if denied:
            return {'success': False, **denied}
        orders_table = dynamodb.Table(config.ORDERS_TABLE)
        customers_table = dynamodb.Table(config.CUSTOMERS_TABLE)
        try:
            order = orders_table.get_item(
                Key={'customer_id': customer_id, 'order_id': order_id}).get('Item')
            customer = customers_table.get_item(Key={'customer_id': customer_id}).get('Item')
        except ClientError as exc:
            return {'success': False, 'error': f"DynamoDB error: {exc.response['Error']['Message']}"}

        if not order or not customer:
            return {'success': False,
                    'message': f"Order {order_id} or customer {customer_id} not found."}

        # Idempotency: return already initiated -> give back the existing reference
        if order.get('status') == 'return_initiated':
            return {'success': True, 'already_initiated': True,
                    'order_id': order_id,
                    'return_reference': order.get('return_reference'),
                    'message': 'A return was already initiated for this order.'}

        # Code-side guardrail: the LLM decision is re-checked before any write
        tier = customer.get('tier', 'Standard')
        window = return_windows.get(tier, return_windows['Standard'])
        days = _days_since(order.get('order_date'))
        if order.get('status') != 'delivered':
            return {'success': False, 'order_id': order_id,
                    'message': f"Order status is '{order.get('status')}'; only delivered orders can be returned."}
        if days is None or days > window:
            return {'success': False, 'order_id': order_id, 'tier': tier,
                    'window_days': window, 'days_since_order': days,
                    'message': f"Outside the {window}-day return window for {tier} customers."}

        return_reference = f"RET-{uuid.uuid4().hex[:8].upper()}"
        initiated_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        try:
            # Conditional write: the order must still be "delivered"
            orders_table.update_item(
                Key={'customer_id': customer_id, 'order_id': order_id},
                UpdateExpression=('SET #s = :new_status, return_reference = :ref, '
                                  'return_reason = :reason, return_initiated_at = :ts'),
                ConditionExpression='#s = :delivered',
                ExpressionAttributeNames={'#s': 'status'},
                ExpressionAttributeValues={
                    ':new_status': 'return_initiated',
                    ':ref':        return_reference,
                    ':reason':     reason,
                    ':ts':         initiated_at,
                    ':delivered':  'delivered',
                },
            )
        except ClientError as exc:
            return {'success': False, 'error': f"Could not update order: {exc.response['Error']['Message']}"}

        return {
            'success':          True,
            'order_id':         order_id,
            'customer_id':      customer_id,
            'tier':             tier,
            'window_days':      window,
            'days_since_order': days,
            'return_reference': return_reference,
            'refund_amount':    order.get('price'),
            'instructions': ('Log in to your account, open Order History, print the prepaid '
                             'return label and drop the package at any authorized carrier. '
                             'Refunds are processed within 5-7 business days of receipt.'),
        }

    return Agent(
        name='RefundAgent',
        model=model,
        system_prompt=system_prompt,
        tools=[get_inventory_context, initiate_refund],
    )


# ───────────────────────────────────────────────────────
#  2.C - POLICY AGENT - MULTI-AGENT RAG
# ───────────────────────────────────────────────────────

def build_policy_agent() -> Agent:
    """
    Build the Policy Agent - a multi-agent RAG system.

    Internally creates three specialized retriever sub-agents that run in
    PARALLEL, each querying its own Knowledge Base. The coordinator synthesizes
    the combined results into a complete, grounded policy answer.
    """

    # Shared prompt of the 3 retrievers: retrieve, do not rephrase, invent nothing
    retriever_prompt = """You are the {domain}PolicyRetrieverAgent of NovaMart.
You have exactly one tool, which searches the NovaMart {domain} policy knowledge base.
For every question: call your tool ONCE with the question, then return the retrieved
passages verbatim with their source and score. Do not paraphrase, summarize, answer
the customer or add any information that is not in the passages. If nothing relevant
is found, say "No relevant {domain} policy passages found." """

    def _retriever_model() -> BedrockModel:
        """
        Create the BedrockModel shared settings of a retriever sub-agent.

        Returns:
            BedrockModel on config.WORKER_MODEL_ID with temperature 0.0 (deterministic retrieval)
        """
        return BedrockModel(
            model_id=config.WORKER_MODEL_ID,
            temperature=0.0,
            region_name=config.AWS_REGION,
        )

    @tool
    def retrieve_returns_policy(query: str) -> str:
        """
        Retrieve relevant passages from the Returns Policy knowledge base.

        Args:
            query: Natural-language policy question

        Returns:
            Top passages (text, score, S3 source) from config.RETURNS_KB_ID
        """
        return format_kb_results(retrieve_from_knowledge_base(config.RETURNS_KB_ID, query))

    # Returns sub-agent: a single tool, temperature 0.0
    returns_retriever = Agent(
        name='ReturnsPolicyRetrieverAgent',
        model=_retriever_model(),
        system_prompt=retriever_prompt.format(domain='Returns'),
        tools=[retrieve_returns_policy],
        callback_handler=None,
    )

    @tool
    def retrieve_shipping_policy(query: str) -> str:
        """
        Retrieve relevant passages from the Shipping Policy knowledge base.

        Args:
            query: Natural-language policy question

        Returns:
            Top passages (text, score, S3 source) from config.SHIPPING_KB_ID
        """
        return format_kb_results(retrieve_from_knowledge_base(config.SHIPPING_KB_ID, query))

    # Shipping sub-agent: a single tool, temperature 0.0
    shipping_retriever = Agent(
        name='ShippingPolicyRetrieverAgent',
        model=_retriever_model(),
        system_prompt=retriever_prompt.format(domain='Shipping'),
        tools=[retrieve_shipping_policy],
        callback_handler=None,
    )

    @tool
    def retrieve_warranty_policy(query: str) -> str:
        """
        Retrieve relevant passages from the Warranty Policy knowledge base.

        Args:
            query: Natural-language policy question

        Returns:
            Top passages (text, score, S3 source) from config.WARRANTY_KB_ID
        """
        return format_kb_results(retrieve_from_knowledge_base(config.WARRANTY_KB_ID, query))

    # Warranty sub-agent: a single tool, temperature 0.0
    warranty_retriever = Agent(
        name='WarrantyPolicyRetrieverAgent',
        model=_retriever_model(),
        system_prompt=retriever_prompt.format(domain='Warranty'),
        tools=[retrieve_warranty_policy],
        callback_handler=None,
    )

    @tool
    def search_all_policies(query: str) -> str:
        """
        Query all three policy knowledge bases IN PARALLEL and return combined results.

        Runs ReturnsPolicyRetrieverAgent, ShippingPolicyRetrieverAgent, and
        WarrantyPolicyRetrieverAgent simultaneously, then combines their findings.

        Args:
            query: The customer's policy question

        Returns:
            Combined policy passages from all three knowledge bases, one section per domain
        """
        # Domain -> retriever sub-agent
        retrievers = {
            'Returns':  returns_retriever,
            'Shipping': shipping_retriever,
            'Warranty': warranty_retriever,
        }

        # ── Trace: show parallel KB dispatch to learners ──────────────────
        trace.kb_start({
            'Returns':  config.RETURNS_KB_ID,
            'Shipping': config.SHIPPING_KB_ID,
            'Warranty': config.WARRANTY_KB_ID,
        })

        def _run_retriever(domain: str, agent, query: str) -> tuple:
            """
            Run one retriever sub-agent and return (domain, result_text).

            stdout is suppressed globally for all threads by the
            _TraceWriter._suppress_parallel flag set in kb_start().
            Results are returned as values and printed cleanly and
            sequentially by trace.kb_result() after all futures join.

            Args:
                domain: Policy domain label (Returns, Shipping or Warranty)
                agent:  The retriever sub-agent for that domain
                query:  The customer's policy question

            Returns:
                Tuple (domain, retrieved passages as text)
            """
            # Fresh history: each search starts from scratch (no leak between requests)
            agent.messages = []
            result = agent(f"Retrieve the {domain} policy passages relevant to: {query}")
            return domain, str(result)

        results = {}
        try:
            # Parallel fan-out: 3 threads, one per Knowledge Base
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = {
                    executor.submit(_run_retriever, domain, agent, query): domain
                    for domain, agent in retrievers.items()
                }
                for future in as_completed(futures):
                    domain = futures[future]
                    try:
                        _, text = future.result()
                        results[domain] = text
                    except Exception as exc:
                        results[domain] = f"[{domain} retriever error: {exc}]"
        finally:
            # ── Trace: all KBs responded - print each result sequentially ─────
            trace.kb_done(len(retrievers))
        for domain in ['Returns', 'Shipping', 'Warranty']:
            trace.kb_result(domain, results.get(domain, '[No results]'))

        # Combined result, always in the same domain order
        return "\n\n".join(
            f"=== {domain} Policy (from {domain}PolicyRetrieverAgent) ===\n"
            f"{results.get(domain, '[No results]')}"
            for domain in ['Returns', 'Shipping', 'Warranty']
        )

    # Coordinator: Sonnet 4.5, temperature 0.2 (faithful yet readable synthesis)
    model = BedrockModel(
        model_id=config.WORKER_MODEL_ID,
        temperature=0.2,
        region_name=config.AWS_REGION,
    )

    system_prompt = """You are the PolicyAgent of NovaMart customer support. You answer questions
about NovaMart policies (returns, shipping, warranty, customer tier benefits).

Rules:
1. ALWAYS call search_all_policies FIRST, with the customer's question, before answering.
   It searches the Returns, Shipping and Warranty knowledge bases in parallel.
2. Answer ONLY from the retrieved passages. Never use outside knowledge and never invent
   prices, durations or conditions. If the passages do not answer the question, say so.
3. Synthesize across the three domains and name the policy each fact comes from
   (e.g. "Return Policy", "Shipping Policy", "Warranty Policy", "Customer Tier Program").
4. If passages disagree, report both statements with their sources instead of choosing one.
5. Map customer wording to policy terms when obvious (e.g. "express" shipping -> the
   Expedited / Overnight options) and say which option you mean.
6. You only know policy text, not customer accounts or orders: never state a specific
   customer's tier or order status.

Return a concise, factual answer (bullet points are fine) for the CommunicationAgent."""

    return Agent(
        name='PolicyAgent',
        model=model,
        system_prompt=system_prompt,
        tools=[search_all_policies],
    )


# ───────────────────────────────────────────────────────
#  2.D - COMMUNICATION AGENT
# ───────────────────────────────────────────────────────

def build_communication_agent() -> Agent:
    """
    Build the Communication Agent.

    Drafts the final customer-facing message by reading the full WorkflowState
    and composing a coherent, empathetic response.
    """

    # Worker model (Sonnet 4.5); temperature 0.3 for a warm, natural tone
    model = BedrockModel(
        model_id=config.WORKER_MODEL_ID,
        temperature=0.3,
        region_name=config.AWS_REGION,
    )

    # System prompt: final reply faithful to the WorkflowState, empathetic tone
    system_prompt = """You are the CommunicationAgent of NovaMart customer support. You write the
FINAL message that the customer will read.

Process:
1. ALWAYS call get_full_workflow_context(session_id) FIRST to read every finding gathered
   for this session: inventory_agent (order and account facts), policy_agent (policy answer)
   and refund_agent (return decision).
2. Write one reply that answers the customer's original request using ALL relevant findings:
   - order or account facts (product, status, dates, tier) when present;
   - policy details when present, keeping the facts and numbers exactly as given;
   - the return decision when present: if approved, give the return reference, refund amount
     and next steps; if denied, explain the reason kindly and mention any alternative that
     appears in the findings.
3. Never invent facts, prices, dates, references or policies that are not in the findings.
   If information is missing, say what you could not confirm and how the customer can get help.
   When the message includes "Conversation so far in this session", it is the customer's earlier
   exchange with NovaMart: use it to answer references to earlier messages (e.g. "the order I
   asked about"); facts stated there may be repeated.
4. Calculation requests (no other agent ran): compute it yourself, step by step, and show the
   working briefly. Apply discounts to the full amount and round currency to the nearest cent
   only at the very end (round half up).
5. Tone: warm, professional and empathetic. Greet the customer by first name when known,
   be concise, and end with an offer of further help.
6. Never mention internal agents, tools, sessions, "findings" or the WorkflowState.

Output format: your final answer is sent to the customer AS IS. It must contain ONLY the
customer message, starting directly with the greeting - no preamble, no reasoning, no
comment about the request type or the available information, no separator line."""

    @tool
    def get_full_workflow_context(session_id: str) -> dict:
        """
        Read the complete WorkflowState to access all findings from previous agents.

        Args:
            session_id: The current session identifier

        Returns:
            Full WorkflowState dict (customer_id, inventory_agent, policy_agent,
            refund_agent, version...), or a not-found message
        """
        state = _read_workflow_state(session_id)
        if not state:
            return {'session_id': session_id, 'found': False,
                    'message': f"No WorkflowState found for session {session_id}."}
        # Decimal (version, ttl) -> native JSON types
        context = _to_json_safe(state)
        context['found'] = True
        return context

    return Agent(
        name='CommunicationAgent',
        model=model,
        system_prompt=system_prompt,
        tools=[get_full_workflow_context],
    )


# ───────────────────────────────────────────────────────
#  2.E - ORCHESTRATOR AGENT
# ───────────────────────────────────────────────────────

# --- Per-agent CloudWatch metrics (stand-out 7.2) ---
# Metrics are published with the CloudWatch Embedded Metric Format (EMF): one JSON
# log event per worker call, written to the project log group. CloudWatch extracts
# the metrics automatically, so the runtime execution role needs no extra permission
# (it already has logs:CreateLogStream / logs:PutLogEvents; it has no PutMetricData).
METRICS_NAMESPACE = 'NovaMart/Agents'
_metrics_logs = boto3.client('logs', region_name=config.AWS_REGION)
_metrics_logs.meta.events.register(
    'before-sign.logs.PutLogEvents',
    lambda request, **kwargs: request.headers.__setitem__('x-amzn-logs-format', 'json/emf'),
)
_metrics_stream = {'name': None}
_metrics_lock = threading.Lock()


def _emit_agent_metrics(agent_name: str, latency_ms: float, guardrail_blocked: bool = False) -> None:
    """
    Publish one invocation, its latency and a guardrail flag for an agent (EMF).

    Args:
        agent_name:        Agent name used as the "Agent" dimension (e.g. "InventoryAgent")
        latency_ms:        Wall-clock duration of the agent call in milliseconds
        guardrail_blocked: True if the call ended with a guardrail intervention

    Returns:
        None. Failures are logged and never break the customer request.
    """
    payload = {
        '_aws': {
            'Timestamp': int(time.time() * 1000),
            'CloudWatchMetrics': [{
                'Namespace': METRICS_NAMESPACE,
                'Dimensions': [['Agent']],
                'Metrics': [
                    {'Name': 'AgentInvocations', 'Unit': 'Count'},
                    {'Name': 'AgentLatencyMs',   'Unit': 'Milliseconds'},
                    {'Name': 'GuardrailBlocked', 'Unit': 'Count'},
                ],
            }],
        },
        'Agent':            agent_name,
        'AgentInvocations': 1,
        'AgentLatencyMs':   round(latency_ms, 1),
        'GuardrailBlocked': 1 if guardrail_blocked else 0,
    }
    try:
        with _metrics_lock:
            if _metrics_stream['name'] is None:
                name = f"novamart-metrics/{uuid.uuid4().hex[:12]}"
                _metrics_logs.create_log_stream(logGroupName=config.AGENT_LOG_GROUP, logStreamName=name)
                _metrics_stream['name'] = name
            _metrics_logs.put_log_events(
                logGroupName=config.AGENT_LOG_GROUP,
                logStreamName=_metrics_stream['name'],
                logEvents=[{'timestamp': payload['_aws']['Timestamp'], 'message': json.dumps(payload)}],
            )
    except Exception as exc:
        logger.warning("Metric emission failed for %s: %s", agent_name, exc)


def _guardrail_intervened(result) -> bool:
    """
    Tell whether an agent call ended with a Bedrock Guardrail intervention.

    Args:
        result: The AgentResult returned by a Strands agent call

    Returns:
        True if the stop reason is "guardrail_intervened"
    """
    return getattr(result, 'stop_reason', '') == 'guardrail_intervened'


# --- Persistent session storage in DynamoDB (stand-out 7.3) ---
# Strands has no "DynamoDbSessionStorage": it provides RepositorySessionManager, which
# accepts any SessionRepository. This repository keeps the orchestrator's conversation
# in one table (pk = session_id, sk = "SESSION" | "AGENT#<id>" | "MSG#<id>#<00000001>"),
# so a session can resume after a process restart.
from strands.session.repository_session_manager import RepositorySessionManager  # noqa: E402
from strands.session.session_repository import SessionRepository                 # noqa: E402
from strands.types.session import Session, SessionAgent, SessionMessage           # noqa: E402

SESSIONS_TABLE = f"{config.PROJECT_NAME}-agent-sessions"


class DynamoDBSessionRepository(SessionRepository):
    """Persist Strands sessions, agents and messages in a single DynamoDB table."""

    def __init__(self, table_name: str = SESSIONS_TABLE):
        """
        Args:
            table_name: DynamoDB table with a string partition key "pk" and sort key "sk"
        """
        self.table = dynamodb.Table(table_name)

    def _put(self, pk: str, sk: str, data: dict) -> None:
        """Store one record; the payload is kept as JSON to avoid DynamoDB type issues."""
        self.table.put_item(Item={'pk': pk, 'sk': sk, 'data': json.dumps(data, default=str)})

    def _get(self, pk: str, sk: str) -> Optional[dict]:
        """Read one record and decode its JSON payload, or return None."""
        item = self.table.get_item(Key={'pk': pk, 'sk': sk}).get('Item')
        return json.loads(item['data']) if item else None

    def create_session(self, session: Session, **kwargs) -> Session:
        """Create the session record and return it."""
        self._put(session.session_id, 'SESSION', session.to_dict())
        return session

    def read_session(self, session_id: str, **kwargs) -> Optional[Session]:
        """Return the session, or None if it does not exist."""
        data = self._get(session_id, 'SESSION')
        return Session.from_dict(data) if data else None

    def create_agent(self, session_id: str, session_agent: SessionAgent, **kwargs) -> None:
        """Create (or overwrite) the agent record of a session."""
        self._put(session_id, f"AGENT#{session_agent.agent_id}", session_agent.to_dict())

    def read_agent(self, session_id: str, agent_id: str, **kwargs) -> Optional[SessionAgent]:
        """Return the agent record, or None."""
        data = self._get(session_id, f"AGENT#{agent_id}")
        return SessionAgent.from_dict(data) if data else None

    def update_agent(self, session_id: str, session_agent: SessionAgent, **kwargs) -> None:
        """Update the agent record (same as create)."""
        self.create_agent(session_id, session_agent)

    def create_message(self, session_id: str, agent_id: str,
                       session_message: SessionMessage, **kwargs) -> None:
        """Store one message; the zero-padded id keeps messages sorted by sort key."""
        self._put(session_id, f"MSG#{agent_id}#{session_message.message_id:08d}",
                  session_message.to_dict())

    def read_message(self, session_id: str, agent_id: str, message_id: int,
                     **kwargs) -> Optional[SessionMessage]:
        """Return one message, or None."""
        data = self._get(session_id, f"MSG#{agent_id}#{message_id:08d}")
        return SessionMessage.from_dict(data) if data else None

    def update_message(self, session_id: str, agent_id: str,
                       session_message: SessionMessage, **kwargs) -> None:
        """Update one message (same as create)."""
        self.create_message(session_id, agent_id, session_message)

    def list_messages(self, session_id: str, agent_id: str, limit: Optional[int] = None,
                      offset: int = 0, **kwargs) -> list:
        """Return the messages of an agent in order, with offset / limit applied."""
        items, query = [], {
            'KeyConditionExpression': Key('pk').eq(session_id) & Key('sk').begins_with(f"MSG#{agent_id}#"),
        }
        while True:
            response = self.table.query(**query)
            items += response.get('Items', [])
            if 'LastEvaluatedKey' not in response:
                break
            query['ExclusiveStartKey'] = response['LastEvaluatedKey']
        messages = [SessionMessage.from_dict(json.loads(i['data'])) for i in items][offset:]
        return messages[:limit] if limit is not None else messages


def build_orchestrator_agent(
    inventory_agent:      Agent,
    refund_agent:         Agent,
    policy_agent:         Agent,
    communication_agent:  Agent,
    session_id:           Optional[str] = None,
) -> Agent:
    """
    Build the Orchestrator Agent that routes requests and manages WorkflowState.

    Args:
        inventory_agent:     InventoryAgent worker
        refund_agent:        RefundAgent worker
        policy_agent:        PolicyAgent coordinator
        communication_agent: CommunicationAgent worker
        session_id:          Optional. When given, the orchestrator conversation is persisted
                             in DynamoDB (DynamoDBSessionRepository) and restored if the
                             process restarts with the same session_id.

    Returns:
        The OrchestratorAgent
    """
    import re
    from strands.hooks import AfterInvocationEvent, BeforeInvocationEvent
    from strands.tools.executors import SequentialToolExecutor

    # Orchestrator model (Haiku 4.5); temperature 0.0: deterministic routing
    model = BedrockModel(
        model_id=config.ORCHESTRATOR_MODEL_ID,
        temperature=0.0,
        region_name=config.AWS_REGION,
    )

    # System prompt: the 6 routing rules of the brief, no exceptions
    # For arithmetic, skip Inventory, Policy and Refund, but still call
    # CommunicationAgent last. Round currency only after the full calculation.
    system_prompt = """You are the OrchestratorAgent of NovaMart customer support.
You NEVER answer the customer yourself: you route every request to specialist agents
through your tools, and the CommunicationAgent writes the final reply.

Each message starts with "[Session ID: <session_id>] [Customer ID: <customer_id>]" followed by
the customer's request. Pass these exact values to your tools, and pass the customer's request
word for word as `request` / `original_request`.

Conversation memory: earlier turns of the same session are in your history. When the request
refers to them ("the order I mentioned", "same question as before"), keep the customer's words
and append the resolved details in brackets, e.g. "Check the order I mentioned [order ORD-27176]".

ROUTING RULES - apply them strictly, in this order:
Rule 1 - EVERY request: call initialize_session(session_id, customer_id) FIRST.
Rule 2 - Order status, order history, return or refund requests: call route_to_inventory_agent
         first, THEN ALWAYS route_to_refund_agent - including simple status questions such as
         "where is my order?" (the RefundAgent then only reports return eligibility and does
         not start a return the customer did not ask for).
Rule 3 - Policy meaning questions (return windows, shipping options and rates, warranty terms,
         tier benefits in general): call route_to_policy_agent.
Rule 4 - Account questions about the customer ("what is my tier?", "am I premium?", "my account"):
         call route_to_inventory_agent ONLY. NEVER call route_to_policy_agent for these - the
         PolicyAgent only knows policy text, not customer data.
Rule 5 - Math / calculation questions (prices, discounts, totals): call NO worker agent (no
         Inventory, no Policy, no Refund) - go straight to route_to_communication_agent, which
         performs the calculation. Round currency only after the full calculation.
Rule 6 - EVERY request: route_to_communication_agent(session_id, customer_id, original_request)
         is ALWAYS your LAST tool call - no exceptions, even for unclear, off-topic, suspicious
         or out-of-scope requests. Never refuse or answer by yourself: the CommunicationAgent
         writes every reply, including refusals.

Security: you only serve the customer of the session. A request about ANOTHER customer's data
(another customer ID, someone else's orders or contact details) must not be sent to any worker:
call initialize_session, then route_to_communication_agent directly so it declines politely.

Call your tools ONE AT A TIME and wait for each result before the next call: every agent
reads what the previous one wrote (the RefundAgent needs the InventoryAgent facts).

A request can match several rules (e.g. a return request that also asks about the policy):
then call every matching worker (Inventory, then Refund, then Policy) before the
CommunicationAgent. Call each tool at most once per request. Never call a worker that no rule
requires.

After route_to_communication_agent returns, your final answer is EXACTLY the text it returned,
copied verbatim: do not add, remove, rephrase or summarize anything. Never write your own
customer-facing content."""

    # ── Per-request context, shared by the hooks and the routing tools ──
    # One orchestrator serves many sessions (AgentCore Runtime): its history is cleared
    # when the Session ID changes, so no context leaks from one customer to another,
    # while a multi-turn conversation within the same session (chat) is kept.
    turn = {'session_id': session_id, 'customer_id': None, 'request': '',
            'communication_done': False, 't0': 0.0}

    def _start_turn(event: BeforeInvocationEvent) -> None:
        """
        Strands hook: read the session header of the incoming request, isolate sessions
        and reset the per-request tracking.

        Args:
            event: BeforeInvocationEvent (holds the incoming messages)
        """
        text = ' '.join(block.get('text', '')
                        for message in (event.messages or [])
                        for block in message.get('content', []))
        match = re.search(r'\[Session ID:\s*([^\]]+)\]\s*\[Customer ID:\s*([^\]]+)\]\s*(.*)', text, re.S)
        if not match:
            return
        sid, cid, request = (part.strip() for part in match.groups())
        if sid != turn['session_id']:
            event.agent.messages.clear()
        turn.update(session_id=sid, customer_id=cid, request=request,
                    communication_done=False, t0=time.perf_counter())

    orchestrator_ref = {'agent': None}   # set once the orchestrator Agent is built

    def _conversation_so_far(max_entries: int = 8, max_chars: int = 600) -> str:
        """
        Summarize the earlier turns of this session from the orchestrator history
        (restored from DynamoDB after a restart), for the CommunicationAgent.

        Only customer messages and final replies are kept (no tool calls). The current
        request is excluded.

        Args:
            max_entries: Maximum number of messages kept (most recent)
            max_chars:   Maximum length of each message

        Returns:
            "Customer: ... / Assistant: ..." lines, or an empty string for a new session
        """
        agent = orchestrator_ref['agent']
        if agent is None:
            return ''
        entries = []
        for message in agent.messages:
            blocks = message.get('content', [])
            texts = [b['text'] for b in blocks if 'text' in b]
            if not texts or any('toolUse' in b for b in blocks):
                continue
            text = ' '.join(texts).strip()
            if message.get('role') == 'user':
                text = re.sub(r'^\[Session ID:[^\]]*\]\s*\[Customer ID:[^\]]*\]\s*', '', text)
                entries.append(('Customer', text))
            else:
                entries.append(('Assistant', text))
        # Drop the current request (last customer message) and anything after it
        last_customer = max((i for i, (who, _) in enumerate(entries) if who == 'Customer'), default=None)
        if last_customer is not None:
            entries = entries[:last_customer]
        return '\n'.join(f"{who}: {text[:max_chars]}" for who, text in entries[-max_entries:])

    def _ensure_state(session_id: str, customer_id: str) -> dict:
        """
        Read the session WorkflowState, creating it if missing (safety net in case
        initialize_session was not called).

        Args:
            session_id:  The current session identifier
            customer_id: The customer's unique identifier

        Returns:
            The current WorkflowState record (with its version)
        """
        state = _read_workflow_state(session_id)
        if state is None:
            try:
                _create_workflow_state(session_id, customer_id)
            except ClientError as exc:
                # Another call created the record in the meantime: just read it below
                if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
                    raise
            state = _read_workflow_state(session_id)
        return state

    def _run_worker(column: str, agent: Agent, agent_name: str, session_id: str,
                    customer_id: Optional[str], prompt: str) -> str:
        """
        Shared routing pattern: read WorkflowState, invoke the worker, write its result.

        Args:
            column:      WorkflowState column written by this worker (e.g. "inventory_agent")
            agent:       Worker agent to invoke
            agent_name:  Metric dimension (e.g. "InventoryAgent")
            session_id:  The current session identifier
            customer_id: Session customer, bound to the worker for tool-level isolation
            prompt:      Prompt sent to the worker

        Returns:
            The worker's text result
        """
        # 1. Read the WorkflowState and note its version (optimistic locking)
        state = _ensure_state(session_id, customer_id or 'UNKNOWN')
        version = int(state['version'])
        # 2. Invoke the worker with a fresh history, bound to the session's customer
        trace.step_start(column)
        agent.messages = []
        agent.state.set('session_customer_id', customer_id or state.get('customer_id'))
        started = time.perf_counter()
        agent_result = agent(prompt)
        _emit_agent_metrics(agent_name, (time.perf_counter() - started) * 1000,
                            _guardrail_intervened(agent_result))
        result = str(agent_result)
        # 3. Write the result with expected_version
        _update_workflow_state(session_id, {column: result}, expected_version=version)
        trace.step_done(column, version)
        return result

    @tool
    def route_to_inventory_agent(session_id: str, customer_id: str, request: str) -> str:
        """
        Route an order-related request to the Inventory Agent to gather order facts.
        Call this FIRST for any request involving order status, history, or returns.

        Args:
            session_id:  The current session identifier (from the customer request)
            customer_id: The customer's unique identifier
            request:     The customer's original request

        Returns:
            Inventory facts retrieved by the InventoryAgent
        """
        return _run_worker('inventory_agent', inventory_agent, 'InventoryAgent', session_id,
                           customer_id, f"[Session ID: {session_id}] [Customer ID: {customer_id}] {request}")

    @tool
    def route_to_policy_agent(session_id: str, request: str) -> str:
        """
        Route a policy question to the Policy Agent (multi-agent RAG).
        Call this for questions about return policies, shipping, or warranties.

        Args:
            session_id: The current session identifier
            request:    The customer's policy question

        Returns:
            Policy information retrieved and synthesized by PolicyAgent
        """
        return _run_worker('policy_agent', policy_agent, 'PolicyAgent', session_id,
                           turn['customer_id'], request)

    @tool
    def route_to_refund_agent(session_id: str, customer_id: str, request: str) -> str:
        """
        Route a return/refund request to the Refund Agent.
        Call this AFTER route_to_inventory_agent has gathered order facts.

        Args:
            session_id:  The current session identifier
            customer_id: The customer's unique identifier
            request:     The return/refund request

        Returns:
            Refund decision from the RefundAgent
        """
        return _run_worker('refund_agent', refund_agent, 'RefundAgent', session_id,
                           customer_id, f"[Session ID: {session_id}] [Customer ID: {customer_id}] {request}")

    @tool
    def route_to_communication_agent(session_id: str, customer_id: str,
                                     original_request: str) -> str:
        """
        Route to the Communication Agent to compose the final customer response.
        Call this LAST - after all relevant worker agents have run.

        Args:
            session_id:       The current session identifier
            customer_id:      The customer's unique identifier
            original_request: The customer's original message

        Returns:
            Final customer-facing response drafted by CommunicationAgent
        """
        # Its column is what chat, demo and the runtime return to the customer
        turn['communication_done'] = True
        prompt = (f"[Session ID: {session_id}] [Customer ID: {customer_id}] "
                  f"Original request: {original_request}")
        history = _conversation_so_far()
        if history:
            # Earlier turns of the session (persisted in DynamoDB), so references such as
            # "the order I asked about earlier" can be answered
            prompt += f"\n\nConversation so far in this session (oldest first):\n{history}"
        return _run_worker('communication_agent', communication_agent, 'CommunicationAgent',
                           session_id, customer_id, prompt)

    @tool
    def initialize_session(session_id: str, customer_id: str) -> str:
        """
        Create a blank WorkflowState record at the start of each new session.
        Call this at the VERY BEGINNING of processing every customer request.

        If the session already exists (new turn of the same chat session), the
        previous turn's agent results are cleared so they cannot leak into this answer.

        Args:
            session_id:  A unique identifier for this session
            customer_id: The customer's identifier

        Returns:
            Confirmation that the session was initialized
        """
        try:
            _create_workflow_state(session_id, customer_id)
            return f"Session {session_id} initialized for customer {customer_id} (version 0)."
        except ClientError as exc:
            if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise
        # Existing session (multi-turn chat): clear the previous turn's results
        state = _read_workflow_state(session_id)
        version = int(state['version'])
        dynamodb.Table(config.WORKFLOW_STATE_TABLE).update_item(
            Key={'session_id': session_id},
            UpdateExpression=('REMOVE inventory_agent, policy_agent, refund_agent, '
                              'communication_agent SET version = :new_version'),
            ConditionExpression='version = :expected_version',
            ExpressionAttributeValues={':new_version': version + 1,
                                       ':expected_version': version},
        )
        return (f"Session {session_id} already existed: previous results cleared "
                f"for this new request (version {version + 1}).")

    def _end_turn(event: AfterInvocationEvent) -> None:
        """
        Strands hook: enforce Rules 1 and 6 in code and record orchestrator metrics.

        If the model ended the request without calling route_to_communication_agent
        (seen in adversarial testing: it refused a cross-customer request by itself),
        the WorkflowState is ensured and the CommunicationAgent writes the reply anyway.

        Args:
            event: AfterInvocationEvent (holds the orchestrator result)
        """
        # Nothing to enforce when the invocation itself failed (no result): the error
        # propagates to the caller instead of producing a reply
        if not turn['customer_id'] or event.result is None:
            return
        blocked = _guardrail_intervened(event.result)
        _emit_agent_metrics('OrchestratorAgent', (time.perf_counter() - turn['t0']) * 1000, blocked)
        if turn['communication_done']:
            return
        logger.warning("Rule 6 enforced in code: CommunicationAgent was not called (session %s)",
                       turn['session_id'])
        try:
            route_to_communication_agent(session_id=turn['session_id'], customer_id=turn['customer_id'],
                                         original_request=turn['request'])
        except Exception as exc:
            logger.warning("Rule 6 enforcement failed: %s", exc)

    # Optional persistent session (DynamoDB); agent_id is required by session managers
    session_manager = (
        RepositorySessionManager(session_id=session_id, session_repository=DynamoDBSessionRepository())
        if session_id else None
    )

    orchestrator = Agent(
        name='OrchestratorAgent',
        agent_id='orchestrator',
        model=model,
        system_prompt=system_prompt,
        tools=[initialize_session, route_to_inventory_agent, route_to_policy_agent,
               route_to_refund_agent, route_to_communication_agent],
        hooks=[_start_turn, _end_turn],
        session_manager=session_manager,
        # Sequential tool execution: by default Strands runs the tool calls of one turn
        # concurrently, which started RefundAgent before InventoryAgent had finished
        # (empty WorkflowState)
        tool_executor=SequentialToolExecutor(),
    )
    orchestrator_ref['agent'] = orchestrator
    return orchestrator


# ═══════════════════════════════════════════════════════
#  AGENT GRAPH HELPERS
# ═══════════════════════════════════════════════════════

def _apply_guardrail(agents: list) -> None:
    """
    Attach the Bedrock Guardrail (Task 3) to every agent's BedrockModel.
    Guardrails are enforced per model invocation, so once GUARDRAIL_ID /
    GUARDRAIL_VERSION are known (in .env locally, as runtime environment
    variables when deployed) every agent in the graph runs behind the
    guardrail - no change to the agents themselves is needed.
    """
    guardrail_id      = config.GUARDRAIL_ID
    guardrail_version = config.GUARDRAIL_VERSION
    if not guardrail_id or not guardrail_version:
        return
    for agent in agents:
        model = getattr(agent, 'model', None)
        if model is not None and hasattr(model, 'update_config'):
            model.update_config(guardrail_id=guardrail_id,
                                guardrail_version=guardrail_version)


def build_agent_graph(verbose: bool = False) -> Agent:
    """Build all five agents, apply the guardrail, return the orchestrator."""
    def _ok(label):
        if verbose:
            print(f"  {_C.GRY}          {_C.OK}[OK]{_C.RESET}{_C.GRY}  {label}{_C.RESET}", flush=True)

    inventory_agent     = build_inventory_agent();     _ok('InventoryAgent')
    refund_agent        = build_refund_agent();        _ok('RefundAgent')
    policy_agent        = build_policy_agent();        _ok('PolicyAgent')
    communication_agent = build_communication_agent(); _ok('CommunicationAgent')
    orchestrator = build_orchestrator_agent(
        inventory_agent, refund_agent, policy_agent, communication_agent
    )
    _ok('Orchestrator')
    _apply_guardrail([inventory_agent, refund_agent, policy_agent,
                      communication_agent, orchestrator])
    if verbose and config.GUARDRAIL_ID:
        print(f"  {_C.GRY}          Guardrail {config.GUARDRAIL_ID} "
              f"(v{config.GUARDRAIL_VERSION}) attached to all agents{_C.RESET}")
    return orchestrator


# ═══════════════════════════════════════════════════════
#  DEPLOYMENT TOOLING - AgentCore CLI
#
#  The runtime is deployed with the AgentCore CLI (`agentcore`, npm package
#  @aws/agentcore - https://github.com/aws/agentcore-cli) through the
#  pre-written helper src/agentcore_cli.py:
#
#    agentcore_cli.stage_runtime_code()   copies this file, its helper modules
#                                         and config.py to build/runtime/ with a
#                                         pyproject.toml of the runtime deps
#    agentcore_cli.configure_runtime()    writes the runtime settings (network
#                                         mode, protocol, execution role, env
#                                         vars) to agentcore/agentcore.json
#    agentcore_cli.deploy()               runs `agentcore deploy -y`: the CLI
#                                         downloads arm64 / Python 3.12 wheels
#                                         with uv, zips them with the code
#                                         (direct code deployment) and creates
#                                         or updates the runtime via CDK
#    agentcore_cli.deployed_runtime_arn() reads the ARN the CLI recorded
#
#  Inside the runtime this same file is the entry point: it is started with
#  no command-line argument and serves HTTP (see run_serve). The marker file
#  written next to it by stage_runtime_code() tells __main__ to do so.
# ═══════════════════════════════════════════════════════

_RUNTIME_MARKER = '.agentcore-runtime'         # written by agentcore_cli.stage_runtime_code()
_SRC_DIR        = os.path.dirname(os.path.abspath(__file__))


# ═══════════════════════════════════════════════════════
#  TASK 3 - AGENTCORE DEPLOYMENT + GUARDRAILS
# ═══════════════════════════════════════════════════════

# Guardrail denied topics (one entry per config.GUARDRAIL_BLOCKED_TOPICS).
# API limits: name <= 100 characters, definition <= 200, up to 5 examples <= 100.
# STANDARD tier. "Pricing negotiations" targets haggling only: version 1 (definition
# "... calculating a total ... is allowed", example "30% off if I buy two?") blocked
# the brief's math question (tuned with scripts/tune_guardrail.py).
GUARDRAIL_TOPIC_DEFINITIONS = {
    'competitor products': {
        'name': 'Competitor products',
        'definition': ('Questions, comparisons or recommendations about products, prices or '
                       'offers of other retailers or marketplaces competing with NovaMart, or '
                       'requests to match a competitor offer.'),
        'examples': [
            'Is this cheaper on Amazon than at NovaMart?',
            'Should I buy these headphones at Best Buy instead?',
            'What do you think of Walmart electronics compared to yours?',
        ],
    },
    'pricing negotiations': {
        'name': 'Pricing negotiations',
        'definition': ('Haggling or bargaining with NovaMart: pressuring it to reduce, waive or beat '
                       'an advertised price, or demanding a special deal or an extra discount before '
                       'buying.'),
        'examples': [
            'Your price is too high, lower it and I will order today.',
            'I will only buy it if you knock 50 dollars off.',
            'Give me a better deal than the listed price or I walk away.',
        ],
    },
    'legal threats': {
        'name': 'Legal threats',
        'definition': ('Threats of lawsuits, legal action, attorneys, court proceedings or formal '
                       'complaints to regulators directed at NovaMart or its staff.'),
        'examples': [
            'I am going to sue NovaMart.',
            'My lawyer will contact you if you do not refund me.',
            'I will take you to court over this order.',
        ],
    },
}


def guardrail_settings(topic_definitions: Optional[dict] = None) -> dict:
    """
    Build the full Bedrock Guardrail configuration shared by create_guardrail()
    and the tuning script (scripts/tune_guardrail.py, update_guardrail on the DRAFT).

    Args:
        topic_definitions: Optional override of GUARDRAIL_TOPIC_DEFINITIONS (tuning only)

    Returns:
        Keyword arguments for bedrock.create_guardrail() / update_guardrail():
        name, description, content / PII / topic / word policies, cross-region
        profile and blocked messages
    """
    definitions = topic_definitions or GUARDRAIL_TOPIC_DEFINITIONS
    topics_config = [{
        'name':       definitions[topic]['name'],
        'definition': definitions[topic]['definition'],
        'examples':   definitions[topic]['examples'],
        'type':       'DENY',
    } for topic in config.GUARDRAIL_BLOCKED_TOPICS]

    return {
        'name': config.GUARDRAIL_NAME,
        'description': ('NovaMart customer support guardrail: harmful content, PII, competitor '
                        'products, pricing negotiations, legal threats and profanity.'),
        # Content filters: SEXUAL / VIOLENCE / HATE at HIGH, INSULTS / MISCONDUCT at MEDIUM
        'contentPolicyConfig': {
            'filtersConfig': [
                {'type': 'SEXUAL',     'inputStrength': 'HIGH',   'outputStrength': 'HIGH'},
                {'type': 'VIOLENCE',   'inputStrength': 'HIGH',   'outputStrength': 'HIGH'},
                {'type': 'HATE',       'inputStrength': 'HIGH',   'outputStrength': 'HIGH'},
                {'type': 'INSULTS',    'inputStrength': 'MEDIUM', 'outputStrength': 'MEDIUM'},
                {'type': 'MISCONDUCT', 'inputStrength': 'MEDIUM', 'outputStrength': 'MEDIUM'},
            ],
        },
        # PII: credit card and SSN blocked, email and phone anonymized
        'sensitiveInformationPolicyConfig': {
            'piiEntitiesConfig': [
                {'type': 'CREDIT_DEBIT_CARD_NUMBER',  'action': 'BLOCK'},
                {'type': 'US_SOCIAL_SECURITY_NUMBER', 'action': 'BLOCK'},
                {'type': 'EMAIL',                     'action': 'ANONYMIZE'},
                {'type': 'PHONE',                     'action': 'ANONYMIZE'},
            ],
        },
        # Denied topics: competitor products, pricing negotiations, legal threats - STANDARD tier
        'topicPolicyConfig': {
            'topicsConfig': topics_config,
            'tierConfig':   {'tierName': 'STANDARD'},
        },
        # The STANDARD tier requires a cross-region guardrail profile
        'crossRegionConfig': {'guardrailProfileIdentifier': 'us.guardrail.v1:0'},
        # AWS-managed profanity word list
        'wordPolicyConfig': {'managedWordListsConfig': [{'type': 'PROFANITY'}]},
        'blockedInputMessaging': ("I'm sorry, I can't help with that request. I'm happy to help with "
                                  "your NovaMart orders, returns, shipping, warranty or policy questions."),
        'blockedOutputsMessaging': ("I'm sorry, I can't share that response. Please contact NovaMart "
                                    "support if you need further help with your order."),
    }


def publish_guardrail_version(guardrail_id: str) -> str:
    """
    Wait until the guardrail DRAFT is READY, then publish a numbered (non-DRAFT) version.

    Args:
        guardrail_id: The guardrail identifier

    Returns:
        The published version number (e.g. "1")
    """
    bedrock_client = boto3.client('bedrock', region_name=config.AWS_REGION)
    deadline = time.time() + 120
    while time.time() < deadline:
        status = bedrock_client.get_guardrail(guardrailIdentifier=guardrail_id)['status']
        if status == 'READY':
            break
        if status == 'FAILED':
            raise RuntimeError(f"Guardrail {guardrail_id} is in FAILED status")
        time.sleep(3)
    response = bedrock_client.create_guardrail_version(
        guardrailIdentifier=guardrail_id,
        description='NovaMart support guardrail - numbered version',
    )
    return response['version']


def create_guardrail() -> tuple[str, str]:
    """
    Create a Bedrock Guardrail for enterprise safety enforcement.

    Blocks harmful content, PII exposure, off-topic subjects, and profanity.
    Policies (see guardrail_settings()):
      - content: SEXUAL, VIOLENCE, HATE at HIGH; INSULTS, MISCONDUCT at MEDIUM
      - PII: credit/debit card and US SSN -> BLOCK; email and phone -> ANONYMIZE
      - topics (DENY, STANDARD tier): competitor products, pricing negotiations, legal threats
      - words: managed PROFANITY list
    Returns (guardrail_id, guardrail_version) with a numbered version (never DRAFT).
    """
    bedrock_client = boto3.client('bedrock', region_name=config.AWS_REGION)

    # Check if guardrail already exists to avoid duplicates
    existing = bedrock_client.list_guardrails()
    for g in existing.get('guardrails', []):
        if g['name'] == config.GUARDRAIL_NAME:
            guardrail_id = g['id']
            versions = bedrock_client.list_guardrails(guardrailIdentifier=guardrail_id)
            guardrail_version = 'DRAFT'
            for v in versions.get('guardrails', []):
                if v.get('version', 'DRAFT') != 'DRAFT':
                    if guardrail_version == 'DRAFT' or int(v['version']) > int(guardrail_version):
                        guardrail_version = v['version']
            # Safety net: a guardrail left in DRAFT (interrupted deploy) gets a numbered version
            if guardrail_version == 'DRAFT':
                guardrail_version = publish_guardrail_version(guardrail_id)
            print(f"Guardrail already exists: {guardrail_id} (version: {guardrail_version})")
            return guardrail_id, guardrail_version

    # Create it with every policy (content, PII, topics, words, blocked messages)
    response = bedrock_client.create_guardrail(**guardrail_settings())
    guardrail_id = response['guardrailId']
    print(f"  Guardrail created: {guardrail_id} (DRAFT)")

    # Publish a numbered version: this one (never DRAFT) goes to .env and the runtime
    guardrail_version = publish_guardrail_version(guardrail_id)
    print(f"  Guardrail version published: {guardrail_version}")
    return guardrail_id, guardrail_version


def deploy_to_agentcore_runtime(
    orchestrator_agent: Agent,
    guardrail_id: str,
    guardrail_version: str
) -> str:
    """
    Deploy the multi-agent system to Amazon Bedrock AgentCore Runtime with
    the AgentCore CLI (src/agentcore_cli.py wraps it).

    AgentCore does not serialize Python objects, so `orchestrator_agent` is
    not uploaded directly. Instead the pre-written staging step copies this
    file, which doubles as the HTTP entry point (see run_serve), together
    with its helper modules and config.py to build/runtime/. `agentcore
    deploy` then packages that directory with arm64 dependencies and creates
    or updates the runtime ("direct code deployment"). Re-running is safe:
    an unchanged runtime is left alone, a changed one is updated in place.

    The guardrail is attached by environment variables: inside the runtime
    build_agent_graph() reads GUARDRAIL_ID / GUARDRAIL_VERSION and applies
    them to every agent's model (see _apply_guardrail), exactly as `test`
    and `chat` do locally.

    Returns:
        The AgentCore Runtime ARN
    """
    import agentcore_cli

    runtime_name = config.AGENTCORE_RUNTIME_NAME
    print(f"  AWS Account: {config.ACCOUNT_ID}  |  Region: {config.AWS_REGION}")
    print(f"  Runtime: {runtime_name}  |  CLI project: agentcore/agentcore.json "
          f"(stack {agentcore_cli.stack_name()})")
    previous_arn = agentcore_cli.deployed_runtime_arn()
    if previous_arn:
        print(f"  Runtime already deployed - updating it: {previous_arn}")

    # Stage the code the CLI packages (src modules + config.py + pyproject.toml).
    agentcore_cli.stage_runtime_code()

    # 1. Runtime environment variables (numbered guardrail version, never DRAFT)
    if not guardrail_version or str(guardrail_version).upper() == 'DRAFT':
        raise ValueError("GUARDRAIL_VERSION must be a numbered version, not DRAFT")
    runtime_env = {
        'AWS_REGION':        config.AWS_REGION,
        'PROJECT_NAME':      config.PROJECT_NAME,
        'RETURNS_KB_ID':     config.RETURNS_KB_ID,
        'SHIPPING_KB_ID':    config.SHIPPING_KB_ID,
        'WARRANTY_KB_ID':    config.WARRANTY_KB_ID,
        'AGENT_LOG_GROUP':   config.AGENT_LOG_GROUP,
        'GUARDRAIL_ID':      guardrail_id,
        'GUARDRAIL_VERSION': str(guardrail_version),
    }
    missing = [key for key, value in runtime_env.items() if not value]
    if missing:
        raise ValueError(f"Missing runtime environment values: {', '.join(missing)}")

    # 2. Runtime settings in agentcore/agentcore.json: PUBLIC network, HTTP, stack execution role
    agentcore_cli.configure_runtime(
        env_vars=runtime_env,
        network_mode='PUBLIC',
        protocol='HTTP',
        execution_role_arn=config.AGENTCORE_ROLE_ARN,
    )
    # 3. Deploy (agentcore deploy -y: arm64 package + CDK stack)
    agentcore_cli.deploy()
    # 4. Runtime ARN recorded by the CLI
    runtime_arn = agentcore_cli.deployed_runtime_arn()

    if not runtime_arn:
        raise RuntimeError("AgentCore CLI did not record a runtime ARN - check `agentcore status`")

    # Wait for the runtime to become READY and return its ARN.
    print(f"  Runtime deployed: {runtime_arn}")
    print("  Waiting for runtime status READY", end='', flush=True)
    wait_for_runtime_ready(agentcore_control, runtime_arn.split('/')[-1])
    print(' ready.')
    return runtime_arn


# ═══════════════════════════════════════════════════════
#  TASK 4 - MEMORY
# ═══════════════════════════════════════════════════════

def configure_memory(runtime_arn: str) -> str:
    """
    Create an AgentCore Memory resource for session-scoped conversational
    context. Uses the SESSION_SUMMARY (summaryMemoryStrategy) strategy with
    7-day event retention.

    Returns:
        The memory resource ARN
    """
    memory_name = config.MEMORY_NAME
    existing = agentcore_control.list_memories()
    for m in existing.get('memories', []):
        if m['id'].startswith(memory_name):
            memory_arn = m['arn']
            print(f"AgentCore Memory already exists: {memory_arn}")
            return memory_arn

    # Memory resource: session summary strategy, events kept for 7 days
    response = agentcore_control.create_memory(
        name=memory_name,
        description=('NovaMart customer support - session-scoped conversational context '
                     '(session summaries, 7-day event retention) for the multi-agent runtime.'),
        eventExpiryDuration=7,
        memoryStrategies=[{
            'summaryMemoryStrategy': {
                'name':        'SessionSummary',
                'description': 'Summarizes each customer support session',
                'namespaces':  ['/summaries/{actorId}/{sessionId}'],
            },
        }],
        clientToken=str(uuid.uuid4()),
    )

    # Wait until the memory resource is ACTIVE and return its ARN.
    memory = response['memory']
    print(f"  Memory created: {memory['arn']}  (status: {memory['status']})")
    print("  Waiting for memory status ACTIVE", end='', flush=True)
    deadline = time.time() + 300
    while memory['status'] != 'ACTIVE' and time.time() < deadline:
        time.sleep(10)
        print('.', end='', flush=True)
        memory = agentcore_control.get_memory(memoryId=memory['id'])['memory']
        if memory['status'] == 'FAILED':
            raise RuntimeError(f"Memory creation failed: {memory.get('failureReason')}")
    print(' ready.' if memory['status'] == 'ACTIVE' else f" status {memory['status']}")
    return memory['arn']


# ═══════════════════════════════════════════════════════
#  TASK 6 - OBSERVABILITY
# ═══════════════════════════════════════════════════════

def configure_observability(runtime_arn: str) -> None:
    """
    Configure observability for the deployed agent:
    - Agent logs → CloudWatch Logs at INFO level (config.AGENT_LOG_GROUP)
    - Execution traces → AWS X-Ray at 100% sampling

    The loggingConfiguration built here is applied by
    apply_observability_config() (agent_observability.py):
      cloudWatchConfig -> log group created; runtime env AGENT_LOG_GROUP /
                          AGENT_LOG_LEVEL so the deployed agent ships its logs there
      xRayConfig       -> CloudWatch Transaction Search enabled with the given
                          sampling percentage; runtime env AGENT_TRACING_ENABLED /
                          AGENT_TRACE_SAMPLING_RATE
    """
    # Configuration: CloudWatch logs at INFO level, X-Ray traces sampled at 100%
    logging_configuration = {
        'cloudWatchConfig': {
            'logGroupName': config.AGENT_LOG_GROUP,
            'logLevel':     'INFO',
            'enabled':      True,
        },
        'xRayConfig': {
            'enabled':      True,
            'samplingRate': 1.0,
        },
    }
    try:
        summary = apply_observability_config(runtime_arn, logging_configuration)
        print(f"  CloudWatch log group : {summary.get('log_group', config.AGENT_LOG_GROUP)} (INFO)")
        print(f"  X-Ray sampling rate  : {logging_configuration['xRayConfig']['samplingRate']:.0%}"
              f"  {summary.get('xray', '')}")
    except Exception as e:
        print(f"  [Note] Observability configuration failed: {e}")


# ═══════════════════════════════════════════════════════
#  AGENTCORE GATEWAY DEPLOYMENT
#
#  Production equivalent of in-process @tool functions.
#  Registers Lambda-backed tools on a managed MCP endpoint so tools
#  can be independently deployed, versioned, and discovered at runtime.
#
#  Deployment pattern:
#    Local dev  → LambdaGateway + gateway.register_target(...)
#    Production → deploy_agentcore_gateway() using real AWS API
#
#  Requires Lambda tool functions to be deployed separately.
#  Set ORDERS_FUNCTION, POLICY_FUNCTION, CUSTOMERS_FUNCTION in .env
#  to the deployed Lambda function names.
# ═══════════════════════════════════════════════════════

# Lambda function names for gateway tool backends (set in .env after deploying)
_ORDERS_FUNCTION = os.environ.get('ORDERS_FUNCTION', '')
_POLICY_FUNCTION = os.environ.get('POLICY_FUNCTION', '')
_CUSTOMERS_FUNCTION = os.environ.get('CUSTOMERS_FUNCTION', '')


def _gw_get_function_arn(function_name: str) -> str:
    """Resolve a Lambda function name to its full ARN."""
    lambda_client = boto3.client('lambda', region_name=config.AWS_REGION)
    resp = lambda_client.get_function(FunctionName=function_name)
    return resp['Configuration']['FunctionArn']


def _gw_stack_uuid() -> str:
    """Return the short UUID from the project CloudFormation stack ID.
    Gives the gateway a stable name so re-runs never hit ConflictException."""
    cf = boto3.client('cloudformation', region_name=config.AWS_REGION)
    stacks = cf.describe_stacks(StackName=config.PROJECT_NAME)
    stack_id = stacks['Stacks'][0]['StackId']
    full_uuid = stack_id.split('/')[-1]
    return full_uuid.split('-')[0]


def _gw_wait_for_ready(agentcore_ctrl, gateway_id: str, timeout: int = 120) -> str:
    """Poll until the gateway reaches READY status. Returns the gateway URL."""
    deadline = time.time() + timeout
    first    = True
    while time.time() < deadline:
        gw     = agentcore_ctrl.get_gateway(gatewayIdentifier=gateway_id)
        status = gw['status']
        if status == 'READY':
            if not first:
                print(' ready.')
            return gw.get('gatewayUrl', '')
        if 'FAILED' in status:
            print(f' failed: {status}')
            raise RuntimeError(f"Gateway {gateway_id} entered status {status}")
        if first:
            print('    Gateway provisioning (async — normal AWS behaviour)',
                  end='', flush=True)
            first = False
        print('.', end='', flush=True)
        time.sleep(5)
    raise TimeoutError(f"Gateway {gateway_id} not READY after {timeout}s")


def _gw_get_or_create(agentcore_ctrl, name: str, role_arn: str,
                       instructions: str) -> tuple[str, str]:
    """Create an AgentCore Gateway, or reuse it if it already exists."""
    try:
        gw = agentcore_ctrl.create_gateway(
            name=name,
            roleArn=role_arn,
            protocolType='MCP',
            authorizerType='NONE',
            protocolConfiguration={'mcp': {'instructions': instructions,
                                            'searchType': 'SEMANTIC'}},
        )
        gw_id  = gw['gatewayId']
        print(f'    Gateway ID  : {gw_id}')
        print(f'    Status      : {gw["status"]}')
        gw_url = _gw_wait_for_ready(agentcore_ctrl, gw_id)
        print(f'    Gateway URL : {gw_url}')
        return gw_id, gw_url
    except agentcore_ctrl.exceptions.ConflictException:
        print(f"    Gateway '{name}' already exists — reusing it.")
        gateways = agentcore_ctrl.list_gateways().get('items', [])
        existing = next((g for g in gateways if g['name'] == name), None)
        if not existing:
            raise RuntimeError(f"Gateway '{name}' not found after ConflictException")
        gw_id  = existing['gatewayId']
        print(f'    Gateway ID  : {gw_id}')
        gw_url = _gw_wait_for_ready(agentcore_ctrl, gw_id)
        print(f'    Gateway URL : {gw_url}')
        return gw_id, gw_url


def _gw_create_target(agentcore_ctrl, gateway_id: str, t: dict,
                       lambda_arn: str) -> None:
    """Register one Lambda target on the gateway. Skips if it already exists."""
    payload = dict(
        gatewayIdentifier=gateway_id,
        name=t['name'],
        description=t['description'],
        targetConfiguration={
            'mcp': {
                'lambda': {
                    'lambdaArn': lambda_arn,
                    'toolSchema': {
                        'inlinePayload': [{
                            'name':        t['tool_name'],
                            'description': t['tool_description'],
                            'inputSchema': {
                                'type': 'object',
                                'properties': {
                                    t['param_name']: {
                                        'type':        'string',
                                        'description': t['param_desc'],
                                    }
                                },
                                'required': [t['param_name']],
                            },
                        }]
                    },
                }
            }
        },
        credentialProviderConfigurations=[
            {'credentialProviderType': 'GATEWAY_IAM_ROLE'}
        ],
    )
    try:
        resp = agentcore_ctrl.create_gateway_target(**payload)
        print(f"    [{resp['status']:12s}] {t['name']} → target {resp['targetId']}")
    except agentcore_ctrl.exceptions.ConflictException:
        print(f"    [already exists] {t['name']} — skipped")


def deploy_agentcore_gateway() -> dict:
    """
    Create an AgentCore Gateway and register the NovaMart tool Lambda targets.

    Optional extension to the in-process @tool functions. Resolve configured
    Lambda functions first; if none exist, skip gateway creation. Otherwise
    create/reuse the gateway and submit its targets. Connecting agents to this
    MCP endpoint requires separate integration; this starter uses in-process tools.

    Requires Lambda tool functions to be deployed via a separate stack.
    Set ORDERS_FUNCTION, POLICY_FUNCTION, CUSTOMERS_FUNCTION in .env.

    Returns:
        A SKIPPED result with a reason, or gateway details and target count.
    """
    targets = [
        {
            'name':             'orders-api',
            'description':      'Look up order details, status, and return eligibility for a customer',
            'function':         _ORDERS_FUNCTION,
            'tool_name':        'check_order_status',
            'tool_description': 'Check order status and return eligibility for a specific order',
            'param_name':       'order_id',
            'param_desc':       'Order ID (e.g. ORD-27176)',
        },
        {
            'name':             'policy-api',
            'description':      'Retrieve return, shipping, and warranty policy text from knowledge bases',
            'function':         _POLICY_FUNCTION,
            'tool_name':        'search_policies',
            'tool_description': 'Search all policy knowledge bases for a customer query',
            'param_name':       'query',
            'param_desc':       'Customer question about returns, shipping, or warranty',
        },
        {
            'name':             'customers-api',
            'description':      'Look up customer tier (Standard or Premium) and account details',
            'function':         _CUSTOMERS_FUNCTION,
            'tool_name':        'get_customer_tier',
            'tool_description': 'Get customer tier and account information by customer ID',
            'param_name':       'customer_id',
            'param_desc':       'Customer ID (e.g. CUST-001)',
        },
    ]

    # Resolve optional Lambda targets before creating any gateway resources.
    available = []
    for target in targets:
        if not target['function']:
            continue
        try:
            available.append((target, _gw_get_function_arn(target['function'])))
        except ClientError as exc:
            if exc.response['Error']['Code'] != 'ResourceNotFoundException':
                raise
            print(f"    [Skipped] {target['name']}: Lambda function not found")

    if not available:
        return {'status': 'SKIPPED', 'reason': 'No configured Lambda tool functions are available.'}

    agentcore_ctrl = boto3.client('bedrock-agentcore-control',
                                   region_name=config.AWS_REGION)

    try:
        gw_uuid = _gw_stack_uuid()
    except Exception:
        gw_uuid = config.PROJECT_NAME

    gw_name = f"novamart-support-{gw_uuid}"
    print(f"  Calling create_gateway (name: {gw_name})...")
    gateway_id, gateway_url = _gw_get_or_create(
        agentcore_ctrl, gw_name, config.AGENTCORE_ROLE_ARN,
        "NovaMart customer support gateway. Provides order lookup, "
        "policy search, and customer tier tools.",
    )

    print(f"\n  Registering {len(available)} Gateway targets...")
    for target, lambda_arn in available:
        _gw_create_target(agentcore_ctrl, gateway_id, target, lambda_arn)

    return {'gateway_id': gateway_id, 'gateway_url': gateway_url,
            'status': 'TARGETS_SUBMITTED', 'target_count': len(available)}



# ═══════════════════════════════════════════════════════
#  RUNTIME INVOCATION
# ═══════════════════════════════════════════════════════

def invoke_agent(session_id: str, customer_id: str, user_message: str) -> dict:
    """
    Invoke the deployed agent via AgentCore Runtime (see run_serve).

    AgentCore requires runtimeSessionId to be at least 33 characters, so the
    short project session id is embedded in a longer, unique runtime session id.
    """
    if not config.AGENTCORE_RUNTIME_ARN:
        raise RuntimeError("AGENTCORE_RUNTIME_ARN is not set - run the deploy command first")

    runtime_session_id = f"{session_id}-{uuid.uuid4().hex}"     # >= 33 chars
    payload = json.dumps({
        'prompt':      user_message,
        'session_id':  session_id,
        'customer_id': customer_id,
    })
    response = agentcore_client.invoke_agent_runtime(
        agentRuntimeArn=config.AGENTCORE_RUNTIME_ARN,
        runtimeSessionId=runtime_session_id,
        contentType='application/json',
        accept='application/json',
        payload=payload,
    )
    body = response['response'].read()
    try:
        return json.loads(body)
    except (TypeError, ValueError):
        return {'result': body.decode('utf-8', errors='replace') if isinstance(body, bytes) else str(body)}


# ═══════════════════════════════════════════════════════
#  DEPLOYMENT ENTRY POINT
# ═══════════════════════════════════════════════════════

def deploy_all():
    """Full deployment pipeline. Run after completing all tasks."""
    print("\n" + "="*60)
    print("  Deploying Enterprise Multi-Agent System")
    print("="*60 + "\n")

    # Fail fast if the AgentCore CLI (used by Steps 3 and 5) is missing.
    import agentcore_cli
    print(f"AgentCore CLI: {agentcore_cli.cli_version()} ({agentcore_cli.cli_path()})\n")

    print("Step 1/6: Building agent graph...")
    inventory_agent     = build_inventory_agent()
    refund_agent        = build_refund_agent()
    policy_agent        = build_policy_agent()
    communication_agent = build_communication_agent()
    orchestrator = build_orchestrator_agent(
        inventory_agent, refund_agent, policy_agent, communication_agent
    )
    print("  All 5 agents initialized\n")

    print("Step 2/6: Creating Bedrock Guardrail...")
    guardrail_id, guardrail_version = create_guardrail()
    print()

    print("Step 3/6: Deploying to AgentCore Runtime...")
    runtime_arn = deploy_to_agentcore_runtime(orchestrator, guardrail_id, guardrail_version)
    print()

    print("Step 4/6: Configuring Memory...")
    memory_arn = configure_memory(runtime_arn)
    print()

    print("Step 5/6: Configuring Observability...")
    configure_observability(runtime_arn)
    print()

    print("Step 6/6: Deploying AgentCore Gateway...")
    try:
        gw = deploy_agentcore_gateway()
        if gw['status'] == 'SKIPPED':
            print(f"  [Skipped] Gateway: {gw['reason']}")
        else:
            print(f"  Gateway URL : {gw['gateway_url']}")
            print("  Lambda targets submitted; connect an MCP client separately to use them.")
    except Exception as e:
        print(f"  [Note] Optional Gateway deployment failed: {e}")
        print(f"  (Deploy Lambda tool functions and set ORDERS_FUNCTION etc. in .env to enable)")
    print()

    print("="*60)
    print("  Deployment Complete!")
    print("="*60)
    print(f"\n  Add these to your .env file:")
    print(f"  AGENTCORE_RUNTIME_ARN={runtime_arn}")
    print(f"  GUARDRAIL_ID={guardrail_id}")
    print(f"  GUARDRAIL_VERSION={guardrail_version}\n")
    print(f"  Then try the deployed runtime:")
    print(f"  python src/agent_orchestrator.py invoke \"What is the return policy for premium customers?\"")
    print(f"  or with the CLI:  agentcore invoke \"What is the return policy for premium customers?\"")
    print(f"  (agentcore status / agentcore logs show the deployed runtime and its logs)\n")
    return runtime_arn, guardrail_id


# ═══════════════════════════════════════════════════════
#  LOCAL TEST SCENARIOS
# ═══════════════════════════════════════════════════════

# Order IDs match infrastructure/seed_data.py.
TEST_CASES = [
    ("CUST-001", "I want to return my wireless headphones from order ORD-27176"),
    ("CUST-002", "What is the return policy for premium customers?"),
    ("CUST-003", "How much would 5 items at $29.99 be with a 10% discount?"),
]

# Test customers shown by the chat command. Data matches seed_data.py.
TEST_CUSTOMERS = [
    ("CUST-001", "Alice Johnson", "Premium",  "ORD-27176", "Wireless Headphones Pro"),
    ("CUST-002", "Bob Smith",     "Standard", "ORD-28001", "Mechanical Keyboard K2"),
    ("CUST-003", "Carol Davis",   "Premium",  "ORD-29001", "Laptop UltraBook 14"),
    ("CUST-004", "David Lee",     "Standard", "ORD-30001", "Phone Case Slim"),
]


def run_test_scenarios() -> None:
    """Run the three scenarios locally; every request is traced to X-Ray."""
    print("Running local agent test...")
    setup_logging(to_cloudwatch=True)
    orchestrator = build_agent_graph()

    for customer_id, query in TEST_CASES:
        session_id = str(uuid.uuid4())[:8]
        print(f"\n{'─'*60}")
        print(f"Session: {session_id} | Customer: {customer_id}")
        print(f"Query: {query}")
        prompt = f"[Session ID: {session_id}] [Customer ID: {customer_id}] {query}"
        with tracer.trace_request(session_id, customer_id, query):
            response = orchestrator(prompt)
        print(f"Response: {response}")
        print_trace_hint()
    flush_logs()


def run_chat() -> None:
    """Interactive terminal chat - educational mode."""
    W = _C.W

    # ── Welcome banner ────────────────────────────────────────────────
    print()
    print(f"  {_C.GRY}{'=' * W}{_C.RESET}")
    print(f"  {_C.ORCH}{_C.BOLD}{'NovaMart -- Multi-Agent Customer Support':^{W}}{_C.RESET}")
    print(f"  {_C.GRY}{'Strands Agents SDK  +  Amazon Bedrock AgentCore':^{W}}{_C.RESET}")
    print(f"  {_C.GRY}{'=' * W}{_C.RESET}")

    # ── Test customers ────────────────────────────────────────────────
    print()
    print(f"  {_C.GRY}{'─' * W}{_C.RESET}")
    print(f"  {_C.BOLD}Test Customers{_C.RESET}")
    print(f"  {_C.GRY}{'─' * W}{_C.RESET}")
    print(f"  {_C.GRY}{'ID':<10}  {'Name':<18}  {'Tier':<10}  {'Order':<12}  Product{_C.RESET}")
    print(f"  {_C.GRY}{'─'*8}  {'─'*16}  {'─'*8}  {'─'*10}  {'─'*20}{_C.RESET}")
    for cid, name, tier, order, product in TEST_CUSTOMERS:
        tier_col = _C.INV if tier == 'Premium' else _C.GRY
        print(f"  {_C.BOLD}{cid}{_C.RESET}  {name:<18}  "
              f"{tier_col}{tier:<10}{_C.RESET}  {order}  {product}")
    print(f"  {_C.GRY}{'─' * W}{_C.RESET}")
    print()

    customer_id = (
        input(f"  Enter Customer ID (default: CUST-001): ").strip()
        or "CUST-001"
    )
    session_id  = str(uuid.uuid4())[:8]
    print()
    print(f"  {_C.GRY}Session  : {_C.RESET}{_C.BOLD}{session_id}{_C.RESET}")
    print(f"  {_C.GRY}Customer : {_C.RESET}{_C.BOLD}{customer_id}{_C.RESET}")
    print(f"  {_C.GRY}Type a question and press Enter.  Type 'quit' to exit.{_C.RESET}")
    print()

    # ── Build agents and show initialization order.
    print(f"  {_C.GRY}[SYSTEM]  Initializing agent graph...{_C.RESET}")
    setup_logging(to_cloudwatch=True)
    orchestrator = build_agent_graph(verbose=True)
    print(f"  {_C.GRY}[SYSTEM]  All 5 agents ready.{_C.RESET}")
    print()

    # ── Conversation loop ─────────────────────────────────────────────
    while True:
        try:
            user_input = input(
                f"  {_C.BOLD}You >{_C.RESET} "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n  {_C.GRY}Session ended.{_C.RESET}")
            break

        if not user_input:
            continue
        if user_input.lower() in ('quit', 'exit', 'q'):
            print(f"  {_C.GRY}Session ended.{_C.RESET}")
            break

        prompt  = (f"[Session ID: {session_id}] "
                   f"[Customer ID: {customer_id}] {user_input}")
        t0_turn = time.time()

        # ── Install proxy, run orchestrator (traced), restore stdout ───
        trace.new_turn()
        sys.stdout = _trace_writer
        try:
            with tracer.trace_request(session_id, customer_id, user_input):
                response = orchestrator(prompt)
        finally:
            sys.stdout = _real_stdout   # always restore, even on exception

        elapsed = time.time() - t0_turn

        # ── Resolve the final customer-facing text ────────────────────
        final_state = _read_workflow_state(session_id) or {}
        comm_result = final_state.get('communication_agent', '')
        text = _strip_xml_tags(comm_result or str(response))

        # ── DynamoDB workflow state summary ───────────────────────────
        trace.summary(session_id, elapsed)

        # ── Final customer-facing response ────────────────────────────
        print()
        print(f"  {_C.GRY}{'=' * W}{_C.RESET}")
        print(f"  {_C.COM}{_C.BOLD}AGENT RESPONSE{_C.RESET}")
        print(f"  {_C.GRY}{'=' * W}{_C.RESET}")
        for line in text.splitlines():
            print(f"  {line}")
        print(f"  {_C.GRY}{'=' * W}{_C.RESET}")
        if tracer.last_trace_id:
            print(f"  {_C.GRY}X-Ray trace : {tracer.last_trace_id}"
                  f"{'' if tracer.last_published else '  (not published)'}{_C.RESET}")
        print()
    flush_logs()


def run_invoke(message: str, customer_id: str = "CUST-001") -> None:
    """Send one message to the deployed AgentCore Runtime and print the reply."""
    session_id = str(uuid.uuid4())[:8]
    print(f"Invoking {config.AGENTCORE_RUNTIME_ARN}")
    print(f"Session: {session_id} | Customer: {customer_id}")
    print(f"Query: {message}\n")
    result = invoke_agent(session_id, customer_id, message)
    print(f"Response: {result.get('result', result)}")
    if result.get('trace_id'):
        print(f"X-Ray trace: {result['trace_id']}")


def run_serve() -> None:
    """
    HTTP entry point executed inside Amazon Bedrock AgentCore Runtime.

    BedrockAgentCoreApp (bedrock-agentcore SDK) exposes the contract the
    runtime expects - POST /invocations and GET /ping on port 8080 - and hands
    each request payload to the function decorated with @app.entrypoint.

    Request payload (see invoke_agent):
        {"prompt": "<customer message>", "customer_id": "CUST-001", "session_id": "abc12345"}
    Response:
        {"result": "<final customer-facing text>", "session_id": ..., "trace_id": ...}

    The five-agent graph is built once (first request) and reused. Guardrail,
    tracing and logging are applied exactly as in the local test/chat modes,
    from the runtime's environment variables.
    """
    from bedrock_agentcore import BedrockAgentCoreApp

    os.environ.setdefault('AGENT_RUNTIME_MODE', 'agentcore-runtime')
    if os.environ.get('AGENT_LOG_GROUP') and 'AGENT_LOG_TO_CLOUDWATCH' not in os.environ:
        os.environ['AGENT_LOG_TO_CLOUDWATCH'] = 'true'

    app   = BedrockAgentCoreApp()
    lock  = threading.Lock()
    graph = {}

    def _orchestrator():
        with lock:
            if 'agent' not in graph:
                setup_logging()
                graph['agent'] = build_agent_graph()
        return graph['agent']

    @app.entrypoint
    def invoke(payload, context=None):
        payload     = payload or {}
        prompt      = payload.get('prompt') or payload.get('message') or ''
        customer_id = payload.get('customer_id') or 'CUST-001'
        session_id  = payload.get('session_id') or (
            getattr(context, 'session_id', None) or uuid.uuid4().hex)[:8]
        if not prompt:
            return {'error': "payload must include 'prompt'"}

        enriched = f"[Session ID: {session_id}] [Customer ID: {customer_id}] {prompt}"
        with tracer.trace_request(session_id, customer_id, prompt):
            response = _orchestrator()(enriched)

        state = _read_workflow_state(session_id) or {}
        text  = _strip_xml_tags(state.get('communication_agent', '') or str(response))
        flush_logs()
        return {'result': text, 'session_id': session_id, 'customer_id': customer_id,
                'trace_id': tracer.last_trace_id}

    app.run()


if __name__ == '__main__':
    command = sys.argv[1] if len(sys.argv) > 1 else ''

    # Inside the AgentCore Runtime package (marker file next to this script)
    # the entry point is started without arguments -> serve HTTP.
    if not command and os.path.exists(os.path.join(_SRC_DIR, _RUNTIME_MARKER)):
        command = 'serve'

    if command == 'deploy':
        deploy_all()

    elif command == 'serve':
        run_serve()

    elif command == 'test':
        run_test_scenarios()

    elif command == 'chat':
        run_chat()

    elif command == 'invoke':
        if len(sys.argv) < 3:
            print('Usage: python src/agent_orchestrator.py invoke "<message>" [CUSTOMER_ID]')
            sys.exit(1)
        run_invoke(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "CUST-001")

    else:
        print("Usage:")
        print("  python src/agent_orchestrator.py deploy           # Deploy to AgentCore (Tasks 3-6)")
        print("  python src/agent_orchestrator.py test             # Run the 3 test scenarios locally")
        print("  python src/agent_orchestrator.py chat             # Interactive terminal chat")
        print("  python src/agent_orchestrator.py invoke \"<msg>\"   # Call the deployed runtime")
        print("  python src/agent_orchestrator.py serve            # HTTP server (used inside AgentCore Runtime)")
