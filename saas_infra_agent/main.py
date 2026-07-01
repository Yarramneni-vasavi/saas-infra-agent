from pathlib import Path

from saas_infra_agent.agent.orchestrator import handle_query
from saas_infra_agent.memory.session import get_current_session, new_session, switch_session
from dotenv import load_dotenv

# Load .env before anything else
load_dotenv(Path(__file__).parent.parent / ".env")

def main() -> None:
    print("saas-cli started. Type /exit to quit.")
    session_id = get_current_session()
    print(f"Session: {session_id}")
    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if user_input == "/exit":
            print("Exiting.")
            break

        if user_input == "/session":
            print(f"Session: {session_id}")
            continue

        if user_input == "/new":
            session_id = new_session()
            print(f"Session: {session_id}")
            continue

        if user_input.startswith("/switch "):
            session_id = switch_session(user_input[len("/switch ") :].strip())
            print(f"Session: {session_id}")
            continue

        if user_input:
            reply = handle_query(user_input, thread_id=session_id)
            print(reply)


if __name__ == "__main__":
    main()
