import time
import threading
from collections import defaultdict, deque


# Metrics tracking
class MetricsTracker:
    def __init__(self, window_minutes=60):
        self.window_minutes = window_minutes
        self.requests = deque()  # Stores (timestamp, type, success) tuples
        self.lock = threading.Lock()
        
        # Counters for total metrics
        self.total_requests = 0
        self.total_by_type = defaultdict(int)
        self.total_success = 0
        self.total_failure = 0
    
    def record_request(self, request_type, success):
        """Record a request with its timestamp, type, and success status"""
        now = time.time()
        
        with self.lock:
            # Add the new request
            self.requests.append((now, request_type, success))
            
            # Update total counters
            self.total_requests += 1
            self.total_by_type[request_type] += 1
            if success:
                self.total_success += 1
            else:
                self.total_failure += 1
            
            # Clean up old requests outside the window
            self._clean_old_requests(now)
    
    def _clean_old_requests(self, current_time):
        """Remove requests older than the window"""
        cutoff = current_time - (self.window_minutes * 60)
        
        while self.requests and self.requests[0][0] < cutoff:
            self.requests.popleft()
    
    def get_requests_per_minute(self):
        """Calculate requests per minute in the current window"""
        if not self.requests:
            return 0
        
        now = time.time()
        self._clean_old_requests(now)
        
        # Calculate time span in minutes
        if not self.requests:
            return 0
            
        oldest = self.requests[0][0]
        time_span = (now - oldest) / 60  # convert to minutes
        
        # Avoid division by zero
        if time_span < 0.01:
            return len(self.requests) * 60  # extrapolate to per minute
            
        return len(self.requests) / time_span
    
    def get_stats(self):
        """Get current statistics"""
        now = time.time()
        self._clean_old_requests(now)
        
        # Count by type in current window
        types_count = defaultdict(int)
        success_count = 0
        failure_count = 0
        
        for _, req_type, success in self.requests:
            types_count[req_type] += 1
            if success:
                success_count += 1
            else:
                failure_count += 1
        
        return {
            "current_window": {
                "requests_total": len(self.requests),
                "requests_per_minute": self.get_requests_per_minute(),
                "requests_by_type": dict(types_count),
                "success_count": success_count,
                "failure_count": failure_count,
                "success_rate": (success_count / len(self.requests) * 100) if self.requests else 0
            },
            "all_time": {
                "requests_total": self.total_requests,
                "requests_by_type": dict(self.total_by_type),
                "success_count": self.total_success,
                "failure_count": self.total_failure,
                "success_rate": (self.total_success / self.total_requests * 100) if self.total_requests else 0
            }
        }
