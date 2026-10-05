"""Command-line runner for the F&O Requirements-to-Delivery Assistant (the workflows of Final_Notebook.ipynb).

It fetches an email, runs the agents, and stops at every human gate so you can approve, revise, reject or pause.
A paused run is saved (data/checkpoints.sqlite) and can be resumed later, for example when the customer answers.

    python run.py setup                 build the knowledge store, prepare the demo mailbox and board (dry-run)
    python run.py inbox                 list the support emails the assistant may read
    python run.py delivery              change request:  email -> clarification -> spec -> plan/designs/tests -> publish
    python run.py support               support email:   email -> ticket with tasks -> acknowledgement email
    python run.py runs                  list saved runs and where each one is waiting
    python run.py resume <run id>       continue a paused run
    python run.py tools                 list the tools of the four MCP servers
    python run.py graph                 print both workflows as Mermaid diagrams

Options: --auto (scripted demo decisions; never approves a live email or live work items),
         --message-id (skip the inbox choice), --request-id (delivery), --data-dir (where runtime files go).
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = APP_DIR / "data"
MENU = {"approve": "Approve", "revise": "Revise (send comments back to the agent)", "reject": "Reject (stop here)",
        "pause": "Pause (save the run and decide later)"}
AUTO = False


class PauseRun(Exception):
    """Raised at a gate when the person chooses to decide later."""


# ---------------------------------------------------------------- environment
def load_environment(data_dir: Path) -> None:
    """Read .env (app/.env first, then the notebook folder's .env), then work inside the data folder."""
    from dotenv import load_dotenv

    for env_file in (APP_DIR / ".env", APP_DIR.parent / ".env"):
        if env_file.exists():
            load_dotenv(env_file, override=False)

    if os.getenv("LANGSMITH_API_KEY") and os.getenv("LANGSMITH_TRACING", "true").lower() != "false":
        # Traces contain the prompts, so customer emails are sent to LangSmith.
        os.environ.setdefault("LANGSMITH_PROJECT", "fo-requirements-to-delivery")
        os.environ["LANGSMITH_TRACING"] = "true"
    else:
        os.environ["LANGSMITH_TRACING"] = "false"

    data_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(data_dir)  # every runtime file (mailbox, approvals, board, output/ ...) is written here
    sys.path.insert(0, str(APP_DIR))


def ollama_running() -> bool:
    import requests

    try:
        return requests.get("http://localhost:11434/api/tags", timeout=3).ok
    except requests.RequestException:
        return False


def check_ready(wf) -> None:
    """Stop early with a clear message instead of failing in the middle of a run."""
    problems = []
    if not ollama_running():
        problems.append("Ollama is not running (needed for the embeddings): install it from https://ollama.com, "
                        f"then 'ollama pull {wf.EMBED_MODEL}'" + (f" and 'ollama pull {wf.OLLAMA_MODEL}'"
                                                                    if wf.LLM_PROVIDER == "ollama" else ""))
    if not Path("fo_knowledge_store.json").exists():
        problems.append("No knowledge store yet: run 'python run.py setup' first.")
    if problems:
        sys.exit("Not ready:\n- " + "\n- ".join(problems))


def checkpointer():
    """SQLite checkpoints survive a restart (pause and resume). Without the package, runs live in memory only."""
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError:
        from langgraph.checkpoint.memory import InMemorySaver

        print("Note: pip install langgraph-checkpoint-sqlite to pause and resume runs. Using memory for now.")
        return InMemorySaver()
    return SqliteSaver(sqlite3.connect("checkpoints.sqlite", check_same_thread=False))


# ---------------------------------------------------------------- the human in the loop
def ask_number(prompt: str, count: int, default: int | None = None) -> int:
    while True:
        answer = input(f"{prompt} [1-{count}{', Enter = ' + str(default + 1) if default is not None else ''}]: ").strip()
        if not answer and default is not None:
            return default
        if answer.isdigit() and 1 <= int(answer) <= count:
            return int(answer) - 1
        print("Please type one of the numbers.")


def read_multiline(prompt: str) -> str:
    print(f"{prompt} Finish with a line containing only END.")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "END":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def decide(request: dict) -> dict:
    """An approval gate: show the artefact and return {"decision", "comments"}."""
    options = list(request.get("options", ("approve", "revise", "reject")))
    live = request.get("live", False)
    print(f"\n{'=' * 20} HUMAN REVIEW: {request['title']} {'=' * 20}\n")
    print(request["artefact"])
    print()
    if live:
        print("!! LIVE ACTION: approving this sends a real email or creates real work items.")

    if AUTO:  # scripted demo decisions, as HUMAN_MODE = "auto" in the notebook
        if request.get("security"):
            print("[auto] possible prompt injection: a person must review this gate.")
            raise PauseRun()
        if live:
            comments = "Auto mode never approves a live action. Run without --auto to decide yourself."
            print(f"[auto] decision = reject: {comments}")
            return {"decision": "reject", "comments": comments}
        print("[auto] decision = approve")
        return {"decision": "approve", "comments": "Approved in auto demo mode."}

    choices = options + ["pause"]
    for number, option in enumerate(choices, start=1):
        print(f"  {number}. {MENU[option]}")
    decision = choices[ask_number("Your decision", len(choices))]
    if decision == "pause":
        raise PauseRun()
    if decision == "approve" and live and input("Type YES to confirm the live action: ").strip() != "YES":
        print("Not confirmed: the run is paused instead.")
        raise PauseRun()
    if decision == "approve" and request.get("security") and \
            input("Possible prompt injection (see the warning above). Type YES to approve anyway: ").strip() != "YES":
        print("Not confirmed: the run is paused instead.")
        raise PauseRun()
    comments = ""
    while decision == "revise" and not comments:
        comments = input("What should change? ").strip()
    if decision != "revise":
        comments = input("Comments (optional): ").strip()
    return {"decision": decision, "comments": comments}


def customer_reply(request: dict, wf) -> dict:
    """The workflow waits for the customer's answers to the clarification email."""
    print(f"\n{'=' * 20} {request['title']} {'=' * 20}\n")
    print("The clarification email that was sent:\n")
    print(request["artefact"])
    if AUTO:
        import demo_data

        print("\n[auto] simulated customer reply:\n" + demo_data.SIMULATED_CUSTOMER_REPLY)
        return {"reply": demo_data.SIMULATED_CUSTOMER_REPLY}

    while True:
        print("\nHow do you want to give the customer's answers?")
        choices = ["Pick the customer's reply from the mailbox", "Paste the reply here",
                   "Load the reply from a text file", "Pause (resume when the customer has answered)"]
        for number, choice in enumerate(choices, start=1):
            print(f"  {number}. {choice}")
        choice = ask_number("Your choice", len(choices))
        if choice == 3:
            raise PauseRun()
        if choice == 1:
            reply = read_multiline("Paste the customer's reply.")
        elif choice == 2:
            path = Path(input("Path of the text file: ").strip().strip('"'))
            reply = path.read_text(encoding="utf-8") if path.is_file() else ""
            if not reply:
                print(f"Could not read {path}.")
        else:
            message_id = pick_email(wf, "Which email is the customer's reply?")
            reply = wf.call_mcp_tool("email", "get_email", message_id=message_id).get("body", "") if message_id else ""
        if reply:
            print("\nCustomer reply:\n" + reply)
            return {"reply": reply}


def choose(request: dict) -> dict:
    """A choice, for example the customer's support Feature in Azure DevOps."""
    print(f"\n{'=' * 20} CHOOSE: {request['title']} {'=' * 20}\n")
    for number, option in enumerate(request["options"], start=1):
        print(f"  {number}. {option}{'   (suggested)' if request['default'] == number - 1 else ''}")
    if AUTO:
        if request["default"] is None:
            raise ValueError(f"No default for '{request['title']}': run without --auto to choose.")
        print(f"[auto] choice = {request['default'] + 1}")
        return {"index": request["default"]}
    return {"index": ask_number("Your choice", len(request["options"]), request["default"])}


def respond(request: dict, wf) -> dict:
    if request["type"] == "customer_reply":
        return customer_reply(request, wf)
    if request["type"] == "choice":
        return choose(request)
    return decide(request)


# ---------------------------------------------------------------- emails
def pick_email(wf, title: str, demo_default: str | None = None) -> str | None:
    """Fetch the support emails through the email MCP server and let the person pick one."""
    inbox = wf.call_mcp_tool("email", "list_support_emails", max_emails=15)
    if not isinstance(inbox, list) or not inbox:
        print("No support emails found: check the mailbox folder and the support filter in .env.")
        return None
    ids = [e["message_id"] for e in inbox]
    default = ids.index(demo_default) if demo_default in ids else 0
    print(f"\n{'=' * 20} {title} {'=' * 20}\n")
    for number, email in enumerate(inbox, start=1):
        attachments = f"  [{', '.join(email['attachments'])}]" if email["attachments"] else ""
        print(f"  {number}. {email['received'][:16]}  {email['subject']}\n      from {email['from']}{attachments}")
        if email.get("injection_flags"):
            print(f"      !! possible prompt injection: {', '.join(email['injection_flags'])}")
        print(f"      {email['preview'][:150]}")
    if AUTO:
        print(f"[auto] email = {default + 1}")
        return ids[default]
    return ids[ask_number("Which email", len(inbox), default)]


# ---------------------------------------------------------------- running and resuming a graph
def drive(graph, config: dict, first_input, wf) -> dict | None:
    """Invoke the graph and answer every interrupt until the end, or until the person pauses."""
    from langgraph.types import Command

    thread_id = config["configurable"]["thread_id"]
    try:
        result = graph.invoke(first_input, config) if first_input is not None else None
        while True:
            if result is None:  # resuming: the pending interrupt is in the saved state
                pending = [i for task in graph.get_state(config).tasks for i in task.interrupts]
            else:
                pending = result.get("__interrupt__") or []
            if not pending:
                return result if result is not None else graph.get_state(config).values
            result = graph.invoke(Command(resume=respond(pending[0].value, wf)), config)
    except (PauseRun, EOFError, KeyboardInterrupt):  # Ctrl+C at a prompt pauses too: the gate is saved
        print(f"\nRun paused and saved. Continue it with:\n    python run.py resume {thread_id}")
        return None


def run_config(thread_id: str, run_name: str, metadata: dict, wf) -> dict:
    return {"configurable": {"thread_id": thread_id}, "run_name": run_name,
            "tags": ["cli", run_name.split()[0]],
            "metadata": {"thread_id": thread_id, "llm_model": wf.LLM_MODEL, **metadata}}


def start_delivery(args, wf) -> None:
    check_ready(wf)
    wf.load_standards()
    message_id = args.message_id or pick_email(wf, "Email with the change request", demo_default="MSG-1001")
    if not message_id:
        return
    request_id = args.request_id or f"CR-{datetime.now():%Y%m%d-%H%M}"
    graph = wf.builder.compile(checkpointer=checkpointer())
    thread_id = f"delivery-{request_id}"
    config = run_config(thread_id, f"{request_id} delivery workflow", {"request_id": request_id}, wf)
    if graph.get_state(config).values:
        sys.exit(f"{thread_id} already exists: 'python run.py resume {thread_id}', or pass a new --request-id.")
    state = drive(graph, config, {"request_id": request_id, "message_id": message_id, "revision_count": 0}, wf)
    if state is not None:
        print_delivery_results(state)


def start_support(args, wf) -> None:
    check_ready(wf)
    wf.load_standards()
    message_id = args.message_id or pick_email(wf, "Support email to turn into a ticket", demo_default="MSG-1004")
    if not message_id:
        return
    graph = wf.support_builder.compile(checkpointer=checkpointer())
    thread_id = f"support-{re.sub(r'[^A-Za-z0-9]', '', message_id)[-12:]}-{datetime.now():%Y%m%d%H%M%S}"
    config = run_config(thread_id, "Support email workflow", {"message_id": message_id}, wf)
    state = drive(graph, config, {"message_id": message_id}, wf)
    if state is not None:
        print_support_results(state)


def resume(args, wf) -> None:
    check_ready(wf)
    wf.load_standards()
    kind = args.run_id.split("-", 1)[0]
    if kind not in ("delivery", "support"):
        sys.exit("A run id starts with 'delivery-' or 'support-': see 'python run.py runs'.")
    graph = (wf.builder if kind == "delivery" else wf.support_builder).compile(checkpointer=checkpointer())
    config = run_config(args.run_id, "Support email workflow" if kind == "support" else f"{args.run_id} delivery workflow",
                        {}, wf)
    snapshot = graph.get_state(config)
    if not snapshot.values:
        sys.exit(f"No saved run {args.run_id}: see 'python run.py runs'.")
    if not snapshot.next:
        print(f"{args.run_id} has already finished.")
        state = snapshot.values
    else:
        state = drive(graph, config, None, wf)
    if state is not None:
        (print_delivery_results if kind == "delivery" else print_support_results)(state)


def list_runs(args, wf) -> None:
    saver = checkpointer()
    threads = []
    for checkpoint in saver.list(None):
        thread_id = checkpoint.config["configurable"]["thread_id"]
        if thread_id not in threads:
            threads.append(thread_id)
    if not threads:
        print("No saved runs.")
        return
    for thread_id in sorted(threads):
        builder = wf.builder if thread_id.startswith("delivery") else wf.support_builder
        snapshot = builder.compile(checkpointer=saver).get_state({"configurable": {"thread_id": thread_id}})
        pending = [i.value.get("title", "") for task in snapshot.tasks for i in task.interrupts]
        status = snapshot.values.get("status") or (f"waiting: {pending[0]}" if pending else
                                                   f"next: {', '.join(snapshot.next)}" if snapshot.next else "finished")
        print(f"{thread_id:<45} {status}")


# ---------------------------------------------------------------- results (notebook section 9 and the support results)
def print_delivery_results(state: dict) -> None:
    import workflow as wf

    print("\nSTATUS:", state.get("status"))
    wf.show("AUDIT LOG", state.get("audit_log", []))
    if state.get("locked_facts"):
        wf.show("IMMUTABLE FACTS", state["locked_facts"])
    if state.get("plan"):
        print("\n========== DELIVERY PLAN ==========")
        wf.print_plan(state["plan"], state.get("owner_recommendations"))
    if state.get("matrix"):
        print("\n========== TRACEABILITY MATRIX ==========\n")
        wf.print_matrix(state["matrix"])
        wf.show("REFLEXION MEMORY", state.get("reflections") or "no revisions were needed")
    if state.get("exported_files"):
        print("\n========== EXPORTED FILES ==========")
        for path in state["exported_files"]:
            print(Path(path).resolve())
    if state.get("sharepoint_folder"):
        print(f"\nSharePoint folder: {state['sharepoint_folder']['path']}\n   {state['sharepoint_folder']['web_url']}")
    if state.get("work_items"):
        print("\n========== AZURE DEVOPS WORK ITEMS ==========")
        for item in state["work_items"]:
            print(f"{'      ' if item.get('parent_id') else ''}#{item['id']} {item['type']:<11} {item['title']}")
    refs = {state.get("request_id"), state.get("message_id"), (state.get("clarification_draft") or {}).get("draft_id")}
    print_ledger(refs)


def print_support_results(state: dict) -> None:
    import workflow as wf

    print("\nSTATUS:", state.get("status"))
    wf.show("SUPPORT AUDIT LOG", state.get("audit_log", []))
    created = state.get("created")
    if created:
        story = created["story"]
        print(f"\n========== AZURE DEVOPS ({created['mode']}, project {created['project']}) ==========\n")
        print(f"#{story['id']} User Story  {story['title']}  (child of Feature #{state['feature']['id']})")
        for task in created["tasks"]:
            print(f"      #{task['id']} Task  {task['title']} | {task['activity']} | {task['estimate_hours']}h | "
                  f"due {task['due_date']}")
        for attachment in created["attachments"]:
            print(f"      attachment {attachment['name']}: {attachment['status']}")
    if state.get("cr"):
        print(f"\nCHANGE REQUEST: {state['cr']['request_type']} -> {'required' if state['cr']['cr_required'] else 'not needed'}")
        if state.get("change_request"):
            print("   form:", Path(state["change_request"]["path"]).resolve())
    if state.get("sharepoint_folder"):
        print(f"\nSHAREPOINT: {state['sharepoint_folder']['path']}\n   {state['sharepoint_folder']['web_url']}")
    if state.get("reply"):
        print(f"\n========== ACKNOWLEDGEMENT ==========\n\nSubject: {state['reply']['subject']}\n")
        print(state["reply"]["body"])
    print_ledger({state.get("message_id"), (state.get("reply_draft") or {}).get("draft_id")})


def print_ledger(refs: set) -> None:
    ledger = Path("approvals.json")
    records = [r for r in json.loads(ledger.read_text(encoding="utf-8")) if r["artefact_ref"] in refs] \
        if ledger.exists() else []
    print("\n========== HUMAN DECISIONS (approval ledger) ==========")
    for r in records:
        print(f"{r['timestamp']}  {r['approval_id']}  {r['artefact_type']:<22} {r['decision']:<9} {r['approver']}"
              f"{'  - ' + r['comments'] if r['comments'] else ''}")


# ---------------------------------------------------------------- setup and helpers
def setup(args, wf_unused=None) -> None:
    import demo_data
    import knowledge_base
    import mail_client
    import sharepoint_client
    from ado_client import AzureDevOpsClient

    mail = mail_client.MailClient()
    print("Mailbox:", mail.mode, "| Azure DevOps:", AzureDevOpsClient().mode,
          "| SharePoint:", sharepoint_client.SharePointClient().mode)

    if not ollama_running():
        sys.exit("Ollama is not running. Install it from https://ollama.com, start it, then run:\n"
                 f"    ollama pull {os.getenv('EMBED_MODEL', 'nomic-embed-text')}\n"
                 + (f"    ollama pull {os.getenv('OLLAMA_MODEL', 'gemma4:e4b')}\n"
                    if os.getenv("LLM_PROVIDER", "ollama").lower() == "ollama" else ""))
    print("Knowledge store:", knowledge_base.build_knowledge_store(), "chunks embedded ->",
          Path(knowledge_base.STORE_FILE).resolve())

    if not mail.live and (args.reset_demo or not Path("mailbox/messages.json").exists()):
        print("Demo mailbox:", demo_data.seed_mailbox(), "emails")
    if not AzureDevOpsClient().live and (args.reset_demo or not Path("ado_dry_run_board.json").exists()):
        demo_data.seed_board()
        print("Demo Azure DevOps board ready (3 customer support Features)")
    if args.reset_demo:
        Path("approvals.json").unlink(missing_ok=True)

    print(mail.sign_in())  # live + delegated: device-code sign-in the first time, cached afterwards
    print(sharepoint_client.SharePointClient().sign_in())
    print("\nReady. Next: python run.py delivery   or   python run.py support")


def show_inbox(args, wf) -> None:
    inbox = wf.call_mcp_tool("email", "list_support_emails", max_emails=15)
    print(json.dumps(inbox, indent=2, ensure_ascii=False))


def show_tools(args, wf) -> None:
    for server in wf.MCP_SERVERS:
        wf.list_mcp_tools(server)


def show_graphs(args, wf) -> None:
    for name, builder in (("Delivery workflow", wf.builder), ("Support workflow", wf.support_builder)):
        print(f"%% {name}\n{builder.compile().get_graph().draw_mermaid()}\n")


def main() -> None:
    global AUTO
    if sys.version_info < (3, 12):
        sys.exit("Python 3.12 or newer is needed.")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="where runtime files are written")
    parser.add_argument("--auto", action="store_true", help="scripted demo decisions (never approves live actions)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="build the knowledge store and the demo data").add_argument(
        "--reset-demo", action="store_true", help="rewrite the demo mailbox and board and clear the approvals")
    commands.add_parser("inbox", help="list the support emails")
    delivery = commands.add_parser("delivery", help="run the delivery workflow on a change request email")
    delivery.add_argument("--message-id")
    delivery.add_argument("--request-id", help="e.g. CR-2026-001 (default: CR-<date-time>)")
    commands.add_parser("support", help="run the support workflow on a support email").add_argument("--message-id")
    commands.add_parser("resume", help="continue a paused run").add_argument("run_id")
    commands.add_parser("runs", help="list saved runs")
    commands.add_parser("tools", help="list the MCP tools")
    commands.add_parser("graph", help="print the workflows as Mermaid")
    args = parser.parse_args()
    AUTO = args.auto

    load_environment(args.data_dir.resolve())
    if args.command == "setup":
        return setup(args)
    import workflow

    {"inbox": show_inbox, "delivery": start_delivery, "support": start_support, "resume": resume,
     "runs": list_runs, "tools": show_tools, "graph": show_graphs}[args.command](args, workflow)


if __name__ == "__main__":
    main()
