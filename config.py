import logging
import os
import secrets

from dotenv import load_dotenv

load_dotenv()

_log = logging.getLogger(__name__)

class Config:
    # A known SECRET_KEY lets anyone forge a session cookie and log in as any
    # user, so the fallback is generated per process rather than shared. That
    # deliberately invalidates sessions on restart in development, which is a
    # visible nuisance -- unlike a hardcoded key, which is an invisible one.
    SECRET_KEY = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
    if not os.environ.get('SECRET_KEY'):
        _log.warning(
            "SECRET_KEY is not set; using a random per-process key. Sessions "
            "will not survive a restart and cannot be shared across workers. "
            "Set SECRET_KEY in .env before deploying.")
    
    # Database Configuration
    db_url = os.environ.get('DATABASE_URL')
    if db_url and db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    
    SQLALCHEMY_DATABASE_URI = db_url or 'sqlite:///site.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # File upload settings
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max file size
    UPLOAD_FOLDER = 'static/uploads'
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'mp4', 'mov', 'avi'}
    FIREBASE_SERVER_KEY = os.environ.get('FIREBASE_SERVER_KEY')
    
    # Multilingual support
    LANGUAGES = ['en', 'ta', 'hi', 'te', 'ml', 'kn']  # English, Tamil, Hindi, Telugu, Malayalam, Kannada
    BABEL_DEFAULT_LOCALE = 'en'
    BABEL_DEFAULT_TIMEZONE = 'UTC'
    
    # Twilio WhatsApp Configuration
    TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
    TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
    TWILIO_WHATSAPP_NUMBER = os.environ.get('TWILIO_WHATSAPP_NUMBER')
    
    # Twilio SMS Configuration
    TWILIO_PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER')