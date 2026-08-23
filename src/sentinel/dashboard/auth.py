"""The password gate.

The spec's requirement is "auth required before any non-local deployment", and
the way that is honoured here is **fail-closed**: with no password configured
the dashboard refuses to serve unless something has explicitly said "this is a
local session". `sentinel dashboard` sets that flag when it binds to localhost;
a container, a VPS or Streamlit Community Cloud will not have it, so an
unprotected deployment stops rather than quietly exposing a portfolio.

The alternative — default open, warn in the UI — fails in the one direction that
matters. A banner nobody reads is not an access control.

``decide`` is a pure function so the whole policy is unit-tested without booting
Streamlit.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Literal

AUTH_VERSION = "dashboard-auth-v1"

PASSWORD_ENV = "SENTINEL_DASHBOARD_PASSWORD"
#: Set by `sentinel dashboard` when it binds to a loopback address.
LOCAL_ENV = "SENTINEL_DASHBOARD_LOCAL"

Outcome = Literal["granted", "prompt", "rejected", "refused"]


@dataclass(frozen=True, slots=True)
class AuthDecision:
    outcome: Outcome
    message: str = ""

    @property
    def may_render(self) -> bool:
        return self.outcome == "granted"


def decide(
    *, configured_password: str | None, submitted: str | None, is_local: bool
) -> AuthDecision:
    """The whole access policy, in one testable function."""
    if not configured_password:
        if is_local:
            return AuthDecision(
                "granted",
                "Running locally with no password set. Set "
                f"{PASSWORD_ENV} before exposing this to a network.",
            )
        return AuthDecision(
            "refused",
            f"{PASSWORD_ENV} is not set and this is not a local session. The dashboard "
            "will not serve portfolio data unprotected — set a password and restart.",
        )

    if submitted is None or submitted == "":
        return AuthDecision("prompt", "Enter the dashboard password.")

    # Constant-time: a naive `==` leaks the password's length and prefix through
    # timing, and this endpoint is by definition reachable by whoever found it.
    if hmac.compare_digest(submitted, configured_password):
        return AuthDecision("granted")
    return AuthDecision("rejected", "Incorrect password.")


def configured_password() -> str | None:
    return os.environ.get(PASSWORD_ENV) or None


def is_local_session() -> bool:
    return os.environ.get(LOCAL_ENV, "").strip().lower() in ("1", "true", "yes")


def gate(st) -> AuthDecision:
    """Render the gate. Returns the decision; the caller renders nothing unless
    ``may_render``."""
    password = configured_password()
    local = is_local_session()

    if not password:
        decision = decide(configured_password=None, submitted=None, is_local=local)
        if decision.outcome == "refused":
            st.error(decision.message, icon="🔒")
        else:
            st.session_state["_auth_notice"] = decision.message
        return decision

    if st.session_state.get("_authenticated"):
        return AuthDecision("granted")

    st.markdown("### Sentinel")
    st.caption("Read-only research dashboard. Research output, not financial advice.")
    submitted = st.text_input("Password", type="password", key="_password")
    decision = decide(configured_password=password, submitted=submitted, is_local=local)
    if decision.outcome == "granted":
        st.session_state["_authenticated"] = True
    elif decision.outcome == "rejected":
        st.error(decision.message, icon="🔒")
    return decision
