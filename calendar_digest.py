#!/usr/bin/env python3
"""Calendar daily digest — for a once-daily `hermes cron --no-agent` job.

For each owner in the calendar store, builds a daily digest of today's events
(falling back to the closest upcoming event when today is empty) and either
emails it or prints it to stdout for chat delivery.

Wire it up (script lives in ~/.hermes/scripts/):
    hermes cron create "0 7 * * *" --name calendar-digest --no-agent \
        --script calendar_digest.py

Behaviour per owner:
  - If the owner has a registered email AND it is in the EMAIL_ALLOWED_USERS
    allowlist: send a styled HTML email via notify.fire().  A one-line note is
    printed to stderr on success.
  - Otherwise: append the owner's markdown digest (prefixed with a ## header)
    to a stdout buffer; the buffer is printed once at the end so the Hermes
    cron posts it into chat.

This mirrors the structure of calendar_tick.py: same _hermes_home(),
_plugin_dir(), _load_env(), and importlib package-loading pattern.
"""

from __future__ import annotations

import importlib.util
import os
import sys


def _hermes_home() -> str:
    return os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))


def _plugin_dir() -> str:
    return os.environ.get("CALENDAR_PLUGIN_DIR") or os.path.join(
        _hermes_home(), "plugins", "calendar"
    )


def _load_env() -> None:
    """Load ~/.hermes/.env so EMAIL_* vars are present regardless of how the
    cron invokes us."""
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_hermes_home(), ".env"))
    except Exception:
        pass


def main() -> int:
    _load_env()
    d = _plugin_dir()
    init_py = os.path.join(d, "__init__.py")
    if not os.path.exists(init_py):
        print(f"calendar_digest: plugin not found at {d}", file=sys.stderr)
        return 1

    spec = importlib.util.spec_from_file_location(
        "calplugin", init_py, submodule_search_locations=[d]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["calplugin"] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:
        print(f"calendar_digest: failed to load plugin: {exc}", file=sys.stderr)
        return 1

    store = mod.store
    notify = mod.notify
    digest_mod = mod.digest

    owners = store.list_owners()
    stdout_parts: list[str] = []

    for owner in owners:
        try:
            digest = digest_mod.build_owner_digest(owner)
            date_str = digest["date_str"]
            subject = f"\U0001f4c5 Calendar digest — {date_str}"
            markdown = digest_mod.render_markdown(digest)
            html_doc = digest_mod.render_html(digest)

            email = store.get_user_email(owner)
            allowed = notify.allowed_email_recipients()

            if email and email.lower() in allowed:
                result = notify.fire(
                    "email",
                    subject,
                    markdown,
                    target=email,
                    html=html_doc,
                )
                if result.get("ok"):
                    print(
                        f"calendar_digest: emailed digest for {owner!r} → {email}",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"calendar_digest: email failed for {owner!r}: "
                        f"{result.get('error')}",
                        file=sys.stderr,
                    )
            else:
                # No usable email — queue for stdout / chat delivery.
                stdout_parts.append(f"## {owner}\n{markdown}")

        except Exception as exc:
            print(
                f"calendar_digest: error processing owner {owner!r}: {exc}",
                file=sys.stderr,
            )

    if stdout_parts:
        print("\n\n".join(stdout_parts))

    return 0


if __name__ == "__main__":
    sys.exit(main())
