"""Parse one bounded Antigravity headless turn; incomplete results fail closed."""
import json
from .workflow import CouncilError


def parse_response(stdout: str, model: str) -> dict:
    events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    results = [event['result'] for event in events if event.get('event') == 'result']
    if len(results) != 1:
        raise CouncilError('Antigravity must return exactly one terminal result')
    result = results[0]
    if result.get('status') != 'SUCCESS' or result.get('error'):
        raise CouncilError('Antigravity did not complete; inspect its local diagnostics. Provider output was not saved to the ledger')
    if result.get('structured_output') is not None:
        return result['structured_output']
    return json.loads(result.get('response', ''))
