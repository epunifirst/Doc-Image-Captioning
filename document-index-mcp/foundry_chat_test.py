from __future__ import annotations

import json
import os

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import MCPTool, PromptAgentDefinition
from azure.identity import AzureCliCredential
from dotenv import load_dotenv
from openai.types.responses.response_input_param import McpApprovalResponse

load_dotenv()

PROJECT_ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").strip()
MCP_CONNECTION_NAME = "doc-image-mcp-connection"
MCP_SERVER_URL = (
    "https://doc-image-mcp-app.agreeableforest-d2f7c861.eastus2."
    "azurecontainerapps.io/mcp"
)
AGENT_NAME = "doc-image-retrieval-test"
MODEL_DEPLOYMENT = "gpt-4.1"


def main() -> None:
    if not PROJECT_ENDPOINT:
        raise SystemExit(
            "FOUNDRY_PROJECT_ENDPOINT is missing. Set it in the local .env file."
        )

    project = AIProjectClient(
        endpoint=PROJECT_ENDPOINT,
        credential=AzureCliCredential(),
    )
    tool = MCPTool(
        server_label="document-images",
        server_url=MCP_SERVER_URL,
        project_connection_id=MCP_CONNECTION_NAME,
        allowed_tools=["retrieve_relevant_images"],
        require_approval="always",
    )
    agent = project.agents.create_version(
        agent_name=AGENT_NAME,
        definition=PromptAgentDefinition(
            model=MODEL_DEPLOYMENT,
            instructions=(
                "Answer the user's question using relevant information from "
                "the image retrieval tool. Use the returned metadata and SAS "
                "image URL as needed. Do not print Base64 image data."
            ),
            tools=[tool],
        ),
    )
    print(f"Created Foundry agent {agent.name}, version {agent.version}.")

    openai = project.get_openai_client()
    conversation = openai.conversations.create()
    question = input(
        "Ask a question about content indexed by the image-captioning pipeline: "
    ).strip()
    if not question:
        raise SystemExit("Enter a non-empty question.")

    response = openai.responses.create(
        conversation=conversation.id,
        input=question,
        extra_body={
            "agent_reference": {
                "name": agent.name,
                "type": "agent_reference",
            }
        },
    )

    while True:
        approvals = [
            item
            for item in response.output
            if item.type == "mcp_approval_request" and item.id
        ]
        if not approvals:
            break

        approval_responses = []
        for item in approvals:
            print("\nMCP tool approval requested")
            print(f"Server: {item.server_label}")
            print(f"Tool: {getattr(item, 'name', '<unknown>')}")
            arguments = getattr(item, "arguments", None)
            print(f"Arguments: {json.dumps(arguments, indent=2, default=str)}")
            approved = (
                input("Approve this image retrieval call? (y/N): ")
                .strip()
                .lower()
                == "y"
            )
            approval_responses.append(
                McpApprovalResponse(
                    type="mcp_approval_response",
                    approve=approved,
                    approval_request_id=item.id,
                )
            )

        response = openai.responses.create(
            input=approval_responses,
            previous_response_id=response.id,
            extra_body={
                "agent_reference": {
                    "name": agent.name,
                    "type": "agent_reference",
                }
            },
        )

    print("\nAgent response:")
    print(response.output_text)
    print(
        "\nThis test creates a persistent Foundry agent version. "
        "Delete it from the Foundry project when you no longer need it."
    )


if __name__ == "__main__":
    main()
