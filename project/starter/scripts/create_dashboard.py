"""
Create the NovaMart-Agents CloudWatch dashboard (stand-out 7.2).

Widgets:
  1. Agent invocations over time      - NovaMart/Agents AgentInvocations (Sum, per Agent)
  2. Average latency per agent (ms)   - NovaMart/Agents AgentLatencyMs (Average over the whole
                                        selected time range, per Agent)
  3. Guardrail interventions          - AWS/Bedrock/Guardrails InvocationsIntervened (native)
                                        + NovaMart/Agents GuardrailBlocked (per Agent)
The custom metrics are emitted by agent_orchestrator.py with the Embedded Metric Format.

Usage (from project/starter, venv active, AWS credentials exported):
    python scripts/create_dashboard.py
Remove it with scripts/cleanup_extras.py: infrastructure/cleanup.py does not know it.
"""
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, ROOT)

import boto3    # noqa: E402
import config   # noqa: E402

DASHBOARD_NAME = 'NovaMart-Agents'
NAMESPACE = 'NovaMart/Agents'
PERIOD = 60


def _search(expression_id: str, query: str, stat: str) -> list:
    """
    Build one SEARCH metric-math row for a dashboard widget.

    Args:
        expression_id: Unique id of the expression inside the widget
        query:         CloudWatch SEARCH query
        stat:          Statistic (Sum, Average...)

    Returns:
        A widget "metrics" row
    """
    return [{'expression': f"SEARCH('{query}', '{stat}', {PERIOD})", 'id': expression_id, 'label': ''}]


def build_body() -> dict:
    """Return the dashboard body (header text + 3 metric widgets)."""
    region = config.AWS_REGION
    agents = f'{{{NAMESPACE},Agent}}'
    return {'widgets': [
        {'type': 'text', 'x': 0, 'y': 0, 'width': 24, 'height': 2, 'properties': {
            'markdown': ('# NovaMart multi-agent support\n'
                         'Per-agent metrics published by the agents (Embedded Metric Format, '
                         f'namespace `{NAMESPACE}`) and native Bedrock Guardrails metrics.')}},
        {'type': 'metric', 'x': 0, 'y': 2, 'width': 12, 'height': 7, 'properties': {
            'title': 'Agent invocations over time', 'region': region, 'view': 'timeSeries',
            'stacked': True, 'period': PERIOD,
            'metrics': [_search('e1', f'{agents} MetricName="AgentInvocations"', 'Sum')]}},
        {'type': 'metric', 'x': 12, 'y': 2, 'width': 12, 'height': 7, 'properties': {
            'title': 'Average latency per agent (ms)', 'region': region, 'view': 'bar',
            'period': PERIOD, 'setPeriodToTimeRange': True,
            'metrics': [_search('e2', f'{agents} MetricName="AgentLatencyMs"', 'Average')]}},
        {'type': 'metric', 'x': 0, 'y': 9, 'width': 24, 'height': 7, 'properties': {
            'title': 'Guardrail interventions', 'region': region, 'view': 'timeSeries',
            'stacked': False, 'period': PERIOD,
            'metrics': [
                _search('g1', '{AWS/Bedrock/Guardrails} MetricName="InvocationsIntervened"', 'Sum'),
                _search('g2', f'{agents} MetricName="GuardrailBlocked"', 'Sum'),
            ]}},
    ]}


def main() -> None:
    """Create or replace the dashboard and print its console URL."""
    response = boto3.client('cloudwatch', region_name=config.AWS_REGION).put_dashboard(
        DashboardName=DASHBOARD_NAME, DashboardBody=json.dumps(build_body()))
    for message in response.get('DashboardValidationMessages', []):
        print(f"Validation: {message}")
    print(f"Dashboard {DASHBOARD_NAME} saved: https://console.aws.amazon.com/cloudwatch/home"
          f"?region={config.AWS_REGION}#dashboards/dashboard/{DASHBOARD_NAME}")


if __name__ == '__main__':
    main()
