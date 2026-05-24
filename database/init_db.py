"""
Database Initialization - PostgreSQL (Supabase)
Creates tables and default settings
"""
import psycopg2
from dotenv import load_dotenv
import os
from database.database import get_db_port

load_dotenv()

def init_database():
    """Initialize PostgreSQL database with tables and default data"""
    
    print("\n🚀 Initializing PostgreSQL Database...")
    print("="*60)
    
    # Connect to PostgreSQL
    try:
        conn = psycopg2.connect(
            host=os.getenv('DB_HOST'),
            database=os.getenv('DB_NAME', 'postgres'),
            user=os.getenv('DB_USER', 'postgres'),
            password=os.getenv('DB_PASSWORD'),
            port=get_db_port(),
            sslmode='require'
        )
        print(f"✅ Connected to: {os.getenv('DB_HOST')}")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return
    
    cursor = conn.cursor()
    
    try:
        # Drop existing tables (fresh start)
        print("\n🗑️  Dropping existing tables...")
        cursor.execute('DROP TABLE IF EXISTS submissions CASCADE')
        cursor.execute('DROP TABLE IF EXISTS settings CASCADE')
        print("   ✅ Old tables dropped")
        
        # Create submissions table
        print("\n📝 Creating submissions table...")
        cursor.execute('''
            CREATE TABLE submissions (
                id SERIAL PRIMARY KEY,
                stock_name TEXT NOT NULL,
                market_price REAL,
                market_cap REAL,
                liquidity REAL,
                adr REAL,
                freefloat REAL,
                sector TEXT,
                linearity TEXT,
                sector_strength BOOLEAN,
                symmetry BOOLEAN,
                institutional_participation BOOLEAN,
                relative_strength BOOLEAN,
                market_trend TEXT,
                previous_move_enabled BOOLEAN,
                move_percentage REAL,
                move_days INTEGER,
                move_ema TEXT,
                market_cap_score REAL,
                linearity_score REAL,
                sector_strength_score REAL,
                symmetry_score REAL,
                institutional_participation_score REAL,
                relative_strength_score REAL,
                liquidity_score REAL,
                adr_score REAL,
                market_trend_score REAL,
                ferocity_score REAL,
                magnitude_score REAL,
                move_ema_score REAL,
                final_score REAL,
                rating TEXT,
                chart_image_path TEXT,
                quarterly_results TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                traded BOOLEAN DEFAULT FALSE,
                entry_price REAL,
                position_size REAL,
                exit_price REAL,
                setup_type TEXT DEFAULT 'large_base_institutional',
                extension_pct REAL,
                move_quality_score REAL DEFAULT 0,
                extension_base_score REAL DEFAULT 0
            )
        ''')
        print("   ✅ Submissions table created (33 columns)")
        
        # Create settings table
        print("\n⚙️  Creating settings table...")
        cursor.execute('''
            CREATE TABLE settings (
                id SERIAL PRIMARY KEY,
                parameter_name TEXT UNIQUE NOT NULL,
                weightage REAL DEFAULT 11.11,
                enabled BOOLEAN DEFAULT TRUE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        print("   ✅ Settings table created")
        
        # Insert default settings
        print("\n📊 Inserting default settings...")
        default_settings = [
            ('sector_strength', 26, True),
            ('magnitude', 14, True),
            ('move_ema', 12, True),
            ('relative_strength', 10, True),
            ('institutional_participation', 10, True),
            ('symmetry', 8, True),
            ('ferocity', 8, True),
            ('adr', 7, True),
            ('market_trend', 5, True),
            ('market_cap', 0, True),      # gatekeeper only
            ('linearity', 0, True),       # gatekeeper only
            ('liquidity', 0, True),       # gatekeeper only
        ]
        
        for param_name, weight, enabled in default_settings:
            cursor.execute(
                'INSERT INTO settings (parameter_name, weightage, enabled) VALUES (%s, %s, %s)',
                (param_name, weight, enabled)
            )
            print(f"   ✅ {param_name}: {weight}%")
        
        conn.commit()
        print("\n" + "="*60)
        print("✅ Database initialized successfully!")
        print("="*60)
        print("\n📋 Summary:")
        print(f"   Tables created: 2")
        print(f"   - submissions (for stock analyses)")
        print(f"   - settings (for parameter weightages)")
        print(f"   Default settings: 9 parameters at 11.11% each")
        print(f"   Total weightage: 100.00%")
        print("\n🎯 Ready to use!")
        
    except Exception as e:
        print(f"\n❌ Error during initialization: {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()


if __name__ == '__main__':
    print("\n" + "="*60)
    print("  PostgreSQL Database Initialization")
    print("  Stock Scoring System")
    print("="*60)
    
    try:
        init_database()
    except Exception as e:
        print(f"\n❌ Fatal Error: {e}")
        print("\n💡 Troubleshooting:")
        print("   1. Check .env file exists in Backend/ folder")
        print("   2. Verify DB_HOST, DB_PASSWORD are correct")
        print("   3. Ensure Supabase database is running")
        print("   4. Check internet connection")
