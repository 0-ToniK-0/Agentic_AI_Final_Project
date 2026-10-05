# Third-Party Notices

This project's own code is released under the MIT License (see `LICENSE`). It does not include or
redistribute any third-party source code. It installs the open-source packages below with `pip`, and each one
remains under its own license.

## Python packages

| Package | License | Used for |
|---|---|---|
| langchain, langchain-core | MIT | LLM calls, prompts, structured output |
| langchain-ollama | MIT | Local chat model and embeddings through Ollama |
| langchain-openai | MIT | Optional OpenAI chat model |
| langchain-text-splitters | MIT | Chunking the knowledge documents |
| langchain-mcp-adapters | MIT | Loading MCP tools into LangChain |
| langgraph | MIT | The supervisor workflows, interrupts and checkpoints |
| langgraph-checkpoint-sqlite | MIT | Saving paused runs in the command-line app |
| langsmith | MIT | Optional tracing of agents, prompts, time and tokens |
| mcp (Model Context Protocol Python SDK) | MIT | The four MCP servers and the client |
| msal (Microsoft Authentication Library) | MIT | Optional sign-in to Microsoft Graph |
| pydantic | MIT | Schemas for the agents' structured outputs |
| python-docx | MIT | Writing the Word documents |
| python-dotenv | BSD-3-Clause | Reading settings from `.env` |
| requests | Apache-2.0 | Azure DevOps, Microsoft Graph and SharePoint REST calls |
| numpy | BSD-3-Clause (with bundled MIT, 0BSD, Zlib and CC0 parts) | Dependency of the vector store |
| docx-mcp-server 0.7.4 (optional, `tools/docx-mcp`) | MIT | Word editing with tracked changes. `tools/docx-mcp/server.py` imports and adapts it at run time; its code is not copied into this repository. Source: https://github.com/SecurityRonin/docx-mcp |

The licenses were read from each package's published metadata (PyPI) for the versions used during development.

## Tools, models and services

- **Ollama** runs the local models. It is installed separately from https://ollama.com and is distributed under the MIT License.
- **Models** (`gemma4:e4b` for the agents, from the Gemma family by Google DeepMind; `nomic-embed-text` for the embeddings,
  from Nomic AI) are downloaded by the user with `ollama pull`. They are not part of this repository, and each
  one is distributed under its publisher's own license and terms.
- **OpenAI API**, **LangSmith**, **Microsoft Graph (Outlook, SharePoint)** and **Azure DevOps** are optional online
  services. They are used only when the user adds their own credentials in `.env`, and they are subject to
  their providers' terms.

## Trademarks

Microsoft, Dynamics 365, Azure DevOps, Outlook, SharePoint, Microsoft Entra ID and Word are trademarks of the
Microsoft group of companies. All other product names are trademarks of their respective owners. This is an
academic project and is not affiliated with or endorsed by any of these companies.
