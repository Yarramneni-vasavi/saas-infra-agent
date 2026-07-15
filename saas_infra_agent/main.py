from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Load .env before anything else (keys, model config, etc.)
load_dotenv(Path(__file__).parent.parent / ".env")

from saas_infra_agent.observability.logger import configure_logging, get_log_file
from saas_infra_agent.memory.long_term import get_long_term_store

configure_logging()

from saas_infra_agent.agent.orchestrator import handle_query, pending_approval_prompt
from saas_infra_agent.memory.session import get_current_session, new_session, switch_session

console = Console()

def _render_long_term(*, limit: int = 30) -> None:
    store = get_long_term_store()
    records = store.search(limit=limit)

    table = Table(title=f"Long-Term Memory (latest {len(records)})", show_lines=False)
    table.add_column("ID", justify="right", style="dim", no_wrap=True)
    table.add_column("Project", style="cyan", no_wrap=True)
    table.add_column("Category", style="magenta", no_wrap=True)
    table.add_column("Key", style="green")
    table.add_column("Value")
    table.add_column("Tags", style="dim")
    table.add_column("Pinned", justify="center", no_wrap=True)
    table.add_column("Updated", style="dim", no_wrap=True)

    def _fmt_value(v: object) -> Text:
        s = str(v)
        if len(s) > 80:
            s = s[:77] + "..."
        return Text(s)

    for r in records:
        table.add_row(
            str(r.id),
            r.project,
            r.category,
            r.memory_key,
            _fmt_value(r.value),
            ", ".join(r.tags),
            "Y" if r.pinned else "",
            r.updated_at.replace("T", " ").replace("+00:00", "Z"),
        )

    if not records:
        console.print("[dim]No long-term memories stored yet.[/dim]")
        return
    console.print(table)


def _show_pending_approval(session_id: str) -> None:
    """Re-display a pending approval left over from a previous run."""
    prompt = pending_approval_prompt(session_id)
    if not prompt:
        return
    console.print(
        Panel.fit(
            prompt,
            title="Approval Required",
            subtitle="Your next message is the reply",
            border_style="yellow",
        )
    )


def main() -> None:
    console.print("[bold cyan]saas-cli[/bold cyan] started. Type [bold]/exit[/bold] to quit.")
    session_id = get_current_session()
    console.print(f"[dim]Session:[/dim] [bold]{session_id}[/bold]")
    log_file = get_log_file()
    if log_file is not None:
        console.print(f"[dim]Logs:[/dim] {log_file}")

    _show_pending_approval(session_id)

    while True:
        try:
            user_input = console.input("\n[bold cyan]> [/bold cyan]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nExiting.")
            break

        if user_input == "/exit":
            console.print("Exiting.")
            break

        if user_input == "/session":
            console.print(f"Session: {session_id}")
            continue

        if user_input == "/list_long_term":
            _render_long_term()
            continue

        if user_input == "/new":
            session_id = new_session()
            console.print(f"Session: {session_id}")
            _show_pending_approval(session_id)
            continue

        if user_input.startswith("/switch "):
            session_id = switch_session(user_input[len("/switch ") :].strip())
            console.print(f"Session: {session_id}")
            _show_pending_approval(session_id)
            continue

        if user_input:
            with console.status("[dim]Working...[/dim]", spinner="dots"):
                reply = handle_query(user_input, thread_id=session_id)
            if reply:
                console.print(Markdown(reply))


if __name__ == "__main__":
    main()
