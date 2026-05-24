from __future__ import annotations

from flask import Flask
from flask_cors import CORS

from database.init_chat_db import init_chat_table
from database.init_valvo_ai_v2_db import init_valvo_ai_v2_tables
from routes.valvo_ai_v2_routes import valvo_ai_v2_bp


app = Flask(__name__)
CORS(app)
app.register_blueprint(valvo_ai_v2_bp)


try:
    init_chat_table()
except Exception as exc:
    print(f"Warning: chat table init skipped: {exc}")

try:
    init_valvo_ai_v2_tables()
except Exception as exc:
    print(f"Warning: valvo ai v2 table init skipped: {exc}")


@app.route("/")
def home():
    return {
        "message": "Valvo AI v2 backend is running",
        "endpoints": {
            "query": "/api/valvo-ai/query",
            "health": "/api/valvo-ai/health",
            "history": "/api/valvo-ai/history",
        },
    }


if __name__ == "__main__":
    app.run(debug=True, port=8081)
