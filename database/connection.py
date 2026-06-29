import pyodbc
import os
from fastapi import HTTPException
from typing import Optional
from config.settings import settings  # Import settings

class AccessDatabase:
    def __init__(self, db_path: Optional[str] = None):
        # Use pydantic settings instead of os.getenv
        self.db_path = db_path or settings.ACCESS_DB_PATH
        self.driver = settings.ACCESS_DRIVER
        
        # Verify file exists
        if not os.path.exists(self.db_path):
            raise FileNotFoundError(
                f"Database file not found at: {self.db_path}\n"
                f"Please verify ACCESS_DB_PATH in your .env file"
            )
    
    def get_connection_string(self) -> str:
        return f'DRIVER={{{self.driver}}};DBQ={self.db_path};'
    
    def get_connection(self):
        try:
            conn_str = self.get_connection_string()
            print(f"DEBUG - Attempting connection with: {conn_str}")  # Remove after testing
            return pyodbc.connect(conn_str)
        except pyodbc.Error as e:
            raise HTTPException(status_code=500, detail=f"Database connection error: {str(e)}")

access_db = AccessDatabase()

def get_db_connection():
    return access_db.get_connection()