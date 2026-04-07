"""Simple multi-user authentication for the dashboard.

Supports up to 10 users. Each user gets isolated data:
- CVs: data/users/{user_id}/cvs/
- Database: data/users/{user_id}/auto_apply.db
- Settings: data/users/{user_id}/settings.yaml
- Credentials: data/users/{user_id}/.env

When no users are configured (users.yaml doesn't exist or is empty),
the system falls back to single-user mode with the original paths.
"""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path
from typing import Any

import yaml

from src.config import PROJECT_ROOT

USERS_FILE = PROJECT_ROOT / "config" / "users.yaml"
USERS_DATA_DIR = PROJECT_ROOT / "data" / "users"
MAX_USERS = 10


def _hash_password(password: str, salt: str = "") -> str:
    """Hash a password with optional salt."""
    if not salt:
        salt = secrets.token_hex(16)
    hashed = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return f"{salt}:{hashed}"


def _verify_password(password: str, stored: str) -> bool:
    """Verify a password against a stored hash."""
    if ":" not in stored:
        return password == stored  # plaintext fallback for initial setup
    salt = stored.split(":", 1)[0]
    return _hash_password(password, salt) == stored


def load_users() -> list[dict[str, Any]]:
    """Load user list from users.yaml."""
    if not USERS_FILE.exists():
        return []
    try:
        data = yaml.safe_load(USERS_FILE.read_text()) or {}
        return data.get("users", [])
    except Exception:
        return []


def save_users(users: list[dict[str, Any]]) -> None:
    """Save user list to users.yaml."""
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(yaml.dump({"users": users}, default_flow_style=False))


def is_multi_user_enabled() -> bool:
    """Check if multi-user mode is active."""
    return len(load_users()) > 0


def authenticate(email: str, password: str) -> dict[str, Any] | None:
    """Authenticate a user. Returns user dict or None."""
    users = load_users()
    for user in users:
        if user.get("email", "").lower() == email.lower():
            if _verify_password(password, user.get("password", "")):
                return user
    return None


def add_user(
    name: str,
    email: str,
    password: str,
    is_admin: bool = False,
) -> tuple[bool, str]:
    """Add a new user. Returns (success, message)."""
    users = load_users()

    if len(users) >= MAX_USERS:
        return False, f"Maximum {MAX_USERS} users reached"

    # Check duplicate email
    for u in users:
        if u.get("email", "").lower() == email.lower():
            return False, f"User with email {email} already exists"

    user_id = email.split("@")[0].lower().replace(".", "_").replace(" ", "_")

    # Ensure unique user_id
    existing_ids = {u.get("id", "") for u in users}
    base_id = user_id
    counter = 1
    while user_id in existing_ids:
        user_id = f"{base_id}_{counter}"
        counter += 1

    new_user = {
        "id": user_id,
        "name": name,
        "email": email,
        "password": _hash_password(password),
        "is_admin": is_admin,
    }

    users.append(new_user)
    save_users(users)

    # Create user data directory
    _ensure_user_dirs(user_id)

    return True, f"User {name} added successfully (ID: {user_id})"


def remove_user(email: str) -> tuple[bool, str]:
    """Remove a user by email. Returns (success, message)."""
    users = load_users()
    new_users = [u for u in users if u.get("email", "").lower() != email.lower()]

    if len(new_users) == len(users):
        return False, f"User {email} not found"

    save_users(new_users)
    return True, f"User {email} removed"


def _ensure_user_dirs(user_id: str) -> None:
    """Create user-specific data directories."""
    user_dir = USERS_DATA_DIR / user_id
    (user_dir / "cvs").mkdir(parents=True, exist_ok=True)


def get_user_paths(user_id: str) -> dict[str, Path]:
    """Get all user-specific file paths.

    Returns dict with keys: cv_dir, db_path, config_path, env_path
    """
    user_dir = USERS_DATA_DIR / user_id
    _ensure_user_dirs(user_id)

    return {
        "cv_dir": user_dir / "cvs",
        "db_path": user_dir / "auto_apply.db",
        "config_path": user_dir / "settings.yaml",
        "env_path": user_dir / ".env",
        "user_dir": user_dir,
    }


def get_default_paths() -> dict[str, Path]:
    """Get default single-user paths (backward compatible)."""
    return {
        "cv_dir": PROJECT_ROOT / "data" / "cvs",
        "db_path": PROJECT_ROOT / "auto_apply.db",
        "config_path": PROJECT_ROOT / "config" / "settings.yaml",
        "env_path": PROJECT_ROOT / ".env",
        "user_dir": PROJECT_ROOT,
    }


def ensure_user_config(user_id: str) -> None:
    """Copy default config to user directory if it doesn't exist yet."""
    paths = get_user_paths(user_id)
    default_config = PROJECT_ROOT / "config" / "settings.yaml"

    if not paths["config_path"].exists() and default_config.exists():
        paths["config_path"].write_text(default_config.read_text())
