"""Framework adapters. None of them import the framework they adapt.

* ``Undolith.tool`` decorator - any Python function or custom agent loop
* ``undolith.integrations.langchain`` - LangChain tools
* ``undolith.integrations.mcp`` - MCP client sessions
* ``undolith.integrations.function_calls`` - OpenAI/Anthropic-style tool-call dicts
"""
