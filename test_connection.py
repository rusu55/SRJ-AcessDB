import pyodbc
import os
from dotenv import load_dotenv

# Load .env
load_dotenv()

db_path = os.getenv('ACCESS_DB_PATH')
driver = os.getenv('ACCESS_DRIVER')

print("=" * 60)
print("SRJ Database Connection Test")
print("=" * 60)
print(f"Database path: {db_path}")
print(f"Driver: {driver}")
print(f"File exists: {os.path.exists(db_path) if db_path else 'No path'}")
print("=" * 60)
print()

if not db_path:
    print("❌ ERROR: ACCESS_DB_PATH not found in .env file")
    exit()

if not os.path.exists(db_path):
    print(f"❌ ERROR: File not found at: {db_path}")
    exit()

try:
    conn_str = f'DRIVER={{{driver}}};DBQ={db_path};'
    print(f"Connecting with: {conn_str}\n")
    
    conn = pyodbc.connect(conn_str)
    print("✅ CONNECTION SUCCESSFUL!\n")
    
    cursor = conn.cursor()
    
    # List all tables
    print("📋 Tables in SRJDatabase:")
    print("-" * 60)
    tables = []
    for table in cursor.tables(tableType='TABLE'):
        table_name = table.table_name
        tables.append(table_name)
        print(f"   ✓ {table_name}")
    
    print(f"\n📊 Total tables found: {len(tables)}\n")
    
    # Check for Orders table
    orders_tables = [t for t in tables if 'order' in t.lower()]
    if orders_tables:
        print("🔍 Order-related tables found:")
        for table in orders_tables:
            print(f"   → {table}")
            
            # Try to count records
            try:
                cursor.execute(f"SELECT COUNT(*) FROM [{table}]")
                count = cursor.fetchone()[0]
                print(f"      Records: {count}")
            except:
                print(f"      (Unable to count records)")
    
    # Check for specific table from your form
    if 'Orders New' in tables:
        print("\n✅ Found 'Orders New' table!")
        cursor.execute("SELECT COUNT(*) FROM [Orders New]")
        count = cursor.fetchone()[0]
        print(f"   Total orders: {count}")
        
        # Get sample order
        cursor.execute("SELECT TOP 1 Order_ID FROM [Orders New]")
        sample = cursor.fetchone()
        if sample:
            print(f"   Sample Order ID: {sample[0]}")
    
    # Check for Order Details table
    if 'Order Details Extended subform2' in tables:
        print("\n✅ Found 'Order Details Extended subform2' table!")
        cursor.execute("SELECT COUNT(*) FROM [Order Details Extended subform2]")
        count = cursor.fetchone()[0]
        print(f"   Total order details: {count}")
    
    conn.close()
    print("\n" + "=" * 60)
    print("✅ All checks passed! You can now start your FastAPI app.")
    print("=" * 60)
    
except pyodbc.Error as e:
    print(f"❌ CONNECTION FAILED!")
    print(f"Error: {e}\n")
    print("Possible solutions:")
    print("1. Install Microsoft Access Database Engine:")
    print("   https://www.microsoft.com/en-us/download/details.aspx?id=54920")
    print("2. Make sure Microsoft Access is closed")
    print("3. Try changing driver in .env to:")
    print("   ACCESS_DRIVER=Microsoft Access Driver (*.mdb)")
except Exception as e:
    print(f"❌ UNEXPECTED ERROR: {e}")