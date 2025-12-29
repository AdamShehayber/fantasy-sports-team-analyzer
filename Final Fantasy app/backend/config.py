"""
Configuration module for FantasyAnalyzer
Handles secure secret key generation and session management settings
"""

import os
import logging
import secrets
from datetime import timedelta

class Config:
    """Base configuration class"""
    
    # Generate secure secret key
    SECRET_KEY = os.environ.get('FANTASY_SECRET_KEY') or secrets.token_hex(32)
    
    # Session configuration
    PERMANENT_SESSION_LIFETIME = timedelta(hours=24)  # 24 hours default
    SESSION_COOKIE_SECURE = False  # Set to True in production with HTTPS
    SESSION_COOKIE_HTTPONLY = True  # Prevent XSS attacks
    SESSION_COOKIE_SAMESITE = 'Lax'  # CSRF protection
    
    # CSRF Protection
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = None  # No time limit for CSRF tokens
    
    # Rate limiting (explicit memory backend to silence warning in desktop app)
    RATELIMIT_STORAGE_URI = "memory://"
    RATELIMIT_DEFAULT = "100 per hour"

    # Live data integration
    LIVE_CACHE_TTL_MINUTES = 360  # 6 hours
    USE_LIVE_PROJECTIONS_DEFAULT = True
    STATS_PROVIDER = "sleeper"
    
    # Remember me configuration
    REMEMBER_COOKIE_DURATION = timedelta(days=30)  # 30 days for remember me
    REMEMBER_COOKIE_SECURE = False  # Set to True in production
    REMEMBER_COOKIE_HTTPONLY = True
    
    # Session analytics
    TRACK_SESSION_ANALYTICS = True
    
    @staticmethod
    def _app_data_dir():
        base = (
            os.environ.get('LOCALAPPDATA')
            or os.environ.get('APPDATA')
            or os.path.expanduser('~')
        )
        path = os.path.join(base, 'FantasyAnalyzer')
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            pass
        return path

    @staticmethod
    def save_secret_key():
        """Save the generated secret key to a file for persistence"""
        secret_file = os.environ.get('FANTASY_SECRET_FILE') or os.path.join(Config._app_data_dir(), '.secret_key')
        
        if not os.path.exists(secret_file):
            with open(secret_file, 'w') as f:
                f.write(Config.SECRET_KEY)
            logging.getLogger(__name__).info(f"Generated new secret key saved to {secret_file}")
        else:
            # Load existing secret key
            with open(secret_file, 'r') as f:
                Config.SECRET_KEY = f.read().strip()
            logging.getLogger(__name__).info(f"Loaded existing secret key from {secret_file}")
    
    @staticmethod
    def init_app(app):
        """Initialize app with configuration"""
        Config.save_secret_key()
        
        # Apply configuration to Flask app
        app.config.from_object(Config)
        
        # Additional security headers
        @app.after_request
        def security_headers(response):
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['X-Frame-Options'] = 'DENY'
            response.headers['X-XSS-Protection'] = '1; mode=block'
            return response

class DevelopmentConfig(Config):
    """Development configuration"""
    DEBUG = True
    SESSION_COOKIE_SECURE = False
    WTF_CSRF_ENABLED = False  # Disable CSRF in dev to avoid iframe cookie issues

class ProductionConfig(Config):
    """Production configuration"""
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True

# Configuration dictionary
config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig
}
