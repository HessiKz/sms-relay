"""The bot's conversation flow.

Everything the user can do is a button. The flow is:

    /sms  ->  [ Pick a contact ]  [ Custom number ]
                 |                      |
           contact list / search     ask for the number
                 |                      |
           pick -> ask for text    ask for the text
                 \\                    /
                   -> queue the SMS -> the phone sends it

The state of the conversation lives in memory (one entry per chat) and
only exists while a conversation is running.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app import db
from app.tg_ui import (button, contact_button, keyboard, normalize_number,
                       web_app_button)

# Conversation modes
MODE_IDLE = "idle"
MODE_NUMBER = "number"      # waiting for a phone number
MODE_TEXT = "text"          # waiting for the message body
MODE_SEARCH = "search"      # waiting for a contact name/number

MAX_CONTACTS_PER_PAGE = 8
MAX_RESULTS = 8
MAX_CONTACTS_LOADED = 2000


@dataclass
class Session:
    mode: str = MODE_IDLE
    target: str = ""          # the number the text will go to
    target_name: str = ""     # its display name
    query: str = ""           # the contact search text
    results: list = field(default_factory=list)   # all loaded contacts
    shown: list = field(default_factory=list)     # the ones on this page


class Flow:
    """Per-chat conversation state."""

    def __init__(self, db_conn, miniapp_url: str | None = None):
        self.db_conn = db_conn
        self.sessions: dict[str, Session] = {}
        self.miniapp_url = (miniapp_url or "").strip() or None

    def session(self, chat_id) -> Session:
        key = str(chat_id)
        if key not in self.sessions:
            self.sessions[key] = Session()
        return self.sessions[key]

    def reset(self, chat_id) -> None:
        self.sessions.pop(str(chat_id), None)

    # --- the keyboards the user sees ------------------------------------
    def main_menu(self) -> dict:
        rows = []
        if self.miniapp_url:
            rows.append([
                web_app_button("\U0001F4E8 Open inbox", self.miniapp_url),
            ])
        rows.extend([
            [
                button("\U0001F4E8 Send an SMS", "menu:sms"),
                button("\U0001F4F7 Contacts", "menu:contacts"),
            ],
            [
                button("\U0001F6D1 Status", "menu:status"),
                button("❓ Help", "menu:help"),
            ],
        ])
        return keyboard(*rows)

    def sms_menu(self) -> dict:
        return keyboard(
            [
                button("\U0001F4CD Pick a contact", "sms:contacts"),
                button("✏️ Custom number", "sms:number"),
            ],
            [
                button("🔍 Search contacts", "sms:search"),
                button("⬅️ Back", "menu:home"),
            ],
        )

    def contact_keyboard(self, chat_id, query: str = "", page: int = 0) -> dict:
        """Contacts as buttons, one page at a time.

        The whole list is kept in the session so the next page is instant
        and the buttons can point at a stable entry.
        """
        session = self.session(chat_id)
        if query:
            contacts = db.search_contacts(self.db_conn, query, limit=MAX_RESULTS)
        else:
            contacts = db.list_contacts(self.db_conn, limit=MAX_CONTACTS_LOADED)
        session.results = contacts
        session.query = query

        pages = max(1, (len(contacts) + MAX_CONTACTS_PER_PAGE - 1)
                    // MAX_CONTACTS_PER_PAGE)
        page = max(0, min(page, pages - 1))
        start = page * MAX_CONTACTS_PER_PAGE
        chunk = contacts[start:start + MAX_CONTACTS_PER_PAGE]
        session.shown = chunk

        rows = [[contact_button(c["name"], c["number"], index)]
                for index, c in enumerate(chunk)]
        if not rows:
            rows = [[button("⬅️ Back", "menu:home")]]
        else:
            navigation = []
            if page > 0:
                navigation.append(button("◀️ Previous", f"contacts:{page - 1}"))
            if page + 1 < pages:
                navigation.append(
                    button(f"▶️ Next ({page + 1}/{pages})", f"contacts:{page + 1}"))
            if navigation:
                rows.append(navigation)
            rows.append([button("⬅️ Back", "menu:home")])
        return keyboard(*rows)

    # --- the actions ----------------------------------------------------
    def resolve_target(self, raw: str):
        """Turn what the user gave us into (number, name).

        A contact name is resolved through the uploaded contact list; a
        number is normalised (0939…, 0098939…, Persian numerals, spaces).
        Returns (number, name) or (None, None).
        """
        text = (raw or "").strip()
        if not text:
            return None, None

        number = normalize_number(text)
        if number:
            for contact in db.list_contacts(self.db_conn):
                if contact["number"] == number:
                    return number, contact["name"]
            return number, ""

        for contact in db.list_contacts(self.db_conn):
            if contact["name"].lower() == text.lower():
                return contact["number"], contact["name"]
        resolved = db.resolve_contact(self.db_conn, text)
        if resolved:
            for contact in db.list_contacts(self.db_conn):
                if contact["number"] == resolved:
                    return resolved, contact["name"]
        return None, None

    def contact_at(self, chat_id, index: int):
        """The contact behind a tapped button (of the current page)."""
        session = self.session(chat_id)
        if 0 <= index < len(session.shown):
            return session.shown[index]
        return None
