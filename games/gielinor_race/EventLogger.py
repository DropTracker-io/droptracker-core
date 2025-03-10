import os
from datetime import datetime
import logging

debug = True

class EventLogger:
    def __init__(self):
        self.logger = logging.getLogger('GielinorRace')
        self.logger.setLevel(logging.INFO)
        
        # Create logs directory if it doesn't exist
        if not os.path.exists('logs'):
            os.makedirs('logs')
            
        # Create handlers
        log_file = f"logs/{datetime.now().strftime('%Y-%m-%d')}.log"
        file_handler = logging.FileHandler(log_file)
        console_handler = logging.StreamHandler()
        
        # Create formatters and add it to handlers
        log_format = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(log_format)
        console_handler.setFormatter(log_format)
        
        # Add handlers to the logger
        self.logger.addHandler(file_handler)
        if debug:
            self.logger.addHandler(console_handler)
            
    def info(self, message):
        self.logger.info(message)
        
    def warning(self, message):
        self.logger.warning(message)
        
    def error(self, message):
        self.logger.error(message)
        
    def debug(self, message):
        self.logger.debug(message)
