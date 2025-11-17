import os, json, time, threading, requests
from flask import Flask, request, jsonify
from redis import Redis, ConnectionError, TimeoutError

# ===== קונפיג בסיסי =====
TOKEN = os.getenv("TOKEN")  # Render → Environment: TOKEN=123456:ABC...
if not TOKEN or ":" not in TOKEN:
