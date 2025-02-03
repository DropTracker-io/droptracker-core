import aiohttp
import json
from typing import Optional, Dict, Any

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
        self.token = "zLsv8WsPjlh9YP3UZllZs85zlBrireqU1LG8xC8Aqa6pgSjWW2rFiOjzRHewLa9h"
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
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    'type': log_type,
                    'message': message,
                    'context': context or {}
                }
                
                async with session.post(
                    f'{self.base_url}/php-api/log.php',
                    headers=self.headers,
                    json=payload
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        raise ValueError(f'Logging failed: {error_text}')
                    
                    #data = await response.json()
                    #return data.get('success', False)
                    return True
        except aiohttp.ClientError as e:
            raise ValueError(f'Network error while logging: {str(e)}')
        finally:
            print(f"[{log_type}] {message}")


