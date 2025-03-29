import aiohttp
import json
from typing import Optional, Dict, Any
import os


base_url = "https://www.droptracker.io/"
class LoggerClient:
    def __init__(self, token: str):
        """
        Initialize the logger client.
        
        Args:
            base_url: Base URL of the API (e.g., 'https://droptracker.io')
            token: Authentication token for logging
        """
        self.base_url = base_url.rstrip('/')
        self.token = os.getenv('LOGGER_TOKEN')
        self.headers = {
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json'
        }
    
    async def log(self, 
                 log_type: str, 
                 message: str, 
                 context: Optional[Dict[str, Any]] = None) -> bool:
        """
        Send a log entry to the server.
        
        Args:
            log_type: Type of log (error, access, or cron)
            message: The log message
            context: Optional dictionary of additional context
            
        Returns:
            bool: True if logging was successful
            
        Raises:
            aiohttp.ClientError: If there's a network or HTTP error
            ValueError: If the server returns an error response
        """
        print(f"[{log_type}] {message}")


