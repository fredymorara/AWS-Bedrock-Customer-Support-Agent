"""
Customer Support AI Agent — Starter Code
==========================================
Your task is to complete this file by implementing all sections marked
with # TODO comments.

Reference the step-by-step solution files and INSTRUCTIONS.md for guidance.
Do NOT copy the solution directly — work through each section yourself.

Run locally (after filling in config values):
  uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
  agentcore deploy

Invoke deployed agent:
  agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
"""

# ── Imports ───────────────────────────────────────────────────────────────────
# These imports are provided. Do not remove them.
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamable_http_client
import argparse, json
import os, asyncio, boto3
from strands.hooks import (
    HookProvider, AfterInvocationEvent, HookRegistry, MessageAddedEvent,
)
import logging
import uuid
from typing import Dict
from bedrock_agentcore.tools.code_interpreter_client import code_session
from strands_tools.browser import AgentCoreBrowser


# Fix Linux execute permissions for Playwright driver (when zipped on Windows):
# /var/task is read-only in AWS runtime, so we copy the node binary to /tmp, chmod +x,
# and set PLAYWRIGHT_NODEJS_PATH so Playwright executes from /tmp.
try:
    import sys, shutil, stat, inspect
    from pathlib import Path
    if sys.platform != "win32":
        import playwright
        _orig_node = Path(inspect.getfile(playwright)).parent / "driver" / "node"
        if _orig_node.exists():
            _tmp_node = Path("/tmp/playwright-node")
            if not _tmp_node.exists():
                shutil.copyfile(_orig_node, _tmp_node)
            _tmp_node.chmod(0o777)
            os.environ["PLAYWRIGHT_NODEJS_PATH"] = str(_tmp_node)
except Exception as _e:
    pass

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")


# ── TODO 1 — App Initialisation ───────────────────────────────────────────────
# Create a BedrockAgentCoreApp instance.
# This registers the ASGI server for AgentCore deployment.
# There must be exactly one instance per deployment.
#
# Hint: app = BedrockAgentCoreApp()

# TODO: Create the BedrockAgentCoreApp instance
app = BedrockAgentCoreApp()  # Replace this line


# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"


# ── TODO 2 — Configuration ────────────────────────────────────────────────────
# Replace the placeholder strings with your actual AWS resource values.
# You collected these in Part 1 of the INSTRUCTIONS.
#
# GATEWAY_URL format: https://<alias>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp
# KB_ID       format: 10-character alphanumeric string from the KB console
# REGION:     your AWS region, e.g. "us-east-1"
# MEMORY_ID   format: shown in the AgentCore Memory console

GATEWAY_URL = "https://customersupportgateway-qtbrczn43b.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"   # TODO: Replace with your Gateway URL
KB_ID       = "U4VYNSTR0C"          # TODO: Replace with your Knowledge Base ID
REGION      = "us-east-1"        # TODO: Replace with your AWS region
MEMORY_ID   = "CustomerSupportMemory-ZO7wcf7JXa"        # TODO: Replace with your Memory ID


# ── TODO 3 — Model and Clients ────────────────────────────────────────────────
# Create:
#   1. A BedrockModel using model_id "global.amazon.nova-2-lite-v1:0"
#   2. A MemoryClient with region_name=REGION
#   3. A boto3 client for the "bedrock-agent-runtime" service in REGION
#
# Hint: model = BedrockModel(model_id=model_id)

model_id = "global.amazon.nova-2-lite-v1:0"

# TODO: Create the BedrockModel instance
model = BedrockModel(model_id=model_id)  # Replace this line

# TODO: Create the MemoryClient instance
memory_client = MemoryClient(region_name=REGION)  # Replace this line

# TODO: Create the boto3 bedrock-agent-runtime client
_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)  # Replace this line


# ── TODO 4 — Namespace Helper ─────────────────────────────────────────────────
# Implement get_namespaces() to return a dict mapping strategy type to
# namespace template string.
#
# Steps:
#   1. Call mem_client.get_memory_strategies(memory_id) to get strategy list
#   2. Return a dict: { strategy["type"]: strategy["namespaces"][0] for each strategy }
#
# Example output:
#   { "SEMANTIC": "cs_agent/{actorId}/facts",
#     "USER_PREFERENCE": "cs_agent/{actorId}/preferences" }

def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    # TODO: Implement this function
    strategies = mem_client.get_memory_strategies(memory_id)
    return {s["type"]: s["namespaces"][0] for s in strategies}


# ── TODO 5 — Memory Hook ──────────────────────────────────────────────────────
# Implement MemoryHook, a HookProvider subclass that adds long-term memory.
#
# The class needs:
#   __init__(self, actor_id, session_id, memory_client, memory_id)
#     — store all four as instance attributes
#     — call get_namespaces() and store the result as self.namespaces
#
#   retrieve_customer_context(self, event: MessageAddedEvent)
#     — only runs for plain-text user messages (not tool results)
#     — for each strategy namespace, call memory_client.retrieve_memories(
#          memory_id, namespace (formatted with actorId), query, top_k=5)
#     — collect non-empty memory texts tagged with their strategy type
#     — if any memories found, prepend them to the user message as:
#          "Customer Context:\n<memories>\n\n<original_message>"
#
#   save_support_interaction(self, event: AfterInvocationEvent)
#     — walk the message list backwards to find the last plain-text user
#       query and the last assistant response
#     — call memory_client.create_event(memory_id, actor_id, session_id,
#          messages=[(customer_query, "USER"), (agent_response, "ASSISTANT")])
#
#   register_hooks(self, registry: HookRegistry)
#     — register retrieve_customer_context on MessageAddedEvent
#     — register save_support_interaction on AfterInvocationEvent

class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_client = memory_client
        self.memory_id = memory_id
        self.namespaces = get_namespaces(memory_client, memory_id)

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        messages = event.agent.messages
        if not messages:
            return

        last_message = messages[-1]

        # Only run for plain-text user messages (not tool results or assistant responses)
        if last_message.get("role") != "user":
            return

        content = last_message.get("content", [])
        user_query = ""
        for block in content:
            if "toolResult" in block:
                return  # Skip tool outputs
            if "text" in block:
                user_query += block["text"]

        if not user_query.strip():
            return

        # Query each strategy namespace for relevant facts & preferences
        memories = []
        for strategy_type, ns_template in self.namespaces.items():
            ns = ns_template.format(actorId=self.actor_id)
            try:
                results = self.memory_client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=ns,
                    query=user_query,
                    top_k=5,
                )
                for record in results:
                    rec_content = record.get("content", {})
                    text = rec_content.get("text", "") if isinstance(rec_content, dict) else str(rec_content)
                    if text:
                        memories.append(f"[{strategy_type}] {text}")
            except Exception as e:
                logger.warning(f"Failed to retrieve memories for namespace {ns}: {e}")

        # Prepend the retrieved context to the user's message
        if memories:
            context_header = "Customer Context:\n" + "\n".join(memories)
            for block in content:
                if "text" in block:
                    block["text"] = f"{context_header}\n\n{block['text']}"
                    break

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        messages = event.agent.messages
        if not messages:
            return

        last_user_query = None
        last_assistant_response = None

        # Walk backwards to find the last user query (plain text) and assistant response
        for msg in reversed(messages):
            role = msg.get("role")
            content = msg.get("content", [])

            # Extract plain text content (skip toolUse and toolResult blocks)
            text_blocks = []
            has_tool_block = False
            for block in content:
                if "toolResult" in block or "toolUse" in block:
                    has_tool_block = True
                    break
                if "text" in block:
                    text_blocks.append(block["text"])

            if has_tool_block or not text_blocks:
                continue

            full_text = "\n".join(text_blocks).strip()

            if role == "assistant" and last_assistant_response is None:
                last_assistant_response = full_text
            elif role == "user" and last_user_query is None:
                # If Customer Context was prepended, strip it so we store clean user query
                if "Customer Context:\n" in full_text:
                    parts = full_text.split("\n\n", 1)
                    last_user_query = parts[-1] if len(parts) > 1 else full_text
                else:
                    last_user_query = full_text

            if last_user_query and last_assistant_response:
                break

        # Save the completed turn into Bedrock AgentCore Memory
        if last_user_query and last_assistant_response:
            try:
                self.memory_client.create_event(
                    memory_id=self.memory_id,
                    actor_id=self.actor_id,
                    session_id=self.session_id,
                    messages=[
                        (last_user_query, "USER"),
                        (last_assistant_response, "ASSISTANT"),
                    ],
                )
            except Exception as e:
                logger.warning(f"Failed to save interaction to memory: {e}")

    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        """Register both memory callbacks."""
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)


# ── TODO 6 — Knowledge Base Tool ─────────────────────────────────────────────
# Implement search_knowledge_base(query) using the @tool decorator.
#
# Steps:
#   1. Guard: if KB_ID is empty return "Knowledge base not configured."
#   2. Call _bedrock_runtime.retrieve(
#          knowledgeBaseId=KB_ID,
#          retrievalQuery={"text": query}
#      )
#   3. Extract resp["retrievalResults"]; return a message if empty
#   4. Join the text chunks with "\n---\n" and return the result
#
# The docstring is the tool description — the model uses it to decide when
# to call this tool, so keep it clear and accurate.

@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    # 1. Guard check
    if not KB_ID or KB_ID == "<kbid>":
        return "Knowledge base not configured."

    try:
        # 2. Call Bedrock Retrieve API
        response = _bedrock_runtime.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query},
        )

        # 3. Extract retrieval results
        results = response.get("retrievalResults", [])
        if not results:
            return "No relevant information found in the knowledge base."

        # 4. Extract and join text chunks
        chunks = [
            r["content"]["text"]
            for r in results
            if "content" in r and "text" in r["content"]
        ]

        if not chunks:
            return "No relevant text found in the knowledge base results."

        return "\n---\n".join(chunks)

    except Exception as e:
        logger.error(f"Error searching knowledge base: {e}")
        return f"Error querying knowledge base: {str(e)}"


# ── TODO 7 — Loyalty Discount Tool (Code Interpreter) ────────────────────────
# Implement calculate_loyalty_discount() using the @tool decorator.
#
# The tool must:
#   1. Build a self-contained Python code string that:
#        • Defines earn_rates: {"standard": 1, "device": 2, "fresh": 5}
#        • Defines tier_rates: {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
#        • Calculates points_redeemed (floor to nearest 500, cap at 50% of order)
#        • Calculates tier_discount (applied to subtotal after points)
#        • Calculates final_total, total_savings, points_earned, remaining_points
#        • Prints a JSON result dict
#   2. Execute the code with code_session(REGION).invoke("executeCode", {...})
#      using language="python" and clearContext=True
#   3. Return the first result event as a JSON string
#   4. Include a fallback that computes only the tier discount if the
#      Code Interpreter is unavailable

@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price
    """
    # 1. Build self-contained Python code string
    code = f"""
import json

earn_rates = {{"standard": 1, "device": 2, "fresh": 5}}
tier_rates = {{"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}}

order_total = float({order_total})
loyalty_points = int({loyalty_points})
tier = "{tier}"
category = "{product_category}"

# 100 points = $1 discount, minimum redemption is 500 points
# Points redemption is capped at 50% of the order total
max_points_discount = order_total * 0.50
max_redeemable_pts = int(max_points_discount * 100)
eligible_pts = min(loyalty_points, max_redeemable_pts)
points_redeemed = (eligible_pts // 500) * 500
points_discount = round(points_redeemed / 100.0, 2)

# Tier discount is applied to the subtotal after points are deducted
subtotal_after_points = round(order_total - points_discount, 2)
tier_rate = tier_rates.get(tier, 0.0)
tier_discount = round(subtotal_after_points * tier_rate, 2)

# Final price calculations
final_total = round(subtotal_after_points - tier_discount, 2)
total_savings = round(points_discount + tier_discount, 2)
points_earned = int(final_total * earn_rates.get(category, 1))
remaining_points = loyalty_points - points_redeemed + points_earned

result = {{
    "order_total": order_total,
    "tier": tier,
    "points_redeemed": points_redeemed,
    "points_discount": points_discount,
    "subtotal_after_points": subtotal_after_points,
    "tier_discount": tier_discount,
    "tier_discount_pct": int(tier_rate * 100),
    "tier_rate_percent": int(tier_rate * 100),
    "final_total": final_total,
    "total_savings": total_savings,
    "points_earned": points_earned,
    "remaining_points": remaining_points,
}}

print(json.dumps(result))
"""

    try:
        # 2. Execute code in the AgentCore Code Interpreter sandbox
        with code_session(REGION) as session:
            resp = session.invoke(
                "executeCode",
                {
                    "code": code,
                    "language": "python",
                    "clearContext": True,
                },
            )

            # 3. Return the result event
            if isinstance(resp, dict):
                if "stream" in resp:
                    for event in resp["stream"]:
                        if "result" in event:
                            return json.dumps(event["result"])
                        if "stdout" in event:
                            return event["stdout"]
                if "stdout" in resp:
                    return resp["stdout"]
                if "result" in resp:
                    return json.dumps(resp["result"])

            return json.dumps(resp)

    except Exception as e:
        # 4. Fallback calculation if the Code Interpreter sandbox is unavailable
        logger.warning(f"Code interpreter unavailable ({e}). Using local fallback.")
        tier_rates = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
        tier_rate = tier_rates.get(tier, 0.0)
        tier_discount = round(order_total * tier_rate, 2)
        final_total = round(order_total - tier_discount, 2)

        fallback_result = {
            "order_total": order_total,
            "tier": tier,
            "points_redeemed": 0,
            "points_discount": 0.0,
            "subtotal_after_points": order_total,
            "tier_discount": tier_discount,
            "tier_discount_pct": int(tier_rate * 100),
            "tier_rate_percent": int(tier_rate * 100),
            "final_total": final_total,
            "total_savings": tier_discount,
            "points_earned": 0,
            "remaining_points": loyalty_points,
            "fallback": True,
            "note": "Calculated via fallback arithmetic (Code Interpreter unavailable)",
        }
        return json.dumps(fallback_result)


# ── Safe Gateway Tool Wrapper ────────────────────────────────────────────────
from strands.types._events import ToolResultEvent
from strands.tools.mcp.mcp_types import MCPToolResult
from strands.types.tools import AgentTool, ToolGenerator, ToolSpec, ToolUse

class SafeGatewayToolWrapper(AgentTool):
    """
    Wraps an MCP gateway tool to safely catch connection, timeout, and execution
    exceptions without exposing stack traces or secrets.
    """
    def __init__(self, tool):
        super().__init__()
        self._tool = tool

    @property
    def tool_name(self) -> str:
        return self._tool.tool_name

    @property
    def tool_spec(self) -> ToolSpec:
        return self._tool.tool_spec

    @property
    def tool_type(self) -> str:
        return self._tool.tool_type

    async def stream(self, tool_use: ToolUse, invocation_state: dict, **kwargs) -> ToolGenerator:
        tool_name = self.tool_name
        try:
            async for event in self._tool.stream(tool_use, invocation_state, **kwargs):
                if hasattr(event, "result") and getattr(event.result, "status", None) == "error":
                    safe_msg = (
                        f"The gateway operation '{tool_name}' encountered an error during execution. "
                        "Please verify the request details or retry in a few moments."
                    )
                    yield ToolResultEvent(MCPToolResult(
                        status="error",
                        toolUseId=tool_use["toolUseId"],
                        content=[{"text": safe_msg}],
                    ))
                    return
                yield event
        except asyncio.TimeoutError:
            logger.error(f"Gateway tool {tool_name} timed out during execution.")
            yield ToolResultEvent(MCPToolResult(
                status="error",
                toolUseId=tool_use["toolUseId"],
                content=[{
                    "text": f"The gateway operation '{tool_name}' timed out. Please retry the request."
                }],
            ))
        except Exception as e:
            logger.error(f"Gateway tool {tool_name} execution error: {e}")
            yield ToolResultEvent(MCPToolResult(
                status="error",
                toolUseId=tool_use["toolUseId"],
                content=[{
                    "text": f"The gateway operation '{tool_name}' failed to execute. Please check the configuration and retry."
                }],
            ))


# ── TODO 8 — Agent Entrypoint ─────────────────────────────────────────────────
# Implement the invoke() function decorated with @app.entrypoint.
#
# Steps:
#   1. Extract user_input, actor_id, and session_id from the payload
#      (generate a UUID if session_id is missing)
#   2. Instantiate MemoryHook for this actor/session
#   3. Instantiate AgentCoreBrowser(region=REGION)
#   4. Build the tools list: [search_knowledge_base, calculate_loyalty_discount,
#                              agent_core_browser.browser]
#   5. Connect to the Gateway via MCPClient, load gateway_tools, extend tools list
#   6. Create and invoke the Agent with all tools, hooks, and system_prompt
#   7. Return the text from the first content block of the response
#   8. Handle exceptions gracefully

@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """
    try:
        # 1. Extract user_input, actor_id, and session_id from the payload
        user_input = payload.get("prompt", "")
        actor_id = payload.get("customer_id", "default_customer")
        session_id = payload.get("session_id") or str(uuid.uuid4())

        # 2. Instantiate MemoryHook for this actor/session
        memory_hook = MemoryHook(
            actor_id=actor_id,
            session_id=session_id,
            memory_client=memory_client,
            memory_id=MEMORY_ID,
        )

        # 3. Instantiate AgentCoreBrowser(region=REGION)
        agent_core_browser = AgentCoreBrowser(region=REGION, session_timeout=600)

        # 4. Build tools list
        tools = [
            search_knowledge_base,
            calculate_loyalty_discount,
            agent_core_browser.browser,
        ]

        # 5. Connect to Gateway via MCPClient, load gateway_tools, extend tools list
        if GATEWAY_URL and GATEWAY_URL != "<gateway_url>":
            try:
                mcp_client = MCPClient(lambda: streamable_http_client(GATEWAY_URL))
                raw_gateway_tools = await asyncio.wait_for(mcp_client.load_tools(), timeout=10.0)
                wrapped_tools = [SafeGatewayToolWrapper(t) for t in raw_gateway_tools]
                tools.extend(wrapped_tools)
            except asyncio.TimeoutError:
                logger.error("Gateway connection timed out while loading tools.")
                return "The customer support gateway timed out while connecting to external tools. Please verify the gateway configuration or try again in a few moments."
            except ConnectionError as e:
                logger.error(f"Gateway connection error: {e}")
                return "Unable to establish connection to the customer support gateway. Please check that the gateway service is running and accessible, then try again."
            except Exception as e:
                logger.error(f"Failed to load MCP tools from Gateway: {e}")
                return "Unable to establish connection to the customer support gateway. Please check the gateway status and configuration, then try again."

        # 6. Create system prompt and Agent with all tools, hooks, and model
        system_prompt = (
            "You are a knowledgeable, friendly, and professional customer support assistant for an Amazon store. "
            "You assist customers with order tracking, return labels, refund processing, product specifications, "
            "return policies, and loyalty discount calculations.\n\n"
            "Always greet and address the customer by their name (e.g., 'Hello Jane!') if known from Customer Context. "
            "Always respect customer preferences and facts found in Customer Context (such as communication tone or conciseness).\n\n"
            "If any external tool or gateway operation fails, provide a concise and helpful message stating what operation failed "
            "and suggest a safe next action (such as checking input parameters or retrying later), without exposing internal secrets or stack traces.\n\n"
            "You have access to a live web browser tool (browser). Use it when asked to visit websites:\n"
            "1. Navigate to the requested URL (such as amazon.com or udacity.com)\n"
            "2. Read the page title or content\n"
            "3. Return the requested information clearly and concisely.\n\n"
            "Call the appropriate tools whenever specific information, calculations, or operations are needed."
        )

        agent = Agent(
            model=model,
            tools=tools,
            hooks=[memory_hook],
            system_prompt=system_prompt,
        )

        # Invoke the agent asynchronously
        result = await agent.invoke_async(user_input)

        # 7. Return text from the first content block
        if hasattr(result, "message") and isinstance(result.message, dict):
            for block in result.message.get("content", []):
                if "text" in block:
                    return block["text"]

        return str(result)

    except Exception as e:
        # 8. Handle exceptions gracefully
        logger.error(f"Error during agent invocation: {e}", exc_info=True)
        err_str = str(e).lower()
        if any(term in err_str for term in ["gateway", "mcp", "timeout", "connection", "http"]):
            return "The customer support gateway encountered an error while processing external tool requests. Please verify the service connection and retry."
        return "An unexpected error occurred while processing your request. Please check your configuration and try again."


# ── CLI entry point (do not modify) ──────────────────────────────────────────
def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    app.run()
    # Uncomment the line below and comment app.run() for local CLI testing:
    # main()
