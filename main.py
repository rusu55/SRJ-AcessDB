from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routes import orders, health, products, po, so, inventory, tracking, orders_write
from config.settings import settings
from dotenv import load_dotenv
import os

# Load .env file explicitly
load_dotenv()

# Debug: Print to verify .env is loaded
print("=" * 60)
print("Environment Variables Check:")
print(f"ACCESS_DB_PATH: {os.getenv('ACCESS_DB_PATH')}")
print(f"API_KEY: {os.getenv('API_KEY')}")
print("=" * 60)

app = FastAPI(
    title="Orders API",
    description="API for managing orders with Microsoft Access database",
    version="1.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(orders.router, prefix="/api/orders", tags=["Orders"])
app.include_router(orders_write.router, prefix="/api/orders", tags=["Orders Write"])
app.include_router(health.router, prefix="/api/health", tags=["Health"])
app.include_router(products.router, prefix="/api/products", tags=["Products"])
app.include_router(so.router, prefix="/api/so", tags=["SO"])
app.include_router(po.router, prefix="/api/po", tags=["PO"])
app.include_router(inventory.router, prefix="/api/inventory", tags=["Inventory"])
app.include_router(tracking.router, prefix="/api/tracking", tags=["Tracking"])

@app.get("/")
def root():
    return {
        "message": "Welcome to Orders API", 
        "docs": "/docs",
        "database": settings.ACCESS_DB_PATH  # Shows which database is connected
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
