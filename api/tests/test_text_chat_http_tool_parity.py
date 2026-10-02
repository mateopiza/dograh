"""Text chat runs the same workflow features a voice call does.

Platforms that reuse one workflow for voice and for text channels (e.g. a
WhatsApp bot driven through the text-chat API) depend on the text session
calling HTTP_API tools and extracting variables exactly like the call does.
The workflow below mirrors that shape: greeting -> agent node with an HTTP
tool and variable extraction -> end node, plus a post-call webhook node.
"""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from api.tests.test_workflow_text_chat import _create_user_and_workflow
from pipecat.tests import MockLLMService

TOOL_URL = "https://n8n.example.test/webhook/agendar-cita"
TOOL_HEADERS = {
    "x-agentrix-workspace": "ws_123",
    "x-agentrix-secret": "skill-secret",
    "x-agentrix-skill": "agendar-cita",
}
EXTRACTED = {
    "nombre_completo": "Ana Pérez",
    "interes": "limpieza dental",
    "resumen": "Agendó una limpieza dental para mañana.",
}


def _workflow_definition(tool_uuid: str) -> dict:
    return {
        "nodes": [
            {
                "id": "agentrix_start",
                "type": "startCall",
                "position": {"x": 0, "y": 0},
                "data": {
                    "name": "Saludo",
                    "is_start": True,
                    "greeting_type": "text",
                    "greeting": "Hola, soy Sofía. ¿En qué te ayudo?",
                    "prompt": "Averigua qué necesita la persona.",
                    "allow_interrupt": False,
                    "add_global_prompt": False,
                },
            },
            {
                "id": "agentrix_agent",
                "type": "agentNode",
                "position": {"x": 0, "y": 220},
                "data": {
                    "name": "Atención",
                    "prompt": "Agenda citas con la herramienta agendar_cita.",
                    "allow_interrupt": False,
                    "add_global_prompt": False,
                    "tool_uuids": [tool_uuid],
                    "extraction_enabled": True,
                    "extraction_prompt": "Extrae los datos del cliente.",
                    "extraction_variables": [
                        {"name": name, "type": "string", "prompt": name}
                        for name in EXTRACTED
                    ],
                },
            },
            {
                "id": "agentrix_end",
                "type": "endCall",
                "position": {"x": 0, "y": 440},
                "data": {
                    "name": "Despedida",
                    "prompt": "Despídete.",
                    "is_end": True,
                    "allow_interrupt": False,
                    "add_global_prompt": False,
                },
            },
            {
                "id": "agentrix_post_call",
                "type": "webhook",
                "position": {"x": 420, "y": 0},
                "data": {
                    "name": "Post-llamada",
                    "enabled": True,
                    "http_method": "POST",
                    "endpoint_url": "https://agentrix.example.test/api/dograh/webhook",
                    "custom_headers": [],
                    "payload_template": {"event": "dograh.call.completed"},
                },
            },
        ],
        "edges": [
            {
                "id": "start-agent",
                "source": "agentrix_start",
                "target": "agentrix_agent",
                "data": {"label": "Atender", "condition": "La persona pidió algo."},
            },
            {
                "id": "agent-end",
                "source": "agentrix_agent",
                "target": "agentrix_end",
                "data": {"label": "Finalizar", "condition": "No necesita nada más."},
            },
        ],
    }


@pytest.mark.asyncio
async def test_text_chat_calls_http_tool_and_extracts_variables_like_voice(
    db_session,
    async_session,
    test_client_factory,
):
    user, _ = await _create_user_and_workflow(
        db_session,
        async_session,
        workflow_definition={"nodes": [], "edges": []},
        suffix="http-tool-parity",
    )
    tool = await db_session.create_tool(
        organization_id=user.selected_organization_id,
        user_id=user.id,
        name="agendar_cita",
        description="Agenda una cita para el cliente.",
        definition={
            "schema_version": 1,
            "type": "http_api",
            "config": {
                "method": "POST",
                "url": TOOL_URL,
                "headers": dict(TOOL_HEADERS),
                "parameters": [
                    {
                        "name": "fecha",
                        "type": "string",
                        "description": "Fecha de la cita.",
                        "required": True,
                    }
                ],
                # Text channels need the platform's conversation id (e.g. to
                # request a human handoff); voice calls leave it empty.
                "preset_parameters": [
                    {
                        "name": "conversationId",
                        "type": "string",
                        "value_template": "{{initial_context.agentrix_conversation_id}}",
                        "required": False,
                    }
                ],
                "timeout_ms": 20000,
            },
        },
    )
    # Created directly as the published definition: channel traffic must run
    # what was published, not a draft someone is still editing.
    workflow = await db_session.create_workflow(
        name="Agentrix text parity",
        workflow_definition=_workflow_definition(tool.tool_uuid),
        user_id=user.id,
        organization_id=user.selected_organization_id,
    )

    tool_requests: list[httpx.Request] = []

    def tool_endpoint(request: httpx.Request) -> httpx.Response:
        tool_requests.append(request)
        return httpx.Response(200, json={"ok": True, "mensaje": "Cita agendada"})

    real_async_client = httpx.AsyncClient

    def tool_http_client(*args, **kwargs):
        return real_async_client(
            *args, transport=httpx.MockTransport(tool_endpoint), **kwargs
        )

    extraction = AsyncMock(return_value=dict(EXTRACTED))
    llm_per_turn = [
        # Turn 0: the text greeting needs no inference.
        MockLLMService(mock_steps=[], chunk_delay=0.001),
        # Turn 1: move to the agent node, call the tool, answer.
        MockLLMService(
            mock_steps=[
                MockLLMService.create_function_call_chunks(
                    "atender", {}, tool_call_id="call_atender"
                ),
                MockLLMService.create_function_call_chunks(
                    "agendar_cita", {"fecha": "mañana"}, tool_call_id="call_cita"
                ),
                MockLLMService.create_text_chunks("Listo, tu cita quedó agendada."),
            ],
            chunk_delay=0.001,
        ),
        # Turn 2: leave the agent node (extraction runs) and say goodbye.
        MockLLMService(
            mock_steps=[
                MockLLMService.create_function_call_chunks(
                    "finalizar", {}, tool_call_id="call_fin"
                ),
                MockLLMService.create_text_chunks("¡Hasta pronto!"),
            ],
            chunk_delay=0.001,
        ),
    ]
    enqueue = AsyncMock()

    async with test_client_factory(user) as client:
        with (
            patch(
                "api.services.workflow.text_chat_runner.create_llm_service",
                side_effect=llm_per_turn,
            ),
            patch(
                "api.services.workflow.text_chat_runner.db_client.has_active_recordings",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "api.services.workflow.tools.custom_tool.httpx.AsyncClient",
                side_effect=tool_http_client,
            ),
            patch(
                "api.services.workflow.pipecat_engine_variable_extractor."
                "VariableExtractionManager._perform_extraction",
                new=extraction,
            ),
            patch("api.tasks.arq.enqueue_job", enqueue),
        ):
            created = await client.post(
                f"/api/v1/workflow/{workflow.id}/text-chat/sessions",
                json={
                    "use_draft": False,
                    "initial_context": {
                        "channel": "whatsapp",
                        "agentrix_conversation_id": "conv_42",
                    },
                },
            )
            assert created.status_code == 200, created.text
            session = created.json()
            run_id = session["workflow_run_id"]

            first = await client.post(
                f"/api/v1/workflow/{workflow.id}/text-chat/sessions/{run_id}/messages",
                json={
                    "text": "Quiero agendar una cita",
                    "expected_revision": session["revision"],
                },
            )
            assert first.status_code == 200, first.text
            second = await client.post(
                f"/api/v1/workflow/{workflow.id}/text-chat/sessions/{run_id}/messages",
                json={
                    "text": "Eso es todo, gracias",
                    "expected_revision": first.json()["revision"],
                },
            )
            assert second.status_code == 200, second.text

    greeting = session["session_data"]["turns"][0]["assistant_message"]["text"]
    assert greeting == "Hola, soy Sofía. ¿En qué te ayudo?"
    assert session["initial_context"]["channel"] == "whatsapp"

    # HTTP_API tool: same request a voice call would send.
    assert len(tool_requests) == 1
    request = tool_requests[0]
    assert request.method == "POST"
    assert str(request.url) == TOOL_URL
    for header, value in TOOL_HEADERS.items():
        assert request.headers[header] == value
    assert json.loads(request.content) == {
        "fecha": "mañana",
        "conversationId": "conv_42",
    }

    first_turn = first.json()["session_data"]["turns"][1]
    assert first_turn["assistant_message"]["text"].endswith(
        "Listo, tu cita quedó agendada."
    )
    tool_results = [
        event["payload"]
        for event in first_turn["events"]
        if event["type"] == "tool_call_result"
        and event["payload"].get("function_name") == "agendar_cita"
    ]
    assert tool_results, first_turn["events"]
    assert first.json()["checkpoint"]["current_node_id"] == "agentrix_agent"

    # Variable extraction on leaving the agent node, persisted on the run.
    final = second.json()
    assert extraction.await_count >= 1
    assert final["gathered_context"]["extracted_variables"] == EXTRACTED
    for name, value in EXTRACTED.items():
        assert final["gathered_context"][name] == value

    # Reaching the end node completes the run and schedules the same
    # post-run work (integrations, webhook nodes) a finished call gets.
    assert final["is_completed"] is True
    assert final["session_data"]["status"] == "completed"
    enqueued = [call.args for call in enqueue.await_args_list]
    assert any(args[1] == run_id for args in enqueued)
