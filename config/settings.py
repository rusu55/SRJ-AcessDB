from pydantic_settings import BaseSettings
from typing import List
from pathlib import Path

# Get the project root directory
BASE_DIR = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    ACCESS_DB_PATH: str
    ACCESS_DRIVER: str = r"Microsoft Access Driver (*.mdb, *.accdb)"
    API_KEY: str
    ALLOWED_ORIGINS: List[str] = ["*"]
    
    class Config:
        env_file = str(BASE_DIR / ".env")
        env_file_encoding = 'utf-8'

settings = Settings()

# Debug: Print loaded settings (remove this after testing)
print(f"DEBUG - Loaded ACCESS_DB_PATH: {settings.ACCESS_DB_PATH}")
print(f"DEBUG - Loaded API_KEY: {settings.API_KEY}")