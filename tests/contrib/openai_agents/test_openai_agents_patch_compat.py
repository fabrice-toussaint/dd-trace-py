import asyncio
from types import SimpleNamespace

import pytest

from ddtrace.contrib.internal.openai_agents.patch import _MODULE_RUN_LOOP_WRAP_TARGETS
from ddtrace.contrib.internal.openai_agents.patch import _patched_run_single_turn
from ddtrace.contrib.internal.openai_agents.patch import _patched_run_single_turn_module
from ddtrace.trace import tracer


class TestOpenAIAgentsPatchCompat:
    """Cover the agents-version compatibility shims in patch.py (MLOB-7584).

    agents >= 0.8.0 moved the per-turn function from ``AgentRunner._run_single_turn`` to
    module-level ``agents.run_internal.run_loop``. These tests assert the manifest is captured
    for the right agent across every instance-method and module-level call shape.

    This lives under ``tests/contrib/openai_agents`` so it runs in the ``openai_agents`` riot
    venv, which installs the SDK. The objects below are minimal stand-ins for the four real SDK
    call shapes; the SDK only needs to be importable for the wrappers to resolve their
    integration.
    """

    @pytest.fixture(autouse=True)
    def _enable_llmobs(self, agents):
        # The ``agents`` fixture (conftest) patches and unpatches around each test, so wrapt
        # wrappers do not leak across tests. ``llmobs_enabled`` reads the global
        # ``LLMObs.enabled`` flag, which gates manifest tagging.
        from ddtrace.llmobs import LLMObs

        prior = LLMObs.enabled
        LLMObs.enabled = True
        try:
            yield
        finally:
            LLMObs.enabled = prior

    def _run_module_wrapper(self, args, kwargs):
        import agents

        integration = agents._datadog_integration
        captured_manifest = []
        orig_m = integration._tag_agent_manifest_from_agent
        integration._tag_agent_manifest_from_agent = lambda span, agent: captured_manifest.append(
            getattr(agent, "name", None)
        )

        async def inner(*a, **k):
            return "RESULT"

        async def _run():
            with tracer.trace("test_root"):
                return await _patched_run_single_turn_module(inner, None, args, kwargs)

        try:
            result = asyncio.run(_run())
        finally:
            integration._tag_agent_manifest_from_agent = orig_m
        return result, captured_manifest

    @staticmethod
    def _agent(name="Analyst"):
        return SimpleNamespace(name=name, instructions="x", tools=[{"name": "t"}], handoffs=[], mcp_servers=[])

    def test_module_run_loop_wrap_targets_structure(self):
        # The non-streamed wrap target MUST be on agents.run, not agents.run_internal.run_loop.
        # The import-binding gotcha means wrapping the definition module is too late — run.py
        # already holds a stale ref. See AIDEV-NOTE in patch.py.
        non_streamed = [e for e in _MODULE_RUN_LOOP_WRAP_TARGETS if e[1] == "run_single_turn"]
        assert non_streamed, "expected a non-streamed wrap target"
        assert non_streamed[0][0] == "agents.run", (
            "non-streamed run_single_turn must be wrapped on agents.run (the call-site module), "
            "not agents.run_internal.run_loop (the definition module). See AIDEV-NOTE in patch.py."
        )

    # MLOB-7584 — the module-level per-turn function has FOUR real call shapes across agents
    # versions / streamed variants (verified against shipped wheels 0.8.0-0.17.x). These
    # exercise the shapes the original ``args[0]``/``bindings``-only extraction missed. Each
    # runs the real ``tag_agent_manifest`` (which calls ``_extract_agent_from_call``) and
    # captures the resolved agent via the private ``_tag_agent_manifest_from_agent`` delegate.
    def test_module_wrapper_handles_agent_kwarg_shape(self):
        # agents 0.8.0-0.13.x non-streamed: run_single_turn(agent=<Agent>, ...). The pre-fix
        # wrapper read only kwargs["bindings"] and silently dropped this.
        agent = self._agent()
        result, manifest = self._run_module_wrapper((), {"agent": agent, "all_tools": []})
        assert result == "RESULT"
        assert manifest == ["Analyst"]

    def test_module_wrapper_handles_bindings_kwarg_shape(self):
        # agents >= 0.14.0 non-streamed: run_single_turn(bindings=<AgentBindings>, ...). For the
        # agent manifest (the declared config) the public_agent is preferred over execution_agent,
        # which may be a sandbox-rewritten clone.
        public_agent = self._agent("PublicAgent")
        execution_agent = self._agent("ExecutionAgent")
        bindings = SimpleNamespace(public_agent=public_agent, execution_agent=execution_agent)
        result, manifest = self._run_module_wrapper((), {"bindings": bindings})
        assert result == "RESULT"
        assert manifest == ["PublicAgent"], "must prefer public_agent over execution_agent for the manifest"

    def test_streamed_wrapper_handles_realistic_positional_bindings(self):
        # agents >= 0.14.0 streamed: run_single_turn_streamed(<RunResultStreaming>, <bindings>, ...).
        # The real arg[0] is the streamed result (NOT the bindings); the bindings is arg[1].
        agent = self._agent("StreamedExec")
        stream_result = SimpleNamespace(current_agent=agent)  # RunResultStreaming-like: current_agent only
        bindings = SimpleNamespace(public_agent=agent, execution_agent=None)
        result, manifest = self._run_module_wrapper((stream_result, bindings, "hooks"), {})
        assert result == "RESULT"
        assert manifest == ["StreamedExec"], "must extract agent from bindings at arg[1], not the stream result"

    def test_streamed_wrapper_handles_realistic_positional_agent(self):
        # agents 0.8.0-0.13.x streamed: run_single_turn_streamed(<RunResultStreaming>, <Agent>, ...).
        agent = self._agent("StreamedAgent")
        stream_result = SimpleNamespace(current_agent=agent)
        result, manifest = self._run_module_wrapper((stream_result, agent, "hooks"), {})
        assert result == "RESULT"
        assert manifest == ["StreamedAgent"]

    def test_module_wrapper_skips_run_result_streaming_without_agent(self):
        # Negative control: a RunResultStreaming-like object alone (current_agent only, no
        # bindings attrs / no name+tools+handoffs) must NOT be mistaken for an Agent.
        stream_result = SimpleNamespace(current_agent=self._agent())
        result, manifest = self._run_module_wrapper((stream_result,), {})
        assert result == "RESULT"
        assert manifest == []

    def test_wrapper_swallows_capture_errors_so_user_run_survives(self):
        # A pathological agent whose attribute access raises must NOT propagate out of the wrap
        # site into the user's Runner.run — the SDK does not guard this call site.
        class _BoomAgent:
            name = "Boom"
            handoffs = []

            @property
            def tools(self):
                raise ValueError("boom")

        result, manifest = self._run_module_wrapper((), {"agent": _BoomAgent()})
        assert result == "RESULT"  # the user's run completes
        assert manifest == []  # capture degraded gracefully, no raise

    def test_instance_wrapper_tags_manifest_non_streamed(self):
        # Regression guard for the instance-method path (AgentRunner._run_single_turn): the
        # agent is passed as the ``agent`` kwarg. This is the path that worked pre-0.8.0 and
        # must keep working. Would fail if _extract_agent_from_call stopped scanning kwargs.
        import agents

        integration = agents._datadog_integration
        captured = []
        orig = integration._tag_agent_manifest_from_agent
        integration._tag_agent_manifest_from_agent = lambda span, agent: captured.append(getattr(agent, "name", None))

        agent = self._agent("InstanceAgent")

        async def inner(*a, **k):
            return "RESULT"

        async def _run():
            with tracer.trace("test_root"):
                return await _patched_run_single_turn(inner, None, (), {"agent": agent})

        try:
            result = asyncio.run(_run())
            assert result == "RESULT"
            assert captured == ["InstanceAgent"]
        finally:
            integration._tag_agent_manifest_from_agent = orig

    def test_instance_wrapper_tags_manifest_streamed_positional(self):
        # Regression guard for the streamed instance-method path
        # (AgentRunner._run_single_turn_streamed): arg[0] is the streamed result, the agent is
        # arg[1]. Pre-fix instance-method handling read agent_index=1 here; the unified scanner
        # must still resolve arg[1] while skipping the RunResultStreaming at arg[0].
        import agents

        integration = agents._datadog_integration
        captured = []
        orig = integration._tag_agent_manifest_from_agent
        integration._tag_agent_manifest_from_agent = lambda span, agent: captured.append(getattr(agent, "name", None))

        agent = self._agent("StreamedInstanceAgent")
        stream_result = SimpleNamespace(current_agent=agent)

        async def inner(*a, **k):
            return "RESULT"

        async def _run():
            with tracer.trace("test_root"):
                return await _patched_run_single_turn(inner, None, (stream_result, agent), {})

        try:
            result = asyncio.run(_run())
            assert result == "RESULT"
            assert captured == ["StreamedInstanceAgent"]
        finally:
            integration._tag_agent_manifest_from_agent = orig
