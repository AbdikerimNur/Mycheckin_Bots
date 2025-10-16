import os
import psycopg2
from psycopg2.extras import Json
from dotenv import load_dotenv

load_dotenv()

# ---------------- Database Connection ----------------
def get_connection():
    """Establish a connection to the PostgreSQL database using DATABASE_URL."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("❌ DATABASE_URL is not set in environment.")
    return psycopg2.connect(db_url)

# ---------------- Database Initialization ----------------
def init_db():
    """Create the users table (if not exists)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    chat_id VARCHAR(255) PRIMARY KEY,
                    user_data JSONB NOT NULL
                );
            """)
        conn.commit()
        print("🗄️ Database initialized successfully.")
    finally:
        conn.close()

# ---------------- Load Users ----------------
def load_users_from_db() -> dict:
    """Load all users from database as a dictionary."""
    users_dict = {}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT chat_id, user_data FROM users;")
            for chat_id, user_data in cur.fetchall():
                users_dict[chat_id] = user_data
        print(f"✅ Loaded {len(users_dict)} user(s) from the database.")
        return users_dict
    except psycopg2.Error as e:
        print(f"❌ Error loading users: {e}")
        return {}
    finally:
        conn.close()

# ---------------- Save User ----------------
def save_user_to_db(chat_id: str, user_data: dict):
    """Insert or update a single user's record."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (chat_id, user_data)
                VALUES (%s, %s)
                ON CONFLICT (chat_id)
                DO UPDATE SET user_data = EXCLUDED.user_data;
            """, (str(chat_id), Json(user_data)))
        conn.commit()
    except psycopg2.Error as e:
        print(f"❌ Error saving user {chat_id}: {e}")
    finally:
        conn.close()
