from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper


def build_oauth_storage(directory: Path, encryption_key: bytes):
    """Build FastMCP 4.0.5's official local store with an independent key."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    file_store = FileTreeStore(
        data_directory=directory,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(directory),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(
            directory
        ),
    )
    return FernetEncryptionWrapper(
        key_value=file_store,
        fernet=Fernet(encryption_key),
        raise_on_decryption_error=False,
    )
