"""
invites.py — Single-use invite link system for Chain Sentinel
Only the admin can generate links. Each link works exactly once.
"""

import json
import os
import secrets

INVITES_FILE = "invites.json"
USERS_FILE = "authorized_users.json"


# ── Invite storage ─────────────────────────────────────────────────────────
def _load_invites() -> dict:
    if not os.path.exists(INVITES_FILE):
        return {}
    with open(INVITES_FILE, "r") as f:
        return json.load(f)

def _save_invites(data: dict):
    with open(INVITES_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── Authorized users storage ───────────────────────────────────────────────
def _load_users() -> list:
    if not os.path.exists(USERS_FILE):
        return []
    with open(USERS_FILE, "r") as f:
        return json.load(f)

def _save_users(data: list):
    with open(USERS_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── Public functions ───────────────────────────────────────────────────────
def generate_invite() -> str:
    """Generate a new single-use invite token."""
    token = secrets.token_urlsafe(16)
    invites = _load_invites()
    invites[token] = {"used": False}
    _save_invites(invites)
    return token


def use_invite(token: str, user_id: int) -> bool:
    """
    Attempt to redeem an invite token for a user.
    Returns True if successful, False if invalid or already used.
    """
    invites = _load_invites()

    if token not in invites:
        return False
    if invites[token]["used"]:
        return False

    # Mark invite as used
    invites[token]["used"] = True
    invites[token]["redeemed_by"] = user_id
    _save_invites(invites)

    # Authorize the user
    users = _load_users()
    if user_id not in users:
        users.append(user_id)
        _save_users(users)

    return True


def is_authorized(user_id: int) -> bool:
    """Check if a user is authorized to use the bot."""
    return user_id in _load_users()


def authorize_user(user_id: int):
    """Directly authorize a user (for admin)."""
    users = _load_users()
    if user_id not in users:
        users.append(user_id)
        _save_users(users)


def list_invites() -> dict:
    """Return all invites and their status."""
    return _load_invites()


def get_authorized_users() -> list:
    return _load_users()
