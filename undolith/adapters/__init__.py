"""Built-in adapters. Each is plain stdlib; none needs a network service to test."""

from .email import Email, file_transport, smtp_transport
from .fs import FileSystem
from .http import HTTP
from .shell import Shell, dry_run_argv
from .sqlite import SQLiteDB

__all__ = ["Email", "FileSystem", "HTTP", "SQLiteDB", "Shell", "dry_run_argv", "file_transport", "smtp_transport"]
