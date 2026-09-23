# The human side of the conversation.
#
# The agent reaches the person only through a UserChannel. The CLI uses ConsoleUser; tests and
# evals use ScriptedUser, which answers from a script so a run is repeatable.
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol


class UserChannel(Protocol):
    def ask(self, question: str) -> str: ...

    def approve(self, summary: str, amount_display: str) -> tuple[bool, str]: ...

    def payment_link(self, url: str, amount_display: str, order_id: str) -> None: ...

    def say(self, text: str) -> None: ...


class ConsoleUser:
    def __init__(self, open_browser: bool = False) -> None:
        self.open_browser = open_browser

    def ask(self, question: str) -> str:
        print(f"\nAssistant asks: {question}")
        return input("You: ").strip()

    def approve(self, summary: str, amount_display: str) -> tuple[bool, str]:
        # The summary comes from the STORE (get_checkout_quote), not from the model's wording.
        print("\n" + "─" * 60)
        print("  APPROVE THIS PURCHASE?  (quote computed by the store)")
        print(f"  {summary}")
        print("─" * 60)
        answer = input(f"Type 'yes' to approve {amount_display}, anything else to decline: ")
        return answer.strip().lower() in ("y", "yes"), answer.strip()

    def payment_link(self, url: str, amount_display: str, order_id: str) -> None:
        print(f"\nPay {amount_display} for order {order_id} here: {url}")
        print("   (Razorpay TEST mode: use a test card or UPI 'success@razorpay')")
        if self.open_browser:
            webbrowser.open(url)

    def say(self, text: str) -> None:
        print(f"\nAssistant: {text}")


@dataclass
class ScriptedUser:
    # answers: consumed in order by ask(); "approve": True/False or a list for several approvals.
    answers: list[str] = field(default_factory=list)
    approve_decisions: list[bool] = field(default_factory=lambda: [True])
    on_payment_link: Callable[[str, str], None] | None = None  # (url, order_id), e.g. fake-pay
    transcript: list[tuple[str, str]] = field(default_factory=list)

    def ask(self, question: str) -> str:
        answer = self.answers.pop(0) if self.answers else "I don't know, please decide sensibly."
        self.transcript += [("agent_question", question), ("user_answer", answer)]
        return answer

    def approve(self, summary: str, amount_display: str) -> tuple[bool, str]:
        decision = self.approve_decisions.pop(0) if self.approve_decisions else False
        self.transcript += [("approval_shown", summary), ("approved", str(decision))]
        return decision, "yes" if decision else "no"

    def payment_link(self, url: str, amount_display: str, order_id: str) -> None:
        self.transcript.append(("payment_link", url))
        if self.on_payment_link:
            self.on_payment_link(url, order_id)

    def say(self, text: str) -> None:
        self.transcript.append(("agent_said", text))
