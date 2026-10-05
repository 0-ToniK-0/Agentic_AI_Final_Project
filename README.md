# Agentic AI Requirements-to-Delivery Assistant for Dynamics 365 F&O

**Toni Kanaan - Agentic AI Course, Final Project (LAU)**

A multi-agent system that turns customer emails about Microsoft Dynamics 365 Finance and Operations (F&O)
into approved, traceable delivery work. A person approves every step that matters.

- **Change requests:** an unclear request email becomes a clarification email to the customer, an approved
  specification, user stories with Given/When/Then acceptance criteria, linked Azure DevOps tasks with
  recommended owners, a Functional Design Document (FDD), a Technical Design Document (TDD), test cases with a
  traceability matrix and, when needed, a Change Request form. All of it is exported to Word.
- **Support emails:** a support email becomes an Azure DevOps ticket under the customer's support Feature,
  split into small tasks with estimates and due dates, followed by an acknowledgement email to the customer.

Everything runs locally without any account. Email, Azure DevOps and SharePoint work in a **dry-run mode** on
local files with fictional demo data, and the language models run on **Ollama**.

## The problem and its users

In an F&O partner, consultants and developers receive requests by email that are often incomplete ("block
vendors without bank details", "approve invoices above a certain amount"). Turning them into clarified
requirements, work items, designs and tests is slow and repetitive, and details get lost between steps. The
users are the delivery team: functional consultants, developers, testers and the project manager who approves
what is sent to the customer and what goes into Azure DevOps.

## How it works

```
Change request email
  -> Requirements Agent            extracts needs as REQ-001, REQ-002, ...
  -> Gap and Retrieval Agent       searches approved knowledge (RAG over MCP), finds missing information
  -> CR Assessment Agent           support or Change Request?
  -> Communication Agent           writes the clarification email
  -> [human] approve the email -> draft saved -> [human] approve sending it
  -> wait for the customer's answers
  -> Requirements Agent            refines the specification with the answers
  -> [human] approve the specification      (approved facts are locked and cannot be changed)
  -> Planning Agent                user stories, tasks, estimates, recommended owners
  -> Functional Design Agent and Technical Design Agent, in parallel
  -> Test Agent
  -> Validation Agent              traceability check + LLM consistency review;
                                   problems go back to the responsible agent (reflexion, at most 3 rounds)
  -> [human] final approval
  -> Word documents, linked Azure DevOps work items, SharePoint folder

Support email
  -> customer's support Feature (remembered per email domain) -> Support Planning Agent -> CR Assessment Agent
  -> due dates and recommended owners -> [human] approve the ticket -> ticket, tasks and attachments created
  -> Change Request Agent (only when needed) -> SharePoint -> Support Reply Agent
  -> [human] approve the reply -> draft saved -> [human] approve sending it
```

### Agents

| Agent | Role |
|---|---|
| Supervisor | The LangGraph graph itself: controls the order, the handoffs, the parallel branch and the human gates |
| Requirements Agent | Extracts actors, rules, scope and requirements; refines them with the confirmed answers |
| Gap and Retrieval Agent | Classifies the request, retrieves approved knowledge, lists the missing information |
| Communication Agent | Drafts the clarification email and maps the customer's reply to the open questions |
| Planning Agent | User stories, acceptance criteria, FUNC/TECH tasks with estimates, recommended owners |
| Functional / Technical Design Agents | FDD and TDD, written in parallel from the same approved specification |
| Test Agent | Functional, technical, integration and regression test cases |
| Validation Agent | Completeness, consistency and traceability; sends failures back with a reflexion memory |
| CR Assessment Agent / Change Request Agent | Decide whether a Change Request is needed and fill in the form |
| Support Planning Agent / Support Reply Agent | Support ticket with small tasks, and the acknowledgement email |

### Coordination, tools and memory

- **Pattern:** a supervisor, implemented as a LangGraph `StateGraph`. Handoffs are sequential where a step
  depends on approved earlier results, with one parallel branch (FDD and TDD). After the specification is
  approved, the Planning, Design and Test Agents and their revisions all receive the same structured handoff
  package (requirement IDs, source text, classification, confirmed answers, assumptions, evidence, approval
  status, immutable facts).
- **Tools through MCP:** four local MCP servers (FastMCP over stdio). The agents reach external systems only
  through them:
  - **Knowledge:** semantic search over approved knowledge, plus whole documents.
  - **Email:** Outlook through Microsoft Graph, or a local mailbox.
  - **Azure DevOps:** workload, customer Features and creating work items.
  - **SharePoint:** document filing.

  A fifth, standalone server in `tools/docx-mcp` edits Word documents with tracked changes. The notebooks and
  the app do not use it yet; it is included as groundwork for editing existing customer documents.
- **Memory and retrieval:**
  - **RAG:** retrieval over approved knowledge, embedded with `nomic-embed-text`, with metadata filters.
    Unapproved documents are never returned.
  - **Reflexion memory:** carries the lessons from earlier validation rounds into each revision.
  - **Remembered Features:** the customer's support Feature is remembered per email domain.
  - **Checkpoints:** LangGraph checkpoints let a run pause at a human gate and resume later.

### Safeguards

- **Human gates:** LangGraph `interrupt()` stops the workflow at every consequential step (approve, revise,
  reject).
- **Approval ledger checked outside the model:** every human decision is recorded with an ID. The MCP servers
  refuse to save a draft, send an email or create work items without a matching approval. An email needs two
  approvals: one for the text, and one for sending that exact draft.
- **Auto mode** (scripted demo decisions) never approves a live action: a real email or real work items.
- **Immutable facts:** once the specification is approved, a custom reducer ignores any later attempt to
  change the approved facts.
- **Prompt-injection guard:** customer emails are untrusted input, so three layers protect the agents (see
  the "Prompt-Injection Guard" section of the final notebook):
  - **Input check:** hidden characters and HTML comments are removed and instruction-like text is flagged.
  - **Spotlighting:** customer text is marked as data in every prompt.
  - **Output check:** every outgoing email is checked before it is saved or sent.

  Findings are shown at every gate, and auto mode never approves a flagged run.
- **Scope limits:**
  - **Support filter:** the assistant only reads emails that pass a filter.
  - **Attachment limit:** only generated documents can be attached to an email.
  - **SharePoint links:** only links inside the configured SharePoint site can be added to work items.

## Repository layout

| Path | Contents |
|---|---|
| `Final_Notebook.ipynb` | **The complete system**: both workflows, all agents, the MCP servers, the guardrail checks and the results |
| `01_LangChain_Requirements_Agents.ipynb` | Requirements Agent and Gap and Retrieval Agent with structured outputs |
| `02_RAG_Knowledge_Base.ipynb` | Knowledge base, retrieval with metadata filters, the knowledge MCP server and an agent that calls its tools through function calling |
| `03_Email_Communication.ipynb` | Outlook through Microsoft Graph or a local mailbox, the email MCP server, the approval tool, the Communication Agent |
| `04_Azure_DevOps_Work_Items.ipynb` | Planning Agent, owner recommendation, the Azure DevOps MCP server, support emails to work items |
| `05_Design_Documents_and_Tests.ipynb` | Functional and Technical Design Agents in parallel, Test Agent, Word export, Change Requests |
| `06_Validation_and_Reflexion.ipynb` | Traceability matrix, LLM consistency review, reflexion revision loop |
| `app/` | The same workflows as a command-line program with a human in the loop (see `app/README.md`) |
| `tools/docx-mcp/` | Optional Word documents MCP server with tracked changes (see its README) |

Notebooks 01 to 06 build the parts step by step, and the final notebook combines them. Each one runs on its own.
The one exception is notebook 04, which reads support emails through the email server written by notebook 03.
Without that server, notebook 04 uses a sample email instead.

## Installation

1. **Python 3.12 or newer.**
2. **Ollama** from https://ollama.com. Start it, then download the two models:
   ```bash
   ollama pull gemma4:e4b
   ollama pull nomic-embed-text
   ```
3. **Get the code and install the packages:**
   ```bash
   git clone https://github.com/0-ToniK-0/Agentic_AI_Final_Project.git
   cd Agentic_AI_Final_Project
   python -m venv .venv
   ```
   Activate the environment (Windows: `.venv\Scripts\activate`, macOS/Linux: `source .venv/bin/activate`), then:
   ```bash
   pip install -r requirements.txt jupyterlab
   ```
4. **Settings (optional).** Nothing is needed for the demo.
   - **Change the defaults:** copy `.env.example` to `.env`. The defaults are dry-run mode and the local
     `gemma4:e4b` model.
   - **OpenAI instead of Ollama:** set `LLM_PROVIDER=openai` and `OPEN_AI_KEY`. The embeddings still use Ollama.
   - **Live services:** the Microsoft Graph, Azure DevOps and SharePoint settings switch from dry-run to the
     real services. Notebooks 03 and 04 explain how to register and connect them.
   - **Tracing (optional):** add a LangSmith API key in `.env` to record every agent's prompts, time and tokens.

   Never commit `.env`; it is listed in `.gitignore`.

The notebooks also run on Google Colab. Their first cells install the packages and Ollama there.

## Running the system

**Option 1: the final notebook (recommended).**
```bash
jupyter lab Final_Notebook.ipynb
```
Run all cells from top to bottom.

- **Demo mode:** `HUMAN_MODE = "auto"` approves every gate and uses a simulated customer reply.
- **Your own decisions:** set `HUMAN_MODE = "interactive"` to approve, revise or reject each step and type the
  customer's reply yourself.
- **Results:** section 8 runs the change-request workflow on the demo email. Section 9 shows the audit log,
  plan, traceability matrix, Word files, work items, approval ledger and the two guardrail checks, plus the
  time and tokens per agent when a LangSmith key is set. Section 10 runs the support workflow.

**Option 2: the command-line app.**
```bash
cd app
python run.py setup
python run.py delivery
python run.py support
```
The app lists the emails and stops at every gate with a menu (approve, revise, reject, pause). A paused run is
saved and continues with `python run.py resume <run id>`, for example after the customer has answered. See
`app/README.md` for all commands.

Generated files (knowledge store, local mailbox, approval ledger, dry-run board, Word documents in `output/`)
are written next to the notebook, or in `app/data/` for the app. All of them are ignored by git.

## Evaluation

The system checks its own output and records evidence of every step:

- **Traceability (deterministic):** every requirement must be covered by an acceptance criterion, a design
  section and a test case, and every reference must point to an approved requirement.
- **Consistency review (LLM as a judge):** the Validation Agent compares the plan, designs and tests with the
  locked facts and returns blocking issues to the responsible agent until they pass.
- **Guardrail checks:** in the final notebook, these show that the locked facts cannot be overwritten and that
  a prompt-injection email (`MSG-1005` in the demo mailbox) is flagged and never auto-approved.
- **Audit trail:** the audit log, the approval ledger and the email outbox record what each agent and each
  person did.
- **Observability:** with LangSmith, the time and token count of every agent and tool call.

## Limitations

- **Small test set:** the demo covers one change request and a few support emails. A larger labelled set of
  emails would be needed to measure accuracy, for example of the CR assessment or the gap analysis.
- **Estimates and owners:** hour estimates come from the language model, and owner recommendations use a fixed
  demo team (`team.json`). Owners are recommended, never assigned.
- **Tool calls:** in the final workflow the code calls the MCP tools at fixed steps, so every consequential
  action passes a human gate. LLM-driven tool selection through function calling is shown in notebook 02.
- **Prompt-injection guard:** it uses patterns. It catches common attacks, not every possible one, so the human
  gates and the approval checks in the MCP servers remain the last line of defence.
- **Approval ledger:** it is a local JSON file. Anyone who can write to it could add an approval, and the final
  delivery approval is checked by type rather than tied to one request. A production version would need a
  protected store and approvals bound to the exact artefact.
- **Support filter:** it relies on keywords and sender domains, which can be imitated.
- **Lost runs:** the notebook keeps paused runs in memory, so they are lost when the kernel restarts. The app
  saves them in SQLite.
- **Privacy:** with LangSmith enabled, prompts (including customer emails) are sent to LangSmith. Use test data
  or your own workspace.

## Data and privacy

The demo data was written for this project and contains no real customer data. All demo email addresses use
the reserved `.example` domain. The company standards, the Change Request form and the team in the demo are
generic examples of a Dynamics 365 implementation partner's practice. The repository contains no credentials;
settings are read from a local `.env` file that is not committed.

## License

The code is released under the MIT License (see `LICENSE`). Third-party packages, models and services keep
their own licenses; see `THIRD_PARTY_NOTICES.md`.
