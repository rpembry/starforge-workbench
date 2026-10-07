"""Direct and installed-style stdio regressions for all non-tool surfaces."""
import asyncio
import json
import os
from pathlib import Path
import select
import subprocess
import sys

import pytest
from mcp.shared.exceptions import MCPError

from workbench.vm_mcp import build_server


CANARY = 'SYNTHETIC_PROTOCOL_SECRET_DO_NOT_RELEASE'


def test_direct_resource_and_prompt_calls_return_fixed_errors_without_logging(caplog, capsys):
    server = build_server()
    async def run():
        for call in (server.read_resource('file:///' + CANARY),
                     server.get_prompt(CANARY, {'input': CANARY})):
            with pytest.raises(MCPError) as error:
                await call
            assert str(error.value) == 'Unsupported VM request'
            assert error.value.error.data is None
    asyncio.run(run())
    captured = capsys.readouterr()
    assert CANARY not in caplog.text + captured.out + captured.err


@pytest.mark.parametrize('flag', ['--confidential', '--standard'])
def test_stdio_unsupported_and_malformed_requests_have_content_free_errors(flag):
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    process = subprocess.Popen([sys.executable, '-c', 'from workbench.vm_mcp import main; main()', flag],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, env=env)
    responses = []
    def request(identity, method, params):
        process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': identity,
                                        'method': method, 'params': params}) + '\n')
        process.stdin.flush()
        assert select.select([process.stdout], [], [], 5)[0], 'No bounded stdio response'
        response = json.loads(process.stdout.readline())
        assert response['id'] == identity
        responses.append(response)
        return response
    try:
        initialized = request(1, 'initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                               'clientInfo': {'name': 'synthetic-test', 'version': '1'}})
        assert 'result' in initialized
        process.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        process.stdin.flush()
        cases = [
            ('resources/read', {'uri': 'file:///' + CANARY}),
            ('resources/read', {'uri': {CANARY: CANARY}}),
            ('prompts/get', {'name': CANARY, 'arguments': {CANARY: CANARY}}),
            ('resources/subscribe', {'uri': 'file:///' + CANARY}),
            ('resources/unsubscribe', {'uri': 'file:///' + CANARY}),
            ('completion/complete', {'ref': {'type': 'ref/prompt', 'name': CANARY},
                                      'argument': {'name': CANARY, 'value': CANARY}}),
            ('logging/setLevel', {'level': CANARY}),
            (CANARY, {CANARY: CANARY}),
            ('tools/call', {'name': CANARY, 'arguments': {CANARY: CANARY}}),
            ('tools/call', {'name': [CANARY], 'arguments': {}}),
            ('tools/call', {'name': 'vm_diagnostic', 'arguments': [CANARY]}),
            ('tools/list', {'cursor': [CANARY]}),
            ('initialize', {'protocolVersion': [CANARY], 'capabilities': {},
                            'clientInfo': {'name': CANARY, 'version': CANARY}}),
        ]
        for identity, (method, params) in enumerate(cases, 2):
            response = request(identity, method, params)
            assert 'error' in response or response.get('result', {}).get('isError')
            assert CANARY not in json.dumps(response)
        process.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': CANARY,
                                        'params': {CANARY: CANARY}}) + '\n')
        process.stdin.flush()
        assert request(100, 'ping', {})['result'] == {}
        # Parser failures happen before server middleware; framing guard must
        # prevent validation exceptions carrying raw text into stderr/errors.
        process.stdin.write('{"jsonrpc":"2.0","method":' + CANARY + '}\n')
        process.stdin.flush()
        assert select.select([process.stdout], [], [], 5)[0]
        parse_error = json.loads(process.stdout.readline())
        assert parse_error['error']['code'] == -32700
        assert CANARY not in json.dumps(parse_error)
        assert request(101, 'ping', {})['result'] == {}
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        stderr = process.stderr.read()
        process.stdout.close()
        process.stderr.close()
    assert process.returncode == 0
    assert CANARY not in stderr + json.dumps(responses)


@pytest.mark.parametrize('flag', ['--confidential', '--standard'])
def test_modern_protocol_classification_errors_are_redacted_before_wire_release(flag):
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    process = subprocess.Popen([sys.executable, '-c', 'from workbench.vm_mcp import main; main()', flag],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, env=env)
    try:
        process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'server/discover',
                                        'params': {'_meta': {'io.modelcontextprotocol/protocolVersion': CANARY}}}) + '\n')
        process.stdin.flush()
        assert select.select([process.stdout], [], [], 5)[0]
        response = json.loads(process.stdout.readline())
        assert 'error' in response
        assert response['error']['message'] == 'Invalid VM request'
        assert response['error'].get('data') is None
        assert CANARY not in json.dumps(response)
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        stderr = process.stderr.read()
        process.stdout.close()
        process.stderr.close()
    assert process.returncode == 0 and CANARY not in stderr
