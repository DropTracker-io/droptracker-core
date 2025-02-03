from cryptography.fernet import Fernet
from base64 import b64encode, b64decode

import os
from dotenv import load_dotenv

load_dotenv()

# The key must be 32 url-safe base64-encoded bytes
encryption_key = os.getenv("ENCRYPTION_KEY")

def encrypt_webhook(webhook_url: str) -> str:
    try:
        f = Fernet(encryption_key)
        encrypted_webhook = f.encrypt(webhook_url.encode())
        # Return as base64 string for storage
        return b64encode(encrypted_webhook).decode()
    except Exception as e:
        raise Exception(f"Encryption failed: {str(e)}")

def decrypt_webhook(webhook_hash: str) -> str:
    try:
        f = Fernet(encryption_key)
        # Decode from base64 string back to bytes
        encrypted_data = b64decode(webhook_hash)
        decrypted_webhook = f.decrypt(encrypted_data)
        return decrypted_webhook.decode()
    except Exception as e:
        raise Exception(f"Decryption failed: {str(e)}")