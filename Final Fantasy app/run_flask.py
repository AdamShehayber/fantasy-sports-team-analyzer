#!/usr/bin/env python3

import os
import sys

# Add the current directory to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.app import create_app

if __name__ == "__main__":
    app = create_app()
    print("Starting Flask application on http://127.0.0.1:5003")
    app.run(host='127.0.0.1', port=5003, debug=True)