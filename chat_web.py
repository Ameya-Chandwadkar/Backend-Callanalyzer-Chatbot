"""
chat_web.py
A simple local web interface for chat_query.py, for non-technical users.

Uses only Python's built-in http.server — no extra packages, no Flask,
no install steps beyond what chat_query.py already needs. This avoids
Tkinter entirely (which had window-persistence issues on this machine's
Python 3.14 install) in favour of something that just needs a browser,
which every Windows machine already has.

HOW IT WORKS:
    1. This script starts a small local web server on your own machine
       (nothing leaves your computer, nothing is exposed to the internet).
    2. It automatically opens your default browser to that local page.
    3. Your colleague types a question and clicks Ask — the browser talks
       to this local server, which calls chat_query.py's ask() function
       exactly as the terminal version does, and sends the answer back.

To stop the server, close the console window this was started from
(closing just the browser tab does not stop it).
"""

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import chat_query

PORT = 5050

PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MasonMart Data Assistant</title>
<style>
  * { box-sizing: border-box; }
  body {
    font-family: 'Segoe UI', -apple-system, sans-serif;
    background: #eef1f5;
    margin: 0;
    height: 100vh;
    display: flex;
    flex-direction: column;
  }
  header {
    background: linear-gradient(135deg, #1e293b, #0f172a);
    color: white;
    padding: 18px 28px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.15);
    z-index: 2;
  }
  header h1 { margin: 0; font-size: 19px; font-weight: 600; }
  header p { margin: 4px 0 0; font-size: 13px; color: #94a3b8; }

  #chat-container {
    flex: 1;
    overflow-y: auto;
    padding: 24px 0;
  }
  #chat {
    max-width: 760px;
    margin: 0 auto;
    padding: 0 20px;
  }
  .row { display: flex; margin-bottom: 14px; }
  .row.user { justify-content: flex-end; }
  .row.assistant { justify-content: flex-start; }
  .row.system { justify-content: center; }

  .bubble {
    max-width: 72%;
    padding: 12px 16px;
    border-radius: 16px;
    font-size: 14.5px;
    line-height: 1.5;
    white-space: pre-wrap;
    box-shadow: 0 1px 2px rgba(0,0,0,0.06);
  }
  .row.user .bubble {
    background: #2563eb;
    color: white;
    border-bottom-right-radius: 4px;
  }
  .row.assistant .bubble {
    background: white;
    color: #1e293b;
    border-bottom-left-radius: 4px;
    border: 1px solid #e2e8f0;
  }
  .row.system .bubble {
    background: transparent;
    color: #94a3b8;
    font-style: italic;
    font-size: 13px;
    box-shadow: none;
    padding: 4px 12px;
  }
  .timestamp {
    font-size: 10.5px;
    color: #94a3b8;
    margin-top: 4px;
    text-align: right;
  }
  .row.assistant .timestamp { text-align: left; }

  .typing-dots span {
    display: inline-block;
    width: 6px; height: 6px;
    margin: 0 1px;
    background: #94a3b8;
    border-radius: 50%;
    animation: blink 1.2s infinite ease-in-out;
  }
  .typing-dots span:nth-child(2) { animation-delay: 0.2s; }
  .typing-dots span:nth-child(3) { animation-delay: 0.4s; }
  @keyframes blink { 0%, 80%, 100% { opacity: 0.3; } 40% { opacity: 1; } }

  #input-bar {
    background: white;
    border-top: 1px solid #e2e8f0;
    padding: 14px 20px;
  }
  #input-row {
    max-width: 760px;
    margin: 0 auto;
    display: flex;
    gap: 10px;
  }
  #question {
    flex: 1;
    padding: 12px 16px;
    font-size: 14.5px;
    border: 1px solid #cbd5e1;
    border-radius: 24px;
    outline: none;
    transition: border-color 0.15s;
  }
  #question:focus { border-color: #2563eb; }
  #ask-btn {
    padding: 0 24px;
    font-size: 14.5px;
    font-weight: 600;
    background: #2563eb;
    color: white;
    border: none;
    border-radius: 24px;
    cursor: pointer;
    transition: background 0.15s;
  }
  #ask-btn:hover:not(:disabled) { background: #1d4ed8; }
  #ask-btn:disabled { background: #93c5fd; cursor: default; }

  .examples {
    max-width: 760px;
    margin: 0 auto 16px;
    padding: 0 20px;
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .example-chip {
    background: white;
    border: 1px solid #cbd5e1;
    color: #475569;
    padding: 6px 14px;
    border-radius: 16px;
    font-size: 12.5px;
    cursor: pointer;
    transition: all 0.15s;
  }
  .example-chip:hover { background: #f1f5f9; border-color: #94a3b8; }
</style>
</head>
<body>
<header>
  <h1>MasonMart Data Assistant</h1>
  <p>Ask about calls, leads, or orders in plain English</p>
</header>

<div id="chat-container">
  <div id="chat"></div>
</div>

<div id="input-bar">
  <div class="examples" id="examples">
    <div class="example-chip" onclick="useExample(this)">how many orders came in this week?</div>
    <div class="example-chip" onclick="useExample(this)">which leads haven't been called in 3+ days?</div>
    <div class="example-chip" onclick="useExample(this)">total sales this month</div>
  </div>
  <div id="input-row">
    <input type="text" id="question" placeholder="Type a question and press Enter..." autofocus>
    <button id="ask-btn" onclick="ask()">Ask</button>
  </div>
</div>

<script>
const chatContainer = document.getElementById('chat-container');
const chat = document.getElementById('chat');
const input = document.getElementById('question');
const btn = document.getElementById('ask-btn');
const examples = document.getElementById('examples');

function timeNow() {
  return new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
}

function addRow(text, role, isTyping) {
  const row = document.createElement('div');
  row.className = 'row ' + role;
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  if (isTyping) {
    bubble.innerHTML = '<span class="typing-dots"><span></span><span></span><span></span></span>';
    row.id = 'typing-row';
  } else {
    bubble.textContent = text;
    const ts = document.createElement('div');
    ts.className = 'timestamp';
    ts.textContent = timeNow();
    row.appendChild(bubble);
    row.appendChild(ts);
    chat.appendChild(row);
    chatContainer.scrollTop = chatContainer.scrollHeight;
    return;
  }
  row.appendChild(bubble);
  chat.appendChild(row);
  chatContainer.scrollTop = chatContainer.scrollHeight;
}

function useExample(el) {
  input.value = el.textContent;
  ask();
}

async function ask() {
  const question = input.value.trim();
  if (!question) return;
  input.value = '';
  examples.style.display = 'none';
  addRow(question, 'user');
  addRow('', 'assistant', true);
  btn.disabled = true;

  try {
    const resp = await fetch('/ask', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question})
    });
    const data = await resp.json();
    document.getElementById('typing-row').remove();
    addRow(data.answer, 'assistant');
  } catch (e) {
    document.getElementById('typing-row').remove();
    addRow('Something went wrong: ' + e, 'system');
  }
  btn.disabled = false;
  input.focus();
}

input.addEventListener('keydown', function(e) {
  if (e.key === 'Enter') ask();
});
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # keep the console quiet

    def do_GET(self):
        if self.path == "/":
            body = PAGE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/ask":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                question = json.loads(raw).get("question", "")
                answer = chat_query.ask(question)
            except Exception as e:
                answer = f"Something went wrong answering that: {e}"

            body = json.dumps({"answer": answer}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def main():
    if not chat_query.API_KEY:
        print("WARNING: GROQ_API_KEY not set in .env — questions will fail until it's added.")

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"MasonMart Data Assistant running at {url}")
    print("Leave this window open. Close it to stop the assistant.")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    server.serve_forever()


if __name__ == "__main__":
    main()