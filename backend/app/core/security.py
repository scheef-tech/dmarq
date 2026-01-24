from datetime import datetime, timedelta
from typing import Any, Union

from jose import jwt
from passlib.context import CryptContext

from app.core.config import get_settings

settings = get_settings()

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def create_access_token(
    subject: Union[str, Any], expires_delta: timedelta = None
) -> str:
    """
    Create a JWT access token for authentication
    """
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode = {"exp": expire, "sub": str(subject)}
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a password against its hash
    """
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """
    Hash a password
    """
    return pwd_context.hash(password)


# ============================================================================
# Configuration Encryption for DataSource credentials
# ============================================================================

import base64
import json
import hashlib
from cryptography.fernet import Fernet


def _get_encryption_key() -> bytes:
    """
    Derive a Fernet-compatible encryption key from the application's SECRET_KEY.

    Fernet requires a 32-byte base64-encoded key. We use SHA256 to derive
    a consistent key from the SECRET_KEY regardless of its length.
    """
    # Use SHA256 to get a consistent 32-byte key from SECRET_KEY
    key_bytes = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    # Fernet needs the key to be base64-encoded
    return base64.urlsafe_b64encode(key_bytes)


def encrypt_config(config: dict) -> str:
    """
    Encrypt a configuration dictionary for secure storage.

    Args:
        config: Dictionary containing sensitive configuration data

    Returns:
        Encrypted string that can be stored in the database

    Example:
        >>> config = {"server": "mail.example.com", "password": "secret"}
        >>> encrypted = encrypt_config(config)
        >>> # Store encrypted in database
    """
    fernet = Fernet(_get_encryption_key())
    config_json = json.dumps(config)
    encrypted_bytes = fernet.encrypt(config_json.encode())
    return encrypted_bytes.decode()


def decrypt_config(encrypted_config: str) -> dict:
    """
    Decrypt an encrypted configuration string.

    Args:
        encrypted_config: Encrypted string from the database

    Returns:
        Decrypted configuration dictionary

    Raises:
        InvalidToken: If decryption fails (wrong key or corrupted data)

    Example:
        >>> config = decrypt_config(encrypted_string)
        >>> password = config.get("password")
    """
    fernet = Fernet(_get_encryption_key())
    decrypted_bytes = fernet.decrypt(encrypted_config.encode())
    return json.loads(decrypted_bytes.decode())