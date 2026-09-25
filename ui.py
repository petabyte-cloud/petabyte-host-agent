"""
Petabyte Agent UI - local dashboard interface
"""
from flask import Flask, render_template, jsonify, request
import threading
import time
import logging
import os
import secrets
import httpx

# Import agent configuration
try:
    from task_fetcher import API_KEY, API_URL
    AGENT_ID = __import__("os").getenv("PETABYTE_SPEC_ID", "default")
    FASTAPI_SERVER_URL = API_URL
except ImportError:
    # Fallback if task_fetcher not imported yet
    AGENT_ID = os.getenv("PETABYTE_SPEC_ID", "default")
    API_KEY = os.getenv("PETABYTE_API_KEY", "")
    FASTAPI_SERVER_URL = os.getenv("PETABYTE_API_URL", "")

app = Flask(__name__)
# Random per-process signing key (was a hardcoded constant shared by every install, which let
# anyone forge this local dashboard's session cookies). Override with AGENT_UI_SECRET_KEY to keep
# sessions across restarts.
app.config['SECRET_KEY'] = os.environ.get("AGENT_UI_SECRET_KEY") or secrets.token_hex(32)

# Agent status
agent_status = {
    "agent_id": AGENT_ID,
    "status": "running",
    "current_task": None,
    "tasks_completed": 0,
    "tasks_failed": 0,
    "uptime": 0,
    "last_heartbeat": None,
    "api_connected": False
}

start_time = time.time()

def update_status():
    """Update agent status periodically."""
    global agent_status
    while True:
        try:
            # Check API connection
            response = httpx.get(
                f"{FASTAPI_SERVER_URL}/verify_api_key",
                headers={"X-API-KEY": API_KEY},
                timeout=5
            )
            agent_status["api_connected"] = response.status_code == 200
        except:
            agent_status["api_connected"] = False
        
        agent_status["uptime"] = int(time.time() - start_time)
        agent_status["last_heartbeat"] = time.strftime("%Y-%m-%d %H:%M:%S")
        time.sleep(5)

# Start status update thread
status_thread = threading.Thread(target=update_status, daemon=True)
status_thread.start()

@app.route('/')
def index():
    """Main dashboard."""
    return render_template('index.html')

@app.route('/api/status')
def get_status():
    """Get agent status."""
    return jsonify(agent_status)

@app.route('/api/config', methods=['GET', 'POST'])
def config():
    """Get or update configuration."""
    if request.method == 'POST':
        data = request.json
        global API_KEY, AGENT_ID
        if 'api_key' in data:
            API_KEY = data['api_key']
        if 'agent_id' in data:
            AGENT_ID = data['agent_id']
            agent_status["agent_id"] = AGENT_ID
        return jsonify({"status": "ok"})
    else:
        return jsonify({
            "api_key": API_KEY[:10] + "..." if len(API_KEY) > 10 else API_KEY,
            "agent_id": AGENT_ID,
            "api_url": FASTAPI_SERVER_URL
        })

def run_ui(host='127.0.0.1', port=5000, debug=False):
    """Run the UI server."""
    logging.info(f"Starting Petabyte Agent UI on http://{host}:{port}")
    app.run(host=host, port=port, debug=debug, use_reloader=False)

if __name__ == '__main__':
    run_ui()

