"""Loads and validates multi-account configuration for Telemachus."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional
import yaml

from telemachus.email.models import AccountConfig

PROVIDER_PRESETS = {
    "yahoo": {
        "imap_host": "imap.mail.yahoo.com",
        "imap_port": 993,
        "use_ssl": True,
        "inbox_folder": "INBOX",
        "drafts_folder": "Drafts",
    },
    "gmail": {
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "use_ssl": True,
        "inbox_folder": "INBOX",
        "drafts_folder": "[Gmail]/Drafts",
    },
}


def find_project_root() -> Path:
    """Finds the root directory containing config/accounts.yaml or .env."""
    current = Path(__file__).resolve().parent
    for parent in [current] + list(current.parents):
        if (parent / "config" / "accounts.yaml").exists() or (parent / ".env").exists():
            return parent
    return Path.cwd()


def load_env(env_path: Optional[Path] = None) -> Dict[str, str]:
    """Parses a local .env file without external dependencies and updates os.environ."""
    if env_path is None:
        env_path = find_project_root() / ".env"

    env_vars: Dict[str, str] = {}
    if not env_path.is_file():
        return env_vars

    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    env_vars[key] = val
                    if key not in os.environ:
                        os.environ[key] = val
    except Exception:
        pass
    return env_vars


def load_accounts(config_path: Optional[Path] = None, env_path: Optional[Path] = None) -> List[AccountConfig]:
    """Loads accounts from YAML config and binds credentials from .env."""
    root = find_project_root()
    load_env(env_path or (root / ".env"))

    if config_path is None:
        config_path = root / "config" / "accounts.yaml"

    if not config_path.is_file():
        raise FileNotFoundError(f"Accounts config not found at: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        raw_data = yaml.safe_load(f) or {}

    accounts: List[AccountConfig] = []
    for item in raw_data.get("accounts", []):
        provider = item.get("provider", "custom").lower()
        preset = PROVIDER_PRESETS.get(provider, {})

        email_env_var = item.get("email_env_var", "")
        pass_env_var = item.get("pass_env_var", "")

        email = os.environ.get(email_env_var, item.get("email", "")).strip()
        password = os.environ.get(pass_env_var, item.get("password", "")).strip()

        folders = item.get("folders", {})
        inbox_folder = folders.get("inbox", preset.get("inbox_folder", "INBOX"))
        drafts_folder = folders.get("drafts", preset.get("drafts_folder", "Drafts"))

        acc = AccountConfig(
            id=item["id"],
            name=item.get("name", item["id"]),
            provider=provider,
            email=email,
            password=password,
            imap_host=item.get("imap_host", preset.get("imap_host", "")),
            imap_port=int(item.get("imap_port", preset.get("imap_port", 993))),
            use_ssl=bool(item.get("use_ssl", preset.get("use_ssl", True))),
            inbox_folder=inbox_folder,
            drafts_folder=drafts_folder,
            poll_limit=int(item.get("poll_limit", 10)),
            enabled=bool(item.get("enabled", False)),
        )
        accounts.append(acc)

    return accounts


def get_account(account_id: str) -> Optional[AccountConfig]:
    """Gets a specific account by ID."""
    for acc in load_accounts():
        if acc.id == account_id:
            return acc
    return None


def get_enabled_accounts() -> List[AccountConfig]:
    """Returns only accounts that are enabled and have credentials."""
    return [acc for acc in load_accounts() if acc.enabled and acc.email and acc.password]
