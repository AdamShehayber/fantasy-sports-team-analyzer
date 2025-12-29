#!/usr/bin/env python3
"""
Database Reset Utility for FantasyAnalyzer

This script helps you reset the database when you make schema changes.
It will delete the existing database file and recreate it with the current schema.
"""

import os
import sys
from backend.models import Base, get_engine

def _delete_sqlite_files(db_path):
    paths = [db_path, f"{db_path}-wal", f"{db_path}-shm"]
    ok = True
    for p in paths:
        if os.path.exists(p):
            try:
                os.remove(p)
                print(f"✓ Deleted: {p}")
            except Exception as e:
                print(f"✗ Error deleting {p}: {e}")
                ok = False
    return ok

def reset_database():
    """Reset the database by deleting the old file and creating a new one."""
    from backend.models import DB_PATH
    print(f"Database location: {DB_PATH}")
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    deleted = _delete_sqlite_files(DB_PATH)

    print("Creating new database with current schema...")
    try:
        engine = get_engine()
        if not deleted and os.path.exists(DB_PATH):
            try:
                Base.metadata.drop_all(engine)
            except Exception as e:
                print(f"✗ Error dropping existing tables: {e}")
        Base.metadata.create_all(engine)
        print("✓ Database created successfully")
        print(f"✓ Database file: {DB_PATH}")
        return True
    except Exception as e:
        print(f"✗ Error creating database: {e}")
        return False

if __name__ == "__main__":
    print("FantasyAnalyzer Database Reset Utility")
    print("=" * 40)
    auto_yes = ("-y" in sys.argv) or ("--yes" in sys.argv)
    if not auto_yes:
        response = input("This will delete all existing data. Continue? (y/N): ")
        if response.lower() != 'y':
            print("Operation cancelled.")
            sys.exit(0)
    success = reset_database()
    if success:
        print("\n✓ Database reset completed successfully!")
        print("You can now run the application with: python desktop.py")
    else:
        print("\n✗ Database reset failed!")
        sys.exit(1)
