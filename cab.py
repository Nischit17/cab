
import json
import math
import os
import tempfile
import psycopg2
from psycopg2.extras import DictCursor
import re
import pandas as pd
import numpy as np
from flask import Flask, render_template_string, request, redirect, url_for, flash, g, session, get_flashed_messages
import google.generativeai as genai
from werkzeug.utils import secure_filename
try:
    from pyngrok import ngrok
except ImportError:
    ngrok = None  # pyngrok not available on this platform
import threading
import webbrowser
import pdfplumber
import asyncio
try:
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.storage import StorageFile
except ImportError:
    OcrEngine = None  # winrt not available on this platform

app = Flask(__name__)
app.secret_key = 'supersecretkey'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max upload
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://localhost:5432/cab_app')
# Fix for Render/Railway: they provide postgres:// but psycopg2 needs postgresql://
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

class PgConnection:
    """Wrapper to provide sqlite3-like db.execute() interface over psycopg2."""
    def __init__(self, dsn):
        self.conn = psycopg2.connect(dsn)
    def execute(self, query, params=None):
        cursor = self.conn.cursor(cursor_factory=DictCursor)
        cursor.execute(query, params)
        return cursor
    def commit(self):
        self.conn.commit()
    def rollback(self):
        self.conn.rollback()
    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

# --- GEMINI CONFIGURATION ---
API_KEY = "AIzaSyCkbPbtnTQL0-yXHZHkAC1BKYVUTlgJxw0"
try:
    genai.configure(api_key=API_KEY)
except Exception as e:
    print(f"Error configuring Gemini API: {e}")

# --- DATABASE ---
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = PgConnection(DATABASE_URL)
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        
        # UG Schemes Table
        db.execute('CREATE TABLE IF NOT EXISTS schemes (id SERIAL PRIMARY KEY, name TEXT UNIQUE, department TEXT DEFAULT \'ISE\')')
        db.execute('CREATE TABLE IF NOT EXISTS semesters (id SERIAL PRIMARY KEY, number INTEGER, scheme_id INTEGER REFERENCES schemes(id))')
        db.execute('CREATE TABLE IF NOT EXISTS sections (id SERIAL PRIMARY KEY, semester_id INTEGER REFERENCES semesters(id), name TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS students (id SERIAL PRIMARY KEY, section_id INTEGER REFERENCES sections(id), usn TEXT, name TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS subjects (id SERIAL PRIMARY KEY, semester_id INTEGER REFERENCES semesters(id), section_id INTEGER REFERENCES sections(id), code TEXT, title TEXT, faculty TEXT, awd_test INTEGER DEFAULT 25, awd_quiz INTEGER DEFAULT 15, awd_assign INTEGER DEFAULT 20, awd_see INTEGER DEFAULT 40)')
        db.execute('CREATE TABLE IF NOT EXISTS marks (id SERIAL PRIMARY KEY, student_id INTEGER REFERENCES students(id), subject_id INTEGER REFERENCES subjects(id), mark_type TEXT, value INTEGER DEFAULT 0, ai_prediction TEXT, ai_reason TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS course_outcomes (id SERIAL PRIMARY KEY, subject_id INTEGER REFERENCES subjects(id), co_number INTEGER, description TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS faculty (id SERIAL PRIMARY KEY, name TEXT, department TEXT, email TEXT, phone TEXT, username TEXT, password TEXT DEFAULT \'123\', last_active TIMESTAMP)')
        db.execute('CREATE TABLE IF NOT EXISTS course_subjects (id SERIAL PRIMARY KEY, scheme_id INTEGER REFERENCES schemes(id), semester_number INTEGER, code TEXT, title TEXT)')
        # PG Tables
        db.execute('CREATE TABLE IF NOT EXISTS pg_batches (id SERIAL PRIMARY KEY, program TEXT DEFAULT \'Data Engineering\', start_year INTEGER, end_year INTEGER, UNIQUE(program, start_year, end_year))')
        db.execute('CREATE TABLE IF NOT EXISTS pg_students (id SERIAL PRIMARY KEY, batch_id INTEGER REFERENCES pg_batches(id), usn TEXT, name TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS pg_modules (id SERIAL PRIMARY KEY, batch_id INTEGER REFERENCES pg_batches(id), year INTEGER, code TEXT, title TEXT, faculty TEXT, assignment_mode TEXT DEFAULT \'single\')')
        db.execute('CREATE TABLE IF NOT EXISTS pg_marks (id SERIAL PRIMARY KEY, student_id INTEGER REFERENCES pg_students(id), module_id INTEGER REFERENCES pg_modules(id), mark_type TEXT, value REAL DEFAULT 0, ai_prediction TEXT, ai_reason TEXT)')
        # Auditing & Notifications
        db.execute('CREATE TABLE IF NOT EXISTS audit_logs (id SERIAL PRIMARY KEY, faculty TEXT, action_type TEXT, entity_id INTEGER, old_data TEXT, new_data TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, is_restored INTEGER DEFAULT 0)')
        db.execute('CREATE TABLE IF NOT EXISTS notifications (id SERIAL PRIMARY KEY, message TEXT, is_read INTEGER DEFAULT 0, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, log_id INTEGER REFERENCES audit_logs(id))')
        # Sessions tracking
        db.execute('CREATE TABLE IF NOT EXISTS sessions (id SERIAL PRIMARY KEY, faculty_id INTEGER REFERENCES faculty(id), session_token TEXT, user_agent TEXT, ip_address TEXT, network_name TEXT, login_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP, is_active INTEGER DEFAULT 1)')
        # Student classifications
        db.execute('CREATE TABLE IF NOT EXISTS pg_student_classifications (id SERIAL PRIMARY KEY, student_id INTEGER, module_id INTEGER, category TEXT)')
        db.commit()
        
        # Create default schemes if they don't exist
        for scheme_name in ['Scheme 23', 'Scheme 24', 'Scheme 25']:
            db.execute('INSERT INTO schemes (name, department) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING', (scheme_name, 'ISE'))
        db.commit()
        
        # Get Scheme 23 id for migration
        scheme_23 = db.execute('SELECT id FROM schemes WHERE name = %s', ('Scheme 23',)).fetchone()
        if scheme_23:
            scheme_23_id = scheme_23['id']
            
            # Migrate existing semesters without scheme_id to Scheme 23
            orphan_sems = db.execute('SELECT id FROM semesters WHERE scheme_id IS NULL').fetchall()
            if orphan_sems:
                for sem in orphan_sems:
                    db.execute('UPDATE semesters SET scheme_id = %s WHERE id = %s', (scheme_23_id, sem['id']))
                db.commit()
        
        # Auto-create 8 semesters for each scheme that doesn't have 8 semesters
        schemes = db.execute('SELECT id, name FROM schemes').fetchall()
        for scheme in schemes:
            scheme_id = scheme['id']
            current_sems = db.execute('SELECT number FROM semesters WHERE scheme_id = %s', (scheme_id,)).fetchall()
            existing_numbers = [row['number'] for row in current_sems]
            
            for i in range(1, 9):
                if i not in existing_numbers:
                    db.execute('INSERT INTO semesters (number, scheme_id) VALUES (%s, %s)', (i, scheme_id))
        db.commit()


# --- AI HELPER ---

def get_gemini_response(prompt, file_path=None, file_mime=None):
    # Use available models from ListModels
    models = ["gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-flash-latest", "gemini-1.5-flash"]
    
    errors = []
    for model_name in models:
        try:
            model = genai.GenerativeModel(model_name)
            if file_path and file_mime:
                uploaded_file = genai.upload_file(file_path, mime_type=file_mime)
                response = model.generate_content([prompt, uploaded_file])
            else:
                response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            errors.append(f"{model_name}: {e}")
            continue
    raise Exception(f"All AI models failed. Errors: {'; '.join(errors)}")


# --- STYLES ---
STYLES = """
<style>
:root { --primary: #4361ee; --primary-soft: #eef2f6; --success: #10b981; --warning: #f59e0b; --danger: #ef4444; }
* { box-sizing: border-box; }
body { background: linear-gradient(135deg, #f5f7fa 0%, #e4e8ec 100%); font-family: 'Inter', sans-serif; margin: 0; min-height: 100vh; }
.navbar { background: rgba(255,255,255,0.95); backdrop-filter: blur(10px); box-shadow: 0 2px 15px rgba(0,0,0,0.08); padding: 1rem 2rem; position: sticky; top: 0; z-index: 100; display: flex; justify-content: space-between; align-items: center; }
.navbar-brand { font-weight: 800; color: var(--primary); font-size: 1.4rem; text-decoration: none; }
.navbar-links a { margin-left: 1rem; text-decoration: none; color: #4b5563; font-weight: 500; transition: color 0.2s; }
@media (max-width: 768px) {
    .navbar-links { display: none !important; }
    .navbar { padding: 1rem; }
}
.navbar-links a:hover { color: var(--primary); }
.container { max-width: 1200px; margin: 0 auto; padding: 2rem; }
.card { background: white; border-radius: 16px; box-shadow: 0 4px 20px rgba(0,0,0,0.06); padding: 2rem; margin-bottom: 1.5rem; transition: transform 0.2s, box-shadow 0.2s; }
.card:hover { transform: translateY(-2px); box-shadow: 0 8px 30px rgba(0,0,0,0.1); }
.hover-card { transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1); border-radius: 20px; box-shadow: 0 10px 25px rgba(0,0,0,0.05); }
.hover-card:hover { transform: translateY(-8px) scale(1.02); box-shadow: 0 20px 40px rgba(0,0,0,0.12); z-index: 10; }
.faculty-stat { display: inline-flex; align-items: center; justify-content: center; gap: 0.5rem; background: #f8fafc; padding: 0.4rem 1rem; border-radius: 50px; margin-top: 1rem; color: #475569; font-weight: 500; font-size: 0.9rem; border: 1px solid #e2e8f0; }
.card-clickable { cursor: pointer; text-decoration: none; color: inherit; display: block; }
.btn { padding: 0.7rem 1.5rem; border-radius: 10px; border: none; cursor: pointer; font-weight: 600; transition: all 0.2s; text-decoration: none; display: inline-block; }
.btn-primary { background: var(--primary); color: white; }
.btn-success { background: var(--success); color: white; }
.btn-warning { background: var(--warning); color: white; }
.btn-danger { background: var(--danger); color: white; }
.btn-outline { background: transparent; border: 2px solid var(--primary); color: var(--primary); }
.btn-back { background: linear-gradient(135deg, #4361ee, #3a0ca3); color: white; border-radius: 50px; padding: 0.8rem 2.5rem; font-size: 1.1rem; box-shadow: 0 4px 15px rgba(67, 97, 238, 0.4); text-decoration: none; display: inline-block; transition: transform 0.2s, box-shadow 0.2s; }
.btn-back:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(67, 97, 238, 0.6); color: white; }
.btn-sm { padding: 0.4rem 1rem; font-size: 0.875rem; }
.btn-lg { padding: 1rem 2rem; font-size: 1.1rem; }
.form-control { width: 100%; padding: 0.75rem 1rem; border: 2px solid #e2e8f0; border-radius: 10px; font-size: 1rem; }
.form-control:focus { outline: none; border-color: var(--primary); }
.form-label { display: block; margin-bottom: 0.5rem; font-weight: 600; color: #374151; }
.grid { display: grid; gap: 1.5rem; justify-content: center; }
.grid-2 { grid-template-columns: repeat(auto-fit, minmax(300px, 450px)); }
.grid-3 { grid-template-columns: repeat(auto-fit, minmax(300px, 350px)); }
.grid-4 { grid-template-columns: repeat(auto-fit, minmax(250px, 300px)); }
.text-center { text-align: center; }
.text-muted { color: #6b7280; }
.mb-2 { margin-bottom: 1rem; }
.mb-3 { margin-bottom: 1.5rem; }
.mt-2 { margin-top: 1rem; }
.mt-3 { margin-top: 1.5rem; }
.badge { display: inline-block; padding: 0.3rem 0.8rem; border-radius: 20px; font-size: 0.8rem; font-weight: 600; }
.badge-primary { background: var(--primary-soft); color: var(--primary); }
.badge-success { background: #d1fae5; color: #059669; }
h1, h2, h3 { color: #1e1e1e; margin: 0 0 1rem 0; }
.page-title { font-size: 2rem; font-weight: 800; margin-bottom: 0.5rem; }
.page-subtitle { color: #6b7280; font-size: 1.1rem; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 1rem; text-align: left; border-bottom: 1px solid #e5e7eb; }
th { background: #f9fafb; font-weight: 600; }
tr:hover { background: #f9fafb; }
.marks-input { width: 70px; text-align: center; padding: 0.5rem; border: 2px solid #e2e8f0; border-radius: 8px; font-weight: 600; }
.marks-input:focus { border-color: var(--primary); outline: none; }
.sidebar { position: fixed; left: 0; top: 60px; width: 250px; height: calc(100vh - 60px); background: white; border-right: 1px solid #e5e7eb; padding: 1.5rem 1rem; overflow-y: auto; }
.sidebar-link { display: block; padding: 0.75rem 1rem; margin: 0.25rem 0; border-radius: 10px; color: #4b5563; text-decoration: none; font-weight: 500; transition: all 0.2s; }
.sidebar-link:hover, .sidebar-link.active { background: var(--primary-soft); color: var(--primary); }
.main-with-sidebar { margin-left: 250px; padding: 2rem; }
.prediction-badge { position: relative; cursor: help; }
.prediction-badge .tooltip { visibility: hidden; position: absolute; bottom: 100%; left: 50%; transform: translateX(-50%); background: #1f2937; color: white; padding: 0.75rem 1rem; border-radius: 8px; font-size: 0.8rem; width: 280px; z-index: 10; opacity: 0; transition: opacity 0.2s; }
.prediction-badge:hover .tooltip { visibility: visible; opacity: 1; }
.alert { padding: 1rem 1.5rem; border-radius: 10px; margin-bottom: 1rem; }
.alert-success { background: #d1fae5; color: #065f46; }
.modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); justify-content: center; align-items: center; z-index: 200; }
.sem-tile { text-align: center; padding: 2rem; border: 2px solid #e5e7eb; transition: all 0.3s; }
.sem-tile:hover { border-color: var(--primary); background: var(--primary-soft); }
.sem-tile h2 { font-size: 3rem; margin: 0; color: var(--primary); }
.sem-tile p { margin: 0.5rem 0 0; color: #6b7280; }
.section-card { background: #f9fafb; border-radius: 12px; padding: 1.5rem; margin-bottom: 1rem; border: 1px solid #e5e7eb; }
.section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; }
.add-btn { width: 100%; padding: 1.2rem; border: none; border-radius: 16px; background: transparent; color: #94a3b8; font-weight: 700; cursor: pointer; transition: all 0.3s; position: relative; overflow: hidden; font-size: 1rem; }
.add-btn::before { content: ''; position: absolute; inset: 0; border-radius: 16px; padding: 2.5px; background: linear-gradient(90deg, #4361ee, #3b82f6, #8b5cf6, #ec4899, #4361ee); background-size: 300% 100%; animation: borderRotate 3s linear infinite; -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0); -webkit-mask-composite: xor; mask-composite: exclude; }
@keyframes borderRotate { 0% { background-position: 0% 50%; } 100% { background-position: 300% 50%; } }
.add-btn:hover { color: var(--primary); background: var(--primary-soft); transform: scale(1.01); }
.tabs { display: flex; gap: 0.5rem; margin-bottom: 1.5rem; border-bottom: 2px solid #e5e7eb; padding-bottom: 0.5rem; }
.tab { padding: 0.75rem 1.5rem; border-radius: 8px 8px 0 0; text-decoration: none; color: #6b7280; font-weight: 600; transition: all 0.2s; }
.tab:hover { color: var(--primary); }
.tab.active { background: var(--primary); color: white; }
.table-responsive { width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; border-radius: 8px; }
@media (max-width: 768px) { 
    .grid-2, .grid-3, .grid-4 { grid-template-columns: 1fr; } 
    .sidebar { display: none; } 
    .main-with-sidebar { margin-left: 0; padding: 1rem; } 
    .navbar { flex-wrap: wrap; justify-content: center; gap: 0.5rem; padding: 0.75rem 1rem; }
    .navbar-brand { width: 100%; text-align: center; font-size: 1.2rem; }
    .navbar-links { display: flex; flex-wrap: wrap; justify-content: center; width: 100%; gap: 0.5rem; }
    .navbar-links a { margin-left: 0; padding: 0.4rem 0.8rem; background: #f1f5f9; border-radius: 8px; font-size: 0.85rem; }
    .container { padding: 1rem; }
    .card { padding: 1rem; border-radius: 12px; }
    .page-title { font-size: 1.4rem; }
    .page-subtitle { font-size: 0.9rem; }
    .marks-input { width: 55px; font-size: 0.8rem; padding: 0.3rem; }
    .btn { padding: 0.5rem 0.8rem; font-size: 0.8rem; }
    .btn-sm { padding: 0.3rem 0.6rem; font-size: 0.75rem; }
    .btn-back { padding: 0.6rem 1.5rem; font-size: 0.9rem; }
    .tabs { flex-wrap: wrap; gap: 0.3rem; }
    .tab { padding: 0.5rem 0.8rem; font-size: 0.8rem; }
    .sem-tile h2 { font-size: 2rem; }
    .hover-card:hover { transform: translateY(-4px) scale(1.01); }
    .form-control { font-size: 0.9rem; padding: 0.6rem 0.8rem; }
    .section-card { padding: 1rem; }
    .section-header { flex-direction: column; gap: 0.5rem; align-items: flex-start; }
    h1, h2, h3 { font-size: revert; }
    [style*="grid-template-columns: 1fr 1fr"] { grid-template-columns: 1fr !important; }
    .marks-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; width: 100%; position: relative; }
    .marks-scroll table { border-collapse: separate; border-spacing: 0; min-width: 650px; }
    .marks-scroll table th, .marks-scroll table td { padding: 0.5rem 0.4rem; font-size: 0.8rem; white-space: nowrap; min-width: 75px; }
    .marks-scroll::after { content: 'Swipe → for more'; display: block; text-align: center; padding: 0.5rem; font-size: 0.75rem; color: #94a3b8; font-style: italic; }
}
</style>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/animate.css/4.1.1/animate.min.css"/>
"""

NAV = '''<nav class="navbar animate__animated animate__fadeInDown">
    <a href="/" class="navbar-brand">🎓 CAB Smart System</a>
    <div class="navbar-links">
        <a href="/">Home</a>
        <a href="/faculty">Faculty</a>
        <a href="/logout" style="color: #ef4444;">Logout</a>
    </div>
</nav>'''

# --- LOGIN & SESSION ---
@app.before_request
def require_login():
    if request.endpoint and request.endpoint not in ['login', 'static', 'logout'] and 'user' not in session:
        return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    import re
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        if username.lower() == 'hod' and password == '123':
            session['user'] = 'hod'
            session['role'] = 'hod'
            return redirect(url_for('index'))
            
        # Check Faculty Login
        db = get_db()
        faculty = db.execute('SELECT * FROM faculty WHERE LOWER(username) = LOWER(%s) AND password = %s', (username, password)).fetchone()
        if faculty:
            session['user'] = faculty['name']
            session['faculty_id'] = faculty['id']
            session['role'] = 'faculty'
            
            # Track session
            import uuid
            session_token = str(uuid.uuid4())
            session['session_token'] = session_token
            user_agent = request.headers.get('User-Agent', 'Unknown')
            ip_address = request.remote_addr or 'Unknown'
            db.execute('INSERT INTO sessions (faculty_id, session_token, user_agent, ip_address) VALUES (%s, %s, %s, %s)',
                       (faculty['id'], session_token, user_agent, ip_address))
            db.execute('UPDATE faculty SET last_active = CURRENT_TIMESTAMP WHERE id = %s', (faculty['id'],))
            db.commit()
            
            return redirect(url_for('faculty_dashboard'))
                
        flash('Invalid credentials!')
    
    return """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - CAB System</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        body {
            font-family: 'Inter', sans-serif;
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            background: linear-gradient(-45deg, #0f172a, #1e3a5f, #134e4a, #0f172a);
            background-size: 400% 400%;
            animation: gradientShift 15s ease infinite;
            overflow: hidden;
        }
        
        @keyframes gradientShift {
            0% { background-position: 0% 50%; }
            50% { background-position: 100% 50%; }
            100% { background-position: 0% 50%; }
        }
        
        /* Floating particles */
        .particles {
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            overflow: hidden;
            z-index: 0;
        }
        
        .particle {
            position: absolute;
            width: 10px;
            height: 10px;
            background: rgba(255,255,255,0.3);
            border-radius: 50%;
            animation: float 20s infinite;
        }
        
        @keyframes float {
            0%, 100% { transform: translateY(100vh) rotate(0deg); opacity: 0; }
            10% { opacity: 1; }
            90% { opacity: 1; }
            100% { transform: translateY(-100vh) rotate(720deg); opacity: 0; }
        }
        
        /* Login card */
        .login-container {
            position: relative;
            z-index: 10;
            width: 100%;
            max-width: 420px;
            padding: 1.5rem;
        }
        
        .login-card {
            background: rgba(255, 255, 255, 0.15);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border-radius: 24px;
            border: 1px solid rgba(255, 255, 255, 0.2);
            padding: 3rem 2.5rem;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
            animation: slideUp 0.6s ease-out;
        }
        
        @keyframes slideUp {
            from { opacity: 0; transform: translateY(30px); }
            to { opacity: 1; transform: translateY(0); }
        }
        
        .logo {
            text-align: center;
            margin-bottom: 2rem;
        }
        
        .logo-icon {
            width: 80px;
            height: 80px;
            background: linear-gradient(135deg, #0d9488 0%, #1e3a5f 100%);
            border-radius: 20px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 2.5rem;
            margin: 0 auto 1rem;
            box-shadow: 0 10px 30px rgba(13, 148, 136, 0.4);
        }
        
        .logo h1 {
            color: white;
            font-size: 1.75rem;
            font-weight: 700;
            letter-spacing: -0.5px;
        }
        
        .logo p {
            color: rgba(255,255,255,0.7);
            font-size: 0.9rem;
            margin-top: 0.25rem;
        }
        
        .form-group {
            margin-bottom: 1.5rem;
        }
        
        .form-group label {
            display: block;
            color: rgba(255,255,255,0.9);
            font-size: 0.85rem;
            font-weight: 500;
            margin-bottom: 0.5rem;
        }
        
        .form-group input {
            width: 100%;
            padding: 1rem 1.25rem;
            background: rgba(255,255,255,0.1);
            border: 1px solid rgba(255,255,255,0.2);
            border-radius: 12px;
            color: white;
            font-size: 1rem;
            transition: all 0.3s ease;
        }
        
        .form-group input::placeholder {
            color: rgba(255,255,255,0.5);
        }
        
        .form-group input:focus {
            outline: none;
            background: rgba(255,255,255,0.2);
            border-color: rgba(255,255,255,0.5);
            box-shadow: 0 0 0 4px rgba(255,255,255,0.1);
        }
        
        .btn-login {
            width: 100%;
            padding: 1rem;
            background: linear-gradient(135deg, #0d9488 0%, #0f766e 100%);
            border: none;
            border-radius: 12px;
            color: white;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s ease;
            box-shadow: 0 4px 15px rgba(13, 148, 136, 0.4);
        }
        
        .btn-login:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(102, 126, 234, 0.5);
        }
        
        .btn-login:active {
            transform: translateY(0);
        }
        
        .footer-text {
            text-align: center;
            margin-top: 1.5rem;
            color: rgba(255,255,255,0.6);
            font-size: 0.85rem;
        }
        
        .alert {
            background: rgba(239, 68, 68, 0.2);
            border: 1px solid rgba(239, 68, 68, 0.3);
            color: #fecaca;
            padding: 0.75rem 1rem;
            border-radius: 10px;
            margin-bottom: 1.5rem;
            font-size: 0.9rem;
        }
    </style>
</head>
<body>
    <div class="particles">
        <div class="particle" style="left: 10%; animation-delay: 0s; animation-duration: 25s;"></div>
        <div class="particle" style="left: 20%; animation-delay: 2s; animation-duration: 20s;"></div>
        <div class="particle" style="left: 30%; animation-delay: 4s; animation-duration: 28s;"></div>
        <div class="particle" style="left: 40%; animation-delay: 6s; animation-duration: 22s;"></div>
        <div class="particle" style="left: 50%; animation-delay: 8s; animation-duration: 26s;"></div>
        <div class="particle" style="left: 60%; animation-delay: 10s; animation-duration: 24s;"></div>
        <div class="particle" style="left: 70%; animation-delay: 12s; animation-duration: 21s;"></div>
        <div class="particle" style="left: 80%; animation-delay: 14s; animation-duration: 27s;"></div>
        <div class="particle" style="left: 90%; animation-delay: 16s; animation-duration: 23s;"></div>
    </div>
    
    <div class="login-container">
        <div class="login-card">
            <div class="logo">
                <div class="logo-icon">📊</div>
                <h1>CAB System</h1>
                <p>Course Assessment Board</p>
            </div>
            
            """ + (''.join([f'<div class="alert">{m}</div>' for m in get_flashed_messages()])) + """
            
            <form method="post">
                <div class="form-group">
                    <label>Username</label>
                    <input type="text" name="username" placeholder="Enter your username" required>
                </div>
                <div class="form-group">
                    <label>Password</label>
                    <input type="password" name="password" placeholder="Enter your password" required>
                </div>
                <button type="submit" class="btn-login">Sign In →</button>
            </form>
            
            <div class="footer-text">🔒 Authorized Access Only</div>
        </div>
    </div>
</body>
</html>"""

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

LOADING_OVERLAY = """
<div id="loadingOverlay" style="display:none; position:fixed; inset:0; background:rgba(255,255,255,0.95); z-index:9999; flex-direction:column; justify-content:center; align-items:center; text-align:center;">
    <div style="width: 50px; height: 50px; border: 5px solid #e5e7eb; border-top-color: var(--primary); border-radius: 50%; animation: spin 1s linear infinite;"></div>
    <h2 id="loadingText" class="mt-3 animate__animated animate__pulse animate__infinite">🔮 AI Predicting...</h2>
    <p class="text-muted">Analyzing student performance patterns...</p>
    <button type="button" onclick="cancelLoading()" class="btn btn-outline" style="margin-top: 1.5rem; border-color: #ef4444; color: #ef4444;">Cancel Operation</button>
</div>
<style>
@keyframes spin { to { transform: rotate(360deg); } }
</style>
<script>
function cancelLoading() {
    window.stop();
    document.getElementById('loadingOverlay').style.display = 'none';
}
document.addEventListener('DOMContentLoaded', function() {
    document.querySelectorAll('form').forEach(form => {
        form.addEventListener('submit', function(e) {
            if (this.action.includes('predict') || this.action.includes('parse') || this.action.includes('import')) {
                const text = this.action.includes('predict') ? '🔮 AI Predicting...' : '⚡ AI Analyzing...';
                document.getElementById('loadingText').innerText = text;
                document.getElementById('loadingOverlay').style.display = 'flex';
            }
        });
    });
});
</script>
"""

GLOBAL_MODALS = """
<div id="globalConfirmModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.5); z-index:10000; justify-content:center; align-items:center;">
    <div style="background:white; padding:2rem; border-radius:12px; width:90%; max-width:400px; text-align:center; box-shadow:0 10px 25px rgba(0,0,0,0.2); animation: zoomIn 0.2s ease-out;">
        <div style="width:50px; height:50px; border-radius:50%; background:#fee2e2; color:#ef4444; display:flex; align-items:center; justify-content:center; font-size:1.5rem; margin:0 auto 1rem;">⚠️</div>
        <h3 style="margin:0 0 0.5rem 0; color:#1f2937;">Confirm Action</h3>
        <p id="globalConfirmMessage" style="color:#6b7280; margin-bottom:1.5rem; line-height:1.4;">Are you sure?</p>
        <div style="display:flex; gap:1rem; justify-content:center;">
            <button onclick="closeGlobalConfirm()" class="btn btn-outline" style="flex:1;">Cancel</button>
            <button id="globalConfirmBtn" class="btn" style="flex:1; background:#ef4444; color:white; border:none;">Yes, I'm sure</button>
        </div>
    </div>
</div>
<style>
@keyframes zoomIn { from { transform: scale(0.95); opacity: 0; } to { transform: scale(1); opacity: 1; } }
</style>
<script>
let pendingConfirmUrl = null;
let pendingConfirmForm = null;

function customConfirm(message, url) {
    document.getElementById('globalConfirmMessage').innerText = message;
    pendingConfirmUrl = url;
    pendingConfirmForm = null;
    document.getElementById('globalConfirmBtn').onclick = executeConfirm;
    document.getElementById('globalConfirmModal').style.display = 'flex';
}

function customConfirmForm(event, message, formElement) {
    if (event) event.preventDefault();
    document.getElementById('globalConfirmMessage').innerText = message;
    pendingConfirmForm = formElement;
    pendingConfirmUrl = null;
    document.getElementById('globalConfirmBtn').style.display = 'block';
    document.querySelector('#globalConfirmModal button.btn-outline').innerText = 'Cancel';
    document.getElementById('globalConfirmBtn').onclick = executeConfirm;
    document.getElementById('globalConfirmModal').style.display = 'flex';
}

function customAlert(message) {
    document.getElementById('globalConfirmMessage').innerHTML = '<span style="color:#ef4444; font-weight:bold;">' + message + '</span>';
    document.getElementById('globalConfirmBtn').style.display = 'none';
    const cancelBtn = document.querySelector('#globalConfirmModal button.btn-outline');
    cancelBtn.innerText = 'OK';
    cancelBtn.onclick = function() {
        closeGlobalConfirm();
        // restore
        document.getElementById('globalConfirmBtn').style.display = 'block';
        cancelBtn.innerText = 'Cancel';
        cancelBtn.onclick = closeGlobalConfirm;
    };
    document.getElementById('globalConfirmModal').style.display = 'flex';
}

function closeGlobalConfirm() {
    document.getElementById('globalConfirmModal').style.display = 'none';
    pendingConfirmUrl = null;
    pendingConfirmForm = null;
}

function executeConfirm() {
    if (pendingConfirmUrl) {
        window.location.href = pendingConfirmUrl;
    } else if (pendingConfirmForm) {
        pendingConfirmForm.submit();
    }
    closeGlobalConfirm();
}
</script>
"""

def base_html(title, content, nav_prepend=""):
    role = session.get('role', 'hod')
    if role == 'faculty':
        nav_html = f'''
        <style>@media (max-width: 768px) {{ .faculty-nav .navbar-links {{ display: none !important; }} .faculty-nav {{ padding: 0.8rem 1rem !important; display: flex !important; align-items: center !important; justify-content: center !important; flex-wrap: nowrap !important; }} .faculty-nav .navbar-brand {{ width: auto !important; margin: 0 !important; font-size: 1.2rem !important; white-space: nowrap; }} .nav-prepend-container {{ position: absolute; left: 1rem; display: flex; align-items: center; justify-content: center; height: 100%; }} }}</style>
        <nav class="navbar faculty-nav animate__animated animate__fadeInDown" style="justify-content: flex-start; gap: 1rem; position: sticky; top: 0; z-index: 1000; min-height: 70px;">
            <div class="nav-prepend-container">{nav_prepend}</div>
            <a href="/faculty_dashboard" class="navbar-brand">🎓 CAB System (Faculty)</a>
            <div class="navbar-links" style="margin-left: auto;">
                <a href="/faculty_dashboard">Dashboard</a>
                <a href="/faculty/profile">Profile</a>
                <a href="/logout" style="color: #ef4444;">Logout</a>
            </div>
        </nav>'''
    else:
        try:
            db = get_db()
            unread_count = db.execute('SELECT COUNT(*) FROM notifications WHERE is_read = 0').fetchone()[0]
            badge_html = f'<span style="background: red; color: white; border-radius: 50%; padding: 0.1rem 0.4rem; font-size: 0.7rem; position: absolute; top: -5px; right: -10px;">{unread_count}</span>' if unread_count > 0 else ''
            notif_badge_sidebar = f'<span style="background: red; color: white; border-radius: 50%; padding: 0.1rem 0.5rem; font-size: 0.7rem; margin-left: 0.5rem;">{unread_count}</span>' if unread_count > 0 else ''
        except:
            badge_html = ''
            notif_badge_sidebar = ''
            
        nav_html = f'''
        <style>
            @media (max-width: 768px) {{
                .hod-nav .navbar-links {{ display: none !important; }}
                .hod-nav {{ padding: 0.8rem 1rem !important; display: flex !important; align-items: center !important; justify-content: center !important; flex-wrap: nowrap !important; }}
                .hod-nav .navbar-brand {{ width: auto !important; margin: 0 !important; font-size: 1.2rem !important; white-space: nowrap; }}
                .hod-hamburger {{ display: flex !important; }}
            }}
            .hod-hamburger {{ display: none; }}
            .hod-sidebar-overlay {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.4); z-index: 1999; }}
            .hod-sidebar-overlay.open {{ display: block; }}
            .hod-mobile-sidebar {{ position: fixed; left: -280px; top: 0; width: 270px; height: 100vh; background: white; z-index: 2000; transition: left 0.3s ease; box-shadow: 4px 0 15px rgba(0,0,0,0.1); padding: 1.5rem 1rem; overflow-y: auto; }}
            .hod-mobile-sidebar.open {{ left: 0; }}
            .hod-mobile-sidebar .sidebar-link {{ display: flex; align-items: center; gap: 0.75rem; padding: 0.85rem 1rem; margin: 0.25rem 0; border-radius: 10px; color: #4b5563; text-decoration: none; font-weight: 500; transition: all 0.2s; font-size: 0.95rem; }}
            .hod-mobile-sidebar .sidebar-link:hover {{ background: #e0e7ff; color: #4361ee; }}
        </style>
        <nav class="navbar hod-nav animate__animated animate__fadeInDown" style="position: sticky; top: 0; z-index: 1000; min-height: 70px;">
            <button class="hod-hamburger" onclick="toggleHodSidebar()" style="background:none; border:none; font-size:1.8rem; color:#1e3a8a; cursor:pointer; padding: 0; line-height: 1; margin-right: 0.5rem; align-items:center; z-index: 2; position: absolute; left: 1rem;">☰</button>
            <a href="/" class="navbar-brand">🎓 CAB System</a>
            <div class="navbar-links" style="margin-left: auto;">
                <a href="/">Dashboard</a>
                <a href="/faculty">Faculty Management</a>
                <a href="/hod/notifications" style="position: relative; display: inline-flex; align-items: center;">
                    🔔 Notifications {badge_html}
                </a>
                <a href="/logout" style="color: #ef4444;">Logout</a>
            </div>
        </nav>
        <div class="hod-sidebar-overlay" id="hodSidebarOverlay" onclick="toggleHodSidebar()"></div>
        <div class="hod-mobile-sidebar" id="hodMobileSidebar">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem; padding-bottom: 1rem; border-bottom: 2px solid #e5e7eb;">
                <span style="font-weight: 700; font-size: 1.1rem; color: #1e3a8a;">🎓 CAB System</span>
                <button onclick="toggleHodSidebar()" style="background: none; border: none; font-size: 1.5rem; cursor: pointer; color: #6b7280;">✕</button>
            </div>
            <a href="/" class="sidebar-link">🏠 Dashboard</a>
            <a href="/faculty" class="sidebar-link">👨‍🏫 Faculty Management</a>
            <a href="/hod/notifications" class="sidebar-link">🔔 Notifications {notif_badge_sidebar}</a>
            <div style="border-top: 1px solid #e5e7eb; margin-top: 1rem; padding-top: 1rem;">
                <a href="/logout" class="sidebar-link" style="color: #ef4444;">🚪 Logout</a>
            </div>
        </div>
        <script>
        function toggleHodSidebar() {{
            document.getElementById('hodMobileSidebar').classList.toggle('open');
            document.getElementById('hodSidebarOverlay').classList.toggle('open');
        }}
        </script>
        '''
    return f'<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{title}</title>{STYLES}</head><body>{LOADING_OVERLAY}{GLOBAL_MODALS}{nav_html}<div class="animate__animated animate__fadeIn">{content}</div></body></html>'

# --- FACULTY MANAGEMENT ---
@app.route('/faculty')
def faculty_list():
    db = get_db()
    faculty_members = db.execute('SELECT * FROM faculty ORDER BY name').fetchall()
    
    table_rows = ""
    for f in faculty_members:
        # Check active status: look for active sessions in last 30 min
        active_session = db.execute('SELECT id FROM sessions WHERE faculty_id = %s AND is_active = 1 ORDER BY login_time DESC LIMIT 1', (f['id'],)).fetchone()
        is_active = active_session is not None
        dot_color = '#22c55e' if is_active else '#ef4444'
        dot_title = 'Online' if is_active else 'Offline'
        status_dot = f'<span style="display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:0.5rem; background:{dot_color}; flex-shrink:0; box-shadow: 0 0 0 2px white, 0 0 0 3px {dot_color};" title="{dot_title}"></span>'
        
        table_rows += f'''
        <tr>
            <td>
                <div style="font-weight: 600; color: #1f2937; display:flex; align-items:center;">{status_dot}{f['name']}</div>
            </td>
            <td><span class="badge" style="background: #e0e7ff; color: #4338ca;">{f['department'] or 'ISE'}</span></td>
            <td>{f['email'] or '-'}</td>
            <td>{f['phone'] or '-'}</td>
            <td style="text-align: right; display: flex; gap: 0.5rem; justify-content: flex-end;">
                <button onclick="openEditFacultyModal({f['id']}, '{f['name']}', '{f['department'] or 'ISE'}')" class="btn btn-sm" style="background: rgba(245, 158, 11, 0.9); color: white; padding: 0.3rem 0.6rem; font-size: 0.75rem; border: none; cursor: pointer;">✏️ Edit</button>
                <form action="/faculty/delete/{f['id']}" method="post" onsubmit="customConfirmForm(event, 'Remove this faculty member?', this)">
                    <button type="submit" class="btn btn-sm" style="background: #fee2e2; color: #ef4444; border: none; cursor:pointer; padding: 0.3rem 0.6rem; font-size: 0.75rem;" title="Delete">🗑️ Delete</button>
                </form>
            </td>
        </tr>'''
        
    if not table_rows:
        table_rows = '<tr><td colspan="5" style="text-align:center; padding: 2rem; color: #6b7280;">No faculty members added yet. Add one below!</td></tr>'

    content = f'''
    <div class="container">
        <div class="mb-3" style="display: flex; justify-content: space-between; align-items: center;">
            <div>
                <h1 class="page-title mt-2">👨‍🏫 Faculty Directory</h1>
                <p class="page-subtitle">Manage global faculty members</p>
            </div>
            <a href="/" class="btn-back" style="padding: 0.5rem 1.5rem; font-size: 1rem;"><i class="fas fa-home"></i> Back Home</a>
        </div>
        
        {{% with messages = get_flashed_messages() %}}{{% if messages %}}{{% for m in messages %}}
            {{% if m.startswith('ERROR|') %}}
                <script>document.addEventListener('DOMContentLoaded', () => customAlert('{{{{ m.split('|',1)[1] }}}}'));</script>
            {{% else %}}
                <div class="alert alert-success">{{{{ m }}}}</div>
            {{% endif %}}
        {{% endfor %}}{{% endif %}}{{% endwith %}}
        
        <div class="grid grid-2" style="grid-template-columns: 2fr 1fr; gap: 2rem;">
            <!-- Faculty List -->
            <div class="card">
                <h3 style="margin-bottom: 1.5rem; display: flex; align-items: center; gap: 0.5rem;"><span style="font-size: 1.25rem;">📋</span> All Faculty</h3>
                <div class="table-responsive">
                    <table class="table">
                        <thead>
                            <tr>
                                <th>Name</th>
                                <th>Department</th>
                                <th>Email</th>
                                <th>Phone</th>
                                <th style="text-align: right;">Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {table_rows}
                        </tbody>
                    </table>
                </div>
            </div>
            
            <!-- Add Faculty Form -->
            <div class="card" style="border-left: 4px solid #4361ee; height: fit-content;">
                <h3 style="margin-bottom: 1.5rem; border-bottom: 2px solid #e0e7ff; padding-bottom: 0.5rem;">➕ Add New Faculty</h3>
                <form action="/faculty/add" method="post">
                    <div class="mb-3">
                        <label class="form-label">Full Name <span style="color:red">*</span></label>
                        <input type="text" name="name" class="form-control" required placeholder="e.g., Dr. John Doe">
                    </div>
                    <div class="mb-3">
                        <label class="form-label">Department</label>
                        <select name="department" class="form-control">
                            <option value="ISE">Information Science and Eng (ISE)</option>
                            <option value="CSE">Computer Science and Eng (CSE)</option>
                            <option value="AI">Artificial Intelligence (AI)</option>
                            <option value="ECE">Electronics (ECE)</option>
                            <option value="Basic Science">Basic Science</option>
                            <option value="Other">Other</option>
                        </select>
                    </div>
                    <div class="mb-3">
                        <label class="form-label">Email (Optional)</label>
                        <input type="email" name="email" class="form-control" placeholder="john.doe@university.edu">
                    </div>
                    <div class="mb-3">
                        <label class="form-label">Phone (Optional)</label>
                        <input type="text" name="phone" class="form-control" placeholder="+1 234 567 8900">
                    </div>
                    <button type="submit" class="btn btn-primary mt-2" style="width: 100%; font-size: 1rem;">Add to Directory</button>
                </form>
            </div>
        </div>
        
        <!-- Edit Faculty Modal -->
        <div id="editFacultyModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.5); z-index:200; justify-content:center; align-items:center;">
            <div style="background:white; padding:2rem; border-radius:12px; width:90%; max-width:500px; box-shadow:0 10px 25px rgba(0,0,0,0.2); animation: zoomIn 0.2s ease-out;">
                <h3 style="margin:0 0 1.5rem 0; color:#1f2937; border-bottom: 2px solid #e5e7eb; padding-bottom: 0.5rem;">✏️ Edit Faculty Details</h3>
                <form id="editFacultyForm" method="post">
                    <div class="mb-3" style="text-align:left;">
                        <label class="form-label">Full Name</label>
                        <input type="text" id="editFacName" name="name" class="form-control" required>
                    </div>
                    <div class="mb-3" style="text-align:left;">
                        <label class="form-label">Department</label>
                        <input type="text" id="editFacDept" name="department" class="form-control" required>
                    </div>
                    <div style="display:flex; gap:1rem; margin-top: 1.5rem;">
                        <button type="button" onclick="document.getElementById('editFacultyModal').style.display='none'" class="btn btn-outline" style="flex:1;">Cancel</button>
                        <button type="submit" class="btn btn-primary" style="flex:1;">Save Changes</button>
                    </div>
                </form>
            </div>
        </div>
        <script>
        function openEditFacultyModal(fac_id, name, dept) {{
            document.getElementById('editFacultyForm').action = '/faculty/edit/' + fac_id;
            document.getElementById('editFacName').value = name;
            document.getElementById('editFacDept').value = dept;
            document.getElementById('editFacultyModal').style.display = 'flex';
        }}
        </script>
        
    </div>'''
    return render_template_string(base_html('Faculty Management - CAB', content))

@app.route('/faculty/add', methods=['POST'])
def faculty_add():
    db = get_db()
    name = request.form.get('name', '').strip()
    dept = request.form.get('department', 'ISE')
    email = request.form.get('email', '').strip()
    phone = request.form.get('phone', '').strip()
    
    if name:
        db.execute('INSERT INTO faculty (name, department, email, phone) VALUES (%s, %s, %s, %s)', (name, dept, email, phone))
        db.commit()
        flash(f'Added {name} to faculty list!')
    return redirect(url_for('faculty_list'))

@app.route('/faculty/delete/<int:f_id>', methods=['POST'])
def faculty_delete(f_id):
    db = get_db()
    db.execute('DELETE FROM faculty WHERE id = %s', (f_id,))
    db.commit()
    flash('Faculty member removed.')
    return redirect(url_for('faculty_list'))

@app.route('/faculty/edit/<int:f_id>', methods=['POST'])
def faculty_edit(f_id):
    db = get_db()
    name = request.form.get('name', '').strip()
    dept = request.form.get('department', 'ISE')
    if name:
        db.execute('UPDATE faculty SET name = %s, department = %s WHERE id = %s', (name, dept, f_id))
        db.commit()
        flash(f'Faculty {name} updated successfully!')
    return redirect(url_for('faculty_list'))

# --- FACULTY DASHBOARD CONTROLLERS ---

@app.route('/faculty_dashboard')
def faculty_dashboard():
    if session.get('role') != 'faculty': return redirect(url_for('index'))
    db = get_db()
    fac_name = session.get('user')
    
    ug_sems = db.execute('SELECT count(DISTINCT sem.id) FROM semesters sem JOIN subjects sub ON sub.semester_id = sem.id WHERE sub.faculty = %s', (fac_name,)).fetchone()[0]
    ug_count = db.execute('SELECT count(DISTINCT code) FROM subjects WHERE faculty = %s', (fac_name,)).fetchone()[0]
    pg_count = db.execute('SELECT count(*) FROM pg_modules WHERE faculty = %s', (fac_name,)).fetchone()[0]
    
    cards = ''
    if ug_count > 0:
        cards += f'''
        <a href="/faculty/ug" class="card hover-card" style="text-decoration:none; text-align:center; padding: 2.5rem; border-top: 5px solid #3b82f6; flex: 1 1 300px; max-width: 380px;">
            <div style="font-size: 4rem; margin-bottom: 1rem; filter: drop-shadow(0 4px 6px rgba(59, 130, 246, 0.3));">🎓</div>
            <h2 style="color: #1e293b; margin-bottom: 0.5rem; font-weight: 800;">Undergraduate (UG)</h2>
            <div class="faculty-stat">{ug_sems} Semester{'s' if ug_sems != 1 else ''} • {ug_count} Subject{'s' if ug_count != 1 else ''}</div>
        </a>'''
        
    if pg_count > 0:
        cards += f'''
        <a href="/faculty/pg" class="card hover-card" style="text-decoration:none; text-align:center; padding: 2.5rem; border-top: 5px solid #8b5cf6; flex: 1 1 300px; max-width: 380px;">
            <div style="font-size: 4rem; margin-bottom: 1rem; filter: drop-shadow(0 4px 6px rgba(139, 92, 246, 0.3));">🔬</div>
            <h2 style="color: #1e293b; margin-bottom: 0.5rem; font-weight: 800;">Postgraduate (PG)</h2>
            <div class="faculty-stat">Manage marks for {pg_count} PG modules</div>
        </a>'''
        
    if not cards:
        cards = '<div class="alert alert-success" style="width: 100%; text-align: center;">You have not been assigned any subjects yet. Please contact the HOD.</div>'
        
    content = f'''
    <div class="container mt-4">
        <h1 class="page-title" style="text-align: center;">👋 Welcome, {fac_name}</h1>
        <p class="page-subtitle mb-4" style="text-align: center;">Select a program to manage your assigned subjects.</p>
        
        <div style="display: flex; justify-content: center; flex-wrap: wrap; max-width: 800px; margin: 0 auto; gap: 2rem;">
            {cards}
        </div>
        
        <div class="mobile-only-links" style="margin-top: 3rem; display: flex; justify-content: center; gap: 1rem;">
            <a href="/faculty/profile" class="btn btn-outline" style="display:flex; align-items:center; gap:0.5rem;"><span style="font-size:1.2rem;">⚙️</span> Profile</a>
            <a href="/logout" class="btn btn-danger" style="display:flex; align-items:center; gap:0.5rem;"><span style="font-size:1.2rem;">🚪</span> Logout</a>
        </div>
        <style>@media (min-width: 769px) {{ .mobile-only-links {{ display: none !important; }} }}</style>
    </div>
    '''
    return render_template_string(base_html('Faculty Dashboard - CAB', content))

@app.route('/faculty/ug')
def faculty_ug():
    if session.get('role') != 'faculty': return redirect(url_for('index'))
    db = get_db()
    fac_name = session.get('user')
    sems = db.execute('''
        SELECT DISTINCT sem.id, sem.number, sch.name as scheme_name 
        FROM semesters sem 
        JOIN subjects sub ON sub.semester_id = sem.id 
        JOIN schemes sch ON sem.scheme_id = sch.id
        WHERE sub.faculty = ? 
        ORDER BY sem.number
    ''', (fac_name,)).fetchall()
    sem_cards = ''
    for s in sems:
        sub_count = db.execute('SELECT count(DISTINCT code) FROM subjects WHERE semester_id = %s AND faculty = %s', (s['id'], fac_name)).fetchone()[0]
        sec_count = db.execute('SELECT count(*) FROM subjects WHERE semester_id = %s AND faculty = %s', (s['id'], fac_name)).fetchone()[0]
        sem_cards += f'''
        <a href="/faculty/ug/sem/{s["id"]}" class="card hover-card" style="text-decoration:none; border-top: 5px solid #10b981;">
            <h3 style="color: #0f172a; font-weight: 800; font-size: 1.4rem; margin:0 0 0.5rem 0;">Semester {s["number"]}</h3>
            <p style="color: #64748b; font-size:0.95rem; margin-bottom:1rem;">{s["scheme_name"]}</p>
            <div style="display:inline-block; background: #d1fae5; color: #047857; padding: 0.3rem 0.8rem; border-radius: 50px; font-weight: 600; font-size: 0.8rem;">{sub_count} Subject{'s' if sub_count != 1 else ''} • {sec_count} Section{'s' if sec_count != 1 else ''}</div>
        </a>'''
    content = f'''
    <div class="container">
        <div class="mb-3" style="text-align:center;">
            <h1 class="page-title mt-2">🎓 UG Semesters</h1>
            <p class="page-subtitle">Semesters you are assigned to</p>
        </div>
        <div class="grid grid-3" style="gap: 1.5rem;">{sem_cards}</div>
        <div style="text-align: center; margin-top: 2.5rem;">
            <a href="/faculty_dashboard" class="btn-back">⬅️ Back to Dashboard</a>
        </div>
    </div>
    '''
    return render_template_string(base_html('UG Semesters - CAB', content))

@app.route('/faculty/ug/sem/<int:sem_id>')
def faculty_ug_sem(sem_id):
    if session.get('role') != 'faculty': return redirect(url_for('index'))
    db = get_db()
    fac_name = session.get('user')
    sem = db.execute('SELECT * FROM semesters WHERE id = %s', (sem_id,)).fetchone()
    subjects = db.execute('SELECT sub.*, sec.name as sec_name FROM subjects sub JOIN sections sec ON sub.section_id = sec.id WHERE sub.semester_id = %s AND sub.faculty = %s', (sem_id, fac_name)).fetchall()
    
    grouped_subs = {}
    for sub in subjects:
        if sub['code'] not in grouped_subs:
            grouped_subs[sub['code']] = []
        grouped_subs[sub['code']].append(sub)
        
    sub_cards = ''
    for code, subs_list in grouped_subs.items():
        sub = subs_list[0]
        
        section_buttons = ''
        for s in subs_list:
            section_buttons += f'<a href="/subject/{s["id"]}" class="btn btn-primary" style="flex:1; text-align:center; padding: 0.6rem; font-size: 0.95rem; border-radius: 8px; box-shadow: 0 4px 6px rgba(67, 97, 238, 0.2); min-width: 100px;">Section {s["sec_name"]} →</a>'
            
        sub_cards += f'''
        <div class="card hover-card" style="border-left: 5px solid #3b82f6;">
            <div style="display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:1rem;">
                <div>
                    <h3 style="margin:0 0 0.5rem 0; color:#1e293b; font-weight:800; font-size:1.3rem;">{sub["title"]}</h3>
                    <code style="background:#f1f5f9; padding:4px 8px; border-radius:6px; color:#475569; font-weight:600;">{sub["code"]}</code>
                </div>
                <button onclick="editSubject('{sub["code"]}', '{sub["title"]}', {sem_id})" style="background:none; border:none; cursor:pointer; font-size:1.2rem; color:#64748b; padding:0; margin-left:0.5rem;" title="Edit Subject">✏️</button>
            </div>
            <div style="display:flex; gap:0.5rem; flex-wrap:wrap;">
                {section_buttons}
            </div>
        </div>
        '''
        
    edit_modal = f'''
    <div id="editSubjectModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.6); z-index:10000; justify-content:center; align-items:center; backdrop-filter: blur(4px);">
        <div style="background:white; border-radius:16px; width:90%; max-width:420px; box-shadow:0 25px 50px rgba(0,0,0,0.25); animation: zoomIn 0.2s ease-out; overflow:hidden;">
            <form action="/faculty/edit_subject" method="post">
                <div style="background:linear-gradient(135deg, #3b82f6, #1d4ed8); padding:1.5rem; color:white;">
                    <h3 style="margin:0; font-weight:700; font-size:1.2rem;">✏️ Edit Subject</h3>
                    <p style="margin:0.3rem 0 0; font-size:0.85rem; opacity:0.8;">Update subject code or title</p>
                </div>
                <div style="padding:1.5rem;">
                    <input type="hidden" name="old_code" id="edit_old_code">
                    <input type="hidden" name="sem_id" id="edit_sem_id">
                    <input type="hidden" name="program" value="ug">
                    <div style="margin-bottom:1.2rem;">
                        <label style="display:block; font-weight:600; color:#374151; margin-bottom:0.4rem; font-size:0.9rem;">Subject Code</label>
                        <input type="text" name="new_code" id="edit_new_code" required style="width:100%; padding:0.7rem 1rem; border:2px solid #e2e8f0; border-radius:10px; font-size:1rem; outline:none; transition:border-color 0.2s;" onfocus="this.style.borderColor='#3b82f6'" onblur="this.style.borderColor='#e2e8f0'">
                    </div>
                    <div style="margin-bottom:1rem;">
                        <label style="display:block; font-weight:600; color:#374151; margin-bottom:0.4rem; font-size:0.9rem;">Subject Title</label>
                        <input type="text" name="new_title" id="edit_new_title" required style="width:100%; padding:0.7rem 1rem; border:2px solid #e2e8f0; border-radius:10px; font-size:1rem; outline:none; transition:border-color 0.2s;" onfocus="this.style.borderColor='#3b82f6'" onblur="this.style.borderColor='#e2e8f0'">
                    </div>
                </div>
                <div style="padding:0 1.5rem 1.5rem; display:flex; gap:0.75rem; justify-content:flex-end;">
                    <button type="button" onclick="closeEditModal()" style="padding:0.6rem 1.2rem; border-radius:10px; border:2px solid #e2e8f0; background:white; color:#64748b; cursor:pointer; font-weight:600; font-size:0.9rem;">Cancel</button>
                    <button type="submit" style="padding:0.6rem 1.2rem; border-radius:10px; background:linear-gradient(135deg, #3b82f6, #1d4ed8); border:none; color:white; cursor:pointer; font-weight:600; font-size:0.9rem; box-shadow:0 4px 12px rgba(59,130,246,0.4);">Save Changes</button>
                </div>
            </form>
        </div>
    </div>
    <script>
    function editSubject(code, title, sem_id) {{
        document.getElementById('edit_old_code').value = code;
        document.getElementById('edit_new_code').value = code;
        document.getElementById('edit_new_title').value = title;
        document.getElementById('edit_sem_id').value = sem_id;
        document.getElementById('editSubjectModal').style.display = 'flex';
    }}
    function closeEditModal() {{
        document.getElementById('editSubjectModal').style.display = 'none';
    }}
    document.getElementById('editSubjectModal').addEventListener('click', function(e) {{
        if (e.target === this) closeEditModal();
    }});
    </script>
    '''
    
    content = f'''
    {edit_modal}
    <div class="container">
        <div class="mb-3" style="text-align:center;">
            <h1 class="page-title mt-2">Semester {sem["number"]} Subjects</h1>
            <p class="page-subtitle">Your assigned subjects</p>
        </div>
        <div class="grid grid-2" style="gap: 1.5rem;">{sub_cards}</div>
        <div style="text-align: center; margin-top: 2.5rem;">
            <a href="/faculty/ug" class="btn-back">⬅️ Back to Semesters</a>
        </div>
    </div>
    '''
    return render_template_string(base_html(f'Sem {sem["number"]} Subjects - CAB', content))
@app.route('/faculty/edit_subject', methods=['POST'])
def faculty_edit_subject():
    if session.get('role') != 'faculty': return redirect(url_for('index'))
    db = get_db()
    program = request.form.get('program')
    old_code = request.form.get('old_code', '').strip()
    new_code = request.form.get('new_code', '').strip()
    new_title = request.form.get('new_title', '').strip()
    sem_id = request.form.get('sem_id')
    fac_name = session.get('user')
    
    if program == 'ug' and old_code and new_code and new_title and sem_id:
        # Retrieve ALL sections' subjects that share this code
        subjects = db.execute('SELECT id, code, title FROM subjects WHERE code = %s AND faculty = %s AND semester_id = %s', (old_code, fac_name, sem_id)).fetchall()
        if subjects:
            old_data = json.dumps([dict(s) for s in subjects])
            
            db.execute('UPDATE subjects SET code = %s, title = %s WHERE code = %s AND faculty = %s AND semester_id = %s', (new_code, new_title, old_code, fac_name, sem_id))
            
            new_subjects = db.execute('SELECT id, code, title FROM subjects WHERE code = %s AND faculty = %s AND semester_id = %s', (new_code, fac_name, sem_id)).fetchall()
            new_data = json.dumps([dict(s) for s in new_subjects])
            
            cursor = db.execute('INSERT INTO audit_logs (faculty, action_type, entity_id, old_data, new_data) VALUES (%s, %s, %s, %s, %s) RETURNING id', (fac_name, 'EDIT_SUBJECT', sem_id, old_data, new_data))
            log_id = cursor.fetchone()['id']
            
            db.execute('INSERT INTO notifications (message, log_id) VALUES (%s, %s)', (f"Faculty <b>{fac_name}</b> changed subject <b>{old_code}</b> to <b>{new_code} - {new_title}</b>.", log_id))
            db.commit()
            
            flash('Subject updated successfully.')
        return redirect(f'/faculty/ug/sem/{sem_id}')
        
    return redirect(url_for('faculty_dashboard'))

@app.route('/hod/notifications')
def hod_notifications():
    if session.get('role') != 'hod': return redirect(url_for('index'))
    db = get_db()
    
    # Mark all as read
    db.execute('UPDATE notifications SET is_read = 1 WHERE is_read = 0')
    db.commit()
    
    notifications = db.execute('''
        SELECT n.id as notif_id, n.message, n.timestamp, n.log_id, 
               a.faculty, a.action_type, a.is_restored
        FROM notifications n
        JOIN audit_logs a ON n.log_id = a.id
        ORDER BY n.timestamp DESC LIMIT 100
    ''').fetchall()
    
    cards = ''
    if not notifications:
        cards = '<div style="text-align:center; padding:3rem; color:#94a3b8;"><span style="font-size:3rem;">🔔</span><h3 style="margin-top:1rem; color:#64748b;">No notifications yet</h3><p>Faculty activities will appear here.</p></div>'
    for n in notifications:
        btn = ''
        if not n['is_restored']:
            btn = f'<form action="/hod/restore/{n["log_id"]}" method="post" style="display:inline;"><button class="btn btn-warning btn-sm">↺ Restore</button></form>'
        else:
            btn = '<span class="badge badge-success">Restored</span>'
            
        cards += f'''
        <div class="card mb-3" style="border-left: 4px solid #3b82f6;" id="notif-{n['notif_id']}">
            <div style="display:flex; justify-content:space-between; align-items:center; gap:1rem;">
                <div style="flex:1;">
                    <h5 style="margin:0 0 0.5rem 0; font-size:0.95rem;">{n['message']}</h5>
                    <small class="text-muted">{n['timestamp']} • Action: {n['action_type']}</small>
                </div>
                <div style="display:flex; align-items:center; gap:0.5rem;">
                    {btn}
                    <form action="/hod/dismiss/{n['notif_id']}" method="post" style="display:inline;">
                        <button class="btn btn-sm" style="background:#fee2e2; color:#ef4444; border:none; cursor:pointer; padding:0.3rem 0.6rem; font-size:0.8rem; border-radius:6px;" title="Dismiss">✕</button>
                    </form>
                </div>
            </div>
        </div>
        '''
    
    clear_btn = ''
    if notifications:
        clear_btn = '<form action="/hod/clear_all_notifications" method="post" style="display:inline;"><button class="btn btn-sm" style="background:#fee2e2; color:#ef4444; border:1px solid #fecaca; padding:0.4rem 1rem; border-radius:8px; cursor:pointer; font-weight:600;">🗑️ Clear All</button></form>'
        
    content = f'''
    <div class="container mt-4">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:1.5rem;">
            <div>
                <h1>🔔 HOD Notifications & Audit Log</h1>
                <p class="text-muted">Review faculty activities and restore accidental changes.</p>
            </div>
            {clear_btn}
        </div>
        <div style="margin-top: 1rem;">{cards}</div>
        <div style="text-align: center; margin-top: 2.5rem;">
            <a href="/" class="btn-back">⬅️ Back to Dashboard</a>
        </div>
    </div>
    '''
    return render_template_string(base_html('Notifications - CAB', content))

@app.route('/hod/dismiss/<int:notif_id>', methods=['POST'])
def hod_dismiss_notification(notif_id):
    if session.get('role') != 'hod': return redirect(url_for('index'))
    db = get_db()
    db.execute('DELETE FROM notifications WHERE id = %s', (notif_id,))
    db.commit()
    return redirect(url_for('hod_notifications'))

@app.route('/hod/clear_all_notifications', methods=['POST'])
def hod_clear_all_notifications():
    if session.get('role') != 'hod': return redirect(url_for('index'))
    db = get_db()
    db.execute('DELETE FROM notifications')
    db.commit()
    flash('All notifications cleared!')
    return redirect(url_for('hod_notifications'))

@app.route('/hod/restore/<int:log_id>', methods=['POST'])
def hod_restore(log_id):
    if session.get('role') != 'hod': return redirect(url_for('index'))
    db = get_db()
    
    log = db.execute('SELECT * FROM audit_logs WHERE id = %s', (log_id,)).fetchone()
    if not log or log['is_restored']:
        flash('Log not found or already restored!')
        return redirect(url_for('hod_notifications'))
        
    try:
        old_data = json.loads(log['old_data'])
    except:
        old_data = []
        
    if log['action_type'] == 'EDIT_SUBJECT':
        for sub in old_data:
            db.execute('UPDATE subjects SET code=%s, title=%s WHERE id=%s', (sub['code'], sub['title'], sub['id']))
            
    elif log['action_type'] in ['SAVE_MARKS', 'DELETE_MARK', 'DELETE_ALL_MARKS']:
        # Clear out current marks for what was affected, then restore exact old matching those types
        if log['action_type'] == 'SAVE_MARKS' or log['action_type'] == 'DELETE_ALL_MARKS':
            # We must infer mark types from the old_data, or from a general step format.
            pass
        # Better restore: clear all marks that match the subject and student combination present in OLD OR NEW, then insert OLD
        # But `DELETE_ALL_MARKS` has old data containing all deleted marks. We just insert them back.
        
        try:
            new_data = json.loads(log['new_data'])
        except:
            new_data = []
            
        subject_id = log['entity_id']
            
        # Remove anything in new_data
        for r in new_data:
            db.execute('DELETE FROM marks WHERE student_id=%s AND subject_id=%s AND mark_type=%s', (r['student_id'], subject_id, r['mark_type']))
            
        # Remove anything in old_data to avoid duplicates, then insert them
        for r in old_data:
             db.execute('DELETE FROM marks WHERE student_id=%s AND subject_id=%s AND mark_type=%s', (r['student_id'], subject_id, r['mark_type']))
             db.execute('INSERT INTO marks (student_id, subject_id, mark_type, value) VALUES (%s, %s, %s, %s)', (r['student_id'], subject_id, r['mark_type'], r['value']))
             
    # Mark as restored
    db.execute('UPDATE audit_logs SET is_restored=1 WHERE id=%s', (log_id,))
    
    # Notify faculty
    fac_message = f"HOD reversed your action ({log['action_type']}) on {log['timestamp']}."
    db.execute('INSERT INTO notifications (message, log_id) VALUES (%s, %s)', (fac_message, log_id))
    
    db.commit()
    flash('Successfully restored previous state.')
    return redirect(url_for('hod_notifications'))
    

@app.route('/faculty/pg')
def faculty_pg():
    if session.get('role') != 'faculty': return redirect(url_for('index'))
    db = get_db()
    fac_name = session.get('user')
    batches = db.execute('''
        SELECT DISTINCT b.id, b.program, b.start_year, b.end_year, m.year as sem_number
        FROM pg_batches b
        JOIN pg_modules m ON m.batch_id = b.id
        WHERE m.faculty = ? ORDER BY b.start_year DESC, m.year
    ''', (fac_name,)).fetchall()
    sem_cards = ''
    for b in batches:
        mod_count = db.execute('SELECT count(*) FROM pg_modules WHERE batch_id = %s AND year = %s AND faculty = %s', (b['id'], b['sem_number'], fac_name)).fetchone()[0]
        sem_cards += f'''
        <a href="/faculty/pg/batch/{b["id"]}/sem/{b["sem_number"]}" class="card hover-card" style="text-decoration:none; border-top: 5px solid #8b5cf6;">
            <h3 style="color: #0f172a; font-weight: 800; font-size: 1.4rem; margin:0 0 0.5rem 0;">Semester {b["sem_number"]}</h3>
            <p style="color: #64748b; font-size:0.95rem; margin-bottom:1rem;">{b["program"]} ({b["start_year"]}-{b["end_year"]})</p>
            <div style="display:inline-block; background: #ede9fe; color: #6d28d9; padding: 0.3rem 0.8rem; border-radius: 50px; font-weight: 600; font-size: 0.8rem;">{mod_count} Assigned Modules</div>
        </a>'''
    content = f'''
    <div class="container">
        <div class="mb-3" style="text-align:center;">
            <h1 class="page-title mt-2">🔬 PG Semesters</h1>
            <p class="page-subtitle">Semesters you are assigned to</p>
        </div>
        <div class="grid grid-3" style="gap: 1.5rem;">{sem_cards}</div>
        <div style="text-align: center; margin-top: 2.5rem;">
            <a href="/faculty_dashboard" class="btn-back">⬅️ Back to Dashboard</a>
        </div>
    </div>
    '''
    return render_template_string(base_html('PG Semesters - CAB', content))

@app.route('/faculty/pg/batch/<int:batch_id>/sem/<int:sem_num>')
def faculty_pg_sem(batch_id, sem_num):
    if session.get('role') != 'faculty': return redirect(url_for('index'))
    db = get_db()
    fac_name = session.get('user')
    modules = db.execute('SELECT * FROM pg_modules WHERE batch_id = %s AND year = %s AND faculty = %s', (batch_id, sem_num, fac_name)).fetchall()
    mod_cards = ''
    for m in modules:
        mod_cards += f'''
        <div class="card hover-card" style="border-left: 5px solid #8b5cf6;">
            <div style="display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:1.5rem;">
                <div>
                    <h3 style="margin:0 0 0.5rem 0; color:#1e293b; font-weight:800; font-size:1.3rem;">{m["title"]}</h3>
                    <code style="background:#f1f5f9; padding:4px 8px; border-radius:6px; color:#475569; font-weight:600;">{m["code"]}</code>
                </div>
            </div>
            <div style="display:flex; gap:0.5rem;">
                <a href="/pg/module/{m["id"]}" class="btn btn-primary" style="flex:1; text-align:center; padding: 0.8rem; font-size: 1.1rem; border-radius: 12px; background: #8b5cf6; border-color: #8b5cf6; box-shadow: 0 4px 6px rgba(139, 92, 246, 0.2);">Enter Marks →</a>
            </div>
        </div>
        '''
    content = f'''
    <div class="container">
        <div class="mb-3" style="text-align:center;">
            <h1 class="page-title mt-2">Semester {sem_num} Modules</h1>
            <p class="page-subtitle">Your assigned PG modules</p>
        </div>
        <div class="grid grid-2" style="gap: 1.5rem;">{mod_cards}</div>
        <div style="text-align: center; margin-top: 2.5rem;">
            <a href="/faculty/pg" class="btn-back">⬅️ Back to Semesters</a>
        </div>
    </div>
    '''
    return render_template_string(base_html(f'PG Sem {sem_num} Modules - CAB', content))


@app.route('/faculty/profile', methods=['GET', 'POST'])
def faculty_profile():
    if session.get('role') != 'faculty': return redirect(url_for('index'))
    db = get_db()
    faculty_id = session.get('faculty_id')
    
    if request.method == 'POST':
        new_username = request.form.get('username', '').strip()
        new_password = request.form.get('password', '').strip()
        if new_username and new_password:
            db.execute('UPDATE faculty SET username = %s, password = %s WHERE id = %s', (new_username, new_password, faculty_id))
            db.commit()
            flash('Profile updated successfully! Next time you login, use your new credentials.')
        return redirect(url_for('faculty_profile'))
        
    faculty = db.execute('SELECT * FROM faculty WHERE id = %s', (faculty_id,)).fetchone()
    
    # Auto-create session if current login doesn't have one (for pre-existing logins)
    current_token = session.get('session_token', '')
    if current_token:
        existing = db.execute('SELECT id FROM sessions WHERE session_token = %s', (current_token,)).fetchone()
        if not existing:
            user_agent = request.headers.get('User-Agent', 'Unknown')
            ip_address = request.remote_addr or 'Unknown'
            db.execute('INSERT INTO sessions (faculty_id, session_token, user_agent, ip_address) VALUES (%s, %s, %s, %s)',
                       (faculty_id, current_token, user_agent, ip_address))
            db.commit()
    else:
        import uuid
        current_token = str(uuid.uuid4())
        session['session_token'] = current_token
        user_agent = request.headers.get('User-Agent', 'Unknown')
        ip_address = request.remote_addr or 'Unknown'
        db.execute('INSERT INTO sessions (faculty_id, session_token, user_agent, ip_address) VALUES (%s, %s, %s, %s)',
                   (faculty_id, current_token, user_agent, ip_address))
        db.execute('UPDATE faculty SET last_active = CURRENT_TIMESTAMP WHERE id = %s', (faculty_id,))
        db.commit()
    
    # Get active sessions
    active_sessions = db.execute('SELECT * FROM sessions WHERE faculty_id = %s AND is_active = 1 ORDER BY login_time DESC', (faculty_id,)).fetchall()
    
    session_cards = ''
    current_token = session.get('session_token', '')
    for s in active_sessions:
        ua = s['user_agent'] or 'Unknown Device'
        # Parse user agent for display
        device_icon = '💻'
        device_name = 'Desktop'
        if 'Mobile' in ua or 'Android' in ua or 'iPhone' in ua:
            device_icon = '📱'
            device_name = 'Mobile'
        elif 'Tablet' in ua or 'iPad' in ua:
            device_icon = '📱'
            device_name = 'Tablet'
        
        browser = 'Unknown Browser'
        if 'Chrome' in ua and 'Edg' not in ua:
            browser = 'Chrome'
        elif 'Firefox' in ua:
            browser = 'Firefox'
        elif 'Safari' in ua and 'Chrome' not in ua:
            browser = 'Safari'
        elif 'Edg' in ua:
            browser = 'Edge'
        
        is_current = s['session_token'] == current_token
        current_badge = '<span style="background:#d1fae5; color:#059669; padding:0.2rem 0.5rem; border-radius:4px; font-size:0.7rem; font-weight:600; margin-left:0.5rem;">THIS DEVICE</span>' if is_current else ''
        
        logout_btn = ''
        if not is_current:
            logout_btn = f'<form action="/faculty/session/logout/{s["id"]}" method="post" style="display:inline;"><button class="btn btn-sm" style="background:#fee2e2; color:#ef4444; border:none; cursor:pointer; padding:0.3rem 0.8rem; font-size:0.8rem; border-radius:6px;">Logout</button></form>'
        
        session_cards += f'''
        <div style="display:flex; justify-content:space-between; align-items:center; padding:0.8rem; background:#f8fafc; border-radius:10px; margin-bottom:0.5rem; border:1px solid #e2e8f0;">
            <div style="display:flex; align-items:center; gap:0.8rem;">
                <span style="font-size:1.5rem;">{device_icon}</span>
                <div>
                    <div style="font-weight:600; font-size:0.9rem; color:#1e293b;">{device_name} • {browser}{current_badge}</div>
                    <div style="font-size:0.75rem; color:#94a3b8;">IP: {s['ip_address'] or 'Unknown'} • {s['login_time']}</div>
                </div>
            </div>
            {logout_btn}
        </div>
        '''
    
    if not active_sessions:
        session_cards = '<p class="text-muted" style="text-align:center; padding:1rem;">No active sessions found.</p>'
    
    content = f'''
    <div class="container mt-4 animate__animated animate__zoomIn">
        <h1 class="page-title">👤 My Profile</h1>
        <p class="page-subtitle mb-4">Manage your account credentials</p>
        
        <div class="card hover-card" style="max-width: 500px; margin: 0 auto; padding: 2.5rem; border-top: 5px solid var(--primary);">
            {{% with messages = get_flashed_messages() %}}{{% if messages %}}{{% for m in messages %}}<div class="alert alert-success">{{{{ m }}}}</div>{{% endfor %}}{{% endif %}}{{% endwith %}}
            <form method="post">
                <div class="mb-3">
                    <label class="form-label">Full Name</label>
                    <input type="text" class="form-control" value="{faculty['name']}" disabled style="background:#f1f5f9; cursor:not-allowed; opacity:0.8;">
                </div>
                <div class="mb-3">
                    <label class="form-label">Department</label>
                    <input type="text" class="form-control" value="{faculty['department']}" disabled style="background:#f1f5f9; cursor:not-allowed; opacity:0.8;">
                </div>
                <div class="mb-3">
                    <label class="form-label" style="display:flex; justify-content:space-between;"><span>Username</span> <span style="font-size:0.8rem; font-weight:normal; color:#64748b;">(Case Insensitive)</span></label>
                    <input type="text" name="username" class="form-control" value="{faculty['username']}" required style="font-weight: 600;">
                </div>
                <div class="mb-3">
                    <label class="form-label">Password</label>
                    <input type="text" name="password" class="form-control" value="{faculty['password']}" required style="font-weight: 600;">
                </div>
                <button type="submit" class="btn btn-primary mt-4" style="width: 100%; font-size: 1.1rem; padding: 1rem; border-radius: 12px; box-shadow: 0 4px 15px rgba(67, 97, 238, 0.3);">Save Changes</button>
            </form>
        </div>
        
        <div class="card hover-card" style="max-width: 500px; margin: 2rem auto; padding: 2rem; border-top: 5px solid #f59e0b;">
            <h3 style="margin:0 0 1rem 0; display:flex; align-items:center; gap:0.5rem;"><span>🔒</span> Security & Sessions</h3>
            <p class="text-muted mb-3" style="font-size:0.85rem;">Devices currently logged into your account</p>
            {session_cards}
        </div>
        
        <div style="text-align: center; margin-top: 2.5rem;">
            <a href="/faculty_dashboard" class="btn-back">⬅️ Back to Dashboard</a>
        </div>
    </div>
    '''
    return render_template_string(base_html('My Profile - CAB', content))

@app.route('/faculty/session/logout/<int:session_id>', methods=['POST'])
def faculty_session_logout(session_id):
    if session.get('role') != 'faculty': return redirect(url_for('index'))
    db = get_db()
    db.execute('UPDATE sessions SET is_active = 0 WHERE id = %s AND faculty_id = %s', (session_id, session.get('faculty_id')))
    db.commit()
    flash('Session logged out successfully!')
    return redirect(url_for('faculty_profile'))

# --- ROUTES ---

@app.route('/')
def index():
    if session.get('role') == 'faculty':
        return redirect(url_for('faculty_dashboard'))
    init_db()
    db = get_db()
    
    # Get UG stats
    total_ug_students = db.execute('SELECT COUNT(DISTINCT usn) FROM students').fetchone()[0]
    total_ug_subjects = db.execute('SELECT COUNT(*) FROM subjects').fetchone()[0]
    
    # Get PG stats
    total_pg_students = db.execute('SELECT COUNT(*) FROM pg_students').fetchone()[0]
    total_pg_batches = db.execute('SELECT COUNT(*) FROM pg_batches').fetchone()[0]
    
    content = f'''
    <div class="container">
        <div class="mb-3 text-center">
            <h1 class="page-title">🎓 CAB Smart System</h1>
            <p class="page-subtitle">Select a program to manage courses, students, and assessments</p>
        </div>
        <div class="grid grid-2" style="max-width: 800px; margin: 0 auto;">
            <a href="/ug" class="card card-clickable sem-tile" style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white;">
                <h2 style="font-size: 4rem; margin-bottom: 0.5rem;">🎓</h2>
                <h3 style="color: white; margin: 0;">UG</h3>
                <p style="color: rgba(255,255,255,0.9); margin: 0.5rem 0;">Undergraduate</p>
                <p style="color: rgba(255,255,255,0.8); font-size: 0.9rem;">4 Years • 8 Semesters</p>
                <div class="mt-2" style="font-size: 0.85rem;">
                    <span class="badge" style="background: rgba(255,255,255,0.2); color: white;">{total_ug_students} Students</span>
                    <span class="badge" style="background: rgba(255,255,255,0.2); color: white;">{total_ug_subjects} Subjects</span>
                </div>
            </a>
            <a href="/pg" class="card card-clickable sem-tile" style="background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); color: white;">
                <h2 style="font-size: 4rem; margin-bottom: 0.5rem;">📊</h2>
                <h3 style="color: white; margin: 0;">PG</h3>
                <p style="color: rgba(255,255,255,0.9); margin: 0.5rem 0;">Postgraduate</p>
                <p style="color: rgba(255,255,255,0.8); font-size: 0.9rem;">2 Years • M.Tech</p>
                <div class="mt-2" style="font-size: 0.85rem;">
                    <span class="badge" style="background: rgba(255,255,255,0.2); color: white;">{total_pg_batches} Batches</span>
                    <span class="badge" style="background: rgba(255,255,255,0.2); color: white;">{total_pg_students} Students</span>
                </div>
            </a>
        </div>
    </div>'''
    return base_html('CAB Smart System', content)

# --- UG ROUTES ---

@app.route('/ug')
def ug_home():
    """UG Department selection"""
    db = get_db()
    total_students = db.execute('SELECT COUNT(DISTINCT usn) FROM students').fetchone()[0]
    total_subjects = db.execute('SELECT COUNT(*) FROM subjects').fetchone()[0]
    
    content = f'''
    <div class="container">
        <div class="mb-3">
            <h1 class="page-title mt-2">🎓 Undergraduate Programs</h1>
            <p class="page-subtitle">Select a department</p>
        </div>
        <div class="grid grid-3" style="max-width: 900px;">
            <a href="/ug/ise" class="card card-clickable sem-tile" style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white;">
                <h2 style="font-size: 3rem; margin-bottom: 0.5rem;">💻</h2>
                <h3 style="color: white; margin: 0;">ISE</h3>
                <p style="color: rgba(255,255,255,0.9); margin: 0.5rem 0; font-size: 0.9rem;">Information Science and Engineering</p>
                <p style="color: rgba(255,255,255,0.8); font-size: 0.85rem;">8 Semesters</p>
                <div class="mt-2" style="font-size: 0.85rem;">
                    <span class="badge" style="background: rgba(255,255,255,0.2); color: white;">{total_students} Students</span>
                    <span class="badge" style="background: rgba(255,255,255,0.2); color: white;">{total_subjects} Subjects</span>
                </div>
            </a>
        </div>
        <div style="text-align: center; margin-top: 3rem; margin-bottom: 2rem;">
            <a href="/" class="btn" style="background: linear-gradient(135deg, #4361ee, #3a0ca3); color: white; border-radius: 50px; padding: 0.8rem 2.5rem; font-size: 1.1rem; box-shadow: 0 4px 15px rgba(67, 97, 238, 0.4); text-decoration: none; display: inline-block; transition: transform 0.2s;">
                🏠 Back to Home
            </a>
        </div>
    </div>'''
    return base_html('UG Programs - CAB', content)

@app.route('/ug/ise')
def ug_ise_schemes():
    """UG ISE - Schemes view (Scheme 23, 24, 25, etc.)"""
    db = get_db()
    schemes = db.execute('SELECT * FROM schemes WHERE department = %s ORDER BY name', ('ISE',)).fetchall()
    
    # Get stats for each scheme
    scheme_stats = []
    for scheme in schemes:
        scheme_id = scheme['id']
        sems = db.execute('SELECT id FROM semesters WHERE scheme_id = %s', (scheme_id,)).fetchall()
        sem_ids = [s['id'] for s in sems]
        
        total_students = 0
        total_subjects = 0
        total_sections = 0
        for sem_id in sem_ids:
            total_subjects += db.execute('SELECT COUNT(*) FROM subjects WHERE semester_id = %s', (sem_id,)).fetchone()[0]
        
        # Count unique students and sections across all semesters in this scheme
        if sem_ids:
            placeholders = ','.join(['?' for _ in sem_ids])
            total_students = db.execute(f'SELECT COUNT(DISTINCT st.usn) FROM students st JOIN sections sec ON st.section_id = sec.id WHERE sec.semester_id IN ({placeholders})', sem_ids).fetchone()[0]
            total_sections = db.execute(f'SELECT COUNT(DISTINCT sec.name) FROM sections sec WHERE sec.semester_id IN ({placeholders})', sem_ids).fetchone()[0]
        
        scheme_stats.append({
            'scheme': scheme,
            'sections': total_sections,
            'students': total_students,
            'subjects': total_subjects
        })
    
    tiles = ''
    colors = [
        'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
        'linear-gradient(135deg, #11998e 0%, #38ef7d 100%)',
        'linear-gradient(135deg, #f093fb 0%, #f5576c 100%)',
        'linear-gradient(135deg, #4facfe 0%, #00f2fe 100%)',
        'linear-gradient(135deg, #fa709a 0%, #fee140 100%)'
    ]
    for i, s in enumerate(scheme_stats):
        scheme_id = s['scheme']['id']
        scheme_name = s['scheme']['name']
        color = colors[i % len(colors)]
        tiles += f'''<a href="/ug/ise/scheme/{scheme_id}" class="card card-clickable sem-tile" style="background: {color}; color: white; position: relative;">
            <div style="position: absolute; top: 10px; right: 10px; display: flex; gap: 0.5rem; z-index: 10;">
                <button onclick="event.preventDefault(); event.stopPropagation(); openEditModal({scheme_id}, '{scheme_name}')" class="btn btn-sm" style="background: rgba(255,255,255,0.3); color: white; padding: 0.3rem 0.6rem; font-size: 0.75rem;">✏️ Edit</button>
                <button onclick="event.preventDefault(); event.stopPropagation(); openDeleteSchemeModal({scheme_id}, '{scheme_name}')" class="btn btn-sm" style="background: rgba(239, 68, 68, 0.8); color: white; padding: 0.3rem 0.6rem; font-size: 0.75rem; border: none; cursor: pointer;">🗑️ Delete</button>
            </div>
            <h2 style="font-size: 2.5rem; margin-bottom: 0.5rem;">📋</h2>
            <h3 style="color: white; margin: 0;">{scheme_name}</h3>
            <p style="color: rgba(255,255,255,0.9); margin: 0.5rem 0; font-size: 0.9rem;">8 Semesters</p>
            <div class="mt-2" style="font-size: 0.85rem;">
                <span class="badge" style="background: rgba(255,255,255,0.2); color: white;">{s['sections']} Sections</span>
                <span class="badge" style="background: rgba(255,255,255,0.2); color: white;">{s['students']} Students</span>
                <span class="badge" style="background: rgba(255,255,255,0.2); color: white;">{s['subjects']} Subjects</span>
            </div>
        </a>'''
    
    content = f'''
    <div class="container">
        <div class="mb-3">
            <h1 class="page-title mt-2">💻 ISE - Information Science and Engineering</h1>
            <p class="page-subtitle">Select a scheme to manage semesters, sections, students, and subjects</p>
        </div>
        {{% with messages = get_flashed_messages() %}}{{% if messages %}}{{% for m in messages %}}<div class="alert alert-success">{{{{ m }}}}</div>{{% endfor %}}{{% endif %}}{{% endwith %}}
        <div class="grid grid-3">{tiles}</div>
        
        <div class="mt-3">
            <button onclick="openAddModal()" class="add-btn">+ Add New Scheme</button>
        </div>
        
        <div style="text-align: center; margin-top: 3rem; margin-bottom: 2rem;">
            <a href="/ug" class="btn-back">⬅️ Back to UG</a>
        </div>
    </div>
    
    <!-- Add Scheme Modal -->
    <div id="addModal" class="modal">
        <div class="card" style="max-width: 400px; margin: auto;">
            <h3>Add New Scheme</h3>
            <form action="/ug/ise/scheme/add" method="post">
                <div class="mb-2">
                    <label class="form-label">Scheme Name</label>
                    <input type="text" name="name" class="form-control" placeholder="e.g. Scheme 26" required>
                </div>
                <div style="display: flex; gap: 0.5rem;">
                    <button type="submit" class="btn btn-primary">Add Scheme</button>
                    <button type="button" onclick="closeAddModal()" class="btn btn-outline">Cancel</button>
                </div>
            </form>
        </div>
    </div>
    
    <!-- Edit Scheme Modal -->
    <div id="editModal" class="modal">
        <div class="card" style="max-width: 400px; margin: auto;">
            <h3>Edit Scheme Name</h3>
            <form id="editForm" action="" method="post">
                <div class="mb-2">
                    <label class="form-label">Scheme Name</label>
                    <input type="text" id="editName" name="name" class="form-control" required>
                </div>
                <div style="display: flex; gap: 0.5rem;">
                    <button type="submit" class="btn btn-primary">Save Changes</button>
                    <button type="button" onclick="closeEditModal()" class="btn btn-outline">Cancel</button>
                </div>
            </form>
        </div>
    </div>
    
    <!-- Delete Scheme Modal -->
    <div id="deleteSchemeModal" class="modal">
        <div class="card" style="max-width: 400px; margin: auto; text-align: center;">
            <h3 style="color: #dc2626;">🗑️ Delete Scheme</h3>
            <p id="deleteSchemeText" style="margin: 1rem 0;">Are you sure you want to delete this scheme? This action cannot be undone and will delete all associated data.</p>
            <form id="deleteSchemeForm" action="" method="post">
                <div style="display: flex; gap: 0.5rem; justify-content: center;">
                    <button type="submit" class="btn btn-danger">Yes, Delete</button>
                    <button type="button" onclick="closeDeleteSchemeModal()" class="btn btn-outline">Cancel</button>
                </div>
            </form>
        </div>
    </div>
    
    <script>
    function openAddModal() {{
        document.getElementById('addModal').style.display = 'flex';
    }}
    function closeAddModal() {{
        document.getElementById('addModal').style.display = 'none';
    }}
    function openEditModal(id, name) {{
        document.getElementById('editForm').action = '/ug/ise/scheme/' + id + '/edit';
        document.getElementById('editName').value = name;
        document.getElementById('editModal').style.display = 'flex';
    }}
    function closeEditModal() {{
        document.getElementById('editModal').style.display = 'none';
    }}
    function openDeleteSchemeModal(id, name) {{
        document.getElementById('deleteSchemeForm').action = '/ug/ise/scheme/' + id + '/delete';
        document.getElementById('deleteSchemeText').innerText = 'Are you sure you want to delete ' + name + '? This will delete all associated semesters, sections, subjects, students, and marks!';
        document.getElementById('deleteSchemeModal').style.display = 'flex';
    }}
    function closeDeleteSchemeModal() {{
        document.getElementById('deleteSchemeModal').style.display = 'none';
    }}
    // Close modal when clicking outside
    window.onclick = function(event) {{
        if (event.target.classList.contains('modal')) {{
            event.target.style.display = 'none';
        }}
    }}
    </script>'''
    return render_template_string(base_html('ISE Schemes - CAB', content))

@app.route('/ug/ise/scheme/add', methods=['POST'])
def add_scheme():
    """Add a new scheme"""
    db = get_db()
    name = request.form['name'].strip()
    try:
        db.execute('INSERT INTO schemes (name, department) VALUES (%s, %s)', (name, 'ISE'))
        db.commit()
        
        # Get the new scheme id
        scheme = db.execute('SELECT id FROM schemes WHERE name = %s', (name,)).fetchone()
        if scheme:
            # Create 8 semesters for the new scheme
            for i in range(1, 9):
                db.execute('INSERT INTO semesters (number, scheme_id) VALUES (%s, %s)', (i, scheme['id']))
            db.commit()
        
        flash(f'Scheme "{name}" added with 8 semesters!')
    except Exception as e:
        flash(f'Error: Scheme name already exists or invalid.')
    return redirect(url_for('ug_ise_schemes'))

@app.route('/ug/ise/scheme/<int:scheme_id>/edit', methods=['POST'])
def edit_scheme(scheme_id):
    """Edit scheme name"""
    db = get_db()
    name = request.form['name'].strip()
    try:
        db.execute('UPDATE schemes SET name = %s WHERE id = %s', (name, scheme_id))
        db.commit()
        flash(f'Scheme renamed to "{name}"!')
    except Exception as e:
        flash(f'Error: Could not rename scheme.')
    return redirect(url_for('ug_ise_schemes'))

@app.route('/ug/ise/scheme/<int:scheme_id>/delete', methods=['POST'])
def delete_scheme(scheme_id):
    """Delete a scheme and all its data"""
    db = get_db()
    try:
        scheme = db.execute('SELECT name FROM schemes WHERE id = %s', (scheme_id,)).fetchone()
        if not scheme:
            flash('Scheme not found.')
            return redirect(url_for('ug_ise_schemes'))
            
        # Optional cascade deletes (if foreign keys don't have CASCADE)
        sems = db.execute('SELECT id FROM semesters WHERE scheme_id = %s', (scheme_id,)).fetchall()
        for s in sems:
            sem_id = s['id']
            secs = db.execute('SELECT id FROM sections WHERE semester_id = %s', (sem_id,)).fetchall()
            for sec in secs:
                db.execute('DELETE FROM marks WHERE student_id IN (SELECT id FROM students WHERE section_id = %s)', (sec['id'],))
                db.execute('DELETE FROM students WHERE section_id = %s', (sec['id'],))
            db.execute('DELETE FROM sections WHERE semester_id = %s', (sem_id,))
            db.execute('DELETE FROM course_outcomes WHERE subject_id IN (SELECT id FROM subjects WHERE semester_id = %s)', (sem_id,))
            db.execute('DELETE FROM subjects WHERE semester_id = %s', (sem_id,))
            
        db.execute('DELETE FROM semesters WHERE scheme_id = %s', (scheme_id,))
        db.execute('DELETE FROM schemes WHERE id = %s', (scheme_id,))
        db.commit()
        flash(f'Scheme "{scheme["name"]}" deleted successfully!')
    except Exception as e:
        flash(f'Error deleting scheme: {str(e)}')
    return redirect(url_for('ug_ise_schemes'))

@app.route('/ug/ise/scheme/<int:scheme_id>')
def ug_ise_semesters(scheme_id):
    """UG ISE - 8 Semesters view for a specific scheme"""
    db = get_db()
    
    scheme = db.execute('SELECT * FROM schemes WHERE id = %s', (scheme_id,)).fetchone()
    if not scheme:
        return "Scheme not found", 404
    
    semesters = db.execute('SELECT * FROM semesters WHERE scheme_id = %s ORDER BY number', (scheme_id,)).fetchall()
    
    # Get stats and course subjects for each semester
    sem_stats = []
    for sem in semesters:
        sections = db.execute('SELECT COUNT(DISTINCT name) FROM sections WHERE semester_id = %s', (sem['id'],)).fetchone()[0]
        students = db.execute('SELECT COUNT(DISTINCT st.usn) FROM students st JOIN sections sec ON st.section_id = sec.id WHERE sec.semester_id = %s', (sem['id'],)).fetchone()[0]
        subjects = db.execute('SELECT COUNT(*) FROM subjects WHERE semester_id = %s', (sem['id'],)).fetchone()[0]
        course_docs = db.execute('SELECT code, title FROM course_subjects WHERE scheme_id = %s AND semester_number = %s', (scheme_id, sem['number'])).fetchall()
        sem_stats.append({'sem': sem, 'sections': sections, 'students': students, 'subjects': subjects, 'course_docs': course_docs})
    
    tiles = ''
    for s in sem_stats:
        tiles += f'''<a href="/semester/{s['sem']['id']}?scheme_id={scheme_id}" class="card card-clickable sem-tile" style="display: flex; flex-direction: column; justify-content: flex-start;">
            <h2 style="margin-bottom: 0;">{s['sem']['number']}</h2>
            <p style="margin-top: 0;">Semester {s['sem']['number']}</p>
            <div class="mt-2" style="font-size: 0.85rem;">
                <span class="badge badge-primary">{s['sections']} Sections</span>
                <span class="badge badge-success">{s['students']} Students</span>
                <span class="badge badge-primary">{s['subjects']} Active Subjs</span>
            </div>
        </a>'''
    
    scheme_name = scheme['name']
    content = f'''
    <div class="container">
        <div class="mb-3" style="display: flex; justify-content: space-between; align-items: flex-end;">
            <div>
                <h1 class="page-title mt-2">📋 {scheme_name}</h1>
                <p class="page-subtitle">Select a semester to manage sections, students, and subjects</p>
            </div>
            <div>
                <button onclick="document.getElementById('courseDocUpload').style.display='block'" class="btn btn-success" style="display: flex; align-items: center; gap: 0.5rem;">
                    <span style="font-size: 1.2rem;">📄</span> Upload Course Document
                </button>
            </div>
        </div>
        
        {{% with messages = get_flashed_messages() %}}{{% if messages %}}{{% for m in messages %}}<div class="alert alert-success">{{{{ m }}}}</div>{{% endfor %}}{{% endif %}}{{% endwith %}}
        
        <!-- Course Document Upload Area (Hidden by default) -->
        <div id="courseDocUpload" class="card mb-4 animate__animated animate__fadeIn" style="display: none; border-left: 4px solid #10b981;">
            <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 1rem;">
                <div>
                    <h3 style="margin: 0; display: flex; align-items: center; gap: 0.5rem;">
                        <span style="font-size: 1.5rem;">🤖</span> AI Course Document Auto-Extract
                    </h3>
                    <p class="text-muted" style="margin: 0.25rem 0 0 0;">Upload the official scheme/syllabus PDF. AI will automatically extract subjects for all 8 semesters.</p>
                </div>
                <button onclick="document.getElementById('courseDocUpload').style.display='none'" class="btn btn-outline btn-sm">Close</button>
            </div>
            
            <form id="courseDocForm" action="/ug/ise/scheme/{scheme_id}/process_course_doc" method="post" enctype="multipart/form-data" onsubmit="showExtractionProgress(event)">
                <div style="display: flex; gap: 1rem; align-items: flex-end;">
                    <div style="flex: 1;">
                        <label class="form-label" style="font-weight: bold;">Course Document (PDF/Docx):</label>
                        <input type="file" name="file" id="courseFile" class="form-control" accept=".pdf,.doc,.docx" required>
                    </div>
                    <button type="submit" id="startExtractionBtn" class="btn btn-success" style="height: fit-content; padding: 0.75rem 1.5rem;">
                        ⚡ Start AI Extraction
                    </button>
                </div>
            </form>
            
            <!-- Extraction Progress Overlay -->
            <div id="extractionOverlay" style="display: none; position: absolute; inset: 0; background: rgba(255,255,255,0.95); z-index: 10; border-radius: 8px; flex-direction: column; justify-content: center; align-items: center; text-align: center; border: 2px solid var(--primary);">
                <div style="width: 50px; height: 50px; border: 4px solid #e2e8f0; border-top-color: var(--primary); border-radius: 50%; animation: spin 1s linear infinite; margin-bottom: 1rem;"></div>
                <h3 style="margin: 0; color: var(--primary);" id="extractionStatusText">Initializing AI Connection...</h3>
                <p style="color: #64748b; margin-top: 0.5rem; font-size: 0.9rem;" id="extractionSubText">This may take 10-20 seconds.</p>
                <div style="width: 60%; height: 6px; background: #e2e8f0; border-radius: 3px; margin-top: 1rem; overflow: hidden;">
                    <div style="height: 100%; background: var(--primary); width: 0%; animation: loadProgress 15s ease-out forwards;"></div>
                </div>
            </div>
            
            <style>
                @keyframes spin {{ 100% {{ transform: rotate(360deg); }} }}
                @keyframes loadProgress {{ 
                    0% {{ width: 0%; }}
                    20% {{ width: 30%; }}
                    50% {{ width: 60%; }}
                    80% {{ width: 85%; }}
                    100% {{ width: 95%; }}
                }}
            </style>
            
            <script>
                function showExtractionProgress(e) {{
                    const fileInput = document.getElementById('courseFile');
                    if (!fileInput.files.length) return;
                    
                    // Show overlay
                    document.getElementById('courseDocUpload').style.position = 'relative';
                    document.getElementById('extractionOverlay').style.display = 'flex';
                    
                    const statusTexts = [
                        "Uploading Course Document...",
                        "Reading syllabus contents...",
                        "Identifying semester blocks...",
                        "Extracting subject codes & titles...",
                        "Filtering out non-academic entries...",
                        "Finalizing JSON structure...",
                        "Almost done..."
                    ];
                    
                    let step = 0;
                    const statusEl = document.getElementById('extractionStatusText');
                    
                    const interval = setInterval(() => {{
                        if (step < statusTexts.length) {{
                            statusEl.innerText = statusTexts[step];
                            step++;
                        }} else {{
                            clearInterval(interval);
                        }}
                    }}, 2500); // Change text every 2.5 seconds
                }}
            </script>
        </div>
        
        <div class="grid grid-4">{tiles}</div>
        <div style="text-align: center; margin-top: 3rem; margin-bottom: 2rem;">
            <a href="/ug/ise" class="btn-back">⬅️ Back to Schemes</a>
        </div>
    </div>'''
    return render_template_string(base_html(f'{scheme_name} - CAB', content))

@app.route('/ug/ise/scheme/<int:scheme_id>/process_course_doc', methods=['POST'])
def process_course_doc(scheme_id):
    """Process uploaded course document using Google Gemini"""
    db = get_db()
    
    if 'file' not in request.files or not request.files['file'].filename:
        flash('Please upload a course document file!')
        return redirect(url_for('ug_ise_semesters', scheme_id=scheme_id))
        
    file = request.files['file']
    filename = secure_filename(file.filename)
    file_ext = os.path.splitext(filename)[1].lower()
    
    prompt = '''Extract all academic subjects for Semesters 1 through 8 from this syllabus/scheme document.
For each subject, extract the precise completely unabbreviated Subject Code and Subject Title.

Return ONLY a valid JSON object with keys for each semester number (1 to 8), and an array of subject objects.

Example Output format:
{
    "1": [{"code": "23MAT101", "title": "Mathematics I"}, {"code": "23PHY102", "title": "Physics"}],
    "2": [{"code": "23MAT201", "title": "Mathematics II"}],
    "3": [{"code": "23CS301", "title": "Data Structures"}],
    "4": [], "5": [], "6": [], "7": [], "8": []
}

IMPORTANT:
- ONLY include actual academic courses. Do NOT include labs if they don't have distinct theory components, unless they are separate courses with valid subject codes.
- Do NOT include generic items like "Internship", "Project Work", "Seminar" unless they have explicit distinct subject codes.
- Return ONLY the raw JSON object. Do not use markdown backticks (```json).'''

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = os.path.join(temp_dir, filename)
            file.save(temp_path)
            
            if file_ext == '.pdf':
                mime = 'application/pdf'
            elif file_ext in ['.docx', '.doc']:
                # Read text using python-docx if it's a docx
                if file_ext == '.docx':
                    text = ""
                    doc = docx.Document(temp_path)
                    for para in doc.paragraphs:
                        text += para.text + "\n"
                    txt = get_gemini_response(prompt + "\n\nDOCUMENT CONTENT:\n" + text)
                else:
                    flash('Only .pdf and .docx formats are supported for AI extraction currently.')
                    return redirect(url_for('ug_ise_semesters', scheme_id=scheme_id))
            else:
                flash('Unsupported file format.')
                return redirect(url_for('ug_ise_semesters', scheme_id=scheme_id))
                
            if file_ext == '.pdf':
                txt = get_gemini_response(prompt, file_path=temp_path, file_mime=mime)
                
            # Clean up JSON
            txt = txt.replace('```json', '').replace('```', '').strip()
            
            # Parse JSON
            import json
            extracted_data = json.loads(txt)
            
            subject_count = 0
            
            # Delete any existing course_subjects for this scheme
            db.execute('DELETE FROM course_subjects WHERE scheme_id = %s', (scheme_id,))
            
            # Insert new subjects
            for sem_num_str, subjects in extracted_data.items():
                sem_num = int(sem_num_str)
                for subj in subjects:
                    if 'code' in subj and 'title' in subj:
                        db.execute('INSERT INTO course_subjects (scheme_id, semester_number, code, title) VALUES (%s, %s, %s, %s)',
                                  (scheme_id, sem_num, subj['code'].strip().upper(), subj['title'].strip()))
                        subject_count += 1
                        
            db.commit()
            flash(f'✅ Success: AI extracted and stored {subject_count} subjects across 8 semesters from the Course Document!')
            
    except Exception as e:
        flash(f'AI Extraction Failed: {str(e)}')
        
    return redirect(url_for('ug_ise_semesters', scheme_id=scheme_id))

@app.route('/semester/<int:sem_id>')
def semester_view(sem_id):
    db = get_db()
    tab = request.args.get('tab', 'sections')
    scheme_id = request.args.get('scheme_id')
    
    semester = db.execute('SELECT * FROM semesters WHERE id = %s', (sem_id,)).fetchone()
    if not semester:
        return "Semester not found", 404
    
    # Get scheme_id from semester if not provided
    if not scheme_id and semester['scheme_id']:
        scheme_id = semester['scheme_id']
    
    sections = db.execute('SELECT * FROM sections WHERE semester_id = %s ORDER BY name', (sem_id,)).fetchall()
    subjects = db.execute('SELECT sub.*, sec.name as sec_name FROM subjects sub JOIN sections sec ON sub.section_id = sec.id WHERE sub.semester_id = %s ORDER BY sub.code', (sem_id,)).fetchall()
    
    # Get course subjects for this semester & scheme
    course_subjects = []
    if scheme_id:
        course_subjects = db.execute('SELECT * FROM course_subjects WHERE scheme_id = %s AND semester_number = %s ORDER BY code', (scheme_id, semester['number'])).fetchall()
        
    # Get global faculty
    faculty_list = db.execute('SELECT * FROM faculty ORDER BY name').fetchall()
    
    # Build sections with students
    sections_html = ''
    for sec in sections:
        students = db.execute('SELECT * FROM students WHERE section_id = %s ORDER BY usn', (sec['id'],)).fetchall()
        students_rows = ''
        for i, st in enumerate(students):
            st_id, st_usn, st_name = st['id'], st['usn'], st['name']
            students_rows += f'<tr><td>{i+1}</td><td><strong>{st_usn}</strong></td><td>{st_name}</td><td><a href="javascript:void(0)" class="btn btn-outline btn-sm" onclick="customConfirm(\'Delete student {st_usn}?\', \'/student/delete/{st_id}?sem_id={sem_id}&scheme_id={scheme_id}\')">×</a></td></tr>'
        if not students:
            students_rows = '<tr><td colspan="4" class="text-center text-muted">No students yet</td></tr>'
        
        sec_id, sec_name = sec['id'], sec['name']
        sections_html += f'''
        <div class="section-card">
            <div class="section-header">
                <h3>Section {sec_name}</h3>
                <div style="display: flex; gap: 0.5rem;">
                    <a href="/section/{sec_id}/promote?sem_id={sem_id}&scheme_id={scheme_id}" class="btn btn-success btn-sm">🚀 Promote</a>
                    <a href="javascript:void(0)" class="btn btn-outline btn-sm" onclick="customConfirm('Delete section?', '/section/delete/{sec_id}?sem_id={sem_id}&scheme_id={scheme_id}')">Delete Section</a>
                </div>
            </div>
            <table><tr><th>#</th><th>USN</th><th>Name</th><th></th></tr>{students_rows}</table>
            <form action="/student/add" method="post" class="mt-2" style="display: flex; gap: 0.5rem;">
                <input type="hidden" name="section_id" value="{sec_id}">
                <input type="hidden" name="sem_id" value="{sem_id}">
                <input type="hidden" name="scheme_id" value="{scheme_id}">
                <input type="text" name="usn" class="form-control" placeholder="USN" required style="flex: 1;">
                <input type="text" name="name" class="form-control" placeholder="Name" required style="flex: 2;">
                <button class="btn btn-success btn-sm">+ Add</button>
            </form>
            <form action="/students/import" method="post" enctype="multipart/form-data" class="mt-2">
                <input type="hidden" name="section_id" value="{sec_id}">
                <input type="hidden" name="sem_id" value="{sem_id}">
                <input type="hidden" name="scheme_id" value="{scheme_id}">
                <div style="display: flex; gap: 1rem; margin-bottom: 0.5rem; font-size: 0.85rem;">
                    <label style="cursor: pointer; display: flex; align-items: center; gap: 4px;"><input type="radio" name="import_method" value="ai" checked> ⚡ AI Import</label>
                    <label style="cursor: pointer; display: flex; align-items: center; gap: 4px;"><input type="radio" name="import_method" value="manual"> 📄 Manual Import</label>
                </div>
                <div style="display: flex; gap: 0.5rem;">
                    <input type="file" name="file" class="form-control" style="flex: 1;">
                    <button class="btn btn-primary btn-sm">Upload</button>
                </div>
            </form>
        </div>'''
    
    # Subjects list
    subjects_rows = ''
    for sub in subjects:
        sub_id, sub_code, sub_title = sub['id'], sub['code'], sub['title']
        sub_sec, sub_fac = sub['sec_name'], sub['faculty'] or '-'
        subjects_rows += f'<tr><td><strong>{sub_code}</strong></td><td>{sub_title}</td><td>{sub_sec}</td><td>{sub_fac}</td><td><a href="/subject/{sub_id}" class="btn btn-primary btn-sm" style="padding: 0.3rem 0.6rem; font-size: 0.75rem;">Marks</a> <a href="/subject/{sub_id}/config" class="btn btn-outline btn-sm" style="padding: 0.3rem 0.6rem; font-size: 0.75rem;">Config</a> <button onclick="openEditSubjectModal({sub_id}, \'{sub_code}\', \'{sub_title}\')" class="btn btn-sm" style="background: rgba(245, 158, 11, 0.9); color: white; padding: 0.3rem 0.6rem; font-size: 0.75rem; border: none; cursor: pointer;">✏️ Edit</button> <a href="javascript:void(0)" class="btn btn-outline btn-sm" style="padding: 0.3rem 0.6rem; font-size: 0.75rem; border-color: #ef4444; color: #ef4444;" onclick="customConfirm(\'Delete subject?\', \'/subject/delete/{sub_id}?sem_id={sem_id}&scheme_id={scheme_id}\')">🗑️</a></td></tr>'
    if not subjects:
        subjects_rows = '<tr><td colspan="5" class="text-center text-muted">No subjects yet</td></tr>'
    
    # Section options for subject form
    sec_options = ''.join([f'<option value="{s["id"]}">Section {s["name"]}</option>' for s in sections])
    
    # Build tab content
    if tab == 'sections':
        tab_content = f'''
        <div class="card">
            <h3>Sections & Students</h3>
            {sections_html}
            <form action="/section/add" method="post" class="mt-3">
                <input type="hidden" name="sem_id" value="{sem_id}">
                <input type="hidden" name="scheme_id" value="{scheme_id}">
                <div style="display: flex; gap: 0.5rem;">
                    <input type="text" name="name" class="form-control" placeholder="Section name (A, B, C)" required style="max-width: 200px;">
                    <button class="btn btn-primary">+ Add Section</button>
                </div>
            </form>
        </div>'''
    else:
        add_form = ''
        if sections:
            cs_options = '<option value="">-- Select Subject from Course Doc --</option>'
            for cs in course_subjects:
                cs_options += f'<option value="{cs["code"]}|{cs["title"]}">{cs["code"]} - {cs["title"]}</option>'
                
            fac_options = '<option value="">-- Select Faculty --</option>'
            for f in faculty_list:
                fac_options += f'<option value="{f["name"]}">{f["name"]} ({f["department"] or "ISE"})</option>'
                
            if not course_subjects:
                cs_options = '<option value="">No subjects found for this Sem. Please upload Course Document first!</option>'
                
            add_form = f'''
            <form action="/subject/add" method="post" class="mt-3">
                <input type="hidden" name="sem_id" value="{sem_id}">
                <input type="hidden" name="scheme_id" value="{scheme_id}">
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; align-items: end;">
                    <div><label class="form-label">Sections</label><div class="form-control" style="height: auto; min-height: 42px; display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center;">{''.join([f'<label style="display: flex; align-items: center; gap: 0.25rem; font-weight: normal; margin: 0; cursor: pointer;"><input type="checkbox" name="section_ids" value="{s["id"]}"> {s["name"]}</label>' for s in sections])}</div></div>
                    <div><label class="form-label">Subject</label><select name="course_subject" class="form-control" required {'' if course_subjects else 'disabled'}>{cs_options}</select></div>
                    <div><label class="form-label">Faculty</label><select name="faculty" class="form-control" required>{fac_options}</select></div>
                </div>
                <button class="btn btn-primary mt-2" {'' if course_subjects else 'disabled'}>+ Map Subject & Faculty</button>
            </form>'''
        else:
            add_form = '<p class="text-muted mt-2">Add sections first.</p>'
            
        # UI for course subjects
        cs_display_html = ''
        if course_subjects:
            if sections:
                cs_items = ''
                for cs in course_subjects:
                    cs_items += f'''
                    <form action="/subject/add" method="post" style="background: #f8fafc; border: 1px solid #e2e8f0; padding: 0.75rem; border-radius: 8px; font-size: 0.85rem; display: flex; flex-direction: column; gap: 0.5rem; transition: all 0.2s;" onmouseover="this.style.borderColor='var(--primary)'" onmouseout="this.style.borderColor='#e2e8f0'">
                        <input type="hidden" name="sem_id" value="{sem_id}">
                        <input type="hidden" name="scheme_id" value="{scheme_id}">
                        <input type="hidden" name="course_subject" value="{cs["code"]}|{cs["title"]}">
                        
                        <div style="display: flex; gap: 0.5rem; justify-content: space-between; align-items: flex-start;">
                            <strong style="color: var(--primary);">{cs["code"]}</strong>
                            <span style="font-weight: 500; text-align: right; line-height: 1.2;">{cs["title"]}</span>
                        </div>
                        
                        <div style="margin-top: 0.5rem; display: flex; flex-wrap: wrap; gap: 0.5rem; padding: 0.35rem 0.5rem; background: #fff; border: 1px solid #e2e8f0; border-radius: 6px;">
                            <span style="font-size: 0.8rem; color: #64748b; font-weight: 600;">Sections: </span>
                            {''.join([f'<label style="font-size: 0.85rem; display: flex; align-items: center; gap: 0.2rem; cursor: pointer; margin-bottom: 0; font-weight: 500;"><input type="checkbox" name="section_ids" value="{s["id"]}"> {s["name"]}</label>' for s in sections])}
                        </div>
                        <div style="display: flex; gap: 0.25rem; margin-top: 0.25rem;">
                            <select name="faculty" class="form-control" style="flex: 1; padding: 4px; font-size: 0.8rem;" required>
                                <option value="">Select Faculty</option>
                                {''.join([f'<option value="{f["name"]}">{f["name"]}</option>' for f in faculty_list])}
                            </select>
                            <button type="submit" class="btn btn-primary" style="padding: 4px 12px; font-size: 0.8rem;">+ Map</button>
                        </div>
                    </form>
                    '''
            else:
                cs_items = ''.join([f'<div style="background: #f8fafc; border: 1px solid #e2e8f0; padding: 0.5rem 1rem; border-radius: 6px; font-size: 0.9rem; display: flex; gap: 1rem;"><strong style="color: var(--primary); width: 100px;">{cs["code"]}</strong><span>{cs["title"]}</span></div>' for cs in course_subjects])
                
            cs_display_html = f'''
            <div style="margin-bottom: 2rem;">
                <h4 style="margin: 0 0 0.5rem 0; color: #475569; display: flex; align-items: center; gap: 0.5rem;">
                    <span>📚</span> Click '+ Map' to assign these subjects to a section
                </h4>
                <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 0.75rem;">
                    {cs_items}
                </div>
            </div>
            '''
        else:
            cs_display_html = '''
            <div style="margin-bottom: 2rem; padding: 1rem; background: #fffbeb; border: 1px solid #fde68a; border-radius: 8px; color: #d97706;">
                <strong>⚠️ No Course Document Uploaded!</strong> Go back to the Scheme View and click "Upload Course Document" to automatically populate the subjects for mapping.
            </div>
            '''
        
        tab_content = f'''
        <div class="card mb-4">
            {cs_display_html}
            <h3 style="margin: 0 0 1rem 0; border-top: 1px solid #e5e7eb; padding-top: 1.5rem;">📝 Active Mapped Subjects</h3>
            <div class="marks-scroll" style="overflow-x: auto; -webkit-overflow-scrolling: touch; border-radius: 8px;">
                <table style="min-width: 500px;"><tr><th>Code</th><th>Title</th><th>Section</th><th>Faculty</th><th>Action</th></tr>{subjects_rows}</table>
            </div>
        </div>'''
    
    sem_num = semester['number']
    tab_active_sections = 'active' if tab == 'sections' else ''
    tab_active_subjects = 'active' if tab == 'subjects' else ''
    
    # Build back link based on scheme_id
    back_link = f'/ug/ise/scheme/{scheme_id}' if scheme_id else '/ug/ise'
    
    content = f'''
    <div class="container">
        <div class="mb-3">
            <h1 class="page-title mt-2">Semester {sem_num}</h1>
            <p class="page-subtitle">Manage sections, students, and subjects</p>
        </div>
        <div class="tabs">
            <a href="/semester/{sem_id}?tab=sections&scheme_id={scheme_id}" class="tab {tab_active_sections}">📋 Sections & Students</a>
            <a href="/semester/{sem_id}?tab=subjects&scheme_id={scheme_id}" class="tab {tab_active_subjects}">📚 Subjects</a>
        </div>
        {{% with messages = get_flashed_messages() %}}{{% if messages %}}{{% for m in messages %}}
            {{% if m.startswith('ERROR|') %}}
                <script>document.addEventListener('DOMContentLoaded', () => customAlert('{{{{ m.split('|',1)[1] }}}}'));</script>
            {{% else %}}
                <div class="alert alert-success">{{{{ m }}}}</div>
            {{% endif %}}
        {{% endfor %}}{{% endif %}}{{% endwith %}}
        {tab_content}
        <div style="text-align: center; margin-top: 3rem; margin-bottom: 2rem;">
            <a href="{back_link}" class="btn-back">⬅️ Back to Program</a>
        </div>
        
        <!-- Edit Subject Modal -->
        <div id="editSubjectModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.5); z-index:200; justify-content:center; align-items:center;">
            <div style="background:white; padding:2rem; border-radius:12px; width:90%; max-width:500px; box-shadow:0 10px 25px rgba(0,0,0,0.2); animation: zoomIn 0.2s ease-out;">
                <h3 style="margin:0 0 1.5rem 0; color:#1f2937; border-bottom: 2px solid #e5e7eb; padding-bottom: 0.5rem;">✏️ Edit Subject Details</h3>
                <form id="editSubjectForm" method="post">
                    <input type="hidden" name="sem_id" value="{sem_id}">
                    <input type="hidden" name="scheme_id" value="{scheme_id}">
                    <div class="mb-3" style="text-align:left;">
                        <label class="form-label">Subject Code</label>
                        <input type="text" id="editSubCode" name="code" class="form-control" required>
                    </div>
                    <div class="mb-3" style="text-align:left;">
                        <label class="form-label">Subject Title</label>
                        <input type="text" id="editSubTitle" name="title" class="form-control" required>
                    </div>
                    <div style="display:flex; gap:1rem; margin-top: 1.5rem;">
                        <button type="button" onclick="document.getElementById('editSubjectModal').style.display='none'" class="btn btn-outline" style="flex:1;">Cancel</button>
                        <button type="submit" class="btn btn-primary" style="flex:1;">Save Changes</button>
                    </div>
                </form>
            </div>
        </div>
        <script>
        function openEditSubjectModal(sub_id, code, title) {{
            // Use javascript string replace because Python string formatting makes brace escaping hard
            document.getElementById('editSubjectForm').action = '/subject/edit/' + sub_id;
            document.getElementById('editSubCode').value = code;
            document.getElementById('editSubTitle').value = title;
            document.getElementById('editSubjectModal').style.display = 'flex';
        }}
        </script>
    </div>'''
    return render_template_string(base_html(f'Semester {sem_num} - CAB', content))
    
@app.route('/subject/edit/<int:id>', methods=['POST'])
def edit_subject(id):
    db = get_db()
    sem_id = request.form['sem_id']
    scheme_id = request.form.get('scheme_id', '')
    code = request.form['code'].strip().upper()
    title = request.form['title'].strip()
    
    db.execute('UPDATE subjects SET code = %s, title = %s WHERE id = %s', (code, title, id))
    db.commit()
    flash(f'Subject updated to {code} - {title}!')
    return redirect(url_for('semester_view', sem_id=sem_id, tab='subjects', scheme_id=scheme_id))

@app.route('/section/add', methods=['POST'])
def add_section():
    db = get_db()
    sem_id = request.form['sem_id']
    scheme_id = request.form.get('scheme_id')
    name = request.form['name'].strip().upper()
    db.execute('INSERT INTO sections (semester_id, name) VALUES (%s, %s)', (sem_id, name))
    db.commit()
    flash(f'Section {name} added!')
    return redirect(url_for('semester_view', sem_id=sem_id, tab='sections', scheme_id=scheme_id))

@app.route('/section/delete/<int:id>')
def delete_section(id):
    sem_id = request.args.get('sem_id', 1)
    scheme_id = request.args.get('scheme_id')
    db = get_db()
    # Delete students in this section
    db.execute('DELETE FROM students WHERE section_id = %s', (id,))
    # Delete subjects in this section
    db.execute('DELETE FROM subjects WHERE section_id = %s', (id,))
    db.execute('DELETE FROM sections WHERE id = %s', (id,))
    db.commit()
    flash('Section deleted!')
    return redirect(url_for('semester_view', sem_id=sem_id, tab='sections', scheme_id=scheme_id))

@app.route('/student/add', methods=['POST'])
def add_student():
    db = get_db()
    section_id = request.form['section_id']
    sem_id = request.form['sem_id']
    scheme_id = request.form.get('scheme_id')
    usn = request.form['usn'].strip().upper()
    name = request.form['name'].strip()
    db.execute('INSERT INTO students (section_id, usn, name) VALUES (%s, %s, %s)', (section_id, usn, name))
    db.commit()
    return redirect(url_for('semester_view', sem_id=sem_id, tab='sections', scheme_id=scheme_id))

@app.route('/student/delete/<int:id>')
def delete_student(id):
    sem_id = request.args.get('sem_id', 1)
    scheme_id = request.args.get('scheme_id')
    db = get_db()
    db.execute('DELETE FROM marks WHERE student_id = %s', (id,))
    db.execute('DELETE FROM students WHERE id = %s', (id,))
    db.commit()
    return redirect(url_for('semester_view', sem_id=sem_id, tab='sections', scheme_id=scheme_id))

@app.route('/section/<int:sec_id>/promote')
def promote_section(sec_id):
    """Show promote students preview page"""
    db = get_db()
    sem_id = request.args.get('sem_id')
    scheme_id = request.args.get('scheme_id')
    
    section = db.execute('SELECT * FROM sections WHERE id = %s', (sec_id,)).fetchone()
    if not section:
        flash('Section not found')
        return redirect(url_for('semester_view', sem_id=sem_id, tab='sections', scheme_id=scheme_id))
    
    semester = db.execute('SELECT * FROM semesters WHERE id = %s', (section['semester_id'],)).fetchone()
    current_sem_num = semester['number']
    next_sem_num = current_sem_num + 1
    
    if next_sem_num > 8:
        flash('Cannot promote beyond Semester 8!')
        return redirect(url_for('semester_view', sem_id=sem_id, tab='sections', scheme_id=scheme_id))
    
    # Get students from current section
    students = db.execute('SELECT * FROM students WHERE section_id = %s ORDER BY usn', (sec_id,)).fetchall()
    
    if not students:
        flash('No students to promote in this section!')
        return redirect(url_for('semester_view', sem_id=sem_id, tab='sections', scheme_id=scheme_id))
    
    # Find target semester in the same scheme
    target_sem = db.execute('SELECT id FROM semesters WHERE scheme_id = %s AND number = %s', (scheme_id, next_sem_num)).fetchone()
    
    # Get existing sections in the target semester
    target_sections = []
    if target_sem:
        target_sections = db.execute('SELECT * FROM sections WHERE semester_id = %s ORDER BY name', (target_sem['id'],)).fetchall()
    
    sec_name = section['name']
    
    # Build student rows with checkboxes
    student_rows = ''
    for i, st in enumerate(students):
        student_rows += f'''<tr>
            <td><input type="checkbox" name="student_ids" value="{st['id']}" checked style="transform: scale(1.3);"></td>
            <td>{i+1}</td>
            <td><strong>{st['usn']}</strong></td>
            <td>{st['name']}</td>
        </tr>'''
    
    # Build target section options
    target_sec_options = f'<option value="__same__">Section {sec_name} (same name)</option>'
    for ts in target_sections:
        selected = 'selected' if ts['name'] == sec_name else ''
        target_sec_options += f'<option value="{ts["id"]}" {selected}>Section {ts["name"]} (existing)</option>'
    target_sec_options += '<option value="__new__">+ Create New Section...</option>'
    
    content = f'''
    <div class="container">
        <div class="mb-3">
            <h1 class="page-title mt-2">🚀 Promote Students</h1>
            <p class="page-subtitle">Semester {current_sem_num} → Semester {next_sem_num} &bull; Section {sec_name}</p>
        </div>
        
        {{% with messages = get_flashed_messages() %}}{{% if messages %}}{{% for m in messages %}}<div class="alert alert-success">{{{{ m }}}}</div>{{% endfor %}}{{% endif %}}{{% endwith %}}
        
        <div class="card" style="border-left: 4px solid #10b981;">
            <div style="display: flex; align-items: center; gap: 1rem; margin-bottom: 1.5rem;">
                <div style="background: linear-gradient(135deg, #10b981, #059669); color: white; padding: 1rem 1.5rem; border-radius: 12px; text-align: center;">
                    <div style="font-size: 2rem;">📋</div>
                    <div style="font-weight: bold; font-size: 1.1rem;">Sem {current_sem_num} → {next_sem_num}</div>
                </div>
                <div>
                    <h3 style="margin:0;">Promote {len(students)} Students</h3>
                    <p class="text-muted" style="margin: 0.25rem 0 0 0;">Select the students to promote and choose the target section below.</p>
                </div>
            </div>
            
            <form action="/section/{sec_id}/promote" method="post">
                <input type="hidden" name="sem_id" value="{sem_id}">
                <input type="hidden" name="scheme_id" value="{scheme_id}">
                <input type="hidden" name="current_sem_num" value="{current_sem_num}">
                <input type="hidden" name="next_sem_num" value="{next_sem_num}">
                <input type="hidden" name="sec_name" value="{sec_name}">
                
                <div style="margin-bottom: 1.5rem;">
                    <label class="form-label" style="font-weight: bold;">Target Section in Semester {next_sem_num}:</label>
                    <div style="display: flex; gap: 0.5rem; align-items: center;">
                        <select name="target_section" id="targetSection" class="form-control" style="max-width: 350px;" onchange="document.getElementById('newSecRow').style.display = this.value === '__new__' ? 'flex' : 'none'">
                            {target_sec_options}
                        </select>
                        <div id="newSecRow" style="display: none; gap: 0.5rem; align-items: center;">
                            <input type="text" name="new_section_name" class="form-control" placeholder="New section name (e.g. A)" style="max-width: 200px;">
                        </div>
                    </div>
                </div>
                
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
                    <h4 style="margin: 0;">👥 Students to Promote</h4>
                    <div style="display: flex; gap: 0.5rem;">
                        <button type="button" onclick="document.querySelectorAll('input[name=student_ids]').forEach(c => c.checked = true)" class="btn btn-outline btn-sm">☑️ Select All</button>
                        <button type="button" onclick="document.querySelectorAll('input[name=student_ids]').forEach(c => c.checked = false)" class="btn btn-outline btn-sm">⬜ Deselect All</button>
                    </div>
                </div>
                
                <div style="overflow-x: auto; margin-bottom: 1.5rem;">
                    <table>
                        <tr><th style="width: 40px;"></th><th>#</th><th>USN</th><th>Name</th></tr>
                        {student_rows}
                    </table>
                </div>
                
                <div style="border-top: 2px solid #e2e8f0; padding-top: 1.5rem;">
                    <h4 style="margin-bottom: 1rem;">➕ Add New Students (Optional)</h4>
                    <p class="text-muted" style="font-size: 0.9rem; margin-bottom: 1rem;">Add students who are newly joining this section (e.g. lateral entries). They will be added to the target section along with promoted students.</p>
                    <div id="newStudentsContainer"></div>
                    <button type="button" onclick="addNewStudentRow()" class="btn btn-outline btn-sm" style="margin-top: 0.5rem;">+ Add Student Row</button>
                </div>
                
                <div style="text-align: center; margin-top: 2rem; display: flex; justify-content: center; gap: 1rem;">
                    <button type="submit" class="btn btn-success" style="padding: 0.8rem 2.5rem; font-size: 1.1rem;">🚀 Promote Students to Semester {next_sem_num}</button>
                    <a href="/semester/{sem_id}?tab=sections&scheme_id={scheme_id}" class="btn btn-outline" style="padding: 0.8rem 2.5rem; font-size: 1.1rem;">Cancel</a>
                </div>
            </form>
        </div>
        
        <div style="text-align: center; margin-top: 3rem; margin-bottom: 2rem;">
            <a href="/semester/{sem_id}?tab=sections&scheme_id={scheme_id}" class="btn-back">⬅️ Back to Semester {current_sem_num}</a>
        </div>
    </div>
    
    <script>
    let newRowCount = 0;
    function addNewStudentRow() {{
        newRowCount++;
        const container = document.getElementById('newStudentsContainer');
        const row = document.createElement('div');
        row.style.cssText = 'display: flex; gap: 0.5rem; margin-bottom: 0.5rem; align-items: center;';
        row.innerHTML = `
            <input type="text" name="new_usn_${{newRowCount}}" class="form-control" placeholder="USN" style="flex: 1;">
            <input type="text" name="new_name_${{newRowCount}}" class="form-control" placeholder="Student Name" style="flex: 2;">
            <button type="button" onclick="this.parentElement.remove()" class="btn btn-outline btn-sm" style="color: #ef4444; border-color: #ef4444;">\u00d7</button>
        `;
        container.appendChild(row);
        // Update hidden counter
        let counter = document.getElementById('newStudentCount');
        if (!counter) {{
            counter = document.createElement('input');
            counter.type = 'hidden';
            counter.id = 'newStudentCount';
            counter.name = 'new_student_count';
            container.parentElement.appendChild(counter);
        }}
        counter.value = newRowCount;
    }}
    </script>'''
    return render_template_string(base_html(f'Promote Students - CAB', content))

@app.route('/section/<int:sec_id>/promote', methods=['POST'])
def promote_section_submit(sec_id):
    """Execute student promotion"""
    db = get_db()
    sem_id = request.form.get('sem_id')
    scheme_id = request.form.get('scheme_id')
    next_sem_num = int(request.form.get('next_sem_num', 0))
    sec_name = request.form.get('sec_name', 'A')
    target_section_choice = request.form.get('target_section', '__same__')
    selected_student_ids = request.form.getlist('student_ids')
    new_section_name = request.form.get('new_section_name', '').strip().upper()
    new_student_count = int(request.form.get('new_student_count', 0))
    
    if not selected_student_ids:
        flash('No students selected for promotion!')
        return redirect(url_for('promote_section', sec_id=sec_id, sem_id=sem_id, scheme_id=scheme_id))
    
    try:
        # Find or create target semester
        target_sem = db.execute('SELECT id FROM semesters WHERE scheme_id = %s AND number = %s', (scheme_id, next_sem_num)).fetchone()
        if not target_sem:
            flash(f'Semester {next_sem_num} not found in this scheme!')
            return redirect(url_for('semester_view', sem_id=sem_id, tab='sections', scheme_id=scheme_id))
        target_sem_id = target_sem['id']
        
        # Determine target section
        if target_section_choice == '__new__':
            if not new_section_name:
                new_section_name = sec_name
            existing = db.execute('SELECT id FROM sections WHERE semester_id = %s AND name = %s', (target_sem_id, new_section_name)).fetchone()
            if existing:
                target_sec_id = existing['id']
            else:
                db.execute('INSERT INTO sections (semester_id, name) VALUES (%s, %s)', (target_sem_id, new_section_name))
                db.commit()
                target_sec_id = db.execute('SELECT id FROM sections WHERE semester_id = %s AND name = %s', (target_sem_id, new_section_name)).fetchone()['id']
        elif target_section_choice == '__same__':
            existing = db.execute('SELECT id FROM sections WHERE semester_id = %s AND name = %s', (target_sem_id, sec_name)).fetchone()
            if existing:
                target_sec_id = existing['id']
            else:
                db.execute('INSERT INTO sections (semester_id, name) VALUES (%s, %s)', (target_sem_id, sec_name))
                db.commit()
                target_sec_id = db.execute('SELECT id FROM sections WHERE semester_id = %s AND name = %s', (target_sem_id, sec_name)).fetchone()['id']
        else:
            target_sec_id = int(target_section_choice)
        
        # Get existing USNs in target section to avoid duplicates
        existing_usns = {row['usn'] for row in db.execute('SELECT usn FROM students WHERE section_id = %s', (target_sec_id,)).fetchall()}
        
        # Promote selected students (COPY to target semester)
        promoted = 0
        skipped = 0
        for sid in selected_student_ids:
            student = db.execute('SELECT usn, name FROM students WHERE id = %s', (sid,)).fetchone()
            if student:
                if student['usn'] in existing_usns:
                    skipped += 1
                    continue
                db.execute('INSERT INTO students (section_id, usn, name) VALUES (%s, %s, %s)', (target_sec_id, student['usn'], student['name']))
                existing_usns.add(student['usn'])
                promoted += 1
        
        # Add new students
        new_added = 0
        for i in range(1, new_student_count + 1):
            new_usn = request.form.get(f'new_usn_{i}', '').strip().upper()
            new_name = request.form.get(f'new_name_{i}', '').strip()
            if new_usn and new_name and new_usn not in existing_usns:
                db.execute('INSERT INTO students (section_id, usn, name) VALUES (%s, %s, %s)', (target_sec_id, new_usn, new_name))
                existing_usns.add(new_usn)
                new_added += 1
        
        db.commit()
        
        msg = f'\u2705 Promoted {promoted} students to Semester {next_sem_num}!'
        if skipped > 0:
            msg += f' (Skipped {skipped} duplicates)'
        if new_added > 0:
            msg += f' Added {new_added} new students.'
        flash(msg)
        
        # Redirect to the target semester
        return redirect(url_for('semester_view', sem_id=target_sem_id, tab='sections', scheme_id=scheme_id))
        
    except Exception as e:
        flash(f'Promotion failed: {str(e)}')
        return redirect(url_for('semester_view', sem_id=sem_id, tab='sections', scheme_id=scheme_id))

def extract_students_manual(file_path, file_ext):
    """
    Deterministic extraction of student data from Excel, PDF, or Word.
    Returns a list of dicts: [{'usn': '...', 'name': '...'}, ...]
    """
    students = []
    
    try:
        if file_ext in ['.xlsx', '.xls']:
            df = pd.read_excel(file_path)
            # Normalize Headers: lower case and strip
            df.columns = [str(c).lower().strip() for c in df.columns]
            
            # Find USN and Name columns
            usn_col = next((c for c in df.columns if 'usn' in c), None)
            name_col = next((c for c in df.columns if 'name' in c), None)
            
            if usn_col and name_col:
                for _, row in df.iterrows():
                    usn = str(row[usn_col]).strip()
                    name = str(row[name_col]).strip()
                    if usn and name and usn.lower() != 'nan':
                        students.append({'usn': usn, 'name': name})
                        
        elif file_ext == '.pdf':
            with pdfplumber.open(file_path) as pdf:
                for page in pdf.pages:
                    # Method 1: Table extraction
                    tables = page.extract_tables()
                    for table in tables:
                        # Find headers in first row (or subsequent if first is header)
                        if not table: continue
                        
                        # Simple header detection: look for row containing 'usn' and 'name'
                        header_idx = -1
                        usn_idx = -1
                        name_idx = -1
                        
                        for i, row in enumerate(table):
                            row_lower = [str(c).lower().strip() if c else '' for c in row]
                            if 'usn' in row_lower and 'name' in row_lower:
                                header_idx = i
                                usn_idx = row_lower.index('usn')
                                name_idx = row_lower.index('name')
                                break
                        
                        if header_idx != -1:
                            # Iterate rows after header
                            for row in table[header_idx+1:]:
                                if len(row) > max(usn_idx, name_idx) and row[usn_idx] and row[name_idx]:
                                    usn = row[usn_idx].strip()
                                    name = row[name_idx].strip()
                                    if usn:
                                        students.append({'usn': usn, 'name': name})
                    
                    # Method 2: Regex Fallback if no tables found or empty
                    if not students:
                        text = page.extract_text()
                        if text:
                            # Regex to find lines like "1. U24E01IS057 RAKESH C R"
                            # Pattern: USN followed by Name. 
                            # USN Pattern: U followed by digits/chars. Length approx 10.
                            # We look for the specific pattern seen in user image: U24E01IS057
                            # Regex: \b(U\d{2}[A-Z0-9]{5,})\s+([A-Z\s\.]+)
                            matches = re.findall(r'\b(U\d{2}[A-Z0-9]{5,})\s+([A-Z\s\.]+)', text)
                            for m in matches:
                                students.append({'usn': m[0].strip(), 'name': m[1].strip()})

        elif file_ext in ['.docx', '.doc']:
            doc = docx.Document(file_path)
            # Iterate tables
            for table in doc.tables:
                header_idx = -1
                usn_idx = -1
                name_idx = -1
                
                rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
                
                for i, row in enumerate(rows):
                    row_lower = [c.lower() for c in row]
                    if 'usn' in row_lower and 'name' in row_lower:
                        header_idx = i
                        usn_idx = row_lower.index('usn')
                        name_idx = row_lower.index('name')
                        break
                
                if header_idx != -1:
                    for row in rows[header_idx+1:]:
                        if len(row) > max(usn_idx, name_idx) and row[usn_idx] and row[name_idx]:
                            students.append({'usn': row[usn_idx], 'name': row[name_idx]})
                            
    except Exception as e:
        print(f"Manual extraction error: {e}")
        
    return students

def repair_doubled_text(text):
    """
    Fixes PDF extraction artifact where bold text appears as double chars.
    Example: UU2244EE0011IISS001199 -> U24E01IS019
    """
    if not text: return text
    text = text.strip()
    # Check if string is formed by repeating characters
    # Heuristic: if length > 4 and every char at i is same as i+1 (for even i)
    # Actually, often it's "HHEELLLLOO"
    if len(text) > 4 and len(text) % 2 == 0:
        is_doubled = True
        for i in range(0, len(text), 2):
            if text[i] != text[i+1]:
                is_doubled = False
                break
        if is_doubled:
            return text[::2]
    return text

@app.route('/students/import', methods=['POST'])
def import_students():
    section_id = request.form.get('section_id')
    sem_id = request.form.get('sem_id', 1)
    scheme_id = request.form.get('scheme_id')
    import_method = request.form.get('import_method', 'ai') # 'ai' or 'manual'
    
    if 'file' not in request.files or not section_id:
        flash('Please provide file and section')
        return redirect(url_for('semester_view', sem_id=sem_id, tab='sections', scheme_id=scheme_id))
    
    file = request.files['file']
    if file.filename == '':
        return redirect(url_for('semester_view', sem_id=sem_id, tab='sections', scheme_id=scheme_id))
    
    filename = secure_filename(file.filename)
    file_ext = os.path.splitext(filename)[1].lower()
    
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = os.path.join(temp_dir, filename)
        file.save(temp_path)
        
        data = []
        try:
            if import_method == 'manual':
                data = extract_students_manual(temp_path, file_ext)
                # Repair doubled USNs
                for item in data:
                    item['usn'] = repair_doubled_text(item['usn'])
                    item['name'] = item['name'].strip() # cleanup name too if needed?
                    
                if not data:
                    flash('Manual import found no students. Trying AI fallback...')
                    # Optional: Add AI fallback here if desired
            else:
                # AI Import logic
                prompt = 'Extract student data. Return ONLY JSON array: [{"usn": "...", "name": "..."}]. No markdown.'
                if file_ext in ['.xlsx', '.xls']:
                    df = pd.read_excel(temp_path)
                    txt = get_gemini_response(prompt + "\nData:\n" + df.to_csv(index=False))
                else:
                    mime = 'application/pdf' if file_ext == '.pdf' else 'image/jpeg'
                    txt = get_gemini_response(prompt, file_path=temp_path, file_mime=mime)
                data = json.loads(txt.replace('```json', '').replace('```', '').strip())

            db = get_db()
            count = 0
            duplicates = 0
            existing_usns = {row['usn'] for row in db.execute('SELECT usn FROM students WHERE section_id = %s', (section_id,)).fetchall()}
            
            for item in data:
                if item.get('usn') and item.get('name'):
                    usn = item['usn'].upper().strip()
                    name = item['name'].strip()
                    if usn in existing_usns:
                        duplicates += 1
                        continue # Skip duplicates
                    
                    db.execute('INSERT INTO students (section_id, usn, name) VALUES (%s, %s, %s)', (section_id, usn, name))
                    existing_usns.add(usn) # Add to set to prevent dups within same file
                    count += 1
            db.commit()
            
            msg = f'Imported {count} students via {import_method.upper()}!'
            if duplicates > 0:
                msg += f' (Skipped {duplicates} duplicates)'
            flash(msg)
            
        except Exception as e:
            flash(f'Import failed: {e}')
            
    return redirect(url_for('semester_view', sem_id=sem_id, tab='subjects', scheme_id=scheme_id))



@app.route('/subject/add', methods=['POST'])
def add_subject():
    db = get_db()
    sem_id = request.form['sem_id']
    scheme_id = request.form.get('scheme_id')
    section_ids = request.form.getlist('section_ids')
    course_subj = request.form.get('course_subject', '')
    faculty = request.form.get('faculty', '').strip()
    
    if not course_subj or '|' not in course_subj:
        flash('ERROR|Invalid subject selection.')
        return redirect(url_for('semester_view', sem_id=sem_id, tab='subjects', scheme_id=scheme_id))
        
    if not section_ids:
        flash('ERROR|Please select at least one section.')
        return redirect(url_for('semester_view', sem_id=sem_id, tab='subjects', scheme_id=scheme_id))
        
    code, title = course_subj.split('|', 1)
    
    success_count = 0
    duplicate_msgs = []
    
    for sid in section_ids:
        existing = db.execute('SELECT sub.*, sec.name as sec_name FROM subjects sub JOIN sections sec ON sub.section_id = sec.id WHERE sub.section_id = %s AND sub.code = %s', (sid, code)).fetchone()
        if existing:
            duplicate_msgs.append(f'{existing["sec_name"]} ({existing["faculty"]})')
            continue
        
        db.execute('INSERT INTO subjects (semester_id, section_id, code, title, faculty) VALUES (%s, %s, %s, %s, %s)', (sem_id, sid, code, title, faculty))
        success_count += 1
        
    db.commit()
    
    if duplicate_msgs:
        flash(f'ERROR|Duplicate(s) Ignored: {code} is already mapped in section(s) {", ".join(duplicate_msgs)}.')
        
    if success_count > 0:
        flash(f'Subject {code} successfully mapped to {success_count} section(s) for {faculty}!')
    return redirect(url_for('semester_view', sem_id=sem_id, tab='subjects', scheme_id=scheme_id))

@app.route('/subject/delete/<int:id>')
def delete_subject(id):
    sem_id = request.args.get('sem_id', 1)
    scheme_id = request.args.get('scheme_id')
    db = get_db()
    db.execute('DELETE FROM marks WHERE subject_id = %s', (id,))
    db.execute('DELETE FROM subjects WHERE id = %s', (id,))
    db.commit()
    flash('Subject deleted!')
    return redirect(url_for('semester_view', sem_id=sem_id, tab='subjects', scheme_id=scheme_id))

# --- COURSE CONFIG (AWD & COs) ---

@app.route('/subject/<int:id>/config')
def course_config(id):
    db = get_db()
    subject = db.execute('SELECT sub.*, sem.number as sem_num, sec.name as sec_name FROM subjects sub JOIN semesters sem ON sub.semester_id = sem.id JOIN sections sec ON sub.section_id = sec.id WHERE sub.id = %s', (id,)).fetchone()
    if not subject:
        return "Subject not found", 404
    
    cos = db.execute('SELECT * FROM course_outcomes WHERE subject_id = %s ORDER BY co_number', (id,)).fetchall()
    
    co_rows = ''.join([f'<tr><td>CO{co["co_number"]}</td><td>{co["description"]}</td><td><a href="/subject/{id}/co/delete/{co["id"]}" class="btn btn-outline btn-sm">×</a></td></tr>' for co in cos])
    if not cos:
        co_rows = '<tr><td colspan="3" class="text-center text-muted">No COs added yet</td></tr>'
    
    content = f'''
    <div class="container">
        <div class="mb-3">
            <h1 class="page-title mt-2">{subject['code']} - Configuration</h1>
            <p class="page-subtitle">{subject['title']}</p>
        </div>
        
        {{% with messages = get_flashed_messages() %}}{{% if messages %}}{{% for m in messages %}}<div class="alert alert-success">{{{{ m }}}}</div>{{% endfor %}}{{% endif %}}{{% endwith %}}
        
        <div class="grid grid-2" style="gap: 2rem;">
            <div class="card">
                <h3>📊 Assessment Weightage (AWD)</h3>
                <form action="/subject/{id}/config/awd" method="post">
                    <div class="grid grid-4" style="gap: 1rem; margin-bottom: 1rem;">
                        <div>
                            <label class="form-label">Test/IA (%)</label>
                            <input type="number" name="awd_test" value="{subject['awd_test']}" class="form-control" min="0" max="100">
                        </div>
                        <div>
                            <label class="form-label">Quiz (%)</label>
                            <input type="number" name="awd_quiz" value="{subject['awd_quiz']}" class="form-control" min="0" max="100">
                        </div>
                        <div>
                            <label class="form-label">Assignment (%)</label>
                            <input type="number" name="awd_assign" value="{subject['awd_assign']}" class="form-control" min="0" max="100">
                        </div>
                        <div>
                            <label class="form-label">SEE (%)</label>
                            <input type="number" name="awd_see" value="{subject['awd_see']}" class="form-control" min="0" max="100">
                        </div>
                    </div>
                    <button class="btn btn-primary">💾 Save AWD</button>
                </form>
                
                <hr style="margin: 1.5rem 0;">
                
                <h4>⚡ AI Extract AWD</h4>
                <form action="/subject/{id}/config/awd/parse" method="post" class="mt-2">
                    <textarea name="paste_text" class="form-control" rows="3" placeholder="Paste AWD data here (e.g., Test: 25%, Quiz: 15%, Assignment: 10%, SEE: 50%)"></textarea>
                    <button class="btn btn-success btn-sm mt-2">Parse AWD</button>
                </form>
            </div>
            
            <div class="card">
                <h3>📋 Course Outcomes (COs)</h3>
                <table>
                    <tr><th>CO#</th><th>Description</th><th></th></tr>
                    {co_rows}
                </table>
                
                <form action="/subject/{id}/co/add" method="post" class="mt-3">
                    <div style="display: flex; gap: 0.5rem;">
                        <input type="number" name="co_number" class="form-control" placeholder="#" style="width: 60px;" min="1" max="10" required>
                        <input type="text" name="description" class="form-control" placeholder="CO Description" required style="flex: 1;">
                        <button class="btn btn-primary">+ Add</button>
                    </div>
                </form>
                
                <hr style="margin: 1.5rem 0;">
                
                <h4>⚡ AI Extract COs</h4>
                <form action="/subject/{id}/config/co/parse" method="post" class="mt-2">
                    <textarea name="paste_text" class="form-control" rows="4" placeholder="Paste CO data here (e.g., CO1: Understand data structures, CO2: Apply algorithms...)"></textarea>
                    <button class="btn btn-success btn-sm mt-2">Parse COs</button>
                </form>
            </div>
        </div>
        <div style="text-align: center; margin-top: 3rem; margin-bottom: 2rem;">
            <a href="/semester/{subject['semester_id']}?tab=subjects" class="btn-back">⬅️ Back to Subjects</a>
        </div>
    </div>'''
    return render_template_string(base_html(f'{subject["code"]} Config - CAB', content))

@app.route('/subject/<int:id>/config/awd', methods=['POST'])
def save_awd(id):
    db = get_db()
    awd_test = request.form.get('awd_test', 25)
    awd_quiz = request.form.get('awd_quiz', 15)
    awd_assign = request.form.get('awd_assign', 10)
    awd_see = request.form.get('awd_see', 50)
    db.execute('UPDATE subjects SET awd_test=%s, awd_quiz=%s, awd_assign=%s, awd_see=%s WHERE id=%s', (awd_test, awd_quiz, awd_assign, awd_see, id))
    db.commit()
    flash('AWD saved!')
    return redirect(url_for('course_config', id=id))

@app.route('/subject/<int:id>/config/awd/parse', methods=['POST'])
def parse_awd(id):
    text = request.form.get('paste_text', '')
    if not text.strip():
        flash('No text provided')
        return redirect(url_for('course_config', id=id))
    
    prompt = f'''Extract assessment weightage from this text. Return ONLY JSON: {{"test": 25, "quiz": 15, "assignment": 10, "see": 50}}
No markdown, just the JSON object.

Text: {text}'''
    
    try:
        txt = get_gemini_response(prompt)
        data = json.loads(txt.replace('```json', '').replace('```', '').strip())
        db = get_db()
        db.execute('UPDATE subjects SET awd_test=%s, awd_quiz=%s, awd_assign=%s, awd_see=%s WHERE id=%s', 
                   (data.get('test', 25), data.get('quiz', 15), data.get('assignment', 10), data.get('see', 50), id))
        db.commit()
        flash('AWD extracted and saved!')
    except Exception as e:
        flash(f'Parse failed: {e}')
    return redirect(url_for('course_config', id=id))

@app.route('/subject/<int:id>/co/add', methods=['POST'])
def add_co(id):
    db = get_db()
    co_number = request.form.get('co_number')
    description = request.form.get('description')
    db.execute('INSERT INTO course_outcomes (subject_id, co_number, description) VALUES (%s, %s, %s)', (id, co_number, description))
    db.commit()
    flash(f'CO{co_number} added!')
    return redirect(url_for('course_config', id=id))

@app.route('/subject/<int:id>/co/delete/<int:co_id>')
def delete_co(id, co_id):
    db = get_db()
    db.execute('DELETE FROM course_outcomes WHERE id=%s', (co_id,))
    db.commit()
    flash('CO deleted!')
    return redirect(url_for('course_config', id=id))

@app.route('/subject/<int:id>/config/co/parse', methods=['POST'])
def parse_co(id):
    text = request.form.get('paste_text', '')
    if not text.strip():
        flash('No text provided')
        return redirect(url_for('course_config', id=id))
    
    prompt = f'''Extract course outcomes from this text. Return ONLY JSON array: [{{"number": 1, "description": "Understand..."}}]
No markdown.

Text: {text}'''
    
    try:
        txt = get_gemini_response(prompt)
        data = json.loads(txt.replace('```json', '').replace('```', '').strip())
        db = get_db()
        count = 0
        for co in data:
            db.execute('INSERT INTO course_outcomes (subject_id, co_number, description) VALUES (%s, %s, %s)', 
                       (id, co.get('number'), co.get('description')))
            count += 1
        db.commit()
        flash(f'{count} COs extracted and added!')
    except Exception as e:
        flash(f'Parse failed: {e}')
    return redirect(url_for('course_config', id=id))

# --- GRADE CALCULATION HELPERS ---

def calculate_grade(total):
    """Calculate grade from total marks (out of 100) - VTU Scale for UG"""
    if total >= 91: return 'O'
    elif total >= 81: return 'A+'
    elif total >= 71: return 'A'
    elif total >= 61: return 'B+'
    elif total >= 51: return 'B'
    elif total >= 40: return 'C'
    else: return 'D'  # Not satisfactory

def calculate_pg_grade(total, see_mark=None):
    """Calculate grade from total marks (out of 100) - PG Scale (C = Fail at <50)"""
    if see_mark is not None and see_mark < 50:
        return 'C'
    if total >= 91: return 'O'
    elif total >= 81: return 'A+'
    elif total >= 71: return 'A'
    elif total >= 61: return 'B+'
    elif total >= 50: return 'B'
    else: return 'C'  # Fail for PG

def get_pg_grade_point(grade):
    """Get grade point for PG grades"""
    grade_points = {'O': 10, 'A+': 9, 'A': 8, 'B+': 7, 'B': 6, 'C': 0}
    return grade_points.get(grade, 0)

def get_grade_color(grade):
    colors = {'O': '#22c55e', 'A+': '#84cc16', 'A': '#eab308', 'B+': '#f97316', 'B': '#3b82f6', 'C': '#dc2626', 'D': '#dc2626'}
    return colors.get(grade, '#6b7280')

# --- SUBJECT DASHBOARD (MARKS ENTRY) ---

@app.route('/subject/<int:id>')
def subject_dashboard(id):
    step = request.args.get('step', 'ia1')
    report_mode = request.args.get('mode', 'actual')  # actual, ai_predicted, gaussian
    db = get_db()
    
    subject = db.execute('SELECT sub.*, sem.number as sem_num, sec.name as sec_name FROM subjects sub JOIN semesters sem ON sub.semester_id = sem.id JOIN sections sec ON sub.section_id = sec.id WHERE sub.id = %s', (id,)).fetchone()
    if not subject:
        return "Subject not found", 404
    
    students = db.execute('SELECT * FROM students WHERE section_id = %s ORDER BY usn', (subject['section_id'],)).fetchall()
    marks_data = {}
    for row in db.execute('SELECT * FROM marks WHERE subject_id = %s', (id,)).fetchall():
        if 'REMARKS' in row['mark_type'].upper():
            continue
        marks_data[(row['student_id'], row['mark_type'])] = {'value': row['value'], 'prediction': row['ai_prediction'], 'reason': row['ai_reason']}
    
    students_with_marks = []
    for s in students:
        sd = dict(s)
        for mt in ['ia1', 'ia2', 'ia3', 'q1', 'q2', 'q3', 'a1', 'a2', 'see']:
            m = marks_data.get((s['id'], mt), {'value': 0, 'prediction': None, 'reason': None})
            sd[mt] = m['value']
            sd[f'{mt}_pred'] = m['prediction']
            sd[f'{mt}_reason'] = m['reason']
            # Load AI and Manual assigned marks (separate columns)
            ai_m = marks_data.get((s['id'], f'{mt}_ai'), {'value': 0})
            manual_m = marks_data.get((s['id'], f'{mt}_manual'), {'value': 0})
            scored_m = marks_data.get((s['id'], f'{mt}_MARKS SCORED'), {'value': 0})
            sd[f'{mt}_ai'] = ai_m['value']
            sd[f'{mt}_manual'] = manual_m['value']
            sd[f'{mt}_scored'] = scored_m['value']
        
        # Calculate averages and totals
        sd['ia_avg'] = round((sd['ia1'] + sd['ia2'] + sd['ia3']) / 3, 1)
        sd['quiz_avg'] = round((sd['q1'] + sd['q2'] + sd['q3']) / 3, 1)
        sd['assign_avg'] = round((sd['a1'] + sd['a2']) / 2, 1)
        
        # CIE (60 marks) = Direct sum of all components per user request to match CIE Report exactly
        ia_component = sd['ia1'] + sd['ia2'] + sd['ia3']
        quiz_component = sd['q1'] + sd['q2'] + sd['q3']
        assign_component = sd['a1'] + sd['a2']
        
        # Total is ceiling of sum
        total_cie = math.ceil(ia_component + quiz_component + assign_component)
        
        # Components are now floats/ints but we use raw for display
        ia_disp = ia_component
        quiz_disp = quiz_component
        assign_disp = assign_component
        
        sd['cie'] = int(total_cie)
        sd['comp_test'] = ia_disp
        sd['comp_quiz'] = quiz_disp
        sd['comp_assign'] = assign_disp
        
        # AI Predicted CIE calculation
        ia1_p = int(sd.get('ia1_pred') or sd['ia1'])
        ia2_p = int(sd.get('ia2_pred') or sd['ia2'])
        ia3_p = int(sd.get('ia3_pred') or sd['ia3'])
        q1_p = int(sd.get('q1_pred') or sd['q1'])
        q2_p = int(sd.get('q2_pred') or sd['q2'])
        q3_p = int(sd.get('q3_pred') or sd['q3'])
        a1_p = int(sd.get('a1_pred') or sd['a1'])
        a2_p = int(sd.get('a2_pred') or sd['a2'])
        
        ia_p_component = ia1_p + ia2_p + ia3_p
        quiz_p_component = q1_p + q2_p + q3_p
        assign_p_component = a1_p + a2_p
        
        sd['cie_predicted'] = int(math.ceil(ia_p_component + quiz_p_component + assign_p_component))
        sd['comp_test_pred'] = ia_p_component
        sd['comp_quiz_pred'] = quiz_p_component
        sd['comp_assign_pred'] = assign_p_component
        
        # Check if any AI predictions are available
        sd['has_ai_predictions'] = any([sd.get('ia1_pred'), sd.get('ia2_pred'), sd.get('ia3_pred'), 
                                         sd.get('q1_pred'), sd.get('q2_pred'), sd.get('q3_pred'),
                                         sd.get('a1_pred'), sd.get('a2_pred'), sd.get('see_pred')])
        
        # SEE predicted (if available)
        sd['see_pred_val'] = int(sd['see_pred']) if sd['see_pred'] else 0
        
        # Gaussian SEE
        gaussian_mark = marks_data.get((s['id'], 'see_gaussian'), {'value': 0})
        sd['see_gaussian'] = gaussian_mark['value']
        
        students_with_marks.append(sd)
        
    # Collect dynamic VTU columns for the CURRENT STEP
    dynamic_cols = set()
    system_suffixes = ['_pred', '_reason', '_ai', '_manual']
    for row in db.execute('SELECT mark_type FROM marks WHERE subject_id = %s', (id,)).fetchall():
        mt = row['mark_type']
        if mt.startswith(f"{step}_") and not any(mt.endswith(suf) for suf in system_suffixes):
            col_name = mt.split('_', 1)[1]
            if col_name.upper() != 'REMARKS':
                dynamic_cols.add(col_name)
            
    if 'MARKS SCORED' in dynamic_cols or 'ASSIGNMENT' in dynamic_cols:
        dynamic_cols.add('CONVERTED')
            
    def sort_dyn_cols(c):
        c_up = c.upper()
        if c_up.startswith('Q') and c_up[1:].isdigit():
            return (2, int(c_up[1:]))
        preset = {'ASSIGNMENT': 0, 'MAX MARKS': 1, 'MARKS SCORED': 2, 'CONVERTED': 3, 'TOTAL': 4}
        if c_up in preset:
            return (0, preset[c_up])
        return (1, c)
        
    dynamic_cols = sorted(list(dynamic_cols), key=sort_dyn_cols)
    steps = [('ia1', 'IA 1'), ('ia2', 'IA 2'), ('ia3', 'IA 3'), ('q1', 'Quiz 1'), ('q2', 'Quiz 2'), ('q3', 'Quiz 3'), ('a1', 'Assign 1'), ('a2', 'Assign 2'), ('cie_report', 'CIE Report'), ('see', 'SEE'), ('report', 'Overall')]
    
    # Calculate Prev/Next navigation logic
    prev_step_url = None
    next_step_url = None
    prev_step_name = None
    next_step_name = None
    
    for idx, (s_id, s_name) in enumerate(steps):
        if s_id == step:
            if idx > 0:
                prev_step_url = f"/subject/{id}?step={steps[idx-1][0]}"
                prev_step_name = steps[idx-1][1]
            if idx < len(steps) - 1:
                next_step_url = f"/subject/{id}?step={steps[idx+1][0]}"
                next_step_name = steps[idx+1][1]
            break
            
    pred_options = []
    if step == 'ia2': pred_options = [('ia1', 'Based on IA1'), ('ia1_q1', 'Based on IA1 + Quiz1')]
    elif step == 'ia3': pred_options = [('ia1_ia2', 'Based on IA1 + IA2'), ('ia1_ia2_q1_q2', 'Based on IAs + Quizzes')]
    elif step == 'see': pred_options = [('all', 'Based on All Assessments')]
    
    step_name = dict(steps).get(step, 'Unknown')
    max_marks = 100 if step == 'see' else (25 if step.startswith('ia') else (20 if step.startswith('q') else 10))
    
    sidebar = ''.join([f'<a href="/subject/{id}?step={sid}" class="sidebar-link {"active" if step == sid else ""}">{sname}</a>' for sid, sname in steps])
    
    if step == 'report':
        # Build grade distribution data for each mode
        def build_report_data(students_list, see_field, cie_field='cie', test_field='comp_test', quiz_field='comp_quiz', assign_field='comp_assign'):
            grades = {'O': 0, 'A+': 0, 'A': 0, 'B+': 0, 'B': 0, 'C': 0, 'D': 0}
            report_rows = []
            for s in students_list:
                see_val = s.get(see_field, 0) or 0
                cie_val = s.get(cie_field, s['cie'])
                test_val = s.get(test_field, 0)
                quiz_val = s.get(quiz_field, 0)
                assign_val = s.get(assign_field, 0)
                
                # SEE is out of 100, scale to 40 (apply ceiling)
                see_scaled = math.ceil(see_val * 0.4)
                total = math.ceil(cie_val + see_scaled)  # CIE (60) + SEE scaled (40) = 100
                grade = calculate_grade(total)
                grades[grade] += 1
                report_rows.append({'usn': s['usn'], 'name': s['name'], 'cie': cie_val, 'test': test_val, 'quiz': quiz_val, 'assign': assign_val, 'see': see_val, 'see_scaled': see_scaled, 'total': total, 'grade': grade, 'color': get_grade_color(grade)})
            return report_rows, grades
        
        actual_rows, actual_grades = build_report_data(students_with_marks, 'see', 'cie', 'comp_test', 'comp_quiz', 'comp_assign')
        predicted_rows, predicted_grades = build_report_data(students_with_marks, 'see_pred_val', 'cie_predicted', 'comp_test_pred', 'comp_quiz_pred', 'comp_assign_pred')
        gaussian_rows, gaussian_grades = build_report_data(students_with_marks, 'see_gaussian', 'cie', 'comp_test', 'comp_quiz', 'comp_assign')
        
        # Check if ANY AI predictions exist
        any_ai_predictions = any(s.get('has_ai_predictions') for s in students_with_marks)
        
        # Select current mode data
        ai_warning = ''
        if report_mode == 'ai_predicted':
            if not any_ai_predictions:
                ai_warning = '<div class="alert" style="background: #fef3c7; border: 1px solid #f59e0b; padding: 1rem; border-radius: 8px; margin-bottom: 1rem;">⚠️ <strong>No AI predictions available!</strong> Please generate AI predictions for IAs/Quizzes/Assignments/SEE in the marks entry pages first.</div>'
            rows, grades, title = predicted_rows, predicted_grades, 'With AI Predicted Marks'
        elif report_mode == 'gaussian':
            # Check if gaussian marks generated
            has_gaussian = any(r['see'] > 0 for r in gaussian_rows)
            if not has_gaussian:
                 ai_warning = '<div class="alert" style="background: #fef3c7; border: 1px solid #f59e0b; padding: 1rem; border-radius: 8px; margin-bottom: 1rem;">⚠️ <strong>No Gaussian marks assigned!</strong> Please go to the SEE step and click "📊 Gaussian Assign" first.</div>'
            rows, grades, title = gaussian_rows, gaussian_grades, 'With Gaussian SEE'
        else:
            rows, grades, title = actual_rows, actual_grades, 'Actual Performance'
        
        sticky_name = 'position:sticky; left:0; z-index:2; background:inherit; box-shadow: 2px 0 5px rgba(0,0,0,0.05);'
        def row_bg(r):
            if r['total'] == 0 or r['cie'] == 0:
                return 'background: #fee2e2;'
            return 'background: white;'
        table_rows = ''.join([f'<tr style="{row_bg(r)}"><td><strong>{r["usn"]}</strong></td><td style="{sticky_name}">{r["name"]}</td><td>{r["quiz"]}</td><td>{r["test"]}</td><td>{r["assign"]}</td><td><strong>{r["cie"]}</strong></td><td>{r["see"]} &rarr; {r["see_scaled"]}</td><td><strong>{r["total"]}</strong></td><td><span class="badge" style="background:{r["color"]};color:white;">{r["grade"]}</span></td></tr>' for r in rows])
        
        chart_data = json.dumps(list(grades.values()))
        chart_labels = json.dumps(list(grades.keys()))
        
        main_content = f'''
        <div class="card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; flex-wrap: wrap; gap: 1rem;">
                <h2 style="margin: 0; white-space: nowrap;">📊 {title}</h2>
                <div style="display: flex; gap: 0.5rem; flex-wrap: wrap;">
                    <a href="/subject/{id}?step=report&mode=actual" class="btn {"btn-primary" if report_mode == "actual" else "btn-outline"} btn-sm">Actual</a>
                    <a href="/subject/{id}?step=report&mode=ai_predicted" class="btn {"btn-primary" if report_mode == "ai_predicted" else "btn-outline"} btn-sm">AI Predicted</a>
                    <a href="/subject/{id}?step=report&mode=gaussian" class="btn {"btn-primary" if report_mode == "gaussian" else "btn-outline"} btn-sm">Gaussian</a>
                </div>
            </div>
            
            {ai_warning}
            
            <div style="background: #f8fafc; padding: 1.5rem; border-radius: 10px; margin-bottom: 2rem;">
                <h4 style="margin-bottom: 1rem; text-align: center;">Grade Distribution</h4>
                <div style="height: 300px;"><canvas id="gradeChart"></canvas></div>
                <div style="display: flex; justify-content: center; gap: 1rem; margin-top: 1rem; flex-wrap: wrap; font-size: 0.85rem;">
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #22c55e; border-radius: 3px; display: inline-block;"></span> O (≥91)</span>
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #84cc16; border-radius: 3px; display: inline-block;"></span> A+ (81-90)</span>
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #eab308; border-radius: 3px; display: inline-block;"></span> A (71-80)</span>
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #f97316; border-radius: 3px; display: inline-block;"></span> B+ (61-70)</span>
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #3b82f6; border-radius: 3px; display: inline-block;"></span> B (51-60)</span>
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #dc2626; border-radius: 3px; display: inline-block;"></span> C (40-50)</span>
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #6b7280; border-radius: 3px; display: inline-block;"></span> D (<40)</span>
                </div>
            </div>
            
            <div class="marks-scroll" style="overflow-x: auto; max-width: 100%; border: 1px solid #e2e8f0; border-radius: 8px;">
                <table style="border-collapse: separate; border-spacing: 0; min-width: 100%;">
                    <tr><th>USN</th><th style="position: sticky; left: 0; z-index: 3; background: #f8fafc;">Name</th><th>Assign(20)</th><th>Gaussian(20)</th><th>SEE(&rarr;80)</th><th>Total(100)</th><th>Percentage</th><th>Grade</th><th>GP</th></tr>
                    {table_rows}
                </table>
            </div>
        </div>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <script>
        new Chart(document.getElementById('gradeChart'), {{
            type: 'bar',
            data: {{
                labels: {chart_labels},
                datasets: [{{
                    label: 'Students',
                    data: {chart_data},
                    backgroundColor: ['#22c55e', '#84cc16', '#eab308', '#f97316', '#3b82f6', '#dc2626', '#6b7280']
                }}]
            }},
            options: {{ 
                responsive: true, 
                maintainAspectRatio: false,
                plugins: {{ legend: {{ display: false }} }},
                scales: {{ y: {{ beginAtZero: true, ticks: {{ stepSize: 1 }} }} }}
            }}
        }});
        </script>'''
    elif step == 'cie_report':
        # CIE Report - shows all IAs, Quizzes, Assignments with 4 view modes
        # Prepare data for client-side dynamic rendering
        students_json_data = []
        for s in students_with_marks:
            s_data = {'usn': s['usn'], 'name': s['name'], 'cie_actual': s['cie']}
            for mt in ['ia1', 'ia2', 'ia3', 'q1', 'q2', 'q3', 'a1', 'a2']:
                s_data[mt] = s.get(mt, 0) or 0
                s_data[f'{mt}_scored'] = s.get(f'{mt}_scored', 0) or 0
                s_data[f'{mt}_ai'] = s.get(f'{mt}_ai', 0) or 0
                s_data[f'{mt}_manual'] = s.get(f'{mt}_manual', 0) or 0
            students_json_data.append(s_data)
        
        students_json = json.dumps(students_json_data).replace("'", "&apos;")
        
        # Max marks: scored marks are raw (IA=50, Q=20, A=10), converted are weighted (IA=25, Q=15-equiv, A=10)
        comp_max_marks = {
            'ia1': 25, 'ia2': 25, 'ia3': 25,
            'q1': 20, 'q2': 20, 'q3': 20,
            'a1': 10, 'a2': 10
        }
        comp_max_scored = {
            'ia1': 50, 'ia2': 50, 'ia3': 50,
            'q1': 20, 'q2': 20, 'q3': 20,
            'a1': 10, 'a2': 10
        }
        comp_max_json = json.dumps(comp_max_marks)
        comp_max_scored_json = json.dumps(comp_max_scored)
        
        # Column options for assignment
        assign_cols = [('ia1', 'IA 1 (25)'), ('ia2', 'IA 2 (25)'), ('ia3', 'IA 3 (25)'), 
                       ('q1', 'Quiz 1 (20)'), ('q2', 'Quiz 2 (20)'), ('q3', 'Quiz 3 (20)'),
                       ('a1', 'Assignment 1 (10)'), ('a2', 'Assignment 2 (10)')]
        
        main_content = f'''<div class="card">
            <h2 style="text-align:center; margin-bottom:0.5rem;">📊 CIE Report - Component Breakdown</h2>
            <p class="text-muted" style="text-align:center;">Select view modes and components to filter the table & chart.</p>
            
            <!-- 4 View Mode Checkboxes -->
            <div style="display:flex; flex-wrap:wrap; justify-content:center; gap:0.75rem; margin-bottom:1.5rem; padding:1rem; background:linear-gradient(135deg, #f8fafc, #e0f2fe); border-radius:12px; border:1px solid #bae6fd;">
                <h4 style="width:100%; text-align:center; margin:0 0 0.5rem 0; color:#0369a1; font-size:1rem;">📋 View Modes</h4>
                <label style="display:flex; align-items:center; gap:0.4rem; background:#fff; padding:0.5rem 1rem; border-radius:8px; border:2px solid #f97316; cursor:pointer; font-weight:600; font-size:0.85rem; color:#ea580c;">
                    <input type="checkbox" class="view-mode-check" value="scored" onchange="rebuildTable()"> 📝 Marks Scored
                </label>
                <label style="display:flex; align-items:center; gap:0.4rem; background:#fff; padding:0.5rem 1rem; border-radius:8px; border:2px solid #22c55e; cursor:pointer; font-weight:600; font-size:0.85rem; color:#16a34a;">
                    <input type="checkbox" class="view-mode-check" value="converted" checked onchange="rebuildTable()"> ✅ Marks Converted
                </label>
                <label style="display:flex; align-items:center; gap:0.4rem; background:#fff; padding:0.5rem 1rem; border-radius:8px; border:2px solid #3b82f6; cursor:pointer; font-weight:600; font-size:0.85rem; color:#2563eb;">
                    <input type="checkbox" class="view-mode-check" value="ai" onchange="rebuildTable()"> 🤖 AI Predicted
                </label>
                <label style="display:flex; align-items:center; gap:0.4rem; background:#fff; padding:0.5rem 1rem; border-radius:8px; border:2px solid #a855f7; cursor:pointer; font-weight:600; font-size:0.85rem; color:#7c3aed;">
                    <input type="checkbox" class="view-mode-check" value="gaussian" onchange="rebuildTable()"> 📊 Gaussian
                </label>
            </div>
            
            <div style="background: #f0fdf4; padding: 1rem; border-radius: 10px; border: 1px solid #86efac; margin-bottom: 1.5rem;">
                <h4 style="color: #166534; margin-bottom: 1rem;">🎯 Gaussian Mark Assignment</h4>
                
                <div style="display: flex; gap: 1rem; align-items: center; flex-wrap: wrap; margin-bottom: 1rem;">
                    <label style="display: flex; align-items: center; gap: 0.5rem; font-weight: 600;">
                        <input type="checkbox" id="selectAll" onchange="toggleSelectAll()"> Select All Components
                    </label>
                </div>
                
                <div style="display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1rem;" id="componentCheckboxes">
                    <label style="display:flex;align-items:center;gap:0.25rem;background:#dbeafe;padding:0.25rem 0.5rem;border-radius:4px;">
                        <input type="checkbox" name="cols" value="ia1" class="col-check" onchange="rebuildTable()"> IA1
                    </label>
                    <label style="display:flex;align-items:center;gap:0.25rem;background:#dbeafe;padding:0.25rem 0.5rem;border-radius:4px;">
                        <input type="checkbox" name="cols" value="ia2" class="col-check" onchange="rebuildTable()"> IA2
                    </label>
                    <label style="display:flex;align-items:center;gap:0.25rem;background:#dbeafe;padding:0.25rem 0.5rem;border-radius:4px;">
                        <input type="checkbox" name="cols" value="ia3" class="col-check" onchange="rebuildTable()"> IA3
                    </label>
                    <label style="display:flex;align-items:center;gap:0.25rem;background:#fef3c7;padding:0.25rem 0.5rem;border-radius:4px;">
                        <input type="checkbox" name="cols" value="q1" class="col-check" onchange="rebuildTable()"> Q1
                    </label>
                    <label style="display:flex;align-items:center;gap:0.25rem;background:#fef3c7;padding:0.25rem 0.5rem;border-radius:4px;">
                        <input type="checkbox" name="cols" value="q2" class="col-check" onchange="rebuildTable()"> Q2
                    </label>
                    <label style="display:flex;align-items:center;gap:0.25rem;background:#fef3c7;padding:0.25rem 0.5rem;border-radius:4px;">
                        <input type="checkbox" name="cols" value="q3" class="col-check" onchange="rebuildTable()"> Q3
                    </label>
                    <label style="display:flex;align-items:center;gap:0.25rem;background:#dcfce7;padding:0.25rem 0.5rem;border-radius:4px;">
                        <input type="checkbox" name="cols" value="a1" class="col-check" onchange="rebuildTable()"> A1
                    </label>
                    <label style="display:flex;align-items:center;gap:0.25rem;background:#dcfce7;padding:0.25rem 0.5rem;border-radius:4px;">
                        <input type="checkbox" name="cols" value="a2" class="col-check" onchange="rebuildTable()"> A2
                    </label>
                </div>
                
                <div style="display: flex; gap: 1rem; flex-wrap: wrap;">
                    <form action="/subject/{id}/gaussian_cie_multi" method="post" id="aiForm">
                        <input type="hidden" name="columns" id="aiColumnsInput">
                        <button type="submit" class="btn btn-success" name="method" value="ai" onclick="setColumns('aiColumnsInput');">
                            🤖 AI Assign Selected
                        </button>
                    </form>
                    
                    <form action="/subject/{id}/gaussian_cie_multi" method="post" id="manualForm">
                        <input type="hidden" name="columns" id="manualColumnsInput">
                        <button type="submit" class="btn btn-primary" name="method" value="manual" onclick="setColumns('manualColumnsInput');">
                            📋 Manual Assign Selected
                        </button>
                    </form>
                </div>
            </div>
            
            <!-- Grade Legend -->
            <div style="display: flex; justify-content: center; gap: 1rem; margin-top: 1rem; margin-bottom: 1rem; flex-wrap: wrap; font-size: 0.85rem;">
                <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #22c55e; border-radius: 3px; display: inline-block;"></span> O (≥91%)</span>
                <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #84cc16; border-radius: 3px; display: inline-block;"></span> A+ (81-90%)</span>
                <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #eab308; border-radius: 3px; display: inline-block;"></span> A (71-80%)</span>
                <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #f97316; border-radius: 3px; display: inline-block;"></span> B+ (61-70%)</span>
                <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #3b82f6; border-radius: 3px; display: inline-block;"></span> B (51-60%)</span>
                <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #dc2626; border-radius: 3px; display: inline-block;"></span> C (40-50%)</span>
                <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #6b7280; border-radius: 3px; display: inline-block;"></span> D (<40%)</span>
            </div>
            
            <!-- Dynamic Charts Container -->
            <div id="chartsContainer" style="margin-bottom: 1.5rem;"></div>
            
            <!-- Dynamic Table Container -->
            <div id="tableContainer"></div>
        </div>
        
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <script>
        var studentsData = {students_json};
        var compMaxMarks = {comp_max_json};
        var compMaxScored = {comp_max_scored_json};
        var charts = {{}};
        var components = ['ia1','ia2','ia3','q1','q2','q3','a1','a2'];
        var compLabels = {{'ia1':'IA1','ia2':'IA2','ia3':'IA3','q1':'Q1','q2':'Q2','q3':'Q3','a1':'A1','a2':'A2'}};
        
        var modeConfig = {{
            'scored':    {{ label: 'Marks Scored', suffix: '_scored', color: '#f97316', icon: '📝', maxMarks: compMaxScored }},
            'converted': {{ label: 'Converted', suffix: '', color: '#22c55e', icon: '✅', maxMarks: compMaxMarks }},
            'ai':        {{ label: 'AI Predicted', suffix: '_ai', color: '#3b82f6', icon: '🤖', maxMarks: compMaxMarks }},
            'gaussian':  {{ label: 'Gaussian', suffix: '_manual', color: '#a855f7', icon: '📊', maxMarks: compMaxMarks }}
        }};
        var gradeColors = {{'O':'#22c55e','A+':'#84cc16','A':'#eab308','B+':'#f97316','B':'#3b82f6','C':'#dc2626','D':'#6b7280'}};
        
        function getGrade(score, maxScore) {{
            // CIE Grading Scale is always out of 60 marks total
            if (score >= 55) return 'O';
            if (score >= 49) return 'A+';
            if (score >= 43) return 'A';
            if (score >= 37) return 'B+';
            if (score >= 31) return 'B';
            if (score >= 24) return 'C';
            return 'D';
        }}
        
        function getSelectedModes() {{
            var modes = [];
            document.querySelectorAll('.view-mode-check:checked').forEach(cb => modes.push(cb.value));
            return modes;
        }}
        
        function getSelectedCols() {{
            var cols = [];
            document.querySelectorAll('.col-check:checked').forEach(cb => cols.push(cb.value));
            return cols;
        }}
        
        function rebuildTable() {{
            var modes = getSelectedModes();
            var selectedCols = getSelectedCols();
            
            // Rebuild charts
            var chartsDiv = document.getElementById('chartsContainer');
            chartsDiv.innerHTML = '';
            charts = {{}};
            
            if (modes.length > 0 && selectedCols.length > 0) {{
                var gridCols = modes.length <= 2 ? 'repeat(' + modes.length + ', 1fr)' : 'repeat(' + Math.min(modes.length, 4) + ', 1fr)';
                var chartGrid = '<div style="display:grid; grid-template-columns:' + gridCols + '; gap:1rem;">';
                modes.forEach(function(m) {{
                    var cfg = modeConfig[m];
                    chartGrid += '<div style="text-align:center;"><h4 style="color:' + cfg.color + ';">' + cfg.icon + ' ' + cfg.label + '</h4><div style="height:200px;"><canvas id="chart_' + m + '"></canvas></div></div>';
                }});
                chartGrid += '</div>';
                chartsDiv.innerHTML = chartGrid;
                
                // Init charts
                modes.forEach(function(m) {{
                    var cfg = modeConfig[m];
                    charts[m] = new Chart(document.getElementById('chart_' + m), {{
                        type: 'bar',
                        data: {{ labels: [], datasets: [{{ data: [], backgroundColor: [gradeColors['O'], gradeColors['A+'], gradeColors['A'], gradeColors['B+'], gradeColors['B'], gradeColors['C'], gradeColors['D']] }}] }},
                        options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }}, scales: {{ y: {{ beginAtZero: true, ticks: {{ stepSize: 1 }} }} }} }}
                    }});
                }});
                
                // Calculate grades
                var labels = ['O', 'A+', 'A', 'B+', 'B', 'C', 'D'];
                modes.forEach(function(m) {{
                    var cfg = modeConfig[m];
                    var grades = {{}};
                    labels.forEach(g => grades[g] = 0);
                    
                    studentsData.forEach(function(s) {{
                        var total = 0, maxTotal = 0;
                        selectedCols.forEach(function(c) {{
                            total += (s[c + cfg.suffix] || 0);
                            maxTotal += cfg.maxMarks[c];
                        }});
                        grades[getGrade(total, maxTotal)]++;
                    }});
                    
                    charts[m].data.labels = labels;
                    charts[m].data.datasets[0].data = labels.map(l => grades[l]);
                    charts[m].update();
                }});
            }}
            
            // Rebuild table
            var tableDiv = document.getElementById('tableContainer');
            if (modes.length === 0 || selectedCols.length === 0) {{
                tableDiv.innerHTML = '<div style="text-align:center; padding:2rem; color:#94a3b8;">Select at least one view mode and one component to see data.</div>';
                return;
            }}
            
            var stickyName = 'position:sticky; left:0; z-index:2; background:inherit; box-shadow: 2px 0 5px rgba(0,0,0,0.05);';
            var th_style = "color:#0369a1; font-weight: 700; font-size: 0.75rem; text-transform: uppercase; white-space: nowrap; padding: 0.4rem 0.5rem; cursor: pointer; position: relative; border: none; border-right: 1px solid #bae6fd; text-align: center;";
            
            function makeSortTh(label, colName, extraStyle, rowspan, colspan) {{
                var rs = rowspan || 1;
                var cs = colspan || 1;
                var ex = extraStyle || "";
                return '<th rowspan="' + rs + '" colspan="' + cs + '" style="' + th_style + ' ' + ex + '" onclick="vtuSort(this, \\x27' + colName + '\\x27)">' +
                    '<div style="display:flex; justify-content:space-between; align-items:center; gap: 0.5rem;">' +
                        '<span style="display:flex; align-items:center; gap:0.2rem; flex:1; justify-content:center;">' + label + ' <span style="font-size:0.5rem; color:#0284c7;" class="sort-icon"></span></span>' +
                        '<div class="vtu-filter-btn" onclick="event.stopPropagation(); vtuToggleFilter(this, \\x27' + colName + '\\x27)" style="background:#0284c7; padding:2px 3px; border-radius:3px; cursor:pointer; min-width:14px; text-align:center;">' +
                            '<span style="font-size:0.6rem; color:#fff;">▼</span>' +
                        '</div>' +
                    '</div>' +
                '</th>';
            }}
            
            // Build header
            var hdr1 = '<tr class="vtu-header-row" style="border-bottom: 2px solid #e5e7eb;">' + 
                       makeSortTh('USN', 'USN', 'background:#e0f2fe;', 2, 1) + 
                       makeSortTh('NAME', 'NAME', 'position:sticky;left:0;z-index:3;background:#f8fafc;', 2, 1);
            var hdr2 = '<tr class="vtu-header-row" style="border-bottom: 2px solid #e5e7eb;">';
            
            selectedCols.forEach(function(c) {{
                var bgColors = {{'ia1':'#dbeafe','ia2':'#dbeafe','ia3':'#dbeafe','q1':'#fef3c7','q2':'#fef3c7','q3':'#fef3c7','a1':'#dcfce7','a2':'#dcfce7'}};
                hdr1 += '<th colspan="' + modes.length + '" style="background:' + bgColors[c] + '; text-align:center; padding:0.4rem;">' + compLabels[c] + '</th>';
                modes.forEach(function(m) {{
                    var cfg = modeConfig[m];
                    hdr2 += makeSortTh(cfg.label, c + '_' + cfg.label, 'background:' + bgColors[c] + '; color:' + cfg.color + ';');
                }});
            }});
            
            hdr1 += '<th colspan="' + modes.length + '" style="background:#e0e7ff; text-align:center; padding:0.4rem;">Total</th>';
            hdr1 += '<th colspan="' + modes.length + '" style="text-align:center; padding:0.4rem;">Grade</th></tr>';
            
            modes.forEach(function(m) {{
                var cfg = modeConfig[m];
                hdr2 += makeSortTh(cfg.label, 'Total_' + cfg.label, 'background:#e0e7ff; color:' + cfg.color + ';');
            }});
            modes.forEach(function(m) {{
                var cfg = modeConfig[m];
                hdr2 += makeSortTh(cfg.label, 'Grade_' + cfg.label, 'color:' + cfg.color + ';');
            }});
            hdr2 += '</tr>';
            
            // Build body
            var body = '';
            studentsData.forEach(function(s) {{
                // Compute totals first to determine row color
                var totals = {{}};
                var maxTotals = {{}};
                modes.forEach(function(m) {{ totals[m] = 0; maxTotals[m] = 0; }});
                
                selectedCols.forEach(function(c) {{
                    modes.forEach(function(m) {{
                        var cfg = modeConfig[m];
                        totals[m] += (s[c + cfg.suffix] || 0);
                        maxTotals[m] += cfg.maxMarks[c];
                    }});
                }});
                
                // Check if any mode has zero total
                var hasZero = modes.some(function(m) {{ return Math.ceil(totals[m]) === 0; }});
                var rowBg = hasZero ? '#fee2e2' : 'white';
                
                body += '<tr class="vtu-data-row" style="background:' + rowBg + ';">' + 
                        '<td data-col="USN" data-val="' + s.usn + '"><strong>' + s.usn + '</strong></td>' + 
                        '<td data-col="NAME" data-val="' + s.name + '" style="' + stickyName + '">' + s.name + '</td>';
                
                // Per-component cells
                selectedCols.forEach(function(c) {{
                    modes.forEach(function(m) {{
                        var cfg = modeConfig[m];
                        var val = s[c + cfg.suffix] || 0;
                        body += '<td data-col="' + c + '_' + cfg.label + '" data-val="' + val + '" style="text-align:center; font-size:0.8rem;">' + (val || '-') + '</td>';
                    }});
                }});
                
                // Totals (apply ceiling - e.g. 30.2 becomes 31)
                modes.forEach(function(m) {{
                    var ceilTotal = Math.ceil(totals[m]);
                    var cfg = modeConfig[m];
                    body += '<td data-col="Total_' + cfg.label + '" data-val="' + ceilTotal + '" style="text-align:center; font-weight:bold; background:#f0f0ff;">' + ceilTotal + '</td>';
                }});
                
                // Grades (based on ceiled totals)
                modes.forEach(function(m) {{
                    var ceilTotal = Math.ceil(totals[m]);
                    var cfg = modeConfig[m];
                    var grade = getGrade(ceilTotal, maxTotals[m]);
                    body += '<td data-col="Grade_' + cfg.label + '" data-val="' + grade + '" style="text-align:center;"><span class="badge" style="background:' + gradeColors[grade] + ';color:white;">' + grade + '</span></td>';
                }});
                
                body += '</tr>';
            }});
            
            tableDiv.innerHTML = '<div class="marks-scroll" style="overflow-x:auto; max-width:100%; border:1px solid #e2e8f0; border-radius:8px;">' +
                '<table style="border-collapse:separate; border-spacing:0; min-width:100%; font-size:0.75rem;">' +
                '<thead>' + hdr1 + hdr2 + '</thead><tbody>' + body + '</tbody></table></div>';
        }}
        
        function toggleSelectAll() {{
            var checked = document.getElementById('selectAll').checked;
            document.querySelectorAll('.col-check').forEach(cb => cb.checked = checked);
            rebuildTable();
        }}
        
        function setColumns(inputId) {{
            var cols = [];
            document.querySelectorAll('.col-check:checked').forEach(cb => cols.push(cb.value));
            document.getElementById(inputId).value = cols.join(',');
        }}
        
        // Select all by default and build
        document.getElementById('selectAll').checked = true;
        document.querySelectorAll('.col-check').forEach(cb => cb.checked = true);
        rebuildTable();
        </script>'''

    elif step == 'see':
        pred_form = ''
        if pred_options:
            opts = ''.join([f'<option value="{pid}">{pname}</option>' for pid, pname in pred_options])
            pred_form = f'<form action="/subject/{id}/predict/{step}" method="post" style="display:inline;"><select name="method" class="form-control" style="width:auto;display:inline;">{opts}</select><button class="btn btn-warning btn-sm">🔮 AI Predict</button></form>'
        
        # Header styling mimicking provided screenshot for VTU sorting/filtering
        th_style = "background:#e0f2fe; color:#0369a1; font-weight: 700; font-size: 0.75rem; text-transform: uppercase; white-space: nowrap; padding: 0.4rem 0.5rem; cursor: pointer; position: relative; border: none; border-right: 1px solid #bae6fd; text-align: center;"
        
        def make_th(col_name, sort_dir=""):
            return f'''<th style="{th_style}" onclick="vtuSort(this, '{col_name}')">
                <div style="display:flex; justify-content:space-between; align-items:center; gap: 0.5rem;">
                    <span style="display:flex; align-items:center; gap:0.2rem; flex:1; justify-content:center;">{col_name} <span style="font-size:0.5rem; color:#0284c7;" class="sort-icon">{sort_dir}</span></span>
                    <div class="vtu-filter-btn" onclick="event.stopPropagation(); vtuToggleFilter(this, '{col_name}')" style="background:#0284c7; padding:2px 3px; border-radius:3px; cursor:pointer; min-width:14px; text-align:center;">
                        <span style="font-size:0.6rem; color:#fff;">▼</span>
                    </div>
                </div>
            </th>'''

        main_content = f'''<div class="card" style="padding: 2rem;">
            <div style="display: flex; flex-direction: column; align-items: center; gap: 1rem; margin-bottom: 2rem; border-bottom: 2px solid #e2e8f0; padding-bottom: 1.5rem;">
                <h2 style="margin: 0; color: #1e293b; font-size: 2.2rem; font-weight: 800; text-align: center;">{step_name} Entry</h2>
                <div style="display: flex; flex-wrap: wrap; justify-content: center; gap: 0.5rem; align-items: center; background: #f8fafc; padding: 0.5rem 1rem; border-radius: 12px; border: 1px solid #e2e8f0;">
                    {pred_form}
                    <form action="/subject/{id}/gaussian" method="post" style="display:inline;">
                        <button class="btn btn-success btn-sm" style="border-radius: 8px;">📊 Gaussian Assign</button>
                    </form>
                    <a href="/pg/module/{id}/classify/see" class="btn btn-outline btn-sm" title="Gaussian Settings (SEE)">⚙️</a>
                </div>
            </div>
            
            <div class="mb-3" style="background: #f0f9ff; padding: 1rem; border-radius: 10px; border: 1px solid #bae6fd;">
                <h4 style="color: #0369a1; margin-bottom: 1rem;">⚡ AI Extract</h4>
                
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
                    <div style="border-right: 1px solid #bae6fd; padding-right: 1rem;">
                        <label class="form-label">📁 Upload File (PDF/Excel/Image)</label>
                        <form action="/subject/{id}/import/{step}" method="post" enctype="multipart/form-data" style="display: flex; gap: 0.5rem;">
                            <input type="file" name="file" class="form-control" style="flex: 1;">
                            <button class="btn btn-success btn-sm">Upload</button>
                        </form>
                    </div>
                    <div>
                        <label class="form-label">📋 Or Paste Text Data</label>
                        <form action="/subject/{id}/parse/{step}" method="post">
                            <textarea name="paste_text" class="form-control" rows="3" placeholder="Paste marks data here"></textarea>
                            <div class="mt-2" style="display:flex; gap:0.5rem;">
                                <button class="btn btn-primary btn-sm" name="method" value="ai">⚡ Parse with AI</button>
                                <button class="btn btn-outline btn-sm" name="method" value="manual">📝 Parse Manually</button>
                            </div>
                        </form>
                    </div>
                </div>
            </div>
            
            <p class="text-muted mb-2">💡 <strong>Gaussian Assign</strong>: Auto-assigns SEE marks to create a bell curve with B+ as peak, based on CIE performance.</p>
            
            <form action="/subject/{id}/save/{step}" method="post">
                <div class="marks-scroll">
                <table style="border-collapse:separate; border-spacing:0; min-width:100%; font-size:0.75rem;">
                <thead>
                    <tr class="vtu-header-row" style="border-bottom: 2px solid #e5e7eb;">
                        {make_th('USN', '&#8593;')}
                        {make_th('Name')}
                        {make_th('CIE')}
                        {make_th('Marks')}
                        {make_th('AI Pred')}
                        {make_th('Gaussian')}
                        <th style="background:#e0f2fe; color:#0369a1; font-weight: 700; font-size: 0.75rem; text-transform: uppercase; padding: 0.4rem 0.5rem; text-align:center;">Action</th>
                    </tr>
                </thead>
                <tbody>
                {{% for s in students_with_marks %}}
                <tr class="vtu-data-row">
                    <td data-col="USN" data-val="{{{{ s.usn }}}}"><strong>{{{{ s.usn }}}}</strong><input type="hidden" name="sid_{{{{ loop.index0 }}}}" value="{{{{ s.id }}}}"></td>
                    <td data-col="Name" data-val="{{{{ s.name }}}}">{{{{ s.name }}}}</td>
                    <td data-col="CIE" data-val="{{{{ s.cie }}}}">{{{{ s.cie }}}}</td>
                    <td data-col="Marks" data-val="{{{{ s.{step} }}}}"><input type="number" name="val_{{{{ loop.index0 }}}}" value="{{{{ s.{step} }}}}" class="marks-input" min="0" max="{max_marks}" style="width:60px; text-align:center;"></td>
                    <td data-col="AI Pred" data-val="{{{{ s.{step}_pred }}}}">{{% if s.{step}_pred %}}<span class="badge badge-success">{{{{ s.{step}_pred }}}}</span>{{% else %}}-{{% endif %}}</td>
                    <td data-col="Gaussian" data-val="{{{{ s.see_gaussian }}}}">{{% if s.see_gaussian %}}<span class="badge badge-primary">{{{{ s.see_gaussian }}}}</span>{{% else %}}-{{% endif %}}</td>
                    <td style="text-align:center;"><a href="javascript:void(0)" class="btn btn-danger btn-sm" onclick="customConfirm('Delete marks for {{{{ s.usn }}}}?', '/subject/{id}/delete_mark/{{{{ s.id }}}}/{step}')">🗑️</a></td>
                </tr>
                {{% endfor %}}
                </tbody>
                </table>
                </div>
                <div class="mt-2" style="display:flex; gap:0.5rem; margin-bottom: 2rem;">
                    <button class="btn btn-primary">💾 Save Marks</button>
                    <a href="/subject/{id}/delete_all/{step}" class="btn btn-danger" onclick="return confirm('Delete ALL {step.upper()} marks? This cannot be undone!')">🗑️ Delete All</a>
                </div>
            </form>
            
            <!-- SEE Live Grading Graph -->
            <div style="background: #f8fafc; padding: 1.5rem; border-radius: 10px; margin-bottom: 2rem; border: 1px solid #e2e8f0;">
                <h4 style="margin-bottom: 0.5rem; text-align: center; color: #0f172a;">Real-Time SEE Grade Distribution</h4>
                <p style="text-align: center; font-size: 0.85rem; color: #64748b; margin-bottom: 1rem;">Calculated as: Total (100) = CIE (60) + ceil(SEE &times; 0.4)</p>
                <div style="height: 250px;"><canvas id="seeGradeChart"></canvas></div>
                <div style="display: flex; justify-content: center; gap: 1rem; margin-top: 1rem; flex-wrap: wrap; font-size: 0.85rem;">
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #22c55e; border-radius: 3px; display: inline-block;"></span> O (≥91)</span>
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #84cc16; border-radius: 3px; display: inline-block;"></span> A+ (81-90)</span>
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #eab308; border-radius: 3px; display: inline-block;"></span> A (71-80)</span>
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #f97316; border-radius: 3px; display: inline-block;"></span> B+ (61-70)</span>
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #3b82f6; border-radius: 3px; display: inline-block;"></span> B (51-60)</span>
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #dc2626; border-radius: 3px; display: inline-block;"></span> C (40-50)</span>
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #6b7280; border-radius: 3px; display: inline-block;"></span> D (<40)</span>
                </div>
            </div>
            
            <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
            <script>
            document.addEventListener('DOMContentLoaded', function() {{
                var ctx = document.getElementById('seeGradeChart').getContext('2d');
                var gradeColors = {{'O':'#22c55e','A+':'#84cc16','A':'#eab308','B+':'#f97316','B':'#3b82f6','C':'#dc2626','D':'#6b7280'}};
                var labels = ['O', 'A+', 'A', 'B+', 'B', 'C', 'D'];
                
                var chart = new Chart(ctx, {{
                    type: 'bar',
                    data: {{
                        labels: labels,
                        datasets: [{{
                            label: 'Students',
                            data: [0, 0, 0, 0, 0, 0, 0],
                            backgroundColor: [gradeColors['O'], gradeColors['A+'], gradeColors['A'], gradeColors['B+'], gradeColors['B'], gradeColors['C'], gradeColors['D']]
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {{ legend: {{ display: false }} }},
                        scales: {{ y: {{ beginAtZero: true, ticks: {{ stepSize: 1 }} }} }}
                    }}
                }});

                function updateSeeChart() {{
                    var counts = {{'O':0, 'A+':0, 'A':0, 'B+':0, 'B':0, 'C':0, 'D':0}};
                    
                    var rows = document.querySelectorAll('.marks-scroll tr');
                    // Skip header row
                    for(var i=1; i<rows.length; i++) {{
                        var cells = rows[i].querySelectorAll('td');
                        if(cells.length < 4) continue;
                        
                        var cie = parseInt(cells[2].innerText) || 0;
                        var seeInput = cells[3].querySelector('input');
                        if(!seeInput) continue;
                        
                        var seeVal = parseFloat(seeInput.value) || 0;
                        // Calculate total exactly as backend does: CIE + ceil(SEE * 0.4)
                        var seeScaled = Math.ceil(seeVal * 0.4);
                        var total = Math.ceil(cie + seeScaled);
                        
                        // Grade logic (100 mark UG scale)
                        var grade = 'D';
                        if (total >= 91) grade = 'O';
                        else if (total >= 81) grade = 'A+';
                        else if (total >= 71) grade = 'A';
                        else if (total >= 61) grade = 'B+';
                        else if (total >= 51) grade = 'B';
                        else if (total >= 40) grade = 'C';
                        
                        counts[grade]++;
                    }}
                    
                    chart.data.datasets[0].data = labels.map(l => counts[l]);
                    chart.update();
                }}

                // Listen for changes
                var inputs = document.querySelectorAll('.marks-input');
                inputs.forEach(function(inp) {{
                    inp.addEventListener('input', updateSeeChart);
                    inp.addEventListener('change', updateSeeChart);
                }});
                
                // Initial plot
                updateSeeChart();
            }});
            </script>
        </div>'''
    else:
        # Other assessment steps (IA, Quiz, Assignment)
        pred_form = ''
        if pred_options:
            opts = ''.join([f'<option value="{pid}">{pname}</option>' for pid, pname in pred_options])
            pred_form = f'<form action="/subject/{id}/predict/{step}" method="post" style="display:inline;"><select name="method" class="form-control" style="width:auto;display:inline;">{opts}</select><button class="btn btn-warning btn-sm">🔮 Predict</button></form>'
        if dynamic_cols:
            # Header styling exactly mimicking provided screenshot
            th_style = "background:#e0f2fe; color:#0369a1; font-weight: 700; font-size: 0.75rem; text-transform: uppercase; white-space: nowrap; padding: 0.4rem 0.5rem; cursor: pointer; position: relative; border: none; border-right: 1px solid #bae6fd;"
            
            def make_th(col_name, sort_dir=""):
                return f'''<th style="{th_style}" onclick="vtuSort(this, '{col_name}')">
                    <div style="display:flex; justify-content:space-between; align-items:center; gap: 0.5rem;">
                        <span style="display:flex; align-items:center; gap:0.2rem;">{col_name} <span style="font-size:0.5rem; color:#0284c7;" class="sort-icon">{sort_dir}</span></span>
                        <div class="vtu-filter-btn" onclick="event.stopPropagation(); vtuToggleFilter(this, '{col_name}')" style="background:#0284c7; padding:2px 3px; border-radius:3px; cursor:pointer; min-width:14px; text-align:center;">
                            <span style="font-size:0.6rem; color:#fff;">▼</span>
                        </div>
                    </div>
                </th>'''
                
            dyn_headers = ""
            for c in dynamic_cols:
                dyn_headers += make_th(c)
                if c == 'MARKS SCORED' and step != 'ia1':
                    dyn_headers += f'<th class="ai-pred-col" style="{th_style} text-align:center;">AI Prediction</th>'
            
            header_html = f'''
            <tr class="vtu-header-row" style="border-bottom: 2px solid #e5e7eb;">
                <th style="{th_style}" onclick="vtuSort(this, 'USN')">
                    <div style="display:flex; justify-content:space-between; align-items:center; gap: 0.5rem;">
                        <span style="display:flex; align-items:center; gap:0.2rem;">USN <span style="font-size:0.5rem; color:#0284c7;" class="sort-icon">↑</span></span>
                        <div class="vtu-filter-btn" onclick="event.stopPropagation(); vtuToggleFilter(this, 'USN')" style="background:#0284c7; padding:2px 3px; border-radius:3px; cursor:pointer; min-width:14px; text-align:center;"><span style="font-size:0.6rem; color:#fff;">▼</span></div>
                    </div>
                </th>
                <th style="{th_style} position: sticky; left: 0; z-index: 3; background: #f8fafc;" onclick="vtuSort(this, 'NAME')">
                    <div style="display:flex; justify-content:space-between; align-items:center; gap: 0.5rem;">
                        <span style="display:flex; align-items:center; gap:0.2rem;">NAME <span style="font-size:0.5rem; color:#0284c7;" class="sort-icon"></span></span>
                        <div class="vtu-filter-btn" onclick="event.stopPropagation(); vtuToggleFilter(this, 'NAME')" style="background:#0284c7; padding:2px 3px; border-radius:3px; cursor:pointer; min-width:14px; text-align:center;"><span style="font-size:0.6rem; color:#fff;">▼</span></div>
                    </div>
                </th>
                {dyn_headers}
                <th style="{th_style} text-align:center;">DEL</th>
            </tr>
            '''
            
            students_html = ""
            for i, s in enumerate(students_with_marks):
                dyn_cells = ""
                for c in dynamic_cols:
                    if c == 'MARKS SCORED':
                        val = marks_data.get((s['id'], f"{step}_{c}"), {'value': 0.0})['value']
                        dyn_cells += f'<td style="font-size:0.8rem; color:#4b5563; text-align:center; padding:0.3rem;" data-col="{c}" data-val="{val:.2f}"><input type="number" step="any" name="val_scored_{i}" value="{val:.2f}" class="marks-input" style="width:55px; border:1px solid #cbd5e1; border-radius:4px; padding:2px; text-align:center; font-size:0.8rem;"></td>'
                        if step != 'ia1':
                            pred_badge = f'<span class="prediction-badge badge badge-success">{s.get(step + "_pred")}</span>' if s.get(step + "_pred") else "-"
                            dyn_cells += f'<td class="ai-pred-col" style="text-align:center;">{pred_badge}</td>'
                    elif c == 'CONVERTED':
                        val = s[step]
                        dyn_cells += f'<td style="font-size:0.8rem; color:#4b5563; text-align:center; padding:0.3rem;" data-col="{c}" data-val="{val:.2f}"><input type="number" step="any" name="val_{i}" value="{val:.2f}" class="marks-input" style="width:55px; border:1px solid #94a3b8; border-radius:4px; padding:2px; text-align:center; font-weight:bold; color:#1d4ed8; background:#f0f9ff; font-size:0.8rem;"></td>'
                    else:
                        val = marks_data.get((s['id'], f"{step}_{c}"), {'value': 0.0})['value']
                        dyn_cells += f'<td style="font-size:0.8rem; color:#4b5563; text-align:center; padding:0.3rem;" data-col="{c}" data-val="{val:.2f}">{val:.2f}</td>'
                    
                zero_bg = 'background: #fee2e2;' if s[step] == 0 else 'background: white;'
                students_html += f'''
                <tr class="vtu-data-row" style="border-bottom: 1px solid #f1f5f9; {zero_bg}">
                    <td style="padding: 0.3rem;" data-col="USN" data-val="{s["usn"]}"><strong style="font-size:0.85rem;">{s["usn"]}</strong><input type="hidden" name="sid_{i}" value="{s["id"]}"></td>
                    <td style="padding: 0.3rem; position: sticky; left: 0; z-index: 2; background: inherit; box-shadow: 2px 0 5px rgba(0,0,0,0.05);" data-col="NAME" data-val="{s["name"]}"><span style="white-space:normal;font-size:0.8rem;line-height:1.2;max-width:140px;word-break:break-word;display:inline-block;vertical-align:middle;">{s["name"]}</span></td>
                    {dyn_cells}
                    <td style="text-align:center; padding:0.3rem;"><a href="javascript:void(0)" class="btn btn-danger btn-sm" style="padding:0.2rem 0.4rem; font-size:0.75rem;" onclick="customConfirm('Delete marks for {s['usn']}?', '/subject/{id}/delete_mark/{s['id']}/{step}')">🗑️</a></td>
                </tr>
                '''
        else:
            sticky_name = 'position:sticky; left:0; z-index:2; background:white;'
            
            # Header styling mimicking provided screenshot for VTU sorting/filtering
            th_style = "background:#e0f2fe; color:#0369a1; font-weight: 700; font-size: 0.75rem; text-transform: uppercase; white-space: nowrap; padding: 0.4rem 0.5rem; cursor: pointer; position: relative; border: none; border-right: 1px solid #bae6fd; text-align: center;"

            def make_th(col_name, sort_dir="", extra_style=""):
                return f'''<th style="{th_style} {extra_style}" onclick="vtuSort(this, '{col_name}')">
                    <div style="display:flex; justify-content:space-between; align-items:center; gap: 0.5rem;">
                        <span style="display:flex; align-items:center; gap:0.2rem; flex:1; justify-content:center;">{col_name} <span style="font-size:0.5rem; color:#0284c7;" class="sort-icon">{sort_dir}</span></span>
                        <div class="vtu-filter-btn" onclick="event.stopPropagation(); vtuToggleFilter(this, '{col_name}')" style="background:#0284c7; padding:2px 3px; border-radius:3px; cursor:pointer; min-width:14px; text-align:center;">
                            <span style="font-size:0.6rem; color:#fff;">▼</span>
                        </div>
                    </div>
                </th>'''
            
            if step != 'ia1':
                header_html = f'<tr class="vtu-header-row">{make_th("USN")} {make_th("Name", "", f"{sticky_name} background:#f8fafc;")} {make_th("Marks")} <th class="ai-pred-col" style="{th_style} text-align:center;">AI Prediction / Reason</th> <th style="{th_style} text-align:center;">Action</th></tr>'
            else:
                header_html = f'<tr class="vtu-header-row">{make_th("USN")} {make_th("Name", "", f"{sticky_name} background:#f8fafc;")} {make_th("Marks")} <th style="{th_style} text-align:center;">Action</th></tr>'
                
            students_html = ""
            for i, s in enumerate(students_with_marks):
                
                pred_td = ""
                if step != 'ia1':
                    pred_badge = f'<span class="prediction-badge badge badge-success">{s.get(step + "_pred")}</span>' if s.get(step + "_pred") else "-"
                    pred_td = f'<td class="ai-pred-col" style="text-align:center;">{pred_badge}</td>'
                    
                zero_bg = 'background: #fee2e2;' if s[step] == 0 else 'background: white;'
                students_html += f'''
                <tr class="vtu-data-row" style="{zero_bg}">
                    <td data-col="USN" data-val="{s["usn"]}"><strong>{s["usn"]}</strong><input type="hidden" name="sid_{i}" value="{s["id"]}"></td>
                    <td data-col="Name" data-val="{s["name"]}" style="{sticky_name} box-shadow: 2px 0 5px rgba(0,0,0,0.05); background: inherit;">{s["name"]}</td>
                    <td data-col="Marks" data-val="{s[step]}"><input type="number" name="val_{i}" value="{s[step]}" class="marks-input" min="0" max="{max_marks}"></td>
                    {pred_td}
                    <td style="text-align:center;"><a href="javascript:void(0)" class="btn btn-danger btn-sm" onclick="customConfirm('Delete marks for {s['usn']}?', '/subject/{id}/delete_mark/{s['id']}/{step}')">🗑️</a></td>
                </tr>
                '''
                
        pred_form_html = ""
        if pred_form:
            pred_form_html = '''<div style="display: flex; flex-wrap: wrap; justify-content: center; gap: 0.5rem; background: #f8fafc; padding: 0.5rem 1rem; border-radius: 12px; border: 1px solid #e2e8f0;">''' + str(pred_form) + '''</div>'''
            
        main_content = '''<div class="card" style="padding: 2rem;">
            <div style="display: flex; flex-direction: column; align-items: center; gap: 1rem; margin-bottom: 2rem; border-bottom: 2px solid #e2e8f0; padding-bottom: 1.5rem;">
                <h2 style="margin: 0; color: #1e293b; font-size: 2.2rem; font-weight: 800; text-align: center;">''' + str(step_name) + ''' Entry</h2>
                ''' + pred_form_html + '''
            </div>
            
            <div class="mb-3" style="background: #f0f9ff; padding: 1.5rem; border-radius: 12px; border: 1px solid #bae6fd; box-shadow: 0 4px 6px rgba(186, 230, 253, 0.4);">
                <div style="display: flex; align-items: center; gap: 0.5rem; margin-bottom: 1.5rem;">
                    <span style="font-size: 1.5rem;">⚡</span>
                    <h4 style="color: #0369a1; margin: 0; font-weight: 700;">AI Extract</h4>
                </div>
                
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 2rem;">
                    <div style="border-right: 1px solid #bae6fd; padding-right: 1rem;">
                        <label class="form-label">📁 Upload File (PDF/Excel/Image)</label>
                        <form action="/subject/''' + str(id) + '''/import/''' + str(step) + '''" method="post" enctype="multipart/form-data" style="display: flex; gap: 0.5rem;">
                            <input type="file" name="file" class="form-control" style="flex: 1;">
                            <button class="btn btn-success btn-sm">Upload</button>
                        </form>
                    </div>
                    <div>
                        <label class="form-label">📋 Or Paste Text Data</label>
                        <form action="/subject/''' + str(id) + '''/parse/''' + str(step) + '''" method="post">
                            <textarea name="paste_text" class="form-control" rows="3" placeholder="Paste marks data here"></textarea>
                            <div class="mt-2" style="display:flex; gap:0.5rem;">
                                <button class="btn btn-primary btn-sm" name="method" value="ai">⚡ Parse with AI</button>
                                <button class="btn btn-outline btn-sm" name="method" value="manual">📝 Parse Manually</button>
                            </div>
                        </form>
                    </div>
                </div>
                <p class="text-muted mt-2" style="font-size: 0.8rem;">AI will extract all columns and let you select which to import.</p>
            </div>'''
            
        prediction_toggle = ""
        if step != 'ia1':
            prediction_toggle = '''<div style="margin-bottom: 0.5rem; display: flex; align-items: center; justify-content: flex-end;">
                <label style="display:flex; align-items:center; gap:0.5rem; font-size:0.85rem; cursor:pointer; background: #e0f2fe; padding: 0.3rem 0.8rem; border-radius: 4px; color: #0369a1; font-weight: 600;">
                    <input type="checkbox" checked onchange="document.querySelectorAll('.ai-pred-col').forEach(el => el.style.display = this.checked ? '' : 'none')"> Show AI Predicted Marks
                </label>
            </div>'''
            
        main_content += f'''
        <form action="/subject/{id}/save/{step}" method="post">
            {prediction_toggle}
                <div class="marks-scroll" style="overflow-x: auto; max-width: 100%; border: 1px solid #e2e8f0; border-radius: 8px;">
                <table style="border-collapse: separate; border-spacing: 0; min-width: 100%;">
                    ''' + header_html + '''
                    ''' + students_html + '''
                </table>
                </div>
                <div class="mt-2" style="display:flex; gap:0.5rem;">
                    <button class="btn btn-primary">💾 Save Marks</button>
                    <a href="/subject/''' + str(id) + '''/delete_all/''' + str(step) + '''" class="btn btn-danger" onclick="return confirm('Delete ALL ''' + str(step.upper()) + ''' marks? This cannot be undone!')">🗑️ Delete All</a>
                </div>
            </form>
        </div>'''
    
    content = f'''
    <style>
        .mobile-only-links {{ display: none !important; }}
        .mobile-sidebar-overlay {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 999; opacity: 0; transition: opacity 0.3s ease; }}
        .mobile-sidebar-overlay.show {{ display: block; opacity: 1; }}
        
        @media (max-width: 768px) {{
            .navbar-links {{ display: none !important; }}
            .nav-subject-title {{ display: none !important; }}
        }}
        
        .sidebar {{
            position: fixed !important;
            left: -280px !important;
            top: 70px !important;
            bottom: 0 !important;
            width: 260px !important;
            height: calc(100vh - 70px) !important;
            z-index: 1000 !important;
            background: white !important;
            box-shadow: 2px 0 10px rgba(0,0,0,0.1) !important;
            transition: left 0.3s ease !important;
            overflow-y: auto !important;
            display: block !important;
        }}
        .sidebar.open {{ left: 0 !important; }}
        .main-with-sidebar {{ margin-left: 0 !important; padding: 1rem !important; width: 100% !important; }}
        
        @media (max-width: 768px) {{
            .mobile-only-links {{ display: flex !important; flex-direction: column; gap: 0.5rem; padding: 0 0.5rem; }}
        }}
        
        /* Blue theme for sidebar links */
        .sidebar .btn {{ border: none; background: transparent; color: #1e40af; text-align:left; }}
        .sidebar .btn:hover {{ background: #eff6ff; color: #1d4ed8; }}
        .sidebar .btn.active {{ background: #dbeafe; color: #1e3a8a; font-weight: 600; border-left: 4px solid #2563eb; }}
        .sidebar-link {{ color: #2563eb; font-weight: 500; }}
        .sidebar-link:hover {{ background: #eff6ff; color: #1d4ed8; }}
    </style>
    
    <div id="mobileOverlay" class="mobile-sidebar-overlay" onclick="toggleMobileSidebar()"></div>
    
    <div id="appSidebar" class="sidebar">
        <div style="display:flex; justify-content:space-between; align-items:flex-start; padding-bottom:1rem;">
            <div class="mb-2" style="padding: 0 0.5rem; margin-top:1rem; overflow-wrap: anywhere;">
                <small style="color: #3b82f6; text-transform: uppercase; font-weight: 600;">{subject["code"]}</small>
                <h3 style="margin: 0.25rem 0; color:#1e3a8a; font-size: 1.1rem; line-height: 1.3;">{subject["title"]}</h3>
                <span class="badge" style="background:#eff6ff; color:#1d4ed8; border:1px solid #bfdbfe; font-size:0.75rem; white-space: normal;">Sem {subject["sem_num"]} - Section {subject["sec_name"]}</span>
            </div>
            <button class="mobile-close-btn" onclick="toggleMobileSidebar()" style="display:none; background:none; border:none; font-size:1.8rem; color:#64748b; padding:1rem 0.5rem 0 0; cursor:pointer; line-height: 1;">×</button>
        </div>
        <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 0 0 1rem 0;">
        {sidebar}
        <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 1rem 0;">
        <div class="mobile-only-links">
            <a href="/faculty_dashboard" class="btn" style="display:flex; align-items:center; gap:0.5rem; padding:0.5rem; color:#334155; text-decoration:none;"><span style="font-size:1.2rem;">📊</span> Dashboard</a>
            <a href="/faculty/profile" class="btn" style="display:flex; align-items:center; gap:0.5rem; padding:0.5rem; color:#334155; text-decoration:none;"><span style="font-size:1.2rem;">⚙️</span> Settings / Profile</a>
            <a href="/logout" class="btn" style="display:flex; align-items:center; gap:0.5rem; padding:0.5rem; color:#ef4444; text-decoration:none;"><span style="font-size:1.2rem;">🚪</span> Logout</a>
        </div>
        <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 1rem 0;">
        <div style="padding: 0 0.5rem; text-align: center;">
            <a href="/faculty/ug/sem/{subject['semester_id']}" class="btn-back" style="display:inline-block; padding: 0.75rem 1rem; font-size: 0.95rem; line-height: 1.2; width: 100%;">⬅️ Back to Sections</a>
        </div>
    </div>
    
    <script>
        function toggleMobileSidebar() {{
            const sidebar = document.getElementById('appSidebar');
            const overlay = document.getElementById('mobileOverlay');
            sidebar.classList.toggle('open');
            if (sidebar.classList.contains('open')) {{
                overlay.classList.add('show');
                document.querySelector('.mobile-close-btn').style.display = 'block';
            }} else {{
                overlay.classList.remove('show');
            }}
        }}
    </script>
    '''
    
    prev_btn_html = f'<a href="{prev_step_url}" class="btn btn-sm" style="background:#e2e8f0; color:#475569; font-weight:600; text-decoration:none;">⬅ {prev_step_name}</a>' if prev_step_url else ''
    next_btn_html = f'<a href="{next_step_url}" class="btn btn-sm" style="background:#e2e8f0; color:#475569; font-weight:600; text-decoration:none;">{next_step_name} ➡</a>' if next_step_url else ''

    content += f'''
    <div class="main-with-sidebar">
        {{% with messages = get_flashed_messages() %}}{{% if messages %}}{{% for m in messages %}}<div class="alert alert-success">{{{{ m }}}}</div>{{% endfor %}}{{% endif %}}{{% endwith %}}

        
        <div style="display: flex; justify-content: flex-end; align-items: center; gap: 0.5rem; margin-bottom: 1rem;">
            {prev_btn_html}
            {next_btn_html}
            <div style="width: 1rem; border-left: 1px solid #cbd5e1; height: 1.5rem; margin: 0 0.25rem;"></div>
            <button class="btn btn-outline btn-sm" onclick="document.getElementById('addStudentForm').style.display='block'">+ Add Student</button>
        </div>
        
        <div id="addStudentForm" class="card mb-3 animate__animated animate__fadeIn" style="display: none; padding: 1rem; background: #f8fafc; border: 1px solid #e2e8f0;">
            <h4 style="margin-top: 0; color: #475569;">Add Student to Section</h4>
            <form action="/subject/{id}/add_student" method="post" style="display: flex; gap: 1rem; align-items: flex-end; flex-wrap: wrap;">
                <div style="flex: 1; min-width: 150px;">
                    <label class="form-label">USN</label>
                    <input type="text" name="usn" class="form-control" required placeholder="e.g. 1RV20CS001">
                </div>
                <div style="flex: 2; min-width: 200px;">
                    <label class="form-label">Name</label>
                    <input type="text" name="name" class="form-control" required placeholder="Student Name">
                </div>
                <input type="hidden" name="step" value="{step}">
                <div style="display: flex; gap: 0.5rem;">
                    <button type="submit" class="btn btn-primary">Add</button>
                    <button type="button" class="btn btn-outline" onclick="document.getElementById('addStudentForm').style.display='none'">Cancel</button>
                </div>
            </form>
        </div>
        
        {main_content}
        
    </div>'''
    
    nav_prepend = f'''
    <button onclick="toggleMobileSidebar()" style="background:none; border:none; font-size:1.8rem; color:#1e3a8a; cursor:pointer; padding: 0; line-height: 1; margin-right: 0.5rem; display:flex; align-items:center; z-index: 2;">☰</button>
    '''
    
    # Append the global VTU Filter Menu and Script to the content
    content += '''
    <!-- Global VTU Filter Modal Container -->
    <div id="vtuFilterMenu" style="display:none; position:absolute; background:white; border:1px solid #ccc; box-shadow:0 4px 6px rgba(0,0,0,0.1); border-radius:4px; z-index:100; padding:10px; min-width:200px; font-family:sans-serif;">
        <input type="text" id="vtuFilterSearch" placeholder="Search" style="width:100%; box-sizing:border-box; padding:4px; margin-bottom:8px; border:1px solid #6cb2eb; border-radius:2px;">
        <div style="margin-bottom:8px; border-bottom:1px solid #f0f0f0; padding-bottom:4px;">
            <label style="font-size:0.85rem;"><input type="checkbox" id="vtuFilterSelectAll" checked> Select all</label>
        </div>
        <div id="vtuFilterList" style="max-height:200px; overflow-y:auto; font-size:0.85rem;">
        </div>
        <div style="display:flex; justify-content:flex-end; gap:8px; margin-top:10px;">
            <button type="button" class="btn btn-sm btn-primary" style="padding:4px 12px; font-weight:bold; cursor:pointer;" onclick="vtuApplyFilter()">OK</button>
            <button type="button" class="btn btn-outline btn-sm" style="padding:4px 12px; cursor:pointer;" onclick="document.getElementById('vtuFilterMenu').style.display='none'">CANCEL</button>
        </div>
    </div>

    <script>
    let currentSortCol = 'USN';
    let currentSortAsc = true;
    let activeFilters = {}; // col -> set of allowed values
    let currentFilterCol = null;
    
    function getRowVal(row, col) {
        let td = row.querySelector(`td[data-col="${col}"]`);
        return td ? td.getAttribute('data-val') : '';
    }

    function vtuSort(thElem, colName) {
        if (currentSortCol === colName) {
            currentSortAsc = !currentSortAsc;
        } else {
            currentSortCol = colName;
            currentSortAsc = true;
        }
        
        // clear icons
        document.querySelectorAll('.vtu-header-row th .sort-icon').forEach(el => el.remove());
        // add icon
        let icon = document.createElement('span');
        icon.className = 'sort-icon';
        icon.style.cssText = 'font-size:0.6rem; color:#b45309; margin-left:4px;';
        icon.textContent = currentSortAsc ? '↑' : '↓';
        thElem.querySelector('div').appendChild(icon);

        let tbody = document.querySelector('.marks-scroll table tbody') || document.querySelector('.marks-scroll table');
        let rows = Array.from(tbody.querySelectorAll('tr.vtu-data-row'));
        
        rows.sort((a, b) => {
            let valA = getRowVal(a, colName);
            let valB = getRowVal(b, colName);
            
            // try numeric
            let numA = parseFloat(valA);
            let numB = parseFloat(valB);
            if (!isNaN(numA) && !isNaN(numB)) {
                return currentSortAsc ? numA - numB : numB - numA;
            }
            
            // string sort
            return currentSortAsc ? valA.localeCompare(valB) : valB.localeCompare(valA);
        });
        
        rows.forEach(r => tbody.appendChild(r));
    }
    
    function vtuToggleFilter(btnElem, colName) {
        let menu = document.getElementById('vtuFilterMenu');
        if (menu.style.display === 'block' && currentFilterCol === colName) {
            menu.style.display = 'none';
            return;
        }
        
        currentFilterCol = colName;
        
        // Collect unique values for this col
        let rows = Array.from(document.querySelectorAll('.marks-scroll table tr.vtu-data-row'));
        let uniqueVals = new Set();
        rows.forEach(r => {
            uniqueVals.add(getRowVal(r, colName));
        });
        let vals = Array.from(uniqueVals).sort();
        
        let listDiv = document.getElementById('vtuFilterList');
        listDiv.innerHTML = '';
        
        let allowedVals = activeFilters[colName] || new Set(vals);
        
        vals.forEach(v => {
            let div = document.createElement('div');
            div.style.marginBottom = '4px';
            let isChecked = allowedVals.has(v) ? 'checked' : '';
            div.innerHTML = `<label style="cursor:pointer;"><input type="checkbox" class="vtu-filter-cb" value="${v}" ${isChecked}> ${v}</label>`;
            listDiv.appendChild(div);
        });
        
        // Position menu
        let rect = btnElem.getBoundingClientRect();
        menu.style.left = rect.left + 'px';
        menu.style.top = (rect.bottom + window.scrollY + 5) + 'px';
        menu.style.display = 'block';
        
        // Handle search
        let searchInput = document.getElementById('vtuFilterSearch');
        searchInput.value = '';
        searchInput.oninput = (e) => {
            let term = e.target.value.toLowerCase();
            Array.from(listDiv.children).forEach(child => {
                let text = child.textContent.toLowerCase();
                child.style.display = text.includes(term) ? 'block' : 'none';
            });
        };
        
        // Handle select all
        let selectAllBtn = document.getElementById('vtuFilterSelectAll');
        selectAllBtn.checked = allowedVals.size === vals.length;
        selectAllBtn.onchange = (e) => {
            let checked = e.target.checked;
            Array.from(listDiv.querySelectorAll('.vtu-filter-cb')).forEach(cb => {
                if(cb.parentElement.parentElement.style.display !== 'none') {
                    cb.checked = checked;
                }
            });
        };
    }
    
    function vtuApplyFilter() {
        let cbs = document.querySelectorAll('.vtu-filter-cb');
        let allowed = new Set();
        cbs.forEach(cb => {
            if (cb.checked) allowed.add(cb.value);
        });
        
        if(allowed.size === cbs.length && document.getElementById('vtuFilterSearch').value === '') {
            delete activeFilters[currentFilterCol];
        } else {
            activeFilters[currentFilterCol] = allowed;
        }
        
        document.getElementById('vtuFilterMenu').style.display = 'none';
        vtuApplyAllFilters();
    }
    
    function vtuApplyAllFilters() {
        let rows = document.querySelectorAll('.marks-scroll table tr.vtu-data-row');
        
        // Color headers if active filter exists
        document.querySelectorAll('.vtu-header-row th').forEach(th => {
            let colMatch = th.getAttribute('onclick');
            if(!colMatch) return;
            let col = colMatch.split("'")[1];
            let btn = th.querySelector('.vtu-filter-btn');
            if(btn) {
                if(activeFilters[col]) {
                    btn.style.background = '#fb923c'; // darker orange to show active filter
                } else {
                    btn.style.background = '#eab308';
                }
            }
        });
        
        rows.forEach(r => {
            let show = true;
            for (let col in activeFilters) {
                let v = getRowVal(r, col);
                if (!activeFilters[col].has(v)) {
                    show = false;
                    break;
                }
            }
            
            // Keep zebra striping working nicely
            if(show) {
                r.style.display = '';
                r.style.background = ''; // reset to let css stripe it, though we might need js striping if nth-child gets messed up
            } else {
                r.style.display = 'none';
            }
        });
        
        // Fix zebra striping for visible rows
        let visibleRows = Array.from(rows).filter(r => r.style.display !== 'none');
        visibleRows.forEach((r, idx) => {
            r.style.background = (idx % 2 === 0) ? '#ffffff' : '#f8fafc';
        });
    }
    
    // click outside to close filter menu
    document.addEventListener('click', function(e) {
        let menu = document.getElementById('vtuFilterMenu');
        if(menu && menu.style.display === 'block') {
            if(!menu.contains(e.target) && !e.target.closest('.vtu-filter-btn')) {
                menu.style.display = 'none';
            }
        }
    });
    </script>
    '''
    
    return render_template_string(base_html(f'{subject["code"]} - CAB', content, nav_prepend=nav_prepend), subject=subject, students_with_marks=students_with_marks)

@app.route('/subject/<int:id>/add_student', methods=['POST'])
def subject_add_student(id):
    db = get_db()
    subject = db.execute('SELECT section_id FROM subjects WHERE id = %s', (id,)).fetchone()
    if not subject:
        return "Subject not found", 404
        
    usn = request.form.get('usn', '').strip().upper()
    name = request.form.get('name', '').strip()
    step = request.form.get('step', 'ia1')
    
    if usn and name:
        existing = db.execute('SELECT id FROM students WHERE usn = %s AND section_id = %s', (usn, subject['section_id'])).fetchone()
        if existing:
            flash(f'Student with USN {usn} already exists in this section.')
        else:
            db.execute('INSERT INTO students (section_id, usn, name) VALUES (%s, %s, %s)', (subject['section_id'], usn, name))
            db.commit()
            flash(f'Student {usn} ({name}) added successfully!')
            
    return redirect(url_for('subject_dashboard', id=id, step=step))

@app.route('/subject/<int:id>/save/<step>', methods=['POST'])
def save_marks(id, step):
    db = get_db()
    fac_name = session.get('user')
    
    # 1. Capture exact old state before saving
    old_marks_rows = db.execute('SELECT student_id, mark_type, value FROM marks WHERE subject_id=%s AND mark_type LIKE %s', (id, f"{step}%")).fetchall()
    old_data = json.dumps([dict(row) for row in old_marks_rows])
    
    for key in request.form:
        if key.startswith('sid_'):
            idx = key.split('_')[1]
            student_id = request.form.get(key)
            
            # --- Save Main Mark (val_{idx}) ---
            if f'val_{idx}' in request.form:
                value = request.form.get(f'val_{idx}', 0)
                try: value = float(value)
                except: value = 0.0
                
                existing = db.execute('SELECT id FROM marks WHERE student_id=%s AND subject_id=%s AND mark_type=%s', (student_id, id, step)).fetchone()
                if existing:
                    db.execute('UPDATE marks SET value=%s WHERE id=%s', (value, existing['id']))
                else:
                    db.execute('INSERT INTO marks (student_id, subject_id, mark_type, value) VALUES (%s, %s, %s, %s)', (student_id, id, step, value))
                    
            # --- Save 'MARKS SCORED' (val_scored_{idx}) if present in VTU UI ---
            if f'val_scored_{idx}' in request.form:
                scored_val = request.form.get(f'val_scored_{idx}', 0)
                try: scored_val = float(scored_val)
                except: scored_val = 0.0
                
                scored_mt = f"{step}_MARKS SCORED"
                existing = db.execute('SELECT id FROM marks WHERE student_id=%s AND subject_id=%s AND mark_type=%s', (student_id, id, scored_mt)).fetchone()
                if existing:
                    db.execute('UPDATE marks SET value=%s WHERE id=%s', (scored_val, existing['id']))
                else:
                    db.execute('INSERT INTO marks (student_id, subject_id, mark_type, value) VALUES (%s, %s, %s, %s)', (student_id, id, scored_mt, scored_val))
                    
    # 2. Capture new state and log
    new_marks_rows = db.execute('SELECT student_id, mark_type, value FROM marks WHERE subject_id=%s AND mark_type LIKE %s', (id, f"{step}%")).fetchall()
    new_data = json.dumps([dict(row) for row in new_marks_rows])
    
    cursor = db.execute('INSERT INTO audit_logs (faculty, action_type, entity_id, old_data, new_data) VALUES (%s, %s, %s, %s, %s) RETURNING id', (fac_name, 'SAVE_MARKS', id, old_data, new_data))
    log_id = cursor.fetchone()['id']
    
    subject = db.execute('SELECT code, title FROM subjects WHERE id=%s', (id,)).fetchone()
    sub_info = f"{subject['code']} - {subject['title']}" if subject else f"ID {id}"
    db.execute('INSERT INTO notifications (message, log_id) VALUES (%s, %s)', (f"Faculty <b>{fac_name}</b> saved {step.upper()} marks for <b>{sub_info}</b>.", log_id))
    
    db.commit()
    flash(f'{step.upper()} marks saved!')
    return redirect(url_for('subject_dashboard', id=id, step=step))

@app.route('/subject/<int:id>/delete_mark/<int:student_id>/<step>')
def delete_mark(id, student_id, step):
    """Delete a specific mark entry"""
    db = get_db()
    fac_name = session.get('user')
    
    # 1. Capture old state
    old_marks_rows = db.execute('SELECT student_id, mark_type, value FROM marks WHERE student_id=%s AND subject_id=%s AND mark_type LIKE %s', (student_id, id, f"{step}%")).fetchall()
    old_data = json.dumps([dict(row) for row in old_marks_rows])
    
    db.execute('DELETE FROM marks WHERE student_id=%s AND subject_id=%s AND mark_type LIKE %s', (student_id, id, f"{step}%"))
    
    # 2. Log deletion
    cursor = db.execute('INSERT INTO audit_logs (faculty, action_type, entity_id, old_data, new_data) VALUES (%s, %s, %s, %s, %s) RETURNING id', (fac_name, 'DELETE_MARK', id, old_data, '[]'))
    log_id = cursor.fetchone()['id']
    
    student = db.execute('SELECT usn, name FROM students WHERE id=%s', (student_id,)).fetchone()
    subject = db.execute('SELECT code, title FROM subjects WHERE id=%s', (id,)).fetchone()
    stu_info = f"{student['usn']} ({student['name']})" if student else f"ID {student_id}"
    sub_info = f"{subject['code']} - {subject['title']}" if subject else f"ID {id}"
    
    db.execute('INSERT INTO notifications (message, log_id) VALUES (%s, %s)', (f"Faculty <b>{fac_name}</b> deleted {step.upper()} marks for student <b>{stu_info}</b> in <b>{sub_info}</b>.", log_id))
    
    db.commit()
    flash(f'Mark deleted for student!')
    return redirect(url_for('subject_dashboard', id=id, step=step))

@app.route('/subject/<int:id>/delete_all/<step>')
def delete_all_marks(id, step):
    """Delete ALL marks for this step"""
    db = get_db()
    fac_name = session.get('user')
    
    # 1. Capture old state
    old_marks_rows = db.execute('SELECT student_id, mark_type, value FROM marks WHERE subject_id=%s AND mark_type LIKE %s', (id, f"{step}%")).fetchall()
    old_data = json.dumps([dict(row) for row in old_marks_rows])
    
    db.execute('DELETE FROM marks WHERE subject_id=%s AND mark_type LIKE %s', (id, f"{step}%"))
    
    # 2. Log deletion
    cursor = db.execute('INSERT INTO audit_logs (faculty, action_type, entity_id, old_data, new_data) VALUES (%s, %s, %s, %s, %s) RETURNING id', (fac_name, 'DELETE_ALL_MARKS', id, old_data, '[]'))
    log_id = cursor.fetchone()['id']
    
    subject = db.execute('SELECT code, title FROM subjects WHERE id=%s', (id,)).fetchone()
    sub_info = f"{subject['code']} - {subject['title']}" if subject else f"ID {id}"
    
    db.execute('INSERT INTO notifications (message, log_id) VALUES (%s, %s)', (f"Faculty <b class='text-danger'>{fac_name}</b> deleted ALL {step.upper()} marks for <b>{sub_info}</b>.", log_id))
    
    db.commit()
    flash(f'🗑️ Deleted all {step.upper()} marks!')
    return redirect(url_for('subject_dashboard', id=id, step=step))

@app.route('/subject/<int:id>/gaussian', methods=['POST'])
def gaussian_assign(id):
    """Assign SEE marks using Gaussian distribution with B+ grade as peak"""
    db = get_db()
    
    subject = db.execute('SELECT section_id FROM subjects WHERE id=%s', (id,)).fetchone()
    students = db.execute('SELECT * FROM students WHERE section_id=%s ORDER BY usn', (subject['section_id'],)).fetchall()
    
    # Get CIE scores for each student
    student_cie = []
    for s in students:
        cie_data = {}
        for mt in ['ia1', 'ia2', 'ia3', 'q1', 'q2', 'q3', 'a1', 'a2']:
            row = db.execute('SELECT value FROM marks WHERE student_id=%s AND subject_id=%s AND mark_type=%s', (s['id'], id, mt)).fetchone()
            cie_data[mt] = row['value'] if row else 0
        
        # Calculate CIE
        ias_sorted = sorted([cie_data['ia1'], cie_data['ia2'], cie_data['ia3']], reverse=True)
        quizzes_sorted = sorted([cie_data['q1'], cie_data['q2'], cie_data['q3']], reverse=True)
        cie = round((ias_sorted[0] + ias_sorted[1]) / 2, 1) + round((quizzes_sorted[0] + quizzes_sorted[1]) / 2 * 0.75, 1) + max(cie_data['a1'], cie_data['a2'])
        
        student_cie.append({'id': s['id'], 'usn': s['usn'], 'cie': cie})
    
    # Sort by CIE descending (best first)
    student_cie.sort(key=lambda x: x['cie'], reverse=True)
    n = len(student_cie)
    
    if n == 0:
        flash('No students found!')
        return redirect(url_for('subject_dashboard', id=id, step='see'))
    
    # Gaussian distribution percentages: B+ is peak
    # Target: O=5%, A+=10%, A=20%, B+=30%, B=20%, C=10%, D=5%
    # SEE marks to achieve grades (SEE is 50% of total, CIE is 50%)
    # Grade thresholds: O>=91, A+>=81, A>=71, B+>=61, B>=51, C>=40, D<40
    
    # For each student, calculate what SEE mark they need based on their rank and CIE
    gaussian_percentiles = [
        (0.05, 95),   # Top 5% -> target SEE 95 (for O grade)
        (0.15, 85),   # Next 10% -> target SEE 85 (for A+ grade)
        (0.35, 75),   # Next 20% -> target SEE 75 (for A grade)
        (0.65, 65),   # Next 30% -> target SEE 65 (for B+ grade - PEAK)
        (0.85, 55),   # Next 20% -> target SEE 55 (for B grade)
        (0.95, 45),   # Next 10% -> target SEE 45 (for C grade)
        (1.00, 35),   # Bottom 5% -> target SEE 35 (for D grade)
    ]
    
    count = 0
    for i, student in enumerate(student_cie):
        percentile = (i + 1) / n  # Position in class (0 = best)
        
        # Find the appropriate SEE mark based on percentile
        see_mark = 35  # Default
        for threshold, mark in gaussian_percentiles:
            if percentile <= threshold:
                see_mark = mark
                break
        
        # Add some randomization (+/- 5) for natural look
        import random
        see_mark = max(20, min(100, see_mark + random.randint(-5, 5)))
        
        # Save to database as 'see_gaussian' type
        existing = db.execute('SELECT id FROM marks WHERE student_id=%s AND subject_id=%s AND mark_type=%s', (student['id'], id, 'see_gaussian')).fetchone()
        if existing:
            db.execute('UPDATE marks SET value=%s WHERE id=%s', (see_mark, existing['id']))
        else:
            db.execute('INSERT INTO marks (student_id, subject_id, mark_type, value) VALUES (%s, %s, %s, %s)', (student['id'], id, 'see_gaussian', see_mark))
        count += 1
    
    db.commit()
    flash(f'Gaussian SEE marks assigned to {count} students! View in Report → Gaussian mode.')
    return redirect(url_for('subject_dashboard', id=id, step='see'))

@app.route('/subject/<int:id>/gaussian_cie', methods=['POST'])
def gaussian_cie_assign(id):
    """Assign marks to a CIE component using Gaussian distribution (B+ = peak)"""
    db = get_db()
    column = request.form.get('column', 'ia1')
    method = request.form.get('method', 'manual')  # 'ai' or 'manual'
    
    # Determine max marks for the column
    if column.startswith('ia'):
        max_marks = 25
    elif column.startswith('q'):
        max_marks = 20
    else:  # a1, a2
        max_marks = 10
    
    subject = db.execute('SELECT section_id FROM subjects WHERE id=%s', (id,)).fetchone()
    students = db.execute('SELECT id, usn, name FROM students WHERE section_id=%s ORDER BY usn', (subject['section_id'],)).fetchall()
    n = len(students)
    
    if n == 0:
        flash('No students found!')
        return redirect(url_for('subject_dashboard', id=id, step='cie_report'))
    
    # Gaussian distribution: B+ is peak
    # Percentages: O=5%, A+=10%, A=20%, B+=30%, B=20%, C=10%, D=5%
    # Scale to column's max marks
    # O = 90-100%, A+ = 80-90%, A = 70-80%, B+ = 60-70%, B = 50-60%, C = 40-50%, D = 0-40%
    
    # Build grade buckets based on class size
    # Build grade buckets based on class size
    # Force at least one O and one C if class size allows (e.g. > 5) to show full range
    
    assigned_grades = []
    
    if n >= 10:
        # For valid class sizes, ensure edges are populated
        grade_counts = {
            'O': max(1, int(round(n * 0.05))),
            'A+': int(round(n * 0.10)),
            'A': int(round(n * 0.20)),
            'B+': int(round(n * 0.25)),  # Reduced from 0.30 to make room for B
            'B': max(1, int(round(n * 0.20))),  # Force at least 1 B grade
            'C': max(1, int(round(n * 0.10))),
            'D': 0  # Avoid D for CIE usually
        }
    elif n >= 5:
        # Small class, force 1 O, 1 C to show range
        grade_counts = {'O': 1, 'A+': 1, 'A': 1, 'B+': 1, 'B': 1, 'C': 1, 'D': 0}
        # Fill rest with B+
    else:
        # Very small class
        grade_counts = {'O': 0, 'A+': 1, 'A': 1, 'B+': 1, 'B': 1, 'C': 0, 'D': 0}

    # Construct list
    for g, c in grade_counts.items():
        assigned_grades.extend([g] * c)
    
    # Fill remaining or truncate
    while len(assigned_grades) < n:
        assigned_grades.append('B+')
    assigned_grades = assigned_grades[:n]
    
    # Shuffle for randomness (so not sorted by USN)
    import random
    random.shuffle(assigned_grades)
    
    # Grade to marks range (as percentage of max_marks)
    grade_ranges = {
        'O': (0.90, 1.00),
        'A+': (0.80, 0.90),
        'A': (0.70, 0.80),
        'B+': (0.60, 0.70),
        'B': (0.50, 0.60),
        'C': (0.40, 0.50),
        'D': (0.20, 0.40)
    }
    
    # Use separate mark_type with suffix so original marks are preserved
    # e.g., ia1_ai, ia1_manual, q1_ai, q1_manual
    mark_type_suffix = '_ai' if method == 'ai' else '_manual'
    assigned_mark_type = column + mark_type_suffix
    
    count_updated = 0
    for i, student in enumerate(students):
        grade = assigned_grades[i]
        min_pct, max_pct = grade_ranges[grade]
        
        # Random value within the grade range (INTEGER ONLY)
        mark_value = int(round(random.uniform(min_pct * max_marks, max_pct * max_marks)))
        mark_value = max(0, min(max_marks, mark_value))
        
        # Save to database
        existing = db.execute('SELECT id FROM marks WHERE student_id=%s AND subject_id=%s AND mark_type=%s', 
                              (student['id'], id, assigned_mark_type)).fetchone()
        if existing:
            db.execute('UPDATE marks SET value=%s WHERE id=%s', (mark_value, existing['id']))
        else:
            db.execute('INSERT INTO marks (student_id, subject_id, mark_type, value) VALUES (%s, %s, %s, %s)', 
                       (student['id'], id, assigned_mark_type, mark_value))
        count_updated += 1
    
    db.commit()
    method_name = 'AI' if method == 'ai' else 'Manual'
    flash(f'✅ {method_name} Gaussian marks assigned to {count_updated} students for {column.upper()}! (Original marks preserved)')
    return redirect(url_for('subject_dashboard', id=id, step='cie_report'))


@app.route('/subject/<int:id>/gaussian_cie_multi', methods=['POST'])
def gaussian_cie_multi_assign(id):
    """Assign marks to multiple CIE components at once using Gaussian distribution"""
    import random
    db = get_db()
    columns = request.form.get('columns', '').split(',')
    columns = [c.strip() for c in columns if c.strip()]
    method = request.form.get('method', 'manual')
    
    if not columns:
        flash('⚠️ No columns selected!')
        return redirect(url_for('subject_dashboard', id=id, step='cie_report'))
    
    subject = db.execute('SELECT section_id FROM subjects WHERE id=%s', (id,)).fetchone()
    students = db.execute('SELECT id, usn, name FROM students WHERE section_id=%s ORDER BY usn', (subject['section_id'],)).fetchall()
    n = len(students)
    
    if n == 0:
        flash('No students found!')
        return redirect(url_for('subject_dashboard', id=id, step='cie_report'))
    
    grade_dist = [('O', 0.05), ('A+', 0.10), ('A', 0.20), ('B+', 0.30), ('B', 0.20), ('C', 0.10), ('D', 0.05)]
    grade_ranges = {'O': (0.90, 1.00), 'A+': (0.80, 0.90), 'A': (0.70, 0.80), 'B+': (0.60, 0.70), 'B': (0.50, 0.60), 'C': (0.40, 0.50), 'D': (0.20, 0.40)}
    mark_type_suffix = '_ai' if method == 'ai' else '_manual'
    
    total_updated = 0
    for column in columns:
        # Determine max marks
        if column.startswith('ia'):
            max_marks = 25
        elif column.startswith('q'):
            max_marks = 20
        else:
            max_marks = 10
        
        # Build grade buckets based on class size
        # Force at least one O and one C if class size allows (e.g. > 5) to show full range
        
        assigned_grades = []
        
        if n >= 10:
            # For valid class sizes, ensure edges are populated
            grade_counts = {
                'O': max(1, int(round(n * 0.05))),
                'A+': int(round(n * 0.10)),
                'A': int(round(n * 0.20)),
                'B+': int(round(n * 0.25)),  # Reduced from 0.30 to make room for B
                'B': max(1, int(round(n * 0.20))),  # Force at least 1 B grade
                'C': max(1, int(round(n * 0.10))),
                'D': 0  # Avoid D for CIE usually
            }
        elif n >= 5:
            # Small class, force 1 O, 1 C to show range
            grade_counts = {'O': 1, 'A+': 1, 'A': 1, 'B+': 1, 'B': 1, 'C': 1, 'D': 0}
            # Fill rest with B+
        else:
            # Very small class
            grade_counts = {'O': 0, 'A+': 1, 'A': 1, 'B+': 1, 'B': 1, 'C': 0, 'D': 0}
    
        # Construct list
        for g, c in grade_counts.items():
            assigned_grades.extend([g] * c)
        
        # Fill remaining or truncate
        while len(assigned_grades) < n:
            assigned_grades.append('B+')
        assigned_grades = assigned_grades[:n]
        
        random.shuffle(assigned_grades)
        
        assigned_mark_type = column + mark_type_suffix
        
        for i, student in enumerate(students):
            grade = assigned_grades[i]
            min_pct, max_pct = grade_ranges[grade]
            # INTEGER ONLY
            mark_value = int(round(random.uniform(min_pct * max_marks, max_pct * max_marks)))
            mark_value = max(0, min(max_marks, mark_value))
            
            existing = db.execute('SELECT id FROM marks WHERE student_id=%s AND subject_id=%s AND mark_type=%s', 
                                  (student['id'], id, assigned_mark_type)).fetchone()
            if existing:
                db.execute('UPDATE marks SET value=%s WHERE id=%s', (mark_value, existing['id']))
            else:
                db.execute('INSERT INTO marks (student_id, subject_id, mark_type, value) VALUES (%s, %s, %s, %s)', 
                           (student['id'], id, assigned_mark_type, mark_value))
            total_updated += 1
    
    db.commit()
    method_name = 'AI' if method == 'ai' else 'Manual'
    flash(f'✅ {method_name} Gaussian marks assigned for {len(columns)} columns ({total_updated} entries)! Original marks preserved.')
    return redirect(url_for('subject_dashboard', id=id, step='cie_report'))


@app.route('/subject/<int:id>/import/<step>', methods=['POST'])
def import_marks(id, step):
    # Handle file upload
    if 'file' in request.files and request.files['file'].filename != '':
        file = request.files['file']
        filename = secure_filename(file.filename)
        file_ext = os.path.splitext(filename)[1].lower()
        
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = os.path.join(temp_dir, filename)
            file.save(temp_path)
            
            prompt = f'Extract student marks data. Return ONLY JSON: [{{"usn": "...", "name": "...", "marks": 45}}]. Include all available columns like Q1, Q2, converted marks etc. No markdown.'
            try:
                if file_ext in ['.xlsx', '.xls']:
                    df = pd.read_excel(temp_path)
                    txt = get_gemini_response(prompt + "\nData:\n" + df.to_csv(index=False))
                else:
                    mime = 'application/pdf' if file_ext == '.pdf' else 'image/jpeg'
                    txt = get_gemini_response(prompt, file_path=temp_path, file_mime=mime)
                
                data = json.loads(txt.replace('```json', '').replace('```', '').strip())
                return show_import_preview(id, step, data)
            except Exception as e:
                flash(f'Import failed: {e}')
                return redirect(url_for('subject_dashboard', id=id, step=step))
    
    flash('No file provided')
    return redirect(url_for('subject_dashboard', id=id, step=step))

def parse_marks_manual(text):
    """Regex-based robust parser for copy-pasted marks (Row-based or Block-based)"""
    lines = text.split('\n')
    data = []
    
    current_entry = None
    
    # Regex for USN: Flexible 9-14 alphanumeric, MUST contain at least one digit to avoid matching words like "ASSIGNMENT"
    usn_pattern = r'\b(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{9,14}\b'
    
    # Header detection
    potential_headers = []
    headers_locked = False
    valid_headers = []
    
    for line in lines:
        line = line.strip()
        if not line: continue
        
        # Check if line contains USN
        usn_match = re.search(usn_pattern, line, re.IGNORECASE)
        
        if not usn_match:
            if not headers_locked:
                # Accumulate potential headers before first USN found
                # Filter out likely non-numeric-header tokens
                # Accumulate potential headers before first USN found
                # Filter out likely non-numeric-header tokens ONLY if they are isolated "SUBJECT" lines
                if 'SUBJECT' not in line.upper():
                    potential_headers.append(line)
        else:
            headers_locked = True
            # Once we hit first USN, lock headers.
            # Determine if Vertical or Horizontal headers
            
            # Heuristic: If we have many lines (e.g., > 3), and short lines -> Vertical
            # If few lines (1-2) -> Horizontal
            flat_headers = []
            
            if len(potential_headers) > 3:
                # Likely Vertical headers (one per line)
                flat_headers = [h.strip() for h in potential_headers]
            else:
                # Likely Horizontal headers (lines need splitting)
                for h_line in potential_headers:
                    # Split by Tab or multiple spaces
                    parts = re.split(r'\t|\s{2,}', h_line)
                    # If regex split fails to find parts, try simple space split if meaningful
                    if len(parts) == 1 and ' ' in h_line:
                         parts = h_line.split()
                    flat_headers.extend([p.strip() for p in parts if p.strip()])
            
            # Filter out non-numeric columns to align with 'temp_nums'
            # temp_nums only captures numbers. So headers like "USN", "Name" should be removed from alignment list
            valid_headers = [h for h in flat_headers if h.upper() not in ['USN', 'NAME', 'SL NO', 'SL.NO', 'NO.', 'STUDENT NAME', 'REMARKS']]
        
        # Filter out common headers/noise
        if usn_match and 'SUBJECT' not in line.upper():
            # If we were building an entry, save it
            if current_entry:
                # Convert collected numbers to columns
                for i, v in enumerate(current_entry['temp_nums']):
                    col_name = valid_headers[i] if i < len(valid_headers) else f'col_{i+1}'
                    current_entry[col_name] = v
                del current_entry['temp_nums']
                data.append(current_entry)
            
            usn = usn_match.group(0)
            current_entry = {'usn': usn, 'name': '', 'temp_nums': []}
            
            # Check for numbers/name on SAME line (Row format)
            remainder = line.replace(usn, '').strip()
            if remainder:
                # Extract numbers from remainder
                nums = re.findall(r'\b\d+(?:\.\d+)?\b', remainder)
                for n in nums:
                    if not n: continue
                    val = float(n) if '.' in n else int(n)
                    current_entry['temp_nums'].append(val)
                
                # If no numbers, assume remaining text is Name
                if not nums:
                    current_entry['name'] = remainder
                else:
                    # Name is likely text before first number
                    # This is tricky in regex, simple heuristic:
                    name_match = re.search(r'[A-Za-z .]{3,}', remainder)
                    if name_match:
                        current_entry['name'] = name_match.group(0).strip()
        
        elif current_entry:
            # No USN, but inside an entry. Parse numbers or name.
            # Look for numbers
            nums = re.findall(r'\b\d+(?:\.\d+)?\b', line)
            if nums:
                for n in nums:
                    if not n: continue
                    val = float(n) if '.' in n else int(n)
                    current_entry['temp_nums'].append(val)
            elif not current_entry['name'] and len(line) > 2 and 'SUBJECT' not in line.upper():
                # Assume it's a name line
                current_entry['name'] = line.strip()
    
    # Append last entry
    if current_entry:
        for i, v in enumerate(current_entry['temp_nums']):
            col_name = valid_headers[i] if i < len(valid_headers) else f'col_{i+1}'
            current_entry[col_name] = v
        del current_entry['temp_nums']
        data.append(current_entry)
    
    # Post-processing: Filter out noise (Subject Codes matching USN pattern)
    # 1. Subject Codes usually have NO numeric data on the same line or following lines?
    # In the sample, "UE23IS3501" had 0 numbers. Valid students had 15+ numbers.
    # Filter out entries with 0 numeric columns.
    
    valid_data = []
    
    # 2. Also check for duplicate USNs (Subject Code repeats every block)
    usn_counts = {}
    for row in data:
        u = row['usn']
        usn_counts[u] = usn_counts.get(u, 0) + 1
        
    for row in data:
        # Condition 1: Must have numeric columns (keys starting with col_ or matched headers)
        has_nums = any(isinstance(v, (int, float)) for k, v in row.items())
        
        # Condition 2: USN must be unique (frequency == 1)
        # Actually, sometimes duplicates happen for legitimate reasons? No, in a marks list USN is unique.
        # If duplicated, it's likely the Subject Code (which repeats 37 times).
        # But we must be careful: if a student appears twice?
        # Given the data format (Subject Code every block), the Subject Code appears N times.
        # Students appear 1 time.
        
        is_unique = usn_counts[row['usn']] == 1
        
        if has_nums and is_unique:
            valid_data.append(row)
            
    return valid_data

@app.route('/subject/<int:id>/parse/<step>', methods=['POST'])
def parse_text(id, step):
    """Parse pasted text using AI or Manual Regex"""
    text = request.form.get('paste_text', '')
    method = request.form.get('method', 'ai')
    
    if not text.strip():
        flash('No text provided')
        return redirect(url_for('subject_dashboard', id=id, step=step))
    
    if method == 'manual':
        data = parse_marks_manual(text)
        if not data:
            flash('Manual parse failed: No valid USN rows found')
            return redirect(url_for('subject_dashboard', id=id, step=step))
        return show_import_preview(id, step, data)
    
    prompt = f'''Parse this student marks data. The data may have columns like USN, NAME, QUIZ, MAX MARKS, MARKS SCORED, CONVERTED, Q1-Q12 etc.
Extract ALL available data columns and return as JSON array.
Return ONLY JSON: [{{"usn": "...", "name": "...", "marks": 45, "q1": 2, "q2": 1, ...}}]
Include all numeric columns found. No markdown.

Data:
{text}'''
    
    try:
        txt = get_gemini_response(prompt)
        data = json.loads(txt.replace('```json', '').replace('```', '').strip())
        return show_import_preview(id, step, data)
    except Exception as e:
        flash(f'AI Parse failed: {e}. Try Manual Parse.')
        return redirect(url_for('subject_dashboard', id=id, step=step))

def show_import_preview(id, step, data):
    """Show preview of extracted data with PRE-MATCHED student names"""
    db = get_db()
    subject = db.execute('SELECT sub.*, sem.number as sem_num, sec.name as sec_name FROM subjects sub JOIN semesters sem ON sub.semester_id = sem.id JOIN sections sec ON sub.section_id = sec.id WHERE sub.id = %s', (id,)).fetchone()
    
    if not data:
        flash('No data extracted')
        return redirect(url_for('subject_dashboard', id=id, step=step))
    
    # Get database students for matching
    db_students = db.execute('SELECT id, usn, name FROM students WHERE section_id=%s', (subject['section_id'],)).fetchall()
    
    def normalize_usn(u):
        return ''.join(str(u).upper().split())
    
    # Build student lookup (normalized USN -> {id, name, usn})
    student_lookup = {normalize_usn(s['usn']): {'id': s['id'], 'name': s['name'], 'usn': s['usn']} for s in db_students}
    student_list = list(student_lookup.values())
    
    # Pre-match each row
    matched_data = []
    for row in data:
        raw_usn = str(row.get('usn', '') or row.get('USN', '') or '')
        target_usn = normalize_usn(raw_usn)
        
        matched_student = None
        
        # 1. Exact Match
        if target_usn in student_lookup:
            matched_student = student_lookup[target_usn]
        
        # 2. Suffix Match (find best overlap)
        if not matched_student and target_usn:
            best_match_len = 0
            for s in db_students:
                db_usn = normalize_usn(s['usn'])
                # Find longest common suffix
                overlap = 0
                for k in range(1, min(len(db_usn), len(target_usn)) + 1):
                    if db_usn[-k:] == target_usn[-k:]:
                        overlap = k
                    else:
                        break
                if overlap > best_match_len and overlap >= 3:
                    best_match_len = overlap
                    matched_student = {'id': s['id'], 'name': s['name'], 'usn': s['usn']}
        
        # 3. NAME-BASED FUZZY MATCHING (Fallback when USN matching fails)
        if not matched_student:
            # Combine all potential name sources: 'name' field, 'usn' field (if no digits), any text field
            potential_names = []
            
            # Add name field if exists
            name_field = str(row.get('name', '') or '').upper().strip()
            if name_field and len(name_field) >= 3:
                potential_names.append(name_field)
            
            # Add usn field if it looks like a name (no digits or few digits)
            if target_usn:
                digit_count = sum(1 for c in target_usn if c.isdigit())
                if digit_count <= 2:  # Allow up to 2 digits (like initials "A2")
                    potential_names.append(target_usn.upper())
            
            # Try matching with each potential name
            for parsed_name in potential_names:
                best_score = 0
                
                # Normalize: remove all spaces for comparison
                parsed_name_nospace = parsed_name.replace(' ', '')
                
                for s in db_students:
                    db_name = s['name'].upper()
                    db_name_nospace = db_name.replace(' ', '')
                    score = 0
                    
                    # Method 0: SPACE-STRIPPED MATCH (highest priority)
                    # SRIHARIKA matches SRI HARIKA, DEEKSHITHA matches DEEKSHITH A
                    if parsed_name_nospace == db_name_nospace:
                        score += 20  # Perfect match without spaces
                    elif parsed_name_nospace in db_name_nospace or db_name_nospace in parsed_name_nospace:
                        score += 15  # Substring match without spaces
                    
                    # Method 1: Direct substring check
                    if parsed_name in db_name or db_name in parsed_name:
                        score += 10
                    
                    # Method 2: Word-by-word matching
                    name_words = [w for w in parsed_name.split() if len(w) >= 2]
                    db_words = [w for w in db_name.split() if len(w) >= 2]
                    
                    for pw in name_words:
                        for dw in db_words:
                            if pw in dw or dw in pw:
                                score += 3
                            elif len(set(pw) & set(dw)) >= min(len(pw), len(dw)) * 0.7:
                                score += 1
                    
                    if score > best_score:
                        best_score = score
                        matched_student = {'id': s['id'], 'name': s['name'], 'usn': s['usn']}
        
        # Store match info in row
        row['_student_id'] = matched_student['id'] if matched_student else None
        row['_db_name'] = matched_student['name'] if matched_student else '❌ NOT FOUND'
        row['_db_usn'] = matched_student['usn'] if matched_student else '-'
        row['_parsed_usn'] = target_usn  # Show what was parsed for debugging
        matched_data.append(row)
    
    # Get all available columns from data (excluding internal _fields)
    all_cols = set()
    for row in matched_data:
        all_cols.update([k for k in row.keys() if not k.startswith('_')])
    all_cols = sorted(list(all_cols))
    
    # Build table preview with DB Name column and Parsed USN
    rows_html = ''
    for i, row in enumerate(matched_data):
        status_class = 'style="background:#dcfce7;"' if row['_student_id'] else 'style="background:#fee2e2;"'
        cells = f'<td {status_class}><strong>{row["_db_name"]}</strong><br><small>DB: {row["_db_usn"]}</small><br><small style="color:#888;">Parsed: {row["_parsed_usn"]}</small></td>'
        cells += ''.join([f'<td>{row.get(c, "-")}</td>' for c in all_cols])
        rows_html += f'<tr>{cells}</tr>'
    
    # Store data in hidden field as JSON
    data_json = json.dumps(matched_data).replace('"', '&quot;')
    
    matched_count = sum(1 for r in matched_data if r['_student_id'])
    
    # Auto-detect best column
    best_col = ""
    for c in all_cols:
        if c.lower() in ["marks_scored", "marks scored", "marks", "converted", "total"]:
            best_col = c
            break
            
    # Auto-hide dropdown if best col is found
    if best_col:
        col_select_html = f'''
        <div class="mb-3" style="background: #f0fdf4; border: 1px solid #bbf7d0; padding: 10px 1rem; border-radius: 8px; display: flex; align-items: center; justify-content: space-between;">
            <div>
                <span class="badge badge-success mb-1">Auto-detected Column</span>
                <p style="margin:0; font-size: 0.9rem; color: #166534;">Using <strong>{best_col}</strong> for marks.</p>
                <input type="hidden" name="marks_column" value="{best_col}">
            </div>
            <button type="button" class="btn btn-outline btn-sm" onclick="document.getElementById('manualColSelect').style.display='block'; this.style.display='none';">Change</button>
        </div>
        <div id="manualColSelect" style="display: none; background: #f0f9ff; padding: 1rem; border-radius: 8px; border: 1px solid #bae6fd; margin-bottom: 1rem;">
            <label class="form-label">Change marks column to use:</label>
            <select name="marks_column_override" class="form-control">
                <option value="">-- Keep Auto-detected ({best_col}) --</option>
                {"".join([f'<option value="{c}">{c}</option>' for c in all_cols if c.lower() not in ['usn', 'name']])}
            </select>
        </div>
        '''
    else:
        col_select_html = f'''
        <div class="mb-3" style="background: #fffbeb; border: 1px solid #fde68a; padding: 1rem; border-radius: 8px;">
            <label class="form-label text-warning"><strong>⚠️ Cannot auto-detect column. Please select manually:</strong></label>
            <select name="marks_column" class="form-control" required>
                <option value="">-- Select column --</option>
                {"".join([f'<option value="{c}">{c}</option>' for c in all_cols if c.lower() not in ['usn', 'name']])}
            </select>
        </div>
        '''
    
    content = f'''
    <div style="padding: 1.5rem 2rem; width: 100%; max-width: 100%;">
        <div class="mb-3">
            <a href="/subject/{id}?step={step}" class="btn btn-outline">← Back to {step.upper()}</a>
            <h1 class="page-title mt-2">Preview Import - {step.upper()}</h1>
            <p class="page-subtitle">{subject['code']} - {subject['title']}</p>
        </div>
        
        <div class="card">
            <h3>📋 Pre-Matched Data</h3>
            <p class="text-muted mb-2">✅ Matched: <strong>{matched_count}</strong> / {len(matched_data)} students. Verify names before importing.</p>
            
            <details style="margin-bottom: 1rem; background: #fef3c7; padding: 0.5rem 1rem; border-radius: 8px;">
                <summary style="cursor: pointer; font-weight: 600;">🔍 Debug: View All {len(db_students)} Students in Database</summary>
                <div style="max-height: 200px; overflow-y: auto; margin-top: 0.5rem; font-size: 0.85rem;">
                    {"".join([f'<div style="padding: 0.25rem 0; border-bottom: 1px solid #e5e7eb;"><strong>{s["usn"]}</strong> - {s["name"]}</div>' for s in db_students])}
                </div>
                <p style="font-size: 0.8rem; color: #92400e; margin-top: 0.5rem;">⚠️ If a student shows NOT FOUND, check if their name appears here with a different spelling.</p>
            </details>
            
            <form action="/subject/{id}/confirm/{step}" method="post">
                <input type="hidden" name="data" value="{data_json}">
                
                {col_select_html}
                
                <div class="marks-scroll" style="margin-top: 1rem;">
                    <table>
                        <tr><th style="background:#f1f5f9; position: sticky; left: 0; z-index: 5;">Matched DB Student</th>{"".join([f"<th>{c}</th>" for c in all_cols])}</tr>
                        {rows_html}
                    </table>
                </div>
                
                <div class="mt-3" style="display: flex; gap: 1rem;">
                    <a href="/subject/{id}?step={step}" class="btn btn-outline">Cancel</a>
                    <button type="submit" class="btn btn-success">✓ Import ({matched_count} Matched)</button>
                </div>
            </form>
        </div>
    </div>'''
    
    return render_template_string(base_html(f'Import Preview - CAB', content))

@app.route('/subject/<int:id>/confirm/<step>', methods=['POST'])
def confirm_import(id, step):
    """Confirm and save marks using PRE-MATCHED student IDs"""
    data_str = request.form.get('data', '[]').replace('&quot;', '"')
    marks_column = request.form.get('marks_column', 'marks')
    
    try:
        data = json.loads(data_str)
    except:
        flash('Invalid data')
        return redirect(url_for('subject_dashboard', id=id, step=step))
    
    db = get_db()
    
    updated_count = 0
    inserted_count = 0
    skipped_count = 0
    
    for item in data:
        # Use pre-matched student_id from preview
        student_id = item.get('_student_id')
        
        if not student_id:
            skipped_count += 1
            continue
        
        # Use override if provided, else use auto-detected
        final_marks_column = request.form.get('marks_column_override')
        if not final_marks_column:
            final_marks_column = request.form.get('marks_column')
            
        for col, val in item.items():
            if col.startswith('_') or col.upper() in ['USN', 'NAME', 'SL NO']:
                continue
                
            try:
                numeric_val = float(val) if val else 0.0
            except:
                numeric_val = 0.0
                
            if col == final_marks_column:
                mark_type = step
            else:
                mark_type = f"{step}_{col.upper()}"
                
            existing = db.execute('SELECT id FROM marks WHERE student_id=%s AND subject_id=%s AND mark_type=%s', (student_id, id, mark_type)).fetchone()
            if existing:
                db.execute('UPDATE marks SET value=%s WHERE id=%s', (numeric_val, existing['id']))
                if col == final_marks_column: updated_count += 1
            else:
                db.execute('INSERT INTO marks (student_id, subject_id, mark_type, value) VALUES (%s, %s, %s, %s)', (student_id, id, mark_type, numeric_val))
                if col == final_marks_column: inserted_count += 1
    
    db.commit()
    flash(f'✅ Import Complete! Updated: {updated_count}, New: {inserted_count}' + (f', Skipped: {skipped_count}' if skipped_count else ''))
    return redirect(url_for('subject_dashboard', id=id, step=step))

@app.route('/subject/<int:id>/predict/<step>', methods=['POST'])
def predict_marks(id, step):
    method = request.form.get('method', 'ia1')
    db = get_db()
    
    subject = db.execute('SELECT section_id FROM subjects WHERE id=%s', (id,)).fetchone()
    students = db.execute('SELECT * FROM students WHERE section_id=%s ORDER BY usn', (subject['section_id'],)).fetchall()
    
    # Optimize: Fetch all marks in one query
    all_marks = db.execute('SELECT student_id, mark_type, value FROM marks WHERE subject_id=%s', (id,)).fetchall()
    marks_map = {(m['student_id'], m['mark_type']): m['value'] for m in all_marks}
    
    students_data = []
    for s in students:
        sd = {'id': s['id'], 'usn': s['usn'], 'name': s['name']}
        for mt in ['ia1', 'ia2', 'ia3', 'q1', 'q2', 'q3', 'a1', 'a2']:
            sd[mt] = marks_map.get((s['id'], mt), 0)
        students_data.append(sd)
    
    if method == 'ia1':
        input_desc, data_for_ai = "IA1 only", [{'usn': s['usn'], 'ia1': s['ia1']} for s in students_data]
    elif method == 'ia1_q1':
        input_desc, data_for_ai = "IA1 + Quiz1", [{'usn': s['usn'], 'ia1': s['ia1'], 'q1': s['q1']} for s in students_data]
    elif method == 'ia1_ia2':
        input_desc, data_for_ai = "IA1 + IA2", [{'usn': s['usn'], 'ia1': s['ia1'], 'ia2': s['ia2']} for s in students_data]
    elif method == 'ia1_ia2_q1_q2':
        input_desc, data_for_ai = "IAs + Quizzes", [{'usn': s['usn'], 'ia1': s['ia1'], 'ia2': s['ia2'], 'q1': s['q1'], 'q2': s['q2']} for s in students_data]
    else:
        input_desc, data_for_ai = "All assessments", [{'usn': s['usn'], 'ia1': s['ia1'], 'ia2': s['ia2'], 'ia3': s['ia3'], 'q1': s['q1'], 'q2': s['q2'], 'q3': s['q3'], 'a1': s['a1'], 'a2': s['a2']} for s in students_data]
    
    max_marks = 100 if step == 'see' else (25 if step.startswith('ia') else (20 if step.startswith('q') else 10))
    
    prompt = f"""Predict {step.upper()} marks (max {max_marks}) based on {input_desc}.
For each student provide predicted marks and a brief reason WHY.
Return ONLY JSON: [{{"usn": "...", "predicted": 42, "reason": "Strong performance..."}}]

Data: {json.dumps(data_for_ai)}"""
    
    try:
        txt = get_gemini_response(prompt)
        predictions = json.loads(txt.replace('```json', '').replace('```', '').strip())
    except Exception as e:
        print(f"AI Failed, switching to Statistical Fallback: {e}")
        # Statistical Fallback
        predictions = []
        for s in students_data:
            # Calculate simple average of available marks
            marks_list = [v for k, v in s.items() if k in ['ia1', 'ia2', 'ia3', 'q1', 'q2', 'q3', 'a1', 'a2'] and v > 0]
            avg = int(sum(marks_list) / len(marks_list)) if marks_list else 0
            
            # Predict based on max marks of current step
            pred_val = min(max_marks, avg)
            # If SEE (100), scale it
            if step == 'see': pred_val = int((avg / 20) * 100) if max_marks > 20 else avg # Rough heuristic
            
            predictions.append({
                "usn": s['usn'], 
                "predicted": pred_val, 
                "reason": "Statistical projection based on past performance (AI unavailable)"
            })
            
    try:
        usn_to_id = {s['usn'].upper(): s['id'] for s in students_data}
        
        for pred in predictions:
            usn = pred.get('usn', '').upper()
            if usn in usn_to_id:
                student_id = usn_to_id[usn]
                predicted = str(pred.get('predicted', 0))
                reason = pred.get('reason', '')
                existing = db.execute('SELECT id FROM marks WHERE student_id=%s AND subject_id=%s AND mark_type=%s', (student_id, id, step)).fetchone()
                if existing:
                    db.execute('UPDATE marks SET ai_prediction=%s, ai_reason=%s WHERE id=%s', (predicted, reason, existing['id']))
                else:
                    db.execute('INSERT INTO marks (student_id, subject_id, mark_type, value, ai_prediction, ai_reason) VALUES (%s, %s, %s, 0, %s, %s)', (student_id, id, step, predicted, reason))
        db.commit()
        flash('AI predictions generated!' if 'Statistical' not in str(predictions) else 'Predictions generated (Statistical Fallback)')
    except Exception as e:
        flash(f'Prediction failed: {e}')
    return redirect(url_for('subject_dashboard', id=id, step=step))

@app.route('/subject/<int:id>/gaussian', methods=['POST'])
def assign_gaussian_see(id):
    db = get_db()
    subject = db.execute('SELECT * FROM subjects WHERE id=%s', (id,)).fetchone()
    students = db.execute('SELECT id, usn FROM students WHERE section_id=%s', (subject['section_id'],)).fetchall()
    
    student_cie = []
    for s in students:
        row_vals = {}
        for mt in ['ia1', 'ia2', 'ia3', 'q1', 'q2', 'q3', 'a1', 'a2']:
            m = db.execute('SELECT value FROM marks WHERE student_id=%s AND subject_id=%s AND mark_type=%s', (s['id'], id, mt)).fetchone()
            row_vals[mt] = m['value'] if m else 0
        
        ia_comp = math.ceil((row_vals['ia1'] + row_vals['ia2'] + row_vals['ia3']) / 3)
        q_comp = math.ceil(((row_vals['q1'] + row_vals['q2'] + row_vals['q3']) / 3 / 20) * 15)
        a_comp = math.ceil(row_vals['a1'] + row_vals['a2'])
        cie = int(ia_comp + q_comp + a_comp)
        student_cie.append({'id': s['id'], 'cie': cie})
    
    student_cie.sort(key=lambda x: x['cie'], reverse=True)
    total_students = len(student_cie)
    
    if total_students == 0:
        flash('No students to assign!')
        return redirect(url_for('subject_dashboard', id=id, step='see'))

    counts = {
        'O': max(1, int(total_students * 0.05)),
        'A+': max(1, int(total_students * 0.10)),
        'A': max(1, int(total_students * 0.20)),
        'B+': max(1, int(total_students * 0.25)),
        'B': max(1, int(total_students * 0.20)),
        'C': max(1, int(total_students * 0.10)),
        'D': max(1, int(total_students * 0.05))
    }
    
    assigned_grades = []
    current_idx = 0
    
    import random
    
    for grade in ['O', 'A+', 'A', 'B+', 'B', 'C', 'D']:
        count = counts[grade]
        for _ in range(count):
            if current_idx < total_students:
                assigned_grades.append(grade)
                current_idx += 1
    
    # Fill remainder with B
    while current_idx < total_students:
        assigned_grades.append('B')
        current_idx += 1
    
    while len(assigned_grades) < total_students:
        assigned_grades.append('C')
    
    ranges = {'O': (91, 100), 'A+': (81, 90), 'A': (71, 80), 'B+': (61, 70), 'B': (50, 60), 'C': (35, 49)}
    
    count_updated = 0
    for i, s in enumerate(student_cie):
        target_grade = assigned_grades[i]
        min_tot, max_tot = ranges[target_grade]
        target_total = random.randint(min_tot, max_tot)
        req_see_scaled = target_total - s['cie']
        req_see_raw = int(req_see_scaled / 0.4)
        req_see_raw = max(0, min(100, req_see_raw))
        
        existing = db.execute('SELECT id FROM marks WHERE student_id=%s AND subject_id=%s AND mark_type=%s', (s['id'], id, 'see_gaussian')).fetchone()
        if existing:
            db.execute('UPDATE marks SET value=%s WHERE id=%s', (req_see_raw, existing['id']))
        else:
            db.execute('INSERT INTO marks (student_id, subject_id, mark_type, value) VALUES (%s, %s, %s, %s)', (s['id'], id, 'see_gaussian', req_see_raw))
        count_updated += 1
        
    db.commit()
    flash(f'Assigned Gaussian SEE marks to {count_updated} students!')
    return redirect(url_for('subject_dashboard', id=id, step='see'))

# === PG ROUTES ===

@app.route('/pg')
def pg_home():
    """PG Program selection"""
    db = get_db()
    total_students = db.execute('SELECT COUNT(*) FROM pg_students').fetchone()[0]
    total_batches = db.execute('SELECT COUNT(*) FROM pg_batches').fetchone()[0]
    
    content = f'''
    <div class="container">
        <div class="mb-3">
            <h1 class="page-title mt-2">📊 Postgraduate Programs</h1>
            <p class="page-subtitle">Select a program</p>
        </div>
        <div class="grid grid-3" style="max-width: 900px;">
            <a href="/pg/de" class="card card-clickable sem-tile" style="background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); color: white;">
                <h2 style="font-size: 3rem; margin-bottom: 0.5rem;">📊</h2>
                <h3 style="color: white; margin: 0;">Data Engineering</h3>
                <p style="color: rgba(255,255,255,0.9); margin: 0.5rem 0; font-size: 0.9rem;">M.Tech</p>
                <p style="color: rgba(255,255,255,0.8); font-size: 0.85rem;">2 Years</p>
                <div class="mt-2" style="font-size: 0.85rem;">
                    <span class="badge" style="background: rgba(255,255,255,0.2); color: white;">{total_batches} Batches</span>
                    <span class="badge" style="background: rgba(255,255,255,0.2); color: white;">{total_students} Students</span>
                </div>
            </a>
        </div>
        <div style="text-align: center; margin-top: 3rem; margin-bottom: 2rem;">
            <a href="/" class="btn" style="background: linear-gradient(135deg, #4361ee, #3a0ca3); color: white; border-radius: 50px; padding: 0.8rem 2.5rem; font-size: 1.1rem; box-shadow: 0 4px 15px rgba(67, 97, 238, 0.4); text-decoration: none; display: inline-block; transition: transform 0.2s;">
                🏠 Back to Home
            </a>
        </div>
    </div>'''
    return base_html('PG Programs - CAB', content)

@app.route('/pg/de')
def pg_de_batches():
    """PG Data Engineering - Batch selection"""
    db = get_db()
    batches = db.execute('SELECT * FROM pg_batches WHERE program = "Data Engineering" ORDER BY start_year ASC').fetchall()
    
    batch_tiles = ''
    for b in batches:
        student_count = db.execute('SELECT COUNT(*) FROM pg_students WHERE batch_id = %s', (b['id'],)).fetchone()[0]
        module_count = db.execute('SELECT COUNT(*) FROM pg_modules WHERE batch_id = %s', (b['id'],)).fetchone()[0]
        batch_tiles += f'''<div class="card sem-tile" style="position: relative;">
            <a href="/pg/de/batch/{b['id']}" style="text-decoration: none; color: inherit; display: block;">
                <h2 style="font-size: 2rem; color: var(--primary);">📅</h2>
                <h3>{b['start_year']}-{b['end_year']}</h3>
                <div class="mt-2" style="font-size: 0.85rem;">
                    <span class="badge badge-success">{student_count} Students</span>
                    <span class="badge badge-primary">{module_count} Modules</span>
                </div>
            </a>
            <a href="/pg/batch/{b['id']}/edit_year" class="btn btn-outline btn-sm" style="position: absolute; top: 10px; right: 10px; padding: 0.25rem 0.5rem; font-size: 0.75rem;">✏️</a>
        </div>'''
    
    content = f'''
    <div class="container">
        <div class="mb-3">
            <h1 class="page-title mt-2">📊 Data Engineering - M.Tech</h1>
            <p class="page-subtitle">Select a batch or create new</p>
        </div>
        
        {{% with messages = get_flashed_messages() %}}{{% if messages %}}{{% for m in messages %}}<div class="alert alert-success">{{{{ m }}}}</div>{{% endfor %}}{{% endif %}}{{% endwith %}}
        
        <div class="grid grid-4">{batch_tiles}
            <div class="card sem-tile" style="border: 2px dashed #cbd5e1; background: transparent;">
                <form action="/pg/de/batch/add" method="post">
                    <h4 class="text-muted">+ Add Batch</h4>
                    <div style="display: flex; gap: 0.5rem; align-items: center; margin-top: 1rem;">
                        <input type="number" name="start_year" placeholder="2025" class="form-control" style="width: 80px;" required min="2020" max="2050">
                        <span>-</span>
                        <input type="number" name="end_year" placeholder="2027" class="form-control" style="width: 80px;" required min="2022" max="2052">
                    </div>
                    <button class="btn btn-primary btn-sm mt-2">Create</button>
                </form>
            </div>
        </div>
        <div style="text-align: center; margin-top: 3rem; margin-bottom: 2rem;">
            <a href="/pg" class="btn-back">⬅️ Back to PG</a>
        </div>
    </div>'''
    return render_template_string(base_html('Data Engineering - CAB', content))

@app.route('/pg/de/batch/add', methods=['POST'])
def pg_add_batch():
    db = get_db()
    start_year = request.form.get('start_year')
    end_year = request.form.get('end_year')
    try:
        db.execute('INSERT INTO pg_batches (program, start_year, end_year) VALUES (%s, %s, %s)', ('Data Engineering', start_year, end_year))
        db.commit()
        flash(f'Batch {start_year}-{end_year} created!')
    except:
        flash('Batch already exists!')
    return redirect(url_for('pg_de_batches'))

@app.route('/pg/de/batch/<int:batch_id>')
def pg_batch_view(batch_id):
    """PG Batch view - shows Year 1 and Year 2 cards + student management"""
    db = get_db()
    batch = db.execute('SELECT * FROM pg_batches WHERE id = %s', (batch_id,)).fetchone()
    if not batch:
        return "Batch not found", 404
    
    # Determine scheme
    scheme_year = batch['start_year'] - 1
    scheme_name = f"{scheme_year} Scheme"
    
    # Repurposed 'year' column as 'semester'
    sem_counts = []
    for s in range(1, 5):
        count = db.execute('SELECT COUNT(*) FROM pg_modules WHERE batch_id = %s AND year = %s', (batch_id, s)).fetchone()[0]
        sem_counts.append(count)
    
    students = db.execute('SELECT * FROM pg_students WHERE batch_id = %s ORDER BY usn', (batch_id,)).fetchall()
    
    student_rows = ''
    for i, st in enumerate(students):
        student_rows += f'<tr><td>{i+1}</td><td><strong>{st["usn"]}</strong></td><td>{st["name"]}</td><td><a href="/pg/student/delete/{st["id"]}?batch_id={batch_id}" class="btn btn-outline btn-sm" onclick="return confirm(\'Delete?\')">×</a></td></tr>'
    if not students:
        student_rows = '<tr><td colspan="4" class="text-center text-muted">No students yet</td></tr>'
    
    content = f'''
    <div class="container">
        <div class="mb-3">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <div>
                    <h1 class="page-title mt-2">📊 Batch {batch['start_year']}-{batch['end_year']}</h1>
                    <span class="badge" style="background: #4f46e5; color: white; padding: 0.5rem 1rem; font-size: 1rem;">✨ {scheme_name}</span>
                </div>
                <form action="/pg/batch/{batch_id}/init_modules" method="post" style="display:inline;">
                    <button class="btn btn-outline" type="button" onclick="customConfirmForm(event, 'Initialize standard {scheme_name} modules for Sem 1-4? This will not delete existing modules.', this.closest('form'))">⚙️ Initialize Standard Modules</button>
                </form>
            </div>
            <p class="page-subtitle mt-2">Select a semester to manage modules</p>
        </div>
        
        {{% with messages = get_flashed_messages() %}}{{% if messages %}}{{% for m in messages %}}<div class="alert alert-success">{{{{ m }}}}</div>{{% endfor %}}{{% endif %}}{{% endwith %}}
        
        <div class="grid grid-4" style="margin-bottom: 2rem;">
            <a href="/pg/de/batch/{batch_id}/year/1" class="card card-clickable sem-tile" style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white;">
                <h2 style="font-size: 2.5rem; color: white;">1</h2>
                <h3 style="color: white; font-size: 1.2rem;">Semester 1</h3>
                <span class="badge" style="background: rgba(255,255,255,0.2); color: white;">{sem_counts[0]} Modules</span>
            </a>
            <a href="/pg/de/batch/{batch_id}/year/2" class="card card-clickable sem-tile" style="background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); color: white;">
                <h2 style="font-size: 2.5rem; color: white;">2</h2>
                <h3 style="color: white; font-size: 1.2rem;">Semester 2</h3>
                <span class="badge" style="background: rgba(255,255,255,0.2); color: white;">{sem_counts[1]} Modules</span>
            </a>
            <a href="/pg/de/batch/{batch_id}/year/3" class="card card-clickable sem-tile" style="background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%); color: white;">
                <h2 style="font-size: 2.5rem; color: white;">3</h2>
                <h3 style="color: white; font-size: 1.2rem;">Semester 3</h3>
                <span class="badge" style="background: rgba(255,255,255,0.2); color: white;">{sem_counts[2]} Modules</span>
            </a>
            <a href="/pg/de/batch/{batch_id}/year/4" class="card card-clickable sem-tile" style="background: linear-gradient(135deg, #43e97b 0%, #38f9d7 100%); color: white;">
                <h2 style="font-size: 2.5rem; color: white;">4</h2>
                <h3 style="color: white; font-size: 1.2rem;">Semester 4</h3>
                <span class="badge" style="background: rgba(255,255,255,0.2); color: white;">{sem_counts[3]} Modules</span>
            </a>
        </div>
        
        <div class="card">
            <h3>📋 Students ({len(students)})</h3>
            <table><tr><th>#</th><th>USN</th><th>Name</th><th></th></tr>{student_rows}</table>
            <form action="/pg/student/add" method="post" class="mt-2" style="display: flex; gap: 0.5rem;">
                <input type="hidden" name="batch_id" value="{batch_id}">
                <input type="text" name="usn" class="form-control" placeholder="USN" required style="flex: 1;">
                <input type="text" name="name" class="form-control" placeholder="Name" required style="flex: 2;">
                <button class="btn btn-success btn-sm">+ Add</button>
            </form>
            <form action="/pg/students/import" method="post" enctype="multipart/form-data" class="mt-2" style="display: flex; gap: 0.5rem; align-items: center;">
                <input type="hidden" name="batch_id" value="{batch_id}">
                <span style="font-size: 0.85rem; color: #6b7280;">⚡ AI Import:</span>
                <input type="file" name="file" class="form-control" style="flex: 1;">
                <button class="btn btn-primary btn-sm">Upload</button>
            </form>
            <div class="mt-3" style="background: #f0f9ff; padding: 1rem; border-radius: 10px; border: 1px solid #bae6fd;">
                <h4 style="color: #0369a1; margin-bottom: 0.5rem;">📋 Paste Student List</h4>
                <p style="font-size: 0.85rem; color: #6b7280; margin-bottom: 0.5rem;">Format: USN[Tab]Name (one per line)</p>
                <form action="/pg/students/parse" method="post">
                    <input type="hidden" name="batch_id" value="{batch_id}">
                    <textarea name="paste_text" class="form-control" rows="4" placeholder="P25E03DE001&#9;AMRUTHA M KOTI&#10;P25E03DE002&#9;ANUSHA A NADIGER"></textarea>
                    <button class="btn btn-success btn-sm mt-2">⚡ Parse & Import</button>
                </form>
            </div>
        </div>
        <div style="text-align: center; margin-top: 3rem; margin-bottom: 2rem;">
            <a href="/pg/de" class="btn-back">⬅️ Back to Batches</a>
        </div>
    </div>'''
    return render_template_string(base_html(f'Batch {batch["start_year"]}-{batch["end_year"]} - {scheme_name} - CAB', content))

@app.route('/pg/student/add', methods=['POST'])
def pg_add_student():
    db = get_db()
    batch_id = request.form['batch_id']
    usn = request.form['usn'].strip().upper()
    name = request.form['name'].strip()
    db.execute('INSERT INTO pg_students (batch_id, usn, name) VALUES (%s, %s, %s)', (batch_id, usn, name))
    db.commit()
    return redirect(url_for('pg_batch_view', batch_id=batch_id))

@app.route('/pg/student/delete/<int:id>')
def pg_delete_student(id):
    batch_id = request.args.get('batch_id', 1)
    db = get_db()
    db.execute('DELETE FROM pg_marks WHERE student_id = %s', (id,))
    db.execute('DELETE FROM pg_students WHERE id = %s', (id,))
    db.commit()
    return redirect(url_for('pg_batch_view', batch_id=batch_id))

@app.route('/pg/batch/<int:batch_id>/edit_year', methods=['GET', 'POST'])
def pg_edit_year(batch_id):
    """Edit the academic year for a PG batch"""
    db = get_db()
    batch = db.execute('SELECT * FROM pg_batches WHERE id = %s', (batch_id,)).fetchone()
    if not batch:
        flash('Batch not found')
        return redirect(url_for('pg_home'))
    
    if request.method == 'POST':
        start_year = request.form.get('start_year', batch['start_year'])
        end_year = request.form.get('end_year', batch['end_year'])
        try:
            db.execute('UPDATE pg_batches SET start_year = %s, end_year = %s WHERE id = %s', 
                      (int(start_year), int(end_year), batch_id))
            db.commit()
            flash(f'✅ Academic year updated to {start_year}-{end_year}!')
        except Exception as e:
            flash(f'Error updating year: {e}')
        return redirect(url_for('pg_batch_view', batch_id=batch_id))
    
    # GET - show edit form
    content = f'''
    <div class="container">
        <div class="card" style="max-width: 500px; margin: 2rem auto;">
            <h2 style="margin-bottom: 1.5rem;">✏️ Edit Academic Year</h2>
            <form method="post">
                <div class="mb-3">
                    <label class="form-label">Start Year</label>
                    <input type="number" name="start_year" class="form-control" value="{batch['start_year']}" min="2020" max="2050" required>
                </div>
                <div class="mb-3">
                    <label class="form-label">End Year</label>
                    <input type="number" name="end_year" class="form-control" value="{batch['end_year']}" min="2020" max="2050" required>
                </div>
                <div style="display: flex; gap: 1rem;">
                    <button type="submit" class="btn btn-primary">💾 Save</button>
                    <a href="/pg/de/batch/{batch_id}" class="btn btn-outline">Cancel</a>
                </div>
            </form>
        </div>
    </div>'''
    return render_template_string(base_html('Edit Year - CAB', content))


@app.route('/pg/students/import', methods=['POST'])
def pg_import_students():
    batch_id = request.form.get('batch_id')
    if 'file' not in request.files or not batch_id:
        flash('Please provide file')
        return redirect(url_for('pg_batch_view', batch_id=batch_id))
    file = request.files['file']
    if file.filename == '':
        return redirect(url_for('pg_batch_view', batch_id=batch_id))
    filename = secure_filename(file.filename)
    file_ext = os.path.splitext(filename)[1].lower()
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = os.path.join(temp_dir, filename)
        file.save(temp_path)
        prompt = 'Extract student data. Return ONLY JSON array: [{"usn": "...", "name": "..."}]. No markdown.'
        try:
            if file_ext in ['.xlsx', '.xls']:
                df = pd.read_excel(temp_path)
                txt = get_gemini_response(prompt + "\nData:\n" + df.to_csv(index=False))
            else:
                mime = 'application/pdf' if file_ext == '.pdf' else 'image/jpeg'
                txt = get_gemini_response(prompt, file_path=temp_path, file_mime=mime)
            data = json.loads(txt.replace('```json', '').replace('```', '').strip())
            db = get_db()
            count = 0
            for item in data:
                if item.get('usn') and item.get('name'):
                    db.execute('INSERT INTO pg_students (batch_id, usn, name) VALUES (%s, %s, %s)', (batch_id, item['usn'].upper(), item['name']))
                    count += 1
            db.commit()
            flash(f'Imported {count} students!')
        except Exception as e:
            flash(f'Import failed: {e}')
    return redirect(url_for('pg_batch_view', batch_id=batch_id))

@app.route('/pg/students/parse', methods=['POST'])
def pg_parse_students():
    """Parse pasted student list in tab-separated format: USN[Tab]Name"""
    batch_id = request.form.get('batch_id')
    text = request.form.get('paste_text', '')
    
    if not text.strip() or not batch_id:
        flash('Please paste student list')
        return redirect(url_for('pg_batch_view', batch_id=batch_id))
    
    db = get_db()
    count = 0
    lines = text.strip().split('\n')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Try tab-separated first, then whitespace
        if '\t' in line:
            parts = line.split('\t', 1)
        else:
            # Split on multiple spaces or first space
            parts = re.split(r'\s{2,}|\t', line, maxsplit=1)
            if len(parts) == 1:
                parts = line.split(' ', 1)
        
        if len(parts) >= 2:
            usn = parts[0].strip().upper()
            name = parts[1].strip()
            
            # Skip header rows
            if usn.upper() in ['USN', 'REG', 'REGISTRATION', '_USN', 'REGISTER']:
                continue
            
            # Remove leading underscore if present
            if usn.startswith('_'):
                usn = usn[1:]
            
            if usn and name:
                try:
                    db.execute('INSERT INTO pg_students (batch_id, usn, name) VALUES (%s, %s, %s)', (batch_id, usn, name))
                    count += 1
                except:
                    pass  # Skip duplicates
    
    db.commit()
    flash(f'Imported {count} students!')
    return redirect(url_for('pg_batch_view', batch_id=batch_id))

@app.route('/pg/de/batch/<int:batch_id>/year/<int:year>')
def pg_year_view(batch_id, year):
    """PG Year view - shows modules"""
    db = get_db()
    batch = db.execute('SELECT * FROM pg_batches WHERE id = %s', (batch_id,)).fetchone()
    if not batch:
        return "Batch not found", 404
    modules = db.execute('SELECT * FROM pg_modules WHERE batch_id = %s AND year = %s ORDER BY code', (batch_id, year)).fetchall()
    module_rows = ''
    for m in modules:
        module_rows += f'<tr><td><strong>{m["code"]}</strong></td><td>{m["title"]}</td><td>{m["faculty"] or "-"}</td><td><a href="/pg/module/{m["id"]}" class="btn btn-primary btn-sm">Marks</a> <a href="/pg/module/edit/{m["id"]}" class="btn btn-warning btn-sm" style="color:white;">✏️</a> <a href="/pg/module/delete/{m["id"]}?batch_id={batch_id}&year={year}" class="btn btn-outline btn-sm" style="color:#ef4444; border-color:#ef4444;" onclick="return confirm(\'Delete module {m["code"]}?\')">🗑️</a></td></tr>'
    if not modules:
        module_rows = '<tr><td colspan="4" class="text-center text-muted">No modules yet</td></tr>'
    content = f'''
    <div class="container">
        <div class="mb-3">
            <h1 class="page-title mt-2">📊 Semester {year} - Batch {batch['start_year']}-{batch['end_year']}</h1>
            <p class="page-subtitle">Manage modules</p>
        </div>
        {{% with messages = get_flashed_messages() %}}{{% if messages %}}{{% for m in messages %}}<div class="alert alert-success">{{{{ m }}}}</div>{{% endfor %}}{{% endif %}}{{% endwith %}}
        <div class="card">
            <h3>📚 Modules</h3>
            <table><tr><th>Code</th><th>Title</th><th>Faculty</th><th>Actions</th></tr>{module_rows}</table>
            <form action="/pg/module/add" method="post" class="mt-3">
                <input type="hidden" name="batch_id" value="{batch_id}">
                <input type="hidden" name="year" value="{year}">
                <div class="grid grid-4" style="gap: 1rem; align-items: end;">
                    <div><label class="form-label">Code</label><input type="text" name="code" class="form-control" placeholder="PE25DE..." required></div>
                    <div><label class="form-label">Title</label><input type="text" name="title" class="form-control" placeholder="Module Title" required></div>
                    <div><label class="form-label">Faculty</label><input type="text" name="faculty" class="form-control" placeholder="Dr. Smith"></div>
                    <div><button class="btn btn-primary">+ Add Module</button></div>
                </div>
            </form>
        </div>
        <div style="text-align: center; margin-top: 3rem; margin-bottom: 2rem;">
            <a href="/pg/de/batch/{batch_id}" class="btn-back">⬅️ Back to Batch</a>
        </div>
    </div>'''
    return render_template_string(base_html(f'Semester {year} - CAB', content))

@app.route('/pg/batch/<int:batch_id>/init_modules', methods=['POST'])
def pg_init_modules(batch_id):
    """Initialize standard modules for 24/25 Scheme"""
    db = get_db()
    batch = db.execute('SELECT start_year FROM pg_batches WHERE id=%s', (batch_id,)).fetchone()
    if not batch: return redirect(url_for('pg_home'))
    
    scheme_year = batch['start_year'] - 1
    # Use 2-digit year for prefix (e.g., 2024 -> 24)
    scheme_short = str(scheme_year)[-2:]
    prefix = f'PE{scheme_short}DE'
    
    # Auto-fix: Replace existing PE20xx codes with PExx values if they exist
    # This corrects the PE2024 -> PE24 mistake
    try:
        db.execute("UPDATE pg_modules SET code = REPLACE(code, 'PE20', 'PE') WHERE batch_id = %s AND code LIKE 'PE20%'", (batch_id,))
    except: pass
    
    # Standard Modules Mapping (Sem: [(CodeSuffix, Title)])
    # Note: Using User provided titles
    standard_modules = {
        1: [
            ('5101', 'A Study of Data Life Cycle'),
            ('5102', 'Data Crafting'),
            ('5103', 'Data Storage and Retrieval'),
            ('5104', 'Refining Data')
        ],
        2: [
            ('5201', 'Data Accessibility'),
            ('5202', 'Data Prediction and Decision Making'),
            ('5203', 'Data Analytics Application and Algorithms'),
            ('5204', 'Unconventional Data Engineering')
        ],
        3: [
            ('6301', 'Standardized Quality in Data'),
            ('6302', 'Data Engineering Deployments'),
            ('6303', 'Internship')
        ],
        4: [
            ('6401', 'Project')
        ]
    }
    
    count = 0
    for sem, mods in standard_modules.items():
        for suffix, title in mods:
            code = prefix + suffix
            # Check exist
            exists = db.execute('SELECT id FROM pg_modules WHERE batch_id=%s AND code=%s', (batch_id, code)).fetchone()
            if not exists:
                try:
                    db.execute('INSERT INTO pg_modules (batch_id, year, code, title, faculty) VALUES (%s, %s, %s, %s, %s)',
                              (batch_id, sem, code, title, ''))
                    count += 1
                except: pass
    
    db.commit()
    flash(f'✅ Initialized {count} standard modules for {scheme_year} Scheme!')
    return redirect(url_for('pg_batch_view', batch_id=batch_id))

@app.route('/pg/module/add', methods=['POST'])
def pg_add_module():
    db = get_db()
    batch_id = request.form['batch_id']
    year = request.form['year']
    code = request.form['code'].strip().upper()
    title = request.form['title'].strip()
    faculty = request.form.get('faculty', '').strip()
    db.execute('INSERT INTO pg_modules (batch_id, year, code, title, faculty) VALUES (%s, %s, %s, %s, %s)', (batch_id, year, code, title, faculty))
    db.commit()
    flash(f'Module {code} added!')
    return redirect(url_for('pg_year_view', batch_id=batch_id, year=year))

@app.route('/pg/module/delete/<int:id>')
def pg_delete_module(id):
    batch_id = request.args.get('batch_id', 1)
    year = request.args.get('year', 1)
    db = get_db()
    db.execute('DELETE FROM pg_marks WHERE module_id = %s', (id,))
    db.execute('DELETE FROM pg_modules WHERE id = %s', (id,))
    db.commit()
    flash('Module deleted!')
    return redirect(url_for('pg_year_view', batch_id=batch_id, year=year))

@app.route('/pg/module/edit/<int:id>', methods=['GET', 'POST'])
def pg_edit_module(id):
    """Edit Module Code, Title, Faculty"""
    db = get_db()
    module = db.execute('SELECT m.*, b.start_year, b.end_year FROM pg_modules m JOIN pg_batches b ON m.batch_id = b.id WHERE m.id = %s', (id,)).fetchone()
    if not module: return "Module not found", 404
    
    if request.method == 'POST':
        code = request.form['code'].strip().upper()
        title = request.form['title'].strip()
        faculty = request.form['faculty'].strip()
        db.execute('UPDATE pg_modules SET code=%s, title=%s, faculty=%s WHERE id=%s', (code, title, faculty, id))
        db.commit()
        flash(f'Module {code} updated!')
        return redirect(url_for('pg_year_view', batch_id=module['batch_id'], year=module['year']))
    
    content = f'''
    <div class="container">
        <div class="card" style="max-width: 600px; margin: 2rem auto;">
            <h2 style="margin-bottom: 1.5rem;">✏️ Edit Module</h2>
            <form method="post">
                <div class="mb-3">
                    <label class="form-label">Module Code</label>
                    <input type="text" name="code" class="form-control" value="{module['code']}" required>
                </div>
                <div class="mb-3">
                    <label class="form-label">Module Title</label>
                    <input type="text" name="title" class="form-control" value="{module['title']}" required>
                </div>
                <div class="mb-3">
                    <label class="form-label">Faculty Name</label>
                    <input type="text" name="faculty" class="form-control" value="{module['faculty'] or ''}">
                </div>
                <div style="display: flex; gap: 1rem;">
                    <button class="btn btn-primary">💾 Save Changes</button>
                    <a href="/pg/de/batch/{module['batch_id']}/year/{module['year']}" class="btn btn-outline">Cancel</a>
                </div>
            </form>
        </div>
    </div>'''
    return render_template_string(base_html(f'Edit {module["code"]} - CAB', content))

@app.route('/pg/module/<int:id>')
def pg_module_dashboard(id):
    """PG Module marks entry - Assignment (50) and SEE (100)"""
    step = request.args.get('step', 'assignment')
    report_mode = request.args.get('mode', 'actual')
    db = get_db()
    module = db.execute('SELECT m.*, b.start_year, b.end_year FROM pg_modules m JOIN pg_batches b ON m.batch_id = b.id WHERE m.id = %s', (id,)).fetchone()
    if not module:
        return "Module not found", 404
    students = db.execute('SELECT * FROM pg_students WHERE batch_id = %s ORDER BY usn', (module['batch_id'],)).fetchall()
    
    # Initialize vars to prevent UnboundLocalError
    # Flash messages as Custom Modal
    flash_messages = get_flashed_messages()
    alert_script = ""
    if flash_messages:
        # Join with <br> for HTML display
        msgs = "<br>".join(flash_messages)
        alert_script = f"""
        <div id="customAlert" class="custom-alert-overlay">
            <div class="custom-alert-box">
                <div class="alert-icon">✨</div>
                <div class="alert-message">{msgs}</div>
                <button onclick="closeAlert()" class="alert-btn">OK</button>
            </div>
        </div>
        <style>
        .custom-alert-overlay {{ position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); z-index: 9999; display: flex; justify-content: center; align-items: center; animation: fadeIn 0.3s; }}
        .custom-alert-box {{ background: rgba(255, 255, 255, 0.95); backdrop-filter: blur(10px); padding: 2.5rem; border-radius: 20px; text-align: center; box-shadow: 0 20px 40px rgba(0,0,0,0.2); transform: scale(0.8); animation: popUp 0.5s cubic-bezier(0.19, 1, 0.22, 1) forwards; min-width: 320px; border: 1px solid rgba(255,255,255,0.2); }}
        .alert-icon {{ font-size: 3.5rem; margin-bottom: 1rem; animation: pulse 2s infinite; background: linear-gradient(135deg, #3b82f6, #8b5cf6, #ec4899); -webkit-background-clip: text; -webkit-text-fill-color: transparent; filter: drop-shadow(0 0 10px rgba(139, 92, 246, 0.3)); }}
        .alert-message {{ font-size: 1.3rem; margin-bottom: 2rem; color: #1f2937; font-weight: 700; display: block; letter-spacing: -0.5px; }}
        .alert-btn {{ background: linear-gradient(135deg, #3b82f6, #2563eb); color: white; border: none; padding: 0.8rem 2.5rem; border-radius: 12px; font-weight: 600; cursor: pointer; transition: all 0.3s; box-shadow: 0 4px 12px rgba(59, 130, 246, 0.4); }}
        .alert-btn:hover {{ transform: translateY(-2px); box-shadow: 0 6px 20px rgba(59, 130, 246, 0.6); }}
        @keyframes fadeIn {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
        @keyframes popUp {{ from {{ transform: scale(0.8); opacity: 0; }} to {{ transform: scale(1); opacity: 1; }} }}
        @keyframes pulse {{ 0% {{ transform: scale(1); filter: brightness(1); }} 50% {{ transform: scale(1.1); filter: brightness(1.2); }} 100% {{ transform: scale(1); filter: brightness(1); }} }}
        </style>
        <script>
        function closeAlert() {{ document.getElementById('customAlert').remove(); }}
        </script>
        """
    
    a1_marks = {}
    a2_marks = {}
    a3_marks = {}
    
    # Check for session preview data (for inline preview)
    preview_data = session.get(f'preview_data_{id}', {})
    
    marks_data = {}
    marks_details = {}
    for row in db.execute('SELECT * FROM pg_marks WHERE module_id = %s', (id,)).fetchall():
        marks_data[(row['student_id'], row['mark_type'])] = {'value': row['value'], 'prediction': row['ai_prediction']}
        if row['mark_type'] in ['assignment1', 'assignment2', 'assignment3'] and row['ai_prediction']:
            try:
                marks_details[(row['student_id'], row['mark_type'])] = json.loads(row['ai_prediction'])
            except:
                pass
    
    # Get student classifications for category radios
    classifications = {}
    try:
        classifications = {row['student_id']: row['category'] for row in db.execute('SELECT student_id, category FROM pg_student_classifications WHERE module_id=%s', (id,)).fetchall()}
    except:
        pass
    
    students_with_marks = []
    for s in students:
        sd = dict(s)
        for mt in ['assignment', 'assignment_gaussian', 'see', 'see_gaussian']:
            m = marks_data.get((s['id'], mt), {'value': 0, 'prediction': None})
            sd[mt] = m['value'] or 0
        
        see_scaled = math.ceil((sd['see'] / 100) * 50) if sd['see'] else 0
        sd['see_scaled'] = see_scaled
        sd['total'] = sd['assignment'] + see_scaled
        sd['grade'] = calculate_pg_grade(sd['total'], sd.get('see', 0))
        sd['grade_point'] = get_pg_grade_point(sd['grade'])
        see_gaussian_scaled = math.ceil((sd['see_gaussian'] / 100) * 50) if sd['see_gaussian'] else 0
        sd['see_gaussian_scaled'] = see_gaussian_scaled
        # Gaussian total uses Gaussian assignment (from DB) + Gaussian SEE
        assign_g = sd['assignment_gaussian'] if sd['assignment_gaussian'] > 0 else sd['assignment']
        sd['total_gaussian'] = assign_g + see_gaussian_scaled
        sd['grade_gaussian'] = calculate_pg_grade(sd['total_gaussian'], sd['see_gaussian'] if sd.get('see_gaussian') else sd.get('see', 0))
        students_with_marks.append(sd)
    steps = [('assignment', 'Assignment (50)'), ('see', 'SEE (100)'), ('performance', 'Performance'), ('report', 'Report')]
    sidebar = ''.join([f'<a href="/pg/module/{id}?step={sid}" class="sidebar-link {"active" if step == sid else ""}">{sname}</a>' for sid, sname in steps])
    if step == 'performance':
        # Performance Recording page - matches the uploaded image format
        total_students = len(students_with_marks)
        appeared = sum(1 for s in students_with_marks if s['assignment'] > 0 or s['see'] > 0)
        passed = sum(1 for s in students_with_marks if s['total'] >= 50)
        class_avg = round(sum(s['total'] for s in students_with_marks) / total_students, 2) if total_students > 0 else 0
        avg_grade = calculate_pg_grade(class_avg)
        
        # Grade distribution for report
        grade_counts = {'O': 0, 'A+': 0, 'A': 0, 'B+': 0, 'B': 0, 'C': 0}
        for s in students_with_marks:
            grade_counts[s['grade']] = grade_counts.get(s['grade'], 0) + 1
        
        # Academic year from batch
        academic_year = f"{module['start_year']}-{str(module['end_year'])[-2:]}"
        
        # Calculate grades_scored (actual) and grades_gaussian for the new charts
        grades_scored = {'O': 0, 'A+': 0, 'A': 0, 'B+': 0, 'B': 0, 'C': 0}
        grades_gaussian = {'O': 0, 'A+': 0, 'A': 0, 'B+': 0, 'B': 0, 'C': 0}

        for s in students_with_marks:
            grades_scored[s['grade']] += 1
            grades_gaussian[s['grade_gaussian']] += 1

        total_students_for_charts = len(students_with_marks) or 1
        chart_labels = list(grades_scored.keys())
        chart_data_actual = list(grades_scored.values())
        chart_data_gaussian = list(grades_gaussian.values())

        main_content = f'''<div class="card" id="performanceContentWrapper">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem;">
                <h2>📊 Performance Recording</h2>
                <div style="display: flex; gap: 0.5rem; align-items: center;">
                    <a href="/pg/batch/{module['batch_id']}/edit_year" class="btn btn-outline btn-sm">✏️ Edit Year</a>
                    <div class="dropdown" style="position: relative; display: inline-block;">
                        <button onclick="document.getElementById('downloadPerfMenu').style.display = document.getElementById('downloadPerfMenu').style.display === 'block' ? 'none' : 'block'" class="btn btn-success btn-sm">⬇️ Download</button>
                        <div id="downloadPerfMenu" style="display:none; position: absolute; right: 0; top: 100%; margin-top: 5px; background: white; box-shadow: 0 4px 12px rgba(0,0,0,0.15); border-radius: 8px; z-index: 100; min-width: 150px; overflow: hidden; border: 1px solid #e2e8f0; text-align: left;">
                            <a href="javascript:void(0)" onclick="downloadPerformancePDF(); document.getElementById('downloadPerfMenu').style.display='none'" style="display: block; padding: 0.75rem 1rem; color: #374151; text-decoration: none; border-bottom: 1px solid #e5e7eb; white-space: nowrap;">📄 Download as PDF</a>
                            <a href="javascript:void(0)" onclick="downloadPerformanceExcel(); document.getElementById('downloadPerfMenu').style.display='none'" style="display: block; padding: 0.75rem 1rem; color: #374151; text-decoration: none; white-space: nowrap;">📊 Download as Excel (CSV)</a>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Header Info Table -->
            <table style="margin-bottom: 2rem; border: 2px solid #1e3a5f;">
                <tr style="background: #f0f9ff;">
                    <th style="border: 1px solid #ccc; padding: 0.5rem;">Academic Year</th>
                    <th style="border: 1px solid #ccc; padding: 0.5rem;">Program</th>
                    <th style="border: 1px solid #ccc; padding: 0.5rem;">Year</th>
                    <th style="border: 1px solid #ccc; padding: 0.5rem;">Course Code</th>
                    <th style="border: 1px solid #ccc; padding: 0.5rem;" colspan="2">Course Title</th>
                </tr>
                <tr>
                    <td style="border: 1px solid #ccc; padding: 0.5rem; font-weight: bold;">{academic_year}</td>
                    <td style="border: 1px solid #ccc; padding: 0.5rem;">M.Tech Data Engineering</td>
                    <td style="border: 1px solid #ccc; padding: 0.5rem;">Semester {module['year']}</td>
                    <td style="border: 1px solid #ccc; padding: 0.5rem;">{module['code']}</td>
                    <td style="border: 1px solid #ccc; padding: 0.5rem;" colspan="2">{module['title']}</td>
                </tr>
                <tr>
                    <td style="border: 1px solid #ccc; padding: 0.5rem;" colspan="6">
                        <strong>Course Tutor/s:</strong> {module['faculty'] or 'Not Assigned'}<br>
                        <strong>Tutor's ID/Department:</strong> ISE
                    </td>
                </tr>
            </table>
            
            <!-- Statistics Table -->
            <table style="margin-bottom: 2rem; border: 2px solid #1e3a5f;">
                <tr style="background: #f0f9ff;">
                    <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; font-size: 0.85rem;">Total<br>Students</th>
                    <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; font-size: 0.85rem;">Appeared</th>
                    <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; font-size: 0.85rem;">Passed</th>
                    <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; font-size: 0.85rem;">Class Avg</th>
                    <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; background: #22c55e; color: white;">O Graders<br>≥91</th>
                    <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; background: #84cc16; color: white;">A+ Graders<br>81-90</th>
                    <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; background: #eab308; color: white;">A Graders<br>71-80</th>
                    <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; background: #f97316; color: white;">B+ Graders<br>61-70</th>
                    <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; background: #3b82f6; color: white;">B Graders<br>51-60</th>
                    <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; background: #dc2626; color: white;">C Graders<br>&lt;50</th>
                </tr>
                <tr style="font-size: 1.1rem; font-weight: bold; text-align: center;">
                    <td style="border: 1px solid #ccc; padding: 0.75rem;">{total_students}</td>
                    <td style="border: 1px solid #ccc; padding: 0.75rem;">{appeared}</td>
                    <td style="border: 1px solid #ccc; padding: 0.75rem;">{passed}</td>
                    <td style="border: 1px solid #ccc; padding: 0.75rem;">{class_avg}<br><span class="badge" style="background: {get_grade_color(avg_grade)}; color: white;">{avg_grade}</span></td>
                    <td style="border: 1px solid #ccc; padding: 0.75rem;">{grade_counts['O']}</td>
                    <td style="border: 1px solid #ccc; padding: 0.75rem;">{grade_counts['A+']}</td>
                    <td style="border: 1px solid #ccc; padding: 0.75rem;">{grade_counts['A']}</td>
                    <td style="border: 1px solid #ccc; padding: 0.75rem;">{grade_counts['B+']}</td>
                    <td style="border: 1px solid #ccc; padding: 0.75rem;">{grade_counts['B']}</td>
                    <td style="border: 1px solid #ccc; padding: 0.75rem;">{grade_counts['C']}</td>
                </tr>
            </table>
            
            <!-- Feedback Links -->
            <div style="background: #f8fafc; padding: 1rem; border-radius: 10px; margin-bottom: 1.5rem;">
                <strong>Students Feedback on Teaching and Assessment:</strong>
                <div style="margin-top: 0.5rem; display: flex; gap: 1rem;">
                    <a href="#" class="btn btn-primary btn-sm">📝 Feedback-1</a>
                    <a href="#" class="btn btn-outline btn-sm">📝 Feedback-2</a>
                </div>
            </div>
            
            <!-- Grade Distribution Chart -->
            <div style="background: #f8fafc; padding: 1.5rem; border-radius: 10px; margin-bottom: 2rem;">
                <h4 style="margin-bottom: 1rem; text-align: center;">Grade Distribution</h4>
                <div style="height: 300px;"><canvas id="gradeChart"></canvas></div>
                <div style="display: flex; justify-content: center; gap: 1rem; margin-top: 1rem; flex-wrap: wrap; font-size: 0.85rem;">
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #22c55e; border-radius: 3px; display: inline-block;"></span> O</span>
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #84cc16; border-radius: 3px; display: inline-block;"></span> A+</span>
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #eab308; border-radius: 3px; display: inline-block;"></span> A</span>
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #f97316; border-radius: 3px; display: inline-block;"></span> B+</span>
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #3b82f6; border-radius: 3px; display: inline-block;"></span> B</span>
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #dc2626; border-radius: 3px; display: inline-block;"></span> C/F</span>
                </div>
            </div>
            
            <h2 class="page-title">Comparison Report</h2>
            <div style="display: flex; justify-content: center; gap: 1rem; margin-top: 1rem; flex-wrap: wrap; font-size: 0.85rem;">
                <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #22c55e; border-radius: 3px; display: inline-block;"></span> O</span>
                <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #84cc16; border-radius: 3px; display: inline-block;"></span> A+</span>
                <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #eab308; border-radius: 3px; display: inline-block;"></span> A</span>
                <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #f97316; border-radius: 3px; display: inline-block;"></span> B+</span>
                <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #3b82f6; border-radius: 3px; display: inline-block;"></span> B</span>
                <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #dc2626; border-radius: 3px; display: inline-block;"></span> C/F</span>
            </div>
            <div class="row mt-4" style="display: flex; gap: 1rem;">
                <div class="col-md-6" style="flex: 1;">
                    <div class="card">
                        <h3 style="font-size: 1rem; margin-bottom: 0.5rem;">Actual Distribution</h3>
                        <div style="height: 250px; position: relative;">
                            <canvas id="chartActual"></canvas>
                        </div>
                    </div>
                </div>
                <div class="col-md-6" style="flex: 1;">
                    <div class="card">
                        <h3 style="font-size: 1rem; margin-bottom: 0.5rem;">Gaussian Projection</h3>
                        <div style="height: 250px; position: relative;">
                            <canvas id="chartGaussian"></canvas>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Gaussian Stats Table -->
            <h3 style="margin-top: 2rem; color: #10b981;">Gaussian Projected Performance</h3>
            <table style="margin-bottom: 2rem; border: 2px solid #059669;">
                <tr style="background: #ecfdf5;">
                    <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; font-size: 0.85rem;">Total<br>Students</th>
                    <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; font-size: 0.85rem;">Appeared</th>
                    <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; font-size: 0.85rem;">Passed</th>
                    <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; font-size: 0.85rem;">Class Avg</th>
                    <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; background: #22c55e; color: white;">O Graders<br>≥91</th>
                    <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; background: #84cc16; color: white;">A+ Graders<br>81-90</th>
                    <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; background: #eab308; color: white;">A Graders<br>71-80</th>
                    <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; background: #f97316; color: white;">B+ Graders<br>61-70</th>
                    <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; background: #3b82f6; color: white;">B Graders<br>51-60</th>
                    <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; background: #dc2626; color: white;">C Graders<br>&lt;50</th>
                </tr>
                <tr style="font-size: 1.1rem; font-weight: bold; text-align: center;">
                    <td style="border: 1px solid #ccc; padding: 0.75rem;">{total_students}</td>
                    <td style="border: 1px solid #ccc; padding: 0.75rem;">{appeared}</td>
                    <td style="border: 1px solid #ccc; padding: 0.75rem;">{sum(1 for s in students_with_marks if s['total_gaussian'] >= 50)}</td>
                    <td style="border: 1px solid #ccc; padding: 0.75rem;">{round(sum(s['total_gaussian'] for s in students_with_marks) / total_students, 2) if total_students > 0 else 0}<br>
                        <span class="badge" style="background: {get_grade_color(calculate_pg_grade(round(sum(s['total_gaussian'] for s in students_with_marks) / total_students, 2) if total_students > 0 else 0))}; color: white;">
                        {calculate_pg_grade(round(sum(s['total_gaussian'] for s in students_with_marks) / total_students, 2) if total_students > 0 else 0)}
                        </span>
                    </td>
                    <td style="border: 1px solid #ccc; padding: 0.75rem;">{grades_gaussian['O']}</td>
                    <td style="border: 1px solid #ccc; padding: 0.75rem;">{grades_gaussian['A+']}</td>
                    <td style="border: 1px solid #ccc; padding: 0.75rem;">{grades_gaussian['A']}</td>
                    <td style="border: 1px solid #ccc; padding: 0.75rem;">{grades_gaussian['B+']}</td>
                    <td style="border: 1px solid #ccc; padding: 0.75rem;">{grades_gaussian['B']}</td>
                    <td style="border: 1px solid #ccc; padding: 0.75rem;">{grades_gaussian['C']}</td>
                </tr>
            </table>
            
            <!-- Student Marks Detail Table -->
            <h3 style="margin-top: 2rem; color: #6366f1;">📋 Student Marks Detail (Actual vs Gaussian)</h3>
            <div style="overflow-x: auto; margin-bottom: 2rem;">
                <table style="width: 100%; border: 2px solid #6366f1; font-size: 0.85rem;">
                    <tr style="background: #eef2ff;">
                        <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: left;">USN</th>
                        <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: left;">Name</th>
                        <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; background: #3b82f6; color: white;">Assign<br>(Actual)</th>
                        <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; background: #3b82f6; color: white;">SEE<br>(Actual)</th>
                        <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; background: #3b82f6; color: white;">Total<br>(Actual)</th>
                        <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; background: #3b82f6; color: white;">Grade<br>(Actual)</th>
                        <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; background: #10b981; color: white;">Assign<br>(Gaussian)</th>
                        <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; background: #10b981; color: white;">SEE<br>(Gaussian)</th>
                        <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; background: #10b981; color: white;">Total<br>(Gaussian)</th>
                        <th style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; background: #10b981; color: white;">Grade<br>(Gaussian)</th>
                    </tr>
                    {''.join([f"""<tr style="border-bottom: 1px solid #eee;">
                        <td style="border: 1px solid #ccc; padding: 0.5rem; font-weight: bold;">{s['usn']}</td>
                        <td style="border: 1px solid #ccc; padding: 0.5rem;">{s['name']}</td>
                        <td style="border: 1px solid #ccc; padding: 0.5rem; text-align: center;{' background-color: #fee2e2; color: #dc2626; font-weight: bold;' if s['assignment'] == 0 else ''}">{s['assignment']}</td>
                        <td style="border: 1px solid #ccc; padding: 0.5rem; text-align: center;{' background-color: #fee2e2; color: #dc2626; font-weight: bold;' if s['see'] == 0 else ''}">{s['see']}</td>
                        <td style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; font-weight: bold;">{s['total']}</td>
                        <td style="border: 1px solid #ccc; padding: 0.5rem; text-align: center;"><span class="badge" style="background: {get_grade_color(s['grade'])}; color: white;">{s['grade']}</span></td>
                        <td style="border: 1px solid #ccc; padding: 0.5rem; text-align: center;{' background-color: #fee2e2; color: #dc2626; font-weight: bold;' if s.get('assignment_gaussian', s['assignment']) == 0 else ''}">{s.get('assignment_gaussian', s['assignment'])}</td>
                        <td style="border: 1px solid #ccc; padding: 0.5rem; text-align: center;{' background-color: #fee2e2; color: #dc2626; font-weight: bold;' if s['see_gaussian'] == 0 else ''}">{s['see_gaussian']}</td>
                        <td style="border: 1px solid #ccc; padding: 0.5rem; text-align: center; font-weight: bold;">{s['total_gaussian']}</td>
                        <td style="border: 1px solid #ccc; padding: 0.5rem; text-align: center;"><span class="badge" style="background: {get_grade_color(s['grade_gaussian'])}; color: white;">{s['grade_gaussian']}</span></td>
                    </tr>""" for s in students_with_marks])}
                </table>
            </div>
            
            <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
            <script>
                new Chart(document.getElementById('chartActual'), {{
                    type: 'bar',
                    data: {{
                        labels: {json.dumps(chart_labels)},
                        datasets: [{{ label: 'Students (Actual)', data: {json.dumps(chart_data_actual)}, backgroundColor: '#3b82f6' }}]
                    }},
                    options: {{ 
                        responsive: true, 
                        maintainAspectRatio: false,
                        plugins: {{ legend: {{ display: false }} }},
                        scales: {{ y: {{ beginAtZero: true, ticks: {{ stepSize: 1 }} }} }}
                    }}
                }});
                new Chart(document.getElementById('chartGaussian'), {{
                    type: 'bar',
                    data: {{
                        labels: {json.dumps(chart_labels)},
                        datasets: [{{ label: 'Students (Gaussian)', data: {json.dumps(chart_data_gaussian)}, backgroundColor: '#10b981' }}]
                    }},
                    options: {{ 
                        responsive: true, 
                        maintainAspectRatio: false,
                        plugins: {{ legend: {{ display: false }} }},
                        scales: {{ y: {{ beginAtZero: true, ticks: {{ stepSize: 1 }} }} }}
                    }}
                }});
            </script>
        </div>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <script>
        new Chart(document.getElementById('gradeChart'), {{
            type: 'bar',
            data: {{
                labels: ['O', 'A+', 'A', 'B+', 'B', 'C'],
                datasets: [{{
                    label: 'Students',
                    data: [{grade_counts['O']}, {grade_counts['A+']}, {grade_counts['A']}, {grade_counts['B+']}, {grade_counts['B']}, {grade_counts['C']}],
                    backgroundColor: ['#22c55e', '#84cc16', '#eab308', '#f97316', '#3b82f6', '#dc2626']
                }}]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{ legend: {{ display: false }} }},
                scales: {{ y: {{ beginAtZero: true, ticks: {{ stepSize: 1 }} }} }}
            }}
        }});
        
        function downloadPerformancePDF() {{
            const element = document.getElementById('performanceContentWrapper');
            const opt = {{
                margin:       0.5,
                filename:     'PG_Performance.pdf',
                image:        {{ type: 'jpeg', quality: 0.98 }},
                html2canvas:  {{ scale: 2 }},
                jsPDF:        {{ unit: 'in', format: 'a4', orientation: 'landscape' }}
            }};
            html2pdf().set(opt).from(element).save();
        }}
        
        function downloadPerformanceExcel() {{
            let tables = document.querySelectorAll('#performanceContentWrapper table');
            if (tables.length < 3) return;
            let table = tables[2]; // get the third table (student marks)
            let rows = table.querySelectorAll('tr');
            let csv = [];
            for(let i=0; i<rows.length; i++) {{
                let row = [], cols = rows[i].querySelectorAll('td, th');
                for(let j=0; j<cols.length; j++) {{
                    let data = cols[j].innerText.replace(/(\\r\\n|\\n|\\r)/gm, '').replace(/(\\s\\s)/gm, ' ');
                    data = data.replace(/"/g, '""');
                    row.push('"' + data + '"');
                }}
                csv.push(row.join(','));
            }}
            let csvString = csv.join('\\n');
            let a = document.createElement('a');
            a.href = 'data:text/csv;charset=utf-8,' + encodeURIComponent(csvString);
            a.target = '_blank';
            a.download = 'PG_Performance_Data.csv';
            a.click();
        }}
        
        document.addEventListener('click', function(event) {{
            var menu = document.getElementById('downloadPerfMenu');
            var btn = menu ? menu.previousElementSibling : null;
            if (menu && menu.style.display === 'block' && !menu.contains(event.target) && event.target !== btn) {{
                menu.style.display = 'none';
            }}
        }});
        </script>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.1/html2pdf.bundle.min.js"></script>
        '''
    elif step == 'report':
        def build_pg_report(students_list, see_field='see', use_gaussian_assign=False):
            grades = {'O': 0, 'A+': 0, 'A': 0, 'B+': 0, 'B': 0, 'C': 0}
            rows = []
            for s in students_list:
                # For Gaussian mode: use assignment_gaussian from database
                if use_gaussian_assign:
                    assign_val = s.get('assignment_gaussian', 0) or s.get('assignment', 0)
                else:
                    assign_val = s.get('assignment', 0)
                    
                see_val = s.get(see_field, 0) or 0
                see_scaled = math.ceil((see_val / 100) * 50) if see_val else 0
                total = assign_val + see_scaled
                grade = calculate_pg_grade(total, see_val)
                gp = get_pg_grade_point(grade)
                grades[grade] += 1
                rows.append({'usn': s['usn'], 'name': s['name'], 'assignment': s.get('assignment', 0), 'assignment_gaussian': s.get('assignment_gaussian', 0), 'see': see_val, 'see_scaled': see_scaled, 'total': total, 'percentage': f"{total}%", 'grade': grade, 'gp': gp, 'color': get_grade_color(grade)})
            return rows, grades
        actual_rows, actual_grades = build_pg_report(students_with_marks, 'see', use_gaussian_assign=False)
        gaussian_rows, gaussian_grades = build_pg_report(students_with_marks, 'see_gaussian', use_gaussian_assign=True)
        if report_mode == 'gaussian':
            rows, grades, title = gaussian_rows, gaussian_grades, 'With Gaussian Marks'
            has_gaussian = any(r['assignment_gaussian'] > 0 or r['see'] > 0 for r in gaussian_rows)
            warning = '' if has_gaussian else '<div class="alert" style="background: #fef3c7; border: 1px solid #f59e0b; padding: 1rem; border-radius: 8px; margin-bottom: 1rem;">⚠️ <strong>No Gaussian marks!</strong> Run Gaussian Assign on Assignments and SEE first.</div>'
        else:
            rows, grades, title = actual_rows, actual_grades, 'Actual Performance'
            warning = ''
        
        top_n_str = request.args.get('top_n', '')
        if top_n_str and top_n_str.isdigit():
            top_n = int(top_n_str)
            display_rows = sorted(rows, key=lambda x: x['total'], reverse=True)[:top_n]
            list_title = f"🏆 Top {top_n} Performers"
        else:
            display_rows = sorted(rows, key=lambda x: x['usn'])
            list_title = "📋 Full Class List"

        sticky_name = 'position:sticky; left:0; z-index:2; background:inherit; box-shadow: 2px 0 5px rgba(0,0,0,0.05);'
        table_rows_list = []
        for r in display_rows:
            a_style = ' style="background-color: #fee2e2; color: #dc2626; font-weight: bold;"' if r['assignment'] == 0 else ''
            s_style = ' style="background-color: #fee2e2; color: #dc2626; font-weight: bold;"' if r['see'] == 0 else ''
            ag_style = 'background:#fee2e2; color:#dc2626; font-weight:bold;' if r['assignment_gaussian'] == 0 else 'background:#ecfdf5; color:#10b981; font-weight:bold;'
            row_bg = 'background: #fee2e2;' if r['total'] == 0 else 'background: white;'
            table_rows_list.append(f'<tr style="{row_bg}"><td><strong>{r["usn"]}</strong></td><td style="{sticky_name}">{r["name"]}</td><td{a_style}>{r["assignment"]}</td><td style="{ag_style}">{r["assignment_gaussian"]}</td><td{s_style}>{r["see"]} &rarr; {r["see_scaled"]}</td><td><strong>{r["total"]}</strong></td><td><strong>{r["percentage"]}</strong></td><td><span class="badge" style="background:{r["color"]};color:white;">{r["grade"]}</span></td><td>{r["gp"]}</td></tr>')
        table_rows = ''.join(table_rows_list)
        chart_data = json.dumps(list(grades.values()))
        chart_labels = json.dumps(list(grades.keys()))
        main_content = f'''
        <div class="card" id="reportContentWrapper">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; flex-wrap: wrap; gap: 1rem;">
                <h2 style="margin: 0; white-space: nowrap;">📊 {title}</h2>
                <div style="display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;">
                    <a href="/pg/module/{id}?step=report&mode=actual" class="btn {"btn-primary" if report_mode == "actual" else "btn-outline"} btn-sm">Actual</a>
                    <a href="/pg/module/{id}?step=report&mode=gaussian" class="btn {"btn-primary" if report_mode == "gaussian" else "btn-outline"} btn-sm">Gaussian</a>
                    <div class="dropdown" style="position: relative; display: inline-block;">
                        <button onclick="document.getElementById('downloadMenu').style.display = document.getElementById('downloadMenu').style.display === 'block' ? 'none' : 'block'" class="btn btn-success btn-sm">⬇️ Download Report</button>
                        <div id="downloadMenu" style="display:none; position: absolute; right: 0; top: 100%; margin-top: 5px; background: white; box-shadow: 0 4px 12px rgba(0,0,0,0.15); border-radius: 8px; z-index: 100; min-width: 150px; overflow: hidden; border: 1px solid #e2e8f0; text-align: left;">
                            <a href="javascript:void(0)" onclick="downloadPGReportPDF(); document.getElementById('downloadMenu').style.display='none'" style="display: block; padding: 0.75rem 1rem; color: #374151; text-decoration: none; border-bottom: 1px solid #e5e7eb; white-space: nowrap;">📄 Download as PDF</a>
                            <a href="javascript:void(0)" onclick="downloadPGReportExcel(); document.getElementById('downloadMenu').style.display='none'" style="display: block; padding: 0.75rem 1rem; color: #374151; text-decoration: none; white-space: nowrap;">📊 Download as Excel (CSV)</a>
                        </div>
                    </div>
                </div>
            </div>
            {warning}
            <div style="background: #f0fdf4; padding: 1rem; border-radius: 10px; border: 1px solid #86efac; margin-bottom: 1.5rem;">
                <h4 style="margin-bottom: 0.5rem;">📋 PG Grading Scale</h4>
                <div style="display: flex; gap: 1rem; flex-wrap: wrap; font-size: 0.9rem;">
                    <span><strong>O</strong>: 91-100 (GP: 10)</span>
                    <span><strong>A+</strong>: 81-90 (GP: 9)</span>
                    <span><strong>A</strong>: 71-80 (GP: 8)</span>
                    <span><strong>B+</strong>: 61-70 (GP: 7)</span>
                    <span><strong>B</strong>: 50-60 (GP: 6)</span>
                    <span style="color: #dc2626;"><strong>C</strong>: <50 (GP: 0, Fail)</span>
                </div>
            </div>
            <div style="background: #f8fafc; padding: 1.5rem; border-radius: 10px; margin-bottom: 2rem;">
                <h4 style="margin-bottom: 1rem; text-align: center;">Grade Distribution</h4>
                <div style="height: 300px;"><canvas id="gradeChart"></canvas></div>
                <div style="display: flex; justify-content: center; gap: 1rem; margin-top: 1rem; flex-wrap: wrap; font-size: 0.85rem;">
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #22c55e; border-radius: 3px; display: inline-block;"></span> O</span>
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #84cc16; border-radius: 3px; display: inline-block;"></span> A+</span>
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #eab308; border-radius: 3px; display: inline-block;"></span> A</span>
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #f97316; border-radius: 3px; display: inline-block;"></span> B+</span>
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #3b82f6; border-radius: 3px; display: inline-block;"></span> B</span>
                    <span style="display: flex; align-items: center; gap: 0.3rem;"><span style="width: 12px; height: 12px; background: #dc2626; border-radius: 3px; display: inline-block;"></span> C/F</span>
                </div>
            </div>
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem;">
                <h3>{list_title}</h3>
                <form method="get" action="/pg/module/{id}">
                    <input type="hidden" name="step" value="report">
                    <input type="hidden" name="mode" value="{report_mode}">
                    <select name="top_n" class="form-control" style="width: auto; display: inline-block; padding: 0.5rem; font-weight: bold; border-color: #3b82f6;" onchange="this.form.submit()">
                        <option value="">Show All Students</option>
                        <option value="3" {"selected" if top_n_str == "3" else ""}>🏆 Top 3 Performers</option>
                        <option value="5" {"selected" if top_n_str == "5" else ""}>🏆 Top 5 Performers</option>
                        <option value="10" {"selected" if top_n_str == "10" else ""}>🏆 Top 10 Performers</option>
                    </select>
                </form>
            </div>
            <div style="overflow-x: auto; border-radius: 10px; border: 1px solid #e2e8f0;">
                <table><tr><th>USN</th><th>Name</th><th>Assignment (50)</th><th style="background:#10b981; color:white;">Gaussian Assign</th><th>SEE (100→50)</th><th>Total (100)</th><th>Percentage</th><th>Grade</th><th>GP</th></tr>{table_rows}</table>
            </div>
        </div>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.1/html2pdf.bundle.min.js"></script>
        <script>
        new Chart(document.getElementById('gradeChart'), {{type: 'bar', data: {{labels: {chart_labels}, datasets: [{{label: 'Students', data: {chart_data}, backgroundColor: ['#22c55e', '#84cc16', '#eab308', '#f97316', '#3b82f6', '#dc2626']}}]}}, options: {{responsive: true, maintainAspectRatio: false, plugins: {{legend: {{display: false}}}}, scales: {{y: {{beginAtZero: true, ticks: {{stepSize: 1}}}}}}}}}});
        
        function downloadPGReportPDF() {{
            const element = document.getElementById('reportContentWrapper');
            const opt = {{
                margin:       0.5,
                filename:     'PG_Report.pdf',
                image:        {{ type: 'jpeg', quality: 0.98 }},
                html2canvas:  {{ scale: 2 }},
                jsPDF:        {{ unit: 'in', format: 'a4', orientation: 'landscape' }}
            }};
            html2pdf().set(opt).from(element).save();
        }}
        
        function downloadPGReportExcel() {{
            let table = document.querySelector('#reportContentWrapper table');
            if (!table) return;
            let rows = table.querySelectorAll('tr');
            let csv = [];
            for(let i=0; i<rows.length; i++) {{
                let row = [], cols = rows[i].querySelectorAll('td, th');
                for(let j=0; j<cols.length; j++) {{
                    let data = cols[j].innerText.replace(/(\\r\\n|\\n|\\r)/gm, '').replace(/(\\s\\s)/gm, ' ');
                    data = data.replace(/"/g, '""');
                    row.push('"' + data + '"');
                }}
                csv.push(row.join(','));
            }}
            let csvString = csv.join('\\n');
            let a = document.createElement('a');
            a.href = 'data:text/csv;charset=utf-8,' + encodeURIComponent(csvString);
            a.target = '_blank';
            a.download = 'PG_Report_Data.csv';
            a.click();
        }}
        
        document.addEventListener('click', function(event) {{
            var menu = document.getElementById('downloadMenu');
            var btn = menu.previousElementSibling;
            if (menu && menu.style.display === 'block' && !menu.contains(event.target) && event.target !== btn) {{
                menu.style.display = 'none';
            }}
        }});
        </script>'''
    elif step == 'see':
        main_content = f'''<div class="card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem;">
                <h2>SEE Entry (Max 100)</h2>
                <div style="display:inline-block;">
                    <form action="/pg/module/{id}/gaussian/see" method="post" style="display:inline;"><button class="btn btn-success btn-sm">📊 Gaussian Assign</button></form>
                    <form action="/pg/module/{id}/copy_gaussian/see" method="post" style="display:inline; margin-left:5px;">
                         <button class="btn btn-sm" style="background:#6366f1; color:white;" type="button" onclick="customConfirmForm(event, 'Overwrite Actual marks with Gaussian marks?', this.closest('form'))">© Copy Gaussian</button>
                    </form>
                    <button class="btn btn-sm" style="background:#dc2626; color:white; margin-left:5px;" onclick="document.getElementById('deleteModal_see').style.display='block'">🗑️ Delete</button>
                    
                    <div id="deleteModal_see" class="modal" style="display:none; position:fixed; z-index:100; left:0; top:0; width:100%; height:100%; overflow:auto; background-color:rgba(0,0,0,0.4); padding-top:100px;">
                        <div class="modal-content" style="background-color:#fefefe; margin:auto; padding:20px; border:1px solid #888; width:40%; border-radius:8px; box-shadow:0 4px 6px rgba(0,0,0,0.1);">
                            <h3 style="margin-top:0; color:#dc2626;">🗑️ Delete SEE Marks</h3>
                            <p>Which marks do you want to delete?</p>
                            <form action="/pg/module/{id}/delete_marks/see" method="post">
                                <div style="display:flex; flex-direction:column; gap:10px; margin-top:15px;">
                                    <button name="delete_type" value="actual" class="btn" style="background:#3b82f6; color:white; padding:10px;">Delete Actual Marks Only</button>
                                    <button name="delete_type" value="gaussian" class="btn" style="background:#10b981; color:white; padding:10px;">Delete Gaussian Marks Only</button>
                                    <button name="delete_type" value="both" class="btn" style="background:#dc2626; color:white; padding:10px;">Delete BOTH Actual & Gaussian</button>
                                </div>
                            </form>
                            <div style="text-align:right; margin-top:20px;">
                                <button class="btn btn-outline" onclick="document.getElementById('deleteModal_see').style.display='none'">Cancel</button>
                            </div>
                        </div>
                    </div>
                    
                    <a href="/pg/module/{id}/classify/see" class="btn btn-outline btn-sm" style="margin-left:5px; border:1px solid #ccc;" title="Gaussian SEE Settings">⚙️</a>
                </div>
            </div>
            <div class="mb-3" style="background: #f0f9ff; padding: 1rem; border-radius: 10px; border: 1px solid #bae6fd;">
                <h4 style="color: #0369a1; margin-bottom: 0.5rem;">📋 Manual Import (USN[Tab]Marks)</h4>
                <form action="/pg/module/{id}/manual/see" method="post">
                    <textarea name="paste_text" class="form-control" rows="3" placeholder="P25E03DE001&#9;85&#10;P25E03DE002&#9;78"></textarea>
                    <button class="btn btn-success btn-sm mt-2">⚡ Parse & Import</button>
                </form>
            </div>
            <p class="text-muted mb-2">💡 SEE entered for 100, scaled to 50 with ceiling (86 → 43)</p>
            <form action="/pg/module/{id}/save/see" method="post">
                <table id="seeTable" class="sortable">
                    <thead>
                        <tr style="cursor:pointer;">
                            <th onclick="sortTable('seeTable', 0)">USN</th>
                            <th onclick="sortTable('seeTable', 1)">Name</th>
                            <th onclick="sortTable('seeTable', 2)">Assignment</th>
                            <th onclick="sortTable('seeTable', 3)">SEE (/100)</th>
                            <th onclick="sortTable('seeTable', 4)">SEE Scaled (/50)</th>
                            <th onclick="sortTable('seeTable', 5)">Gaussian (100)</th>
                            <th onclick="sortTable('seeTable', 6)">Gaussian (50)</th>
                        </tr>
                    </thead>
                    <tbody>
                {{% for s in students_with_marks %}}
                <tr><td><strong>{{{{ s.usn }}}}</strong><input type="hidden" name="sid_{{{{ loop.index0 }}}}" value="{{{{ s.id }}}}"></td><td>{{{{ s.name }}}}</td><td>{{{{ s.assignment }}}}</td><td><input type="number" name="val_{{{{ loop.index0 }}}}" value="{{{{ s.see }}}}" class="marks-input" min="0" max="100" oninput="this.parentElement.nextElementSibling.innerText = Math.ceil(this.value / 2)"></td><td><span id="scale_{{{{ loop.index0 }}}}">{{{{ s.see_scaled }}}}</span></td><td><input type="number" name="val_g_{{{{ loop.index0 }}}}" value="{{{{ s.see_gaussian }}}}" class="marks-input" min="0" max="100" oninput="this.parentElement.nextElementSibling.innerText = Math.ceil(this.value / 2)"></td><td><span class="badge badge-info">{{{{ (s.see_gaussian / 2) | round | int }}}}</span></td></tr>
                {{% endfor %}}
                    </tbody>
                </table>
                <button class="btn btn-primary mt-2">💾 Save Marks</button>
            </form>
            <style>
            th.asc::after {{ content: " ↑"; }}
            th.desc::after {{ content: " ↓"; }}
            th {{ cursor: pointer; user-select: none; }}
            </style>
            <script>
            function sortTable(tableId, n) {{
              var table, rows, switching, i, x, y, shouldSwitch, dir, switchcount = 0;
              table = document.getElementById(tableId);
              // Reset headers
              var headers = table.getElementsByTagName("TH");
              if (table.getAttribute("data-sort-col") != n) {{
                  for (var h of headers) h.className = "";
                  dir = "asc";
              }} else {{
                  dir = table.getAttribute("data-sort-dir") == "asc" ? "desc" : "asc";
              }}
              headers[n].className = dir;
              table.setAttribute("data-sort-col", n);
              table.setAttribute("data-sort-dir", dir);

              switching = true;
              while (switching) {{
                switching = false;
                var tbody = table.tBodies[0];
                rows = tbody.rows;
                for (i = 0; i < (rows.length - 1); i++) {{
                  shouldSwitch = false;
                  x = rows[i].getElementsByTagName("TD")[n];
                  y = rows[i + 1].getElementsByTagName("TD")[n];
                  var xInput = x.getElementsByTagName("input")[0];
                  var yInput = y.getElementsByTagName("input")[0];
                  var xContent = xInput ? xInput.value : x.innerText;
                  var yContent = yInput ? yInput.value : y.innerText;
                  var xNum = parseFloat(xContent);
                  var yNum = parseFloat(yContent);
                  var isNum = !isNaN(xNum) && !isNaN(yNum) && xContent.trim() !== "" && yContent.trim() !== "";

                  if (dir == "asc") {{
                    if (isNum) {{ if (xNum > yNum) shouldSwitch = true; }}
                    else {{ if (xContent.toLowerCase() > yContent.toLowerCase()) shouldSwitch = true; }}
                  }} else if (dir == "desc") {{
                    if (isNum) {{ if (xNum < yNum) shouldSwitch = true; }}
                    else {{ if (xContent.toLowerCase() < yContent.toLowerCase()) shouldSwitch = true; }}
                  }}
                  if (shouldSwitch) {{
                    rows[i].parentNode.insertBefore(rows[i + 1], rows[i]);
                    switching = true;
                    switchcount = switchcount + 1;
                    break;
                  }}
                }}
              }}
            }}
            </script>
        </div>'''
    else:
        # Assignment step - check for mode
        amode = request.args.get('amode', '')
        if not amode:
            # Show mode selection
            main_content = f'''<div class="card">
            <h2>Assignment Entry Mode</h2>
            <p class="text-muted">Select how you want to enter assignment marks:</p>
            <div style="display: flex; gap: 1rem; margin-top: 1rem;">
                <a href="/pg/module/{id}?step=assignment&amode=single" class="btn btn-primary" style="flex:1; text-align:center; padding: 2rem;">
                    <div style="font-size: 2rem;">📝</div>
                    <div><strong>Single Assignment</strong></div>
                    <div style="font-size: 0.8rem; color: rgba(255,255,255,0.9);">Direct 50 marks entry</div>
                </a>
                <a href="/pg/module/{id}?step=assignment&amode=multi" class="btn btn-success" style="flex:1; text-align:center; padding: 2rem;">
                    <div style="font-size: 2rem;">📚</div>
                    <div><strong>3 Assignments</strong></div>
                    <div style="font-size: 0.8rem; color: rgba(255,255,255,0.9);">40×3 = 120 → Scaled to 50</div>
                </a>
            </div>
        </div>'''
        elif amode == 'single':
            main_content = f'''<div class="card">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <h2>Assignment Entry (Max 50)</h2>
                <div>
                    <form action="/pg/module/{id}/gaussian/single" method="post" style="display:inline;">
                        <button class="btn btn-success btn-sm">📊 Gaussian Assign</button>
                    </form>
                    <form action="/pg/module/{id}/copy_gaussian/assignment" method="post" style="display:inline; margin-left:5px;">
                         <button class="btn btn-sm" style="background:#6366f1; color:white;" type="button" onclick="customConfirmForm(event, 'Overwrite Actual marks with Gaussian marks?', this.closest('form'))">© Copy Gaussian to Actual</button>
                    </form>
                    <button class="btn btn-sm" style="background:#dc2626; color:white; margin-left:5px;" onclick="document.getElementById('deleteModal_assignment').style.display='block'">🗑️ Delete</button>
                    
                    <!-- Delete Modal -->
                    <div id="deleteModal_assignment" class="modal" style="display:none; position:fixed; z-index:100; left:0; top:0; width:100%; height:100%; overflow:auto; background-color:rgba(0,0,0,0.4); padding-top:100px;">
                        <div class="modal-content" style="background-color:#fefefe; margin:auto; padding:20px; border:1px solid #888; width:40%; border-radius:8px; box-shadow:0 4px 6px rgba(0,0,0,0.1);">
                            <h3 style="margin-top:0; color:#dc2626;">🗑️ Delete Assignment Marks</h3>
                            <p>Which marks do you want to delete?</p>
                            <form action="/pg/module/{id}/delete_marks/assignment" method="post">
                                <div style="display:flex; flex-direction:column; gap:10px; margin-top:15px;">
                                    <button name="delete_type" value="actual" class="btn" style="background:#3b82f6; color:white; padding:10px;">Delete Actual Marks Only</button>
                                    <button name="delete_type" value="gaussian" class="btn" style="background:#10b981; color:white; padding:10px;">Delete Gaussian Marks Only</button>
                                    <button name="delete_type" value="both" class="btn" style="background:#dc2626; color:white; padding:10px;">Delete BOTH Actual & Gaussian</button>
                                </div>
                            </form>
                            <div style="text-align:right; margin-top:20px;">
                                <button class="btn btn-outline" onclick="document.getElementById('deleteModal_assignment').style.display='none'">Cancel</button>
                            </div>
                        </div>
                    </div>
                    
                    <a href="/pg/module/{id}/classify/single" class="btn btn-outline btn-sm" style="margin-left:5px;" title="Student Classification Settings">⚙️</a>
                    <a href="/pg/module/{id}?step=assignment" class="btn btn-sm" style="background:#e5e7eb; margin-left: 10px;">← Change Mode</a>
                </div>
            </div>
            <div class="mb-3" style="background: #f0f9ff; padding: 1rem; border-radius: 10px; border: 1px solid #bae6fd;">
                <h4 style="color: #0369a1; margin-bottom: 0.5rem;">📋 Manual Import (USN[Tab]Marks)</h4>
                <form action="/pg/module/{id}/manual/assignment" method="post">
                    <textarea name="paste_text" class="form-control" rows="3" placeholder="P25E03DE001&#9;45&#10;P25E03DE002&#9;38"></textarea>
                    <button class="btn btn-success btn-sm mt-2">⚡ Parse & Import</button>
                </form>
            </div>
            <form action="/pg/module/{id}/save/assignment_with_cat" method="post">
                <table id="assignTable">
                    <thead>
                        <tr style="cursor:pointer;">
                            <th onclick="sortTable('assignTable', 0)">USN</th>
                            <th onclick="sortTable('assignTable', 1)">Name</th>
                            <th onclick="sortTable('assignTable', 2)">Assignment (/50)</th>
                            <th onclick="sortTable('assignTable', 3)">Gaussian (/50)</th>
                        </tr>
                    </thead>
                    <tbody>
                {{%- set cats = classifications|default({{}}) -%}}
                {{% for s in students_with_marks %}}
                <tr><td><strong>{{{{ s.usn }}}}</strong><input type="hidden" name="sid_{{{{ loop.index0 }}}}" value="{{{{ s.id }}}}"></td><td>{{{{ s.name }}}}</td><td><input type="number" name="val_{{{{ loop.index0 }}}}" value="{{{{ s.assignment }}}}" class="marks-input" min="0" max="50"></td><td><input type="number" name="val_g_{{{{ loop.index0 }}}}" value="{{{{ s.assignment_gaussian }}}}" class="marks-input" min="0" max="50"></td></tr>
                {{% endfor %}}
                    </tbody>
                </table>
                <button class="btn btn-primary mt-2">💾 Save Marks</button>
            </form>
            <style>
            th.asc::after {{ content: " ↑"; }}
            th.desc::after {{ content: " ↓"; }}
            th {{ cursor: pointer; user-select: none; }}
            </style>
            <script>
            function sortTable(tableId, n) {{
              var table, rows, switching, i, x, y, shouldSwitch, dir, switchcount = 0;
              table = document.getElementById(tableId);
              // Reset headers
              var headers = table.getElementsByTagName("TH");
              if (table.getAttribute("data-sort-col") != n) {{
                  for (var h of headers) h.className = "";
                  dir = "asc";
              }} else {{
                  dir = table.getAttribute("data-sort-dir") == "asc" ? "desc" : "asc";
              }}
              headers[n].className = dir;
              table.setAttribute("data-sort-col", n);
              table.setAttribute("data-sort-dir", dir);

              switching = true;
              while (switching) {{
                switching = false;
                var tbody = table.tBodies[0];
                rows = tbody.rows;
                for (i = 0; i < (rows.length - 1); i++) {{
                  shouldSwitch = false;
                  x = rows[i].getElementsByTagName("TD")[n];
                  y = rows[i + 1].getElementsByTagName("TD")[n];
                  var xInput = x.getElementsByTagName("input")[0];
                  var yInput = y.getElementsByTagName("input")[0];
                  var xContent = xInput ? xInput.value : x.innerText;
                  var yContent = yInput ? yInput.value : y.innerText;
                  var xNum = parseFloat(xContent);
                  var yNum = parseFloat(yContent);
                  var isNum = !isNaN(xNum) && !isNaN(yNum) && xContent.trim() !== "" && yContent.trim() !== "";

                  if (dir == "asc") {{
                    if (isNum) {{ if (xNum > yNum) shouldSwitch = true; }}
                    else {{ if (xContent.toLowerCase() > yContent.toLowerCase()) shouldSwitch = true; }}
                  }} else if (dir == "desc") {{
                    if (isNum) {{ if (xNum < yNum) shouldSwitch = true; }}
                    else {{ if (xContent.toLowerCase() < yContent.toLowerCase()) shouldSwitch = true; }}
                  }}
                  if (shouldSwitch) {{
                    rows[i].parentNode.insertBefore(rows[i + 1], rows[i]);
                    switching = true;
                    switchcount = switchcount + 1;
                    break;
                  }}
                }}
              }}
            }}
            </script>
            
            <!-- Grade Distribution Charts -->
            <div style="margin-top: 2rem; background: #fff; padding: 1rem; border-radius: 8px; border: 1px solid #e2e8f0; margin-bottom: 1rem;">
                <h5 style="margin-top: 0; color: #4b5563;">📌 Grade Legend (Max 50)</h5>
                <div style="display: flex; gap: 1rem; flex-wrap: wrap; font-size: 0.9rem;">
                    <span style="color:#22c55e;"><strong>O</strong>: 46-50</span>
                    <span style="color:#84cc16;"><strong>A+</strong>: 41-45</span>
                    <span style="color:#eab308;"><strong>A</strong>: 36-40</span>
                    <span style="color:#f97316;"><strong>B+</strong>: 31-35</span>
                    <span style="color:#3b82f6;"><strong>B</strong>: 25-30</span>
                    <span style="color:#ef4444;"><strong>C</strong>: &lt; 25</span>
                </div>
            </div>
            <h3>📊 Grade Distribution</h3>
            <div style="display: flex; gap: 2rem; margin-top: 1rem;">
                <div style="flex: 1; background: #f8fafc; padding: 1rem; border-radius: 10px;">
                    <h4 style="text-align: center; color: #3b82f6;">Actual Marks</h4>
                    <canvas id="actualChart" height="200"></canvas>
                </div>
                <div style="flex: 1; background: #f0fdf4; padding: 1rem; border-radius: 10px;">
                    <h4 style="text-align: center; color: #10b981;">Gaussian Marks</h4>
                    <canvas id="gaussianChart" height="200"></canvas>
                </div>
            </div>
            
            <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
            <script>
            // Grade Calculation: Scale /50 to /100 with ceiling
            function getGrade(mark50) {{
                var mark100 = Math.ceil((mark50 / 50) * 100);
                if (mark100 >= 91) return 'O';
                if (mark100 >= 81) return 'A+';
                if (mark100 >= 71) return 'A';
                if (mark100 >= 61) return 'B+';
                if (mark100 >= 50) return 'B';
                return 'C';
            }}
            
            function calculateGrades(colIndex) {{
                var grades = {{'O': 0, 'A+': 0, 'A': 0, 'B+': 0, 'B': 0, 'C': 0}};
                var rows = document.getElementById('assignTable').tBodies[0].rows;
                for (var i = 0; i < rows.length; i++) {{
                    var input = rows[i].getElementsByTagName('TD')[colIndex].getElementsByTagName('input')[0];
                    if (input) {{
                        var val = parseFloat(input.value) || 0;
                        var grade = getGrade(val);
                        grades[grade]++;
                    }}
                }}
                return grades;
            }}
            
            // Chart Instances
            var actualChartInstance = null;
            var gaussianChartInstance = null;
            var chartColors = ['#22c55e', '#84cc16', '#facc15', '#f97316', '#3b82f6', '#ef4444'];
            
            function renderCharts() {{
                var actualGrades = calculateGrades(2);  // Column 2 = Assignment
                var gaussianGrades = calculateGrades(3); // Column 3 = Gaussian
                
                var labels = ['O', 'A+', 'A', 'B+', 'B', 'C'];
                var actualData = labels.map(function(g) {{ return actualGrades[g]; }});
                var gaussianData = labels.map(function(g) {{ return gaussianGrades[g]; }});
                
                // Destroy existing charts
                if (actualChartInstance) actualChartInstance.destroy();
                if (gaussianChartInstance) gaussianChartInstance.destroy();
                
                // Actual Chart
                actualChartInstance = new Chart(document.getElementById('actualChart'), {{
                    type: 'bar',
                    data: {{
                        labels: labels,
                        datasets: [{{
                            label: 'Students',
                            data: actualData,
                            backgroundColor: chartColors
                        }}]
                    }},
                    options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }} }}
                }});
                
                // Gaussian Chart
                gaussianChartInstance = new Chart(document.getElementById('gaussianChart'), {{
                    type: 'bar',
                    data: {{
                        labels: labels,
                        datasets: [{{
                            label: 'Students',
                            data: gaussianData,
                            backgroundColor: chartColors
                        }}]
                    }},
                    options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }} }}
                }});
            }}
            
            // Attach oninput handlers for real-time updates
            document.querySelectorAll('#assignTable input[type=number]').forEach(function(input) {{
                input.addEventListener('input', renderCharts);
            }});
            
            // Initial render
            window.addEventListener('load', renderCharts);
            </script>
        </div>'''
        else:  # amode == 'multi'
            # Get individual assignment marks for each student
            a1_marks = {}
            a2_marks = {}
            a3_marks = {}
            for s in students_with_marks:
                m1 = db.execute('SELECT value FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (s['id'], id, 'assignment1')).fetchone()
                m2 = db.execute('SELECT value FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (s['id'], id, 'assignment2')).fetchone()
                m3 = db.execute('SELECT value FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (s['id'], id, 'assignment3')).fetchone()
                a1_marks[s['id']] = m1['value'] if m1 else 0
                a2_marks[s['id']] = m2['value'] if m2 else 0
                a3_marks[s['id']] = m3['value'] if m3 else 0
            
            main_content = f'''<div class="card">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <h2>Multi-Assignment Entry (3×40 = 120 → 50)</h2>
                <a href="/pg/module/{id}?step=assignment" class="btn btn-sm" style="background:#e5e7eb;">← Change Mode</a>
            </div>
            
            <div style="margin: 1rem 0;">
                <div style="display: flex; gap: 0.5rem; border-bottom: 2px solid #e5e7eb; padding-bottom: 0.5rem;">
                    <button onclick="showTab('tab1')" class="btn btn-sm" id="btn-tab1" style="background:#3b82f6; color:white;">Assignment 1</button>
                    <button onclick="showTab('tab2')" class="btn btn-sm" id="btn-tab2">Assignment 2</button>
                    <button onclick="showTab('tab3')" class="btn btn-sm" id="btn-tab3">Assignment 3</button>
                    <button onclick="showTab('tabC')" class="btn btn-sm" id="btn-tabC" style="background:#22c55e; color:white;">📊 Consolidated</button>
                </div>
            </div>
            
            <div id="tab1" class="tab-content">
                <h4>Assignment 1 (Max 40)</h4>
                {{% if preview_data.get('1') %}}
                    <div class="mb-3" style="background:white; padding:1rem; border-radius:10px; border:2px solid #eab308; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);">
                        <h4 style="color:#ca8a04; display:flex; align-items:center; gap:0.5rem;">⚠️ Previewing Assignment 1 Import</h4>
                        <div style="overflow-x: auto; margin: 1rem 0;">
                            <table style="border-collapse: collapse; width: 100%; font-size: 0.85rem;">
                                <tr style="background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);">
                                    <th style="color:white; padding:0.5rem;">USN</th>
                                    <th style="color:white; padding:0.5rem;">Name</th>
                                    <th style="background:#059669; color:white; padding:0.5rem; text-align:center;">Scored</th>
                                    <th style="background:#2563eb; color:white; padding:0.5rem; text-align:center;">Converted</th>
                                    {{% for i in range(1, 13) %}}<th style="color:white; padding:0.25rem; text-align:center;">Q{{{{i}}}}</th>{{% endfor %}}
                                </tr>
                                {{% for row in preview_data['1'] %}}
                                <tr style="background: {{{{ '#dcfce7' if row.student_id else '#fee2e2' }}}}; border-bottom: 1px solid #e5e7eb;">
                                    <td style="padding:0.5rem;"><strong>{{{{ row.usn }}}}</strong></td>
                                    <td style="padding:0.5rem;">{{{{ row.name }}}}</td>
                                    <td style="padding:0.5rem; text-align:center; font-weight:bold; color:#059669;">{{{{ row.marks_scored }}}}</td>
                                    <td style="padding:0.5rem; text-align:center; font-weight:bold; color:#2563eb;">{{{{ row.converted }}}}</td>
                                    {{% for q in row.q_values[:12] %}}<td style="padding:0.25rem; text-align:center; border-left:1px solid #eee;">{{{{ q }}}}</td>{{% endfor %}}
                                </tr>
                                {{% endfor %}}
                            </table>
                        </div>
                        <form action="/pg/module/{id}/confirm/assignment1" method="post">
                            <p style="font-weight:600;">Choose marks to save (Main):</p>
                            <div style="display:flex; gap:1.5rem; margin-bottom:1rem;">
                                <label style="display:flex; align-items:center; gap:0.5rem; cursor:pointer;">
                                    <input type="radio" name="mark_column" value="marks_scored" checked>
                                    <span style="color:#059669; font-weight:bold;">Marks Scored</span>
                                </label>
                                <label style="display:flex; align-items:center; gap:0.5rem; cursor:pointer;">
                                    <input type="radio" name="mark_column" value="converted">
                                    <span style="color:#2563eb; font-weight:bold;">Converted</span>
                                </label>
                            </div>
                            <div style="display:flex; gap:0.5rem;">
                                <button class="btn btn-success">✓ Confirm Import</button>
                                <a href="/pg/module/{id}/cancel_preview" class="btn btn-outline" style="color:#ef4444; border-color:#ef4444;">Cancel</a>
                            </div>
                        </form>
                    </div>
                {{% else %}}
                    <div class="mb-3" style="background: #fef3c7; padding: 1rem; border-radius: 10px; border: 1px solid #fcd34d;">
                        <h5 style="color: #92400e;">📋 Paste Multi-Column Format</h5>
                        <form action="/pg/module/{id}/preview/assignment1" method="post">
                            <textarea name="paste_text" class="form-control" rows="4" placeholder="Paste full table including header..."></textarea>
                            <button class="btn btn-warning btn-sm mt-2">⚡ Preview Assignment 1</button>
                        </form>
                    </div>
                    <div style="display:flex; justify-content:space-between; margin-bottom:0.5rem;">
                             <button class="btn btn-outline btn-sm" style="color:#ef4444; border-color:#ef4444;" onclick="document.getElementById('deleteModal_assignment1').style.display='block'">🗑️ Delete A1</button>
                             
                             <!-- Delete Modal A1 -->
                             <div id="deleteModal_assignment1" class="modal" style="display:none; position:fixed; z-index:100; left:0; top:0; width:100%; height:100%; overflow:auto; background-color:rgba(0,0,0,0.4); padding-top:100px;">
                                <div class="modal-content" style="background-color:#fefefe; margin:auto; padding:20px; border:1px solid #888; width:40%; border-radius:8px; box-shadow:0 4px 6px rgba(0,0,0,0.1);">
                                    <h3 style="margin-top:0; color:#dc2626;">🗑️ Delete Assignment 1 Marks</h3>
                                    <p>Which marks do you want to delete?</p>
                                    <form action="/pg/module/{id}/delete_marks/assignment1" method="post">
                                        <div style="display:flex; flex-direction:column; gap:10px; margin-top:15px;">
                                            <button name="delete_type" value="actual" class="btn" style="background:#3b82f6; color:white; padding:10px;">Delete Actual Marks Only</button>
                                            <button name="delete_type" value="gaussian" class="btn" style="background:#10b981; color:white; padding:10px;">Delete Gaussian Marks Only</button>
                                            <button name="delete_type" value="both" class="btn" style="background:#dc2626; color:white; padding:10px;">Delete BOTH Actual & Gaussian</button>
                                        </div>
                                    </form>
                                    <div style="text-align:right; margin-top:20px;">
                                        <button class="btn btn-outline" onclick="document.getElementById('deleteModal_assignment1').style.display='none'">Cancel</button>
                                    </div>
                                </div>
                            </div>
                             
                             <form action="/pg/module/{id}/gaussian/assignment1" method="post" style="display:inline;">
                                <button class="btn btn-success btn-sm">📊 Gaussian Assign A1</button>
                            </form>
                            <form action="/pg/module/{id}/copy_gaussian/assignment1" method="post" style="display:inline; margin-left:5px;">
                                 <button class="btn btn-sm" style="background:#6366f1; color:white;" type="button" onclick="customConfirmForm(event, 'Overwrite Actual marks with Gaussian marks?', this.closest('form'))">© Copy Gaussian</button>
                            </form>
                            <a href="/pg/module/{id}/classify/assignment1" class="btn btn-outline btn-sm" style="margin-left:5px;" title="Student Classification Settings">⚙️</a>
                        </div>
                    <form action="/pg/module/{id}/save/assignment1" method="post">
                        <div style="overflow-x: auto;">
                            <table style="border-collapse: collapse; width: 100%; font-size: 0.85rem;">
                                <tr style="background: #ea580c; border-bottom: 2px solid #9a3412;">
                                    <th style="color:black; padding:0.5rem; text-align:left;">USN</th>
                                    <th style="color:black; padding:0.5rem; text-align:left;">Name</th>
                                    <th style="background:#059669; color:white; padding:0.5rem; text-align:center;">Scored</th>
                                    <th style="background:#2563eb; color:white; padding:0.5rem; text-align:center;">Converted</th>
                                    {{% for i in range(1, 13) %}}<th style="color:black; padding:0.25rem; text-align:center;">Q{{{{i}}}}</th>{{% endfor %}}
                                    <th style="background:#7c3aed; color:white; padding:0.5rem; text-align:center;">Gaussian</th>
                                    <th style="color:black; padding:0.5rem; text-align:center;">Final</th>
                                </tr>
                                {{% for s in students_with_marks %}}
                                {{% set details = marks_details.get((s.id, 'assignment1'), {{}}) %}}
                                {{% set q_vals = details.get('q_values', []) %}}
                                <tr style="border-bottom: 1px solid #e5e7eb;">
                                    <td style="padding:0.5rem;"><strong>{{{{ s.usn }}}}</strong><input type="hidden" name="sid_{{{{ loop.index0 }}}}" value="{{{{ s.id }}}}"></td>
                                    <td style="padding:0.5rem;">{{{{ s.name }}}}</td>
                                    {{% set v1 = marks_data.get((s.id, 'assignment1_scored'), dict()).get('value', '-') %}}
                                    <td style="padding:0.5rem; text-align:center; color:#059669;">
                                        {{{{ '<span style="color:#ef4444;font-weight:bold;cursor:help;border-bottom:2px dotted #ef4444;" title="Zero Detected: Please verify mark">0</span>'|safe if v1 == 0 or v1 == 0.0 or v1 == '0' else v1 }}}}
                                    </td>
                                    {{% set v2 = marks_data.get((s.id, 'assignment1_converted'), dict()).get('value', '-') %}}
                                    <td style="padding:0.5rem; text-align:center; color:#2563eb;">
                                        {{{{ '<span style="color:#ef4444;font-weight:bold;cursor:help;border-bottom:2px dotted #ef4444;" title="Zero Detected: Please verify mark">0</span>'|safe if v2 == 0 or v2 == 0.0 or v2 == '0' else v2 }}}}
                                    </td>
                                    {{% for i in range(12) %}}
                                        <td style="padding:0.25rem; text-align:center; border-left:1px solid #eee; color:#6b7280;">
                                            {{{{ q_vals[i] if i < len(q_vals) else '-' }}}}
                                        </td>
                                    {{% endfor %}}
                                    {{% set vg = marks_data.get((s.id, 'assignment1_gaussian'), dict()).get('value', '-') %}}
                                    <td style="padding:0.5rem; text-align:center; color:#7c3aed; font-weight:bold;">
                                        {{{{ '<span style="color:#ef4444;font-weight:bold;cursor:help;border-bottom:2px dotted #ef4444;" title="Zero Detected: Please verify mark">0</span>'|safe if vg == 0 or vg == 0.0 or vg == '0' else vg }}}}
                                    </td>
                                    <td style="padding:0.5rem; text-align:center;">
                                        <input type="number" name="val_{{{{ loop.index0 }}}}" value="{{{{ a1_marks.get(s.id, 0) }}}}" class="marks-input" min="0" max="40" style="width:60px; font-weight:bold;">
                                    </td>
                                </tr>
                                {{% endfor %}}
                            </table>
                        </div>
                        <button class="btn btn-primary mt-2">💾 Save A1</button>
                    </form>
                {{% endif %}}
            </div>
            
            <div id="tab2" class="tab-content" style="display:none;">
                <h4>Assignment 2 (Max 40)</h4>
                {{% if preview_data.get('2') %}}
                    <div class="mb-3" style="background:white; padding:1rem; border-radius:10px; border:2px solid #3b82f6; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);">
                        <h4 style="color:#1d4ed8; display:flex; align-items:center; gap:0.5rem;">⚠️ Previewing Assignment 2 Import</h4>
                        <div style="overflow-x: auto; margin: 1rem 0;">
                            <table style="border-collapse: collapse; width: 100%; font-size: 0.85rem;">
                                <tr style="background: linear-gradient(135deg, #3b82f6 0%, #1d4ed8 100%);">
                                    <th style="color:white; padding:0.5rem;">USN</th>
                                    <th style="color:white; padding:0.5rem;">Name</th>
                                    <th style="background:#059669; color:white; padding:0.5rem; text-align:center;">Scored</th>
                                    <th style="background:#2563eb; color:white; padding:0.5rem; text-align:center;">Converted</th>
                                    {{% for i in range(1, 13) %}}<th style="color:white; padding:0.25rem; text-align:center;">Q{{{{i}}}}</th>{{% endfor %}}
                                </tr>
                                {{% for row in preview_data['2'] %}}
                                <tr style="background: {{{{ '#dcfce7' if row.student_id else '#fee2e2' }}}}; border-bottom: 1px solid #e5e7eb;">
                                    <td style="padding:0.5rem;"><strong>{{{{ row.usn }}}}</strong></td>
                                    <td style="padding:0.5rem;">{{{{ row.name }}}}</td>
                                    <td style="padding:0.5rem; text-align:center; font-weight:bold; color:#059669;">{{{{ row.marks_scored }}}}</td>
                                    <td style="padding:0.5rem; text-align:center; font-weight:bold; color:#2563eb;">{{{{ row.converted }}}}</td>
                                    {{% for q in row.q_values[:12] %}}<td style="padding:0.25rem; text-align:center; border-left:1px solid #eee;">{{{{ q }}}}</td>{{% endfor %}}
                                </tr>
                                {{% endfor %}}
                            </table>
                        </div>
                        <form action="/pg/module/{id}/confirm/assignment2" method="post">
                            <p style="font-weight:600;">Choose marks to save (Main):</p>
                            <div style="display:flex; gap:1.5rem; margin-bottom:1rem;">
                                <label style="display:flex; align-items:center; gap:0.5rem; cursor:pointer;">
                                    <input type="radio" name="mark_column" value="marks_scored" checked>
                                    <span style="color:#059669; font-weight:bold;">Marks Scored</span>
                                </label>
                                <label style="display:flex; align-items:center; gap:0.5rem; cursor:pointer;">
                                    <input type="radio" name="mark_column" value="converted">
                                    <span style="color:#2563eb; font-weight:bold;">Converted</span>
                                </label>
                            </div>
                            <div style="display:flex; gap:0.5rem;">
                                <button class="btn btn-success">✓ Confirm Import</button>
                                <a href="/pg/module/{id}/cancel_preview" class="btn btn-outline" style="color:#ef4444; border-color:#ef4444;">Cancel</a>
                            </div>
                        </form>
                    </div>
                {{% else %}}
                    <div class="mb-3" style="background: #dbeafe; padding: 1rem; border-radius: 10px; border: 1px solid #93c5fd;">
                        <h5 style="color: #1e40af;">📋 Paste Multi-Column Format</h5>
                        <form action="/pg/module/{id}/preview/assignment2" method="post">
                            <textarea name="paste_text" class="form-control" rows="4" placeholder="Paste full table including header..."></textarea>
                            <button class="btn btn-primary btn-sm mt-2">⚡ Preview Assignment 2</button>
                        </form>
                    </div>
                    <div style="display:flex; justify-content:space-between; margin-bottom:0.5rem;">
                             <button class="btn btn-outline btn-sm" style="color:#ef4444; border-color:#ef4444;" onclick="document.getElementById('deleteModal_assignment2').style.display='block'">🗑️ Delete A2</button>
                             
                             <!-- Delete Modal A2 -->
                             <div id="deleteModal_assignment2" class="modal" style="display:none; position:fixed; z-index:100; left:0; top:0; width:100%; height:100%; overflow:auto; background-color:rgba(0,0,0,0.4); padding-top:100px;">
                                <div class="modal-content" style="background-color:#fefefe; margin:auto; padding:20px; border:1px solid #888; width:40%; border-radius:8px; box-shadow:0 4px 6px rgba(0,0,0,0.1);">
                                    <h3 style="margin-top:0; color:#dc2626;">🗑️ Delete Assignment 2 Marks</h3>
                                    <p>Which marks do you want to delete?</p>
                                    <form action="/pg/module/{id}/delete_marks/assignment2" method="post">
                                        <div style="display:flex; flex-direction:column; gap:10px; margin-top:15px;">
                                            <button name="delete_type" value="actual" class="btn" style="background:#3b82f6; color:white; padding:10px;">Delete Actual Marks Only</button>
                                            <button name="delete_type" value="gaussian" class="btn" style="background:#10b981; color:white; padding:10px;">Delete Gaussian Marks Only</button>
                                            <button name="delete_type" value="both" class="btn" style="background:#dc2626; color:white; padding:10px;">Delete BOTH Actual & Gaussian</button>
                                        </div>
                                    </form>
                                    <div style="text-align:right; margin-top:20px;">
                                        <button class="btn btn-outline" onclick="document.getElementById('deleteModal_assignment2').style.display='none'">Cancel</button>
                                    </div>
                                </div>
                            </div>
                             
                             <form action="/pg/module/{id}/gaussian/assignment2" method="post" style="display:inline;">
                                <button class="btn btn-success btn-sm">📊 Gaussian Assign A2</button>
                            </form>
                            <form action="/pg/module/{id}/copy_gaussian/assignment2" method="post" style="display:inline; margin-left:5px;">
                                 <button class="btn btn-sm" style="background:#6366f1; color:white;" type="button" onclick="customConfirmForm(event, 'Overwrite Actual marks with Gaussian marks?', this.closest('form'))">© Copy Gaussian</button>
                            </form>
                            <a href="/pg/module/{id}/classify/assignment2" class="btn btn-outline btn-sm" style="margin-left:5px;" title="Student Classification Settings">⚙️</a>
                        </div>
                    <form action="/pg/module/{id}/save/assignment2" method="post">
                        <div style="overflow-x: auto;">
                            <table style="border-collapse: collapse; width: 100%; font-size: 0.85rem;">
                                <tr style="background: #2563eb; border-bottom: 2px solid #1e40af;">
                                    <th style="color:black; padding:0.5rem; text-align:left;">USN</th>
                                    <th style="color:black; padding:0.5rem; text-align:left;">Name</th>
                                    <th style="background:#059669; color:white; padding:0.5rem; text-align:center;">Scored</th>
                                    <th style="background:#2563eb; color:white; padding:0.5rem; text-align:center;">Converted</th>
                                    {{% for i in range(1, 13) %}}<th style="color:black; padding:0.25rem; text-align:center;">Q{{{{i}}}}</th>{{% endfor %}}
                                    <th style="background:#7c3aed; color:white; padding:0.5rem; text-align:center;">Gaussian</th>
                                    <th style="color:black; padding:0.5rem; text-align:center;">Final</th>
                                </tr>
                                {{% for s in students_with_marks %}}
                                {{% set details = marks_details.get((s.id, 'assignment2'), {{}}) %}}
                                {{% set q_vals = details.get('q_values', []) %}}
                                <tr style="border-bottom: 1px solid #e5e7eb;">
                                    <td style="padding:0.5rem;"><strong>{{{{ s.usn }}}}</strong><input type="hidden" name="sid_{{{{ loop.index0 }}}}" value="{{{{ s.id }}}}"></td>
                                    <td style="padding:0.5rem;">{{{{ s.name }}}}</td>
                                    {{% set v1 = marks_data.get((s.id, 'assignment2_scored'), dict()).get('value', '-') %}}
                                    <td style="padding:0.5rem; text-align:center; color:#059669;">
                                        {{{{ '<span style="color:#ef4444;font-weight:bold;cursor:help;border-bottom:2px dotted #ef4444;" title="Zero Detected: Please verify mark">0</span>'|safe if v1 == 0 or v1 == 0.0 or v1 == '0' else v1 }}}}
                                    </td>
                                    {{% set v2 = marks_data.get((s.id, 'assignment2_converted'), dict()).get('value', '-') %}}
                                    <td style="padding:0.5rem; text-align:center; color:#2563eb;">
                                        {{{{ '<span style="color:#ef4444;font-weight:bold;cursor:help;border-bottom:2px dotted #ef4444;" title="Zero Detected: Please verify mark">0</span>'|safe if v2 == 0 or v2 == 0.0 or v2 == '0' else v2 }}}}
                                    </td>
                                    {{% for i in range(12) %}}
                                        <td style="padding:0.25rem; text-align:center; border-left:1px solid #eee; color:#6b7280;">
                                            {{{{ q_vals[i] if i < len(q_vals) else '-' }}}}
                                        </td>
                                    {{% endfor %}}
                                    {{% set vg = marks_data.get((s.id, 'assignment2_gaussian'), dict()).get('value', '-') %}}
                                    <td style="padding:0.5rem; text-align:center; color:#7c3aed; font-weight:bold;">
                                        {{{{ '<span style="color:#ef4444;font-weight:bold;cursor:help;border-bottom:2px dotted #ef4444;" title="Zero Detected: Please verify mark">0</span>'|safe if vg == 0 or vg == 0.0 or vg == '0' else vg }}}}
                                    </td>
                                    <td style="padding:0.5rem; text-align:center;">
                                        <input type="number" name="val_{{{{ loop.index0 }}}}" value="{{{{ a2_marks.get(s.id, 0) }}}}" class="marks-input" min="0" max="40" style="width:60px; font-weight:bold;">
                                    </td>
                                </tr>
                                {{% endfor %}}
                            </table>
                        </div>
                        <button class="btn btn-primary mt-2">💾 Save A2</button>
                    </form>
                {{% endif %}}
            </div>
            
            <div id="tab3" class="tab-content" style="display:none;">
                <h4>Assignment 3 (Max 40)</h4>
                {{% if preview_data.get('3') %}}
                    <div class="mb-3" style="background:white; padding:1rem; border-radius:10px; border:2px solid #db2777; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);">
                        <h4 style="color:#be185d; display:flex; align-items:center; gap:0.5rem;">⚠️ Previewing Assignment 3 Import</h4>
                        <div style="overflow-x: auto; margin: 1rem 0;">
                            <table style="border-collapse: collapse; width: 100%; font-size: 0.85rem;">
                                <tr style="background: linear-gradient(135deg, #db2777 0%, #be185d 100%);">
                                    <th style="color:white; padding:0.5rem;">USN</th>
                                    <th style="color:white; padding:0.5rem;">Name</th>
                                    <th style="background:#059669; color:white; padding:0.5rem; text-align:center;">Scored</th>
                                    <th style="background:#2563eb; color:white; padding:0.5rem; text-align:center;">Converted</th>
                                    {{% for i in range(1, 13) %}}<th style="color:white; padding:0.25rem; text-align:center;">Q{{{{i}}}}</th>{{% endfor %}}
                                </tr>
                                {{% for row in preview_data['3'] %}}
                                <tr style="background: {{{{ '#dcfce7' if row.student_id else '#fee2e2' }}}}; border-bottom: 1px solid #e5e7eb;">
                                    <td style="padding:0.5rem;"><strong>{{{{ row.usn }}}}</strong></td>
                                    <td style="padding:0.5rem;">{{{{ row.name }}}}</td>
                                    <td style="padding:0.5rem; text-align:center; font-weight:bold; color:#059669;">{{{{ row.marks_scored }}}}</td>
                                    <td style="padding:0.5rem; text-align:center; font-weight:bold; color:#2563eb;">{{{{ row.converted }}}}</td>
                                    {{% for q in row.q_values[:12] %}}<td style="padding:0.25rem; text-align:center; border-left:1px solid #eee;">{{{{ q }}}}</td>{{% endfor %}}
                                </tr>
                                {{% endfor %}}
                            </table>
                        </div>
                        <form action="/pg/module/{id}/confirm/assignment3" method="post">
                            <p style="font-weight:600;">Choose marks to save (Main):</p>
                            <div style="display:flex; gap:1.5rem; margin-bottom:1rem;">
                                <label style="display:flex; align-items:center; gap:0.5rem; cursor:pointer;">
                                    <input type="radio" name="mark_column" value="marks_scored" checked>
                                    <span style="color:#059669; font-weight:bold;">Marks Scored</span>
                                </label>
                                <label style="display:flex; align-items:center; gap:0.5rem; cursor:pointer;">
                                    <input type="radio" name="mark_column" value="converted">
                                    <span style="color:#2563eb; font-weight:bold;">Converted</span>
                                </label>
                            </div>
                            <div style="display:flex; gap:0.5rem;">
                                <button class="btn btn-success">✓ Confirm Import</button>
                                <a href="/pg/module/{id}/cancel_preview" class="btn btn-outline" style="color:#ef4444; border-color:#ef4444;">Cancel</a>
                            </div>
                        </form>
                    </div>
                {{% else %}}
                    <div class="mb-3" style="background: #fce7f3; padding: 1rem; border-radius: 10px; border: 1px solid #f9a8d4;">
                        <h5 style="color: #9d174d;">📋 Paste Multi-Column Format</h5>
                        <form action="/pg/module/{id}/preview/assignment3" method="post">
                            <textarea name="paste_text" class="form-control" rows="4" placeholder="Paste full table including header..."></textarea>
                            <button class="btn btn-pink btn-sm mt-2" style="background:#ec4899; color:white;">⚡ Preview Assignment 3</button>
                        </form>
                    </div>
                    <div style="display:flex; justify-content:space-between; margin-bottom:0.5rem;">
                             <button class="btn btn-outline btn-sm" style="color:#ef4444; border-color:#ef4444;" onclick="document.getElementById('deleteModal_assignment3').style.display='block'">🗑️ Delete A3</button>
                             
                             <!-- Delete Modal A3 -->
                             <div id="deleteModal_assignment3" class="modal" style="display:none; position:fixed; z-index:100; left:0; top:0; width:100%; height:100%; overflow:auto; background-color:rgba(0,0,0,0.4); padding-top:100px;">
                                <div class="modal-content" style="background-color:#fefefe; margin:auto; padding:20px; border:1px solid #888; width:40%; border-radius:8px; box-shadow:0 4px 6px rgba(0,0,0,0.1);">
                                    <h3 style="margin-top:0; color:#dc2626;">🗑️ Delete Assignment 3 Marks</h3>
                                    <p>Which marks do you want to delete?</p>
                                    <form action="/pg/module/{id}/delete_marks/assignment3" method="post">
                                        <div style="display:flex; flex-direction:column; gap:10px; margin-top:15px;">
                                            <button name="delete_type" value="actual" class="btn" style="background:#3b82f6; color:white; padding:10px;">Delete Actual Marks Only</button>
                                            <button name="delete_type" value="gaussian" class="btn" style="background:#10b981; color:white; padding:10px;">Delete Gaussian Marks Only</button>
                                            <button name="delete_type" value="both" class="btn" style="background:#dc2626; color:white; padding:10px;">Delete BOTH Actual & Gaussian</button>
                                        </div>
                                    </form>
                                    <div style="text-align:right; margin-top:20px;">
                                        <button class="btn btn-outline" onclick="document.getElementById('deleteModal_assignment3').style.display='none'">Cancel</button>
                                    </div>
                                </div>
                            </div>
                             
                             <form action="/pg/module/{id}/gaussian/assignment3" method="post" style="display:inline;">
                                <button class="btn btn-success btn-sm">📊 Gaussian Assign A3</button>
                            </form>
                            <form action="/pg/module/{id}/copy_gaussian/assignment3" method="post" style="display:inline; margin-left:5px;">
                                 <button class="btn btn-sm" style="background:#6366f1; color:white;" type="button" onclick="customConfirmForm(event, 'Overwrite Actual marks with Gaussian marks?', this.closest('form'))">© Copy Gaussian</button>
                            </form>
                            <a href="/pg/module/{id}/classify/assignment3" class="btn btn-outline btn-sm" style="margin-left:5px;" title="Student Classification Settings">⚙️</a>
                        </div>
                    <form action="/pg/module/{id}/save/assignment3" method="post">
                        <div style="overflow-x: auto;">
                            <table style="border-collapse: collapse; width: 100%; font-size: 0.85rem;">
                                <tr style="background: #db2777; border-bottom: 2px solid #be185d;">
                                    <th style="color:black; padding:0.5rem; text-align:left;">USN</th>
                                    <th style="color:black; padding:0.5rem; text-align:left;">Name</th>
                                    <th style="background:#059669; color:white; padding:0.5rem; text-align:center;">Scored</th>
                                    <th style="background:#2563eb; color:white; padding:0.5rem; text-align:center;">Converted</th>
                                    {{% for i in range(1, 13) %}}<th style="color:black; padding:0.25rem; text-align:center;">Q{{{{i}}}}</th>{{% endfor %}}
                                    <th style="background:#7c3aed; color:white; padding:0.5rem; text-align:center;">Gaussian</th>
                                    <th style="color:black; padding:0.5rem; text-align:center;">Final</th>
                                </tr>
                                {{% for s in students_with_marks %}}
                                {{% set details = marks_details.get((s.id, 'assignment3'), {{}}) %}}
                                {{% set q_vals = details.get('q_values', []) %}}
                                <tr style="border-bottom: 1px solid #e5e7eb;">
                                    <td style="padding:0.5rem;"><strong>{{{{ s.usn }}}}</strong><input type="hidden" name="sid_{{{{ loop.index0 }}}}" value="{{{{ s.id }}}}"></td>
                                    <td style="padding:0.5rem;">{{{{ s.name }}}}</td>
                                    {{% set v1 = marks_data.get((s.id, 'assignment3_scored'), dict()).get('value', '-') %}}
                                    <td style="padding:0.5rem; text-align:center; color:#059669;">
                                        {{{{ '<span style="color:#ef4444;font-weight:bold;cursor:help;border-bottom:2px dotted #ef4444;" title="Zero Detected: Please verify mark">0</span>'|safe if v1 == 0 or v1 == 0.0 or v1 == '0' else v1 }}}}
                                    </td>
                                    {{% set v2 = marks_data.get((s.id, 'assignment3_converted'), dict()).get('value', '-') %}}
                                    <td style="padding:0.5rem; text-align:center; color:#2563eb;">
                                        {{{{ '<span style="color:#ef4444;font-weight:bold;cursor:help;border-bottom:2px dotted #ef4444;" title="Zero Detected: Please verify mark">0</span>'|safe if v2 == 0 or v2 == 0.0 or v2 == '0' else v2 }}}}
                                    </td>
                                    {{% for i in range(12) %}}
                                        <td style="padding:0.25rem; text-align:center; border-left:1px solid #eee; color:#6b7280;">
                                            {{{{ q_vals[i] if i < len(q_vals) else '-' }}}}
                                        </td>
                                    {{% endfor %}}
                                    {{% set vg = marks_data.get((s.id, 'assignment3_gaussian'), dict()).get('value', '-') %}}
                                    <td style="padding:0.5rem; text-align:center; color:#7c3aed; font-weight:bold;">
                                        {{{{ '<span style="color:red;font-weight:bold;">0</span>'|safe if vg == 0 or vg == 0.0 or vg == '0' else vg }}}}
                                    </td>
                                    <td style="padding:0.5rem; text-align:center;">
                                        <input type="number" name="val_{{{{ loop.index0 }}}}" value="{{{{ a3_marks.get(s.id, 0) }}}}" class="marks-input" min="0" max="40" style="width:60px; font-weight:bold;">
                                    </td>
                                </tr>
                                {{% endfor %}}
                            </table>
                        </div>
                        <button class="btn btn-primary mt-2">💾 Save A3</button>
                    </form>
                {{% endif %}}
            </div>
            
            <div id="tabC" class="tab-content" style="display:none;">
                <h4>📊 Consolidated Marks</h4>
                <p class="text-muted">Formula: (A1 + A2 + A3) / 120 × 50 = Final Assignment (/50)</p>
                <div style="background:#f0f9ff; padding:1rem; border-radius:10px; border:1px solid #bae6fd; margin-bottom:1.5rem;">
                    <h5 style="color:#0369a1; margin-bottom:0.75rem;">📋 Select Mark Column for Each Assignment</h5>
                    <p style="font-size:0.85rem; color:#6b7280; margin-bottom:1rem;">Choose whether to use "Marks Scored" or "Converted" for consolidation:</p>
                    <form action="/pg/module/{id}/consolidate" method="post">
                        <div style="display:grid; grid-template-columns:repeat(3,1fr); gap:1rem; margin-bottom:1rem;">
                            <div style="background:white; padding:0.75rem; border-radius:8px; border:1px solid #e5e7eb;">
                                <label style="font-weight:600; font-size:0.9rem;">Assignment 1</label>
                                <select name="a1_col" class="form-control" style="margin-top:0.5rem;">
                                    <option value="scored" selected>Marks Scored</option>
                                    <option value="converted">Converted</option>
                                    <option value="gaussian">Gaussian</option>
                                </select>
                            </div>
                            <div style="background:white; padding:0.75rem; border-radius:8px; border:1px solid #e5e7eb;">
                                <label style="font-weight:600; font-size:0.9rem;">Assignment 2</label>
                                <select name="a2_col" class="form-control" style="margin-top:0.5rem;">
                                    <option value="scored" selected>Marks Scored</option>
                                    <option value="converted">Converted</option>
                                    <option value="gaussian">Gaussian</option>
                                </select>
                            </div>
                            <div style="background:white; padding:0.75rem; border-radius:8px; border:1px solid #e5e7eb;">
                                <label style="font-weight:600; font-size:0.9rem;">Assignment 3</label>
                                <select name="a3_col" class="form-control" style="margin-top:0.5rem;">
                                    <option value="scored" selected>Marks Scored</option>
                                    <option value="converted">Converted</option>
                                    <option value="gaussian">Gaussian</option>
                                </select>
                            </div>
                        </div>
                        <div style="display:flex; gap:1rem; flex-wrap:wrap;">
                            <button name="consolidate_type" value="actual" class="btn btn-success">🔄 Consolidate Actual (/50)</button>
                            <button name="consolidate_type" value="gaussian" class="btn btn-primary" style="background:#10b981;">🔄 Consolidate Gaussian (/50)</button>
                        </div>
                        <p style="font-size:0.8rem; color:#6b7280; margin-top:0.75rem;">
                            <strong>Actual</strong>: Uses selected marks for regular grading • 
                            <strong>Gaussian</strong>: Uses selected marks for Gaussian grading reports
                        </p>
                    </form>
                </div>
                <table><tr><th>USN</th><th>Name</th><th>A1 (40)</th><th>A2 (40)</th><th>A3 (40)</th><th>Total (120)</th><th>Actual (/50)</th><th style="background:#10b981;color:white;">Gaussian (/50)</th></tr>
                {{% for s in students_with_marks %}}
                <tr>
                    <td><strong>{{{{ s.usn }}}}</strong></td>
                    <td>{{{{ s.name }}}}</td>
                    <td>{{{{ a1_marks.get(s.id, 0) }}}}</td>
                    <td>{{{{ a2_marks.get(s.id, 0) }}}}</td>
                    <td>{{{{ a3_marks.get(s.id, 0) }}}}</td>
                    <td>{{{{ a1_marks.get(s.id, 0) + a2_marks.get(s.id, 0) + a3_marks.get(s.id, 0) }}}}</td>
                    <td><strong>{{{{ s.assignment }}}}</strong></td>
                    <td style="background:#ecfdf5; color:#10b981; font-weight:bold;">{{{{ s.assignment_gaussian }}}}</td>
                </tr>
                {{% endfor %}}</table>
                
                <!-- Grade Distribution Charts for Multi Assignment -->
                <div style="margin-top: 2rem; background: #fff; padding: 1rem; border-radius: 8px; border: 1px solid #e2e8f0; margin-bottom: 1rem;">
                    <h5 style="margin-top: 0; color: #4b5563;">📌 Grade Legend (Max 50)</h5>
                    <div style="display: flex; gap: 1rem; flex-wrap: wrap; font-size: 0.9rem;">
                        <span style="color:#22c55e;"><strong>O</strong>: 46-50</span>
                        <span style="color:#84cc16;"><strong>A+</strong>: 41-45</span>
                        <span style="color:#eab308;"><strong>A</strong>: 36-40</span>
                        <span style="color:#f97316;"><strong>B+</strong>: 31-35</span>
                        <span style="color:#3b82f6;"><strong>B</strong>: 25-30</span>
                        <span style="color:#ef4444;"><strong>C</strong>: &lt; 25</span>
                    </div>
                </div>
                <h3>📊 Grade Distribution</h3>
                <div style="display: flex; gap: 2rem; margin-top: 1rem;">
                    <div style="flex: 1; background: #f8fafc; padding: 1rem; border-radius: 10px;">
                        <h4 style="text-align: center; color: #3b82f6;">Actual Marks</h4>
                        <canvas id="multiActualChart" height="200"></canvas>
                    </div>
                    <div style="flex: 1; background: #f0fdf4; padding: 1rem; border-radius: 10px;">
                        <h4 style="text-align: center; color: #10b981;">Gaussian Marks</h4>
                        <canvas id="multiGaussianChart" height="200"></canvas>
                    </div>
                </div>
                
                <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
                <script>
                (function() {{
                    // Grade Calculation: Scale /50 to /100 with ceiling
                    function getGradeMulti(mark50) {{
                        var mark100 = Math.ceil((mark50 / 50) * 100);
                        if (mark100 >= 91) return 'O';
                        if (mark100 >= 81) return 'A+';
                        if (mark100 >= 71) return 'A';
                        if (mark100 >= 61) return 'B+';
                        if (mark100 >= 50) return 'B';
                        return 'C';
                    }}
                    
                    // Read from table text (columns 6=Actual, 7=Gaussian, 0-indexed)
                    var table = document.querySelector('#tabC table');
                    if (!table) return;
                    var rows = table.querySelectorAll('tbody tr, tr:not(:first-child)');
                    
                    var actualGrades = {{'O': 0, 'A+': 0, 'A': 0, 'B+': 0, 'B': 0, 'C': 0}};
                    var gaussianGrades = {{'O': 0, 'A+': 0, 'A': 0, 'B+': 0, 'B': 0, 'C': 0}};
                    
                    rows.forEach(function(row) {{
                        var cells = row.querySelectorAll('td');
                        if (cells.length >= 8) {{
                            var actualVal = parseFloat(cells[6].innerText) || 0;
                            var gaussianVal = parseFloat(cells[7].innerText) || 0;
                            actualGrades[getGradeMulti(actualVal)]++;
                            gaussianGrades[getGradeMulti(gaussianVal)]++;
                        }}
                    }});
                    
                    var labels = ['O', 'A+', 'A', 'B+', 'B', 'C'];
                    var chartColors = ['#22c55e', '#84cc16', '#facc15', '#f97316', '#3b82f6', '#ef4444'];
                    var actualData = labels.map(function(g) {{ return actualGrades[g]; }});
                    var gaussianData = labels.map(function(g) {{ return gaussianGrades[g]; }});
                    
                    new Chart(document.getElementById('multiActualChart'), {{
                        type: 'bar',
                        data: {{ labels: labels, datasets: [{{ label: 'Students', data: actualData, backgroundColor: chartColors }}] }},
                        options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }} }}
                    }});
                    
                    new Chart(document.getElementById('multiGaussianChart'), {{
                        type: 'bar',
                        data: {{ labels: labels, datasets: [{{ label: 'Students', data: gaussianData, backgroundColor: chartColors }}] }},
                        options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }} }}
                    }});
                }})();
                </script>
            </div>
            
            <script>
            function showTab(tabId) {{
                document.querySelectorAll('.tab-content').forEach(t => t.style.display = 'none');
                document.querySelectorAll('[id^="btn-tab"]').forEach(b => {{ b.style.background = '#e5e7eb'; b.style.color = '#374151'; }});
                document.getElementById(tabId).style.display = 'block';
                var btn = document.getElementById('btn-' + tabId);
                if(tabId === 'tabC') {{ btn.style.background = '#22c55e'; btn.style.color = 'white'; }}
                else {{ btn.style.background = '#3b82f6'; btn.style.color = 'white'; }}
            }}
            window.onload = function() {{
                if(window.location.hash) {{
                    var tab = window.location.hash.substring(1);
                    if(document.getElementById(tab)) showTab(tab);
                }}
            }};
            </script>
        </div>'''
    content = f'''
    <div class="sidebar">
        <div class="mb-2" style="padding: 0 0.5rem;">
            <small style="color: #6b7280; text-transform: uppercase; font-weight: 600;">{{{{ module.code }}}}</small>
            <h3 style="margin: 0.25rem 0;">{{{{ module.title }}}}</h3>
            <span class="badge badge-primary">Year {{{{ module.year }}}} • {{{{ module.start_year }}}}-{{{{ module.end_year }}}}</span>
        </div>
        <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 1rem 0;">
        {sidebar}
        <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 1rem 0;">
        <a href="/pg/de/batch/{{{{ module.batch_id }}}}/year/{{{{ module.year }}}}" class="sidebar-link">← Back</a>
    </div>
    <div class="main-with-sidebar">
        {alert_script}
        {main_content}
    </div>'''
    return render_template_string(base_html(f'{module["code"]} - CAB', content), module=module, students_with_marks=students_with_marks, a1_marks=a1_marks, a2_marks=a2_marks, a3_marks=a3_marks, preview_data=preview_data, marks_details=marks_details, marks_data=marks_data, classifications=classifications, len=len)

@app.route('/pg/module/<int:id>/save/<step>', methods=['POST'])
def pg_save_marks(id, step):
    db = get_db()
    for key in request.form:
        if key.startswith('sid_'):
            idx = key.split('_')[1]
            student_id = request.form.get(key)
            value = request.form.get(f'val_{idx}', 0)
            try:
                value = float(value) if value else 0
            except:
                value = 0
            existing = db.execute('SELECT id FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (student_id, id, step)).fetchone()
            if existing:
                db.execute('UPDATE pg_marks SET value=%s WHERE id=%s', (value, existing['id']))
            else:
                db.execute('INSERT INTO pg_marks (student_id, module_id, mark_type, value) VALUES (%s, %s, %s, %s)', (student_id, id, step, value))
            
            # Handle Gaussian Manual Input (val_g_{idx})
            val_g = request.form.get(f'val_g_{idx}')
            if val_g is not None:
                try:
                    val_g = float(val_g) if val_g else 0
                except:
                    val_g = 0
                g_step = f'{step}_gaussian'
                existing_g = db.execute('SELECT id FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (student_id, id, g_step)).fetchone()
                if existing_g:
                    db.execute('UPDATE pg_marks SET value=%s WHERE id=%s', (val_g, existing_g['id']))
                else:
                    db.execute('INSERT INTO pg_marks (student_id, module_id, mark_type, value) VALUES (%s, %s, %s, %s)', (student_id, id, g_step, val_g))
    db.commit()
    flash(f'{step.upper()} marks saved!')
    # Redirect back with amode preserved for multi-assignment
    if step in ['assignment1', 'assignment2', 'assignment3']:
        t_id = 'tab' + step[-1]
        return redirect(url_for('pg_module_dashboard', id=id, step='assignment', amode='multi') + '#' + t_id)
    return redirect(url_for('pg_module_dashboard', id=id, step=step))

@app.route('/pg/module/<int:id>/delete/assignment', methods=['POST'])
def pg_delete_all_assignments(id):
    """Delete all assignment marks for a module"""
    db = get_db()
    # Delete both actual and gaussian assignment marks
    db.execute("DELETE FROM pg_marks WHERE module_id=%s AND mark_type LIKE 'assignment%'", (id,))
    db.commit()
@app.route('/pg/module/<int:id>/copy_gaussian/<step>', methods=['POST'])
def pg_copy_gaussian(id, step):
    """Copy Gaussian marks to Actual marks for a given step"""
    db = get_db()
    
    # Identify source (Gaussian) and target (Actual)
    # For A1/A2/A3/SEE, the gaussian type is {step}_gaussian
    source_type = f'{step}_gaussian'
    target_type = step
    
    # Special handling if needed (e.g. for A1/A2/A3 'scored' vs 'converted')
    # Assuming we copy to the main 'value' of the step.
    # For sub-assignments, step might be 'assignment1', 'assignment2' etc.
    
    # Fetch all gaussian marks
    gaussian_marks = db.execute('SELECT student_id, value FROM pg_marks WHERE module_id=%s AND mark_type=%s', (id, source_type)).fetchall()
    
    count = 0
    for row in gaussian_marks:
        sid = row['student_id']
        val = row['value']
        
        # Check if target exists
        existing = db.execute('SELECT id FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (sid, id, target_type)).fetchone()
        
        if existing:
            db.execute('UPDATE pg_marks SET value=%s WHERE id=%s', (val, existing['id']))
        else:
            db.execute('INSERT INTO pg_marks (student_id, module_id, mark_type, value) VALUES (%s, %s, %s, %s)', (sid, id, target_type, val))
        count += 1
        
    db.commit()
    flash(f'Copied {count} Gaussian marks to Actual!')
    
    # Redirect logic
    if step.startswith('assignment'):
        if step in ['assignment1', 'assignment2', 'assignment3']:
            return redirect(url_for('pg_module_dashboard', id=id, step='assignment', amode='multi') + '#tab' + step[-1])
        return redirect(url_for('pg_module_dashboard', id=id, step='assignment', amode='single') if step == 'assignment' else url_for('pg_module_dashboard', id=id, step=step))
    else:
        return redirect(url_for('pg_module_dashboard', id=id, step=step))

@app.route('/pg/module/<int:id>/delete_marks/<step>', methods=['POST'])
def pg_delete_marks(id, step):
    """Generalized delete for Actual/Gaussian/Both"""
    delete_type = request.form.get('delete_type', 'actual') # actual, gaussian, both
    db = get_db()
    
    types_to_delete = []
    
    # Determine mark types based on step and delete_type
    base_type = step
    gaussian_type = f'{step}_gaussian'
    
    if delete_type == 'actual':
        types_to_delete.append(base_type)
        # If A1/A2/A3, might need to handle 'scored' vs 'main' if they differ, but usually we just delete the main one.
        # Actually for A1/A2/A3 preview we have 'scored' and 'converted'. 
        # But 'assignment1' is the main type used for consolidation.
    elif delete_type == 'gaussian':
        types_to_delete.append(gaussian_type)
    elif delete_type == 'both':
        types_to_delete.append(base_type)
        types_to_delete.append(gaussian_type)
        
    for mtype in types_to_delete:
        db.execute('DELETE FROM pg_marks WHERE module_id=%s AND mark_type=%s', (id, mtype))
        
    db.commit()
    flash(f'Deleted {delete_type.upper()} marks for {step}!')
    
    if step.startswith('assignment'):
        if step in ['assignment1', 'assignment2', 'assignment3']:
             return redirect(url_for('pg_module_dashboard', id=id, step='assignment', amode='multi') + '#tab' + step[-1])
        return redirect(url_for('pg_module_dashboard', id=id, step='assignment', amode='single') if step == 'assignment' else url_for('pg_module_dashboard', id=id, step=step))
    else:
        return redirect(url_for('pg_module_dashboard', id=id, step=step))

@app.route('/pg/module/<int:id>/save/assignment_with_cat', methods=['POST'])
def pg_save_assignment_with_cat(id):
    """Save assignment marks and student categories together"""
    db = get_db()
    
    # Ensure classifications table exists
    db.execute('''CREATE TABLE IF NOT EXISTS pg_student_classifications (
        id SERIAL PRIMARY KEY,
        student_id INTEGER,
        module_id INTEGER,
        category TEXT
    )''')
    
    for key in request.form:
        if key.startswith('sid_'):
            idx = key.split('_')[1]
            student_id = request.form.get(key)
            
            # Save assignment mark
            value = request.form.get(f'val_{idx}', 0)
            try:
                value = float(value) if value else 0
            except:
                value = 0
            existing = db.execute('SELECT id FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (student_id, id, 'assignment')).fetchone()
            if existing:
                db.execute('UPDATE pg_marks SET value=%s WHERE id=%s', (value, existing['id']))
            else:
                db.execute('INSERT INTO pg_marks (student_id, module_id, mark_type, value) VALUES (%s, %s, %s, %s)', (student_id, id, 'assignment', value))
            
            # Save gaussian mark
            val_g = request.form.get(f'val_g_{idx}')
            if val_g is not None:
                try:
                    val_g = float(val_g) if val_g else 0
                except:
                    val_g = 0
                existing_g = db.execute('SELECT id FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (student_id, id, 'assignment_gaussian')).fetchone()
                if existing_g:
                    db.execute('UPDATE pg_marks SET value=%s WHERE id=%s', (val_g, existing_g['id']))
                else:
                    db.execute('INSERT INTO pg_marks (student_id, module_id, mark_type, value) VALUES (%s, %s, %s, %s)', (student_id, id, 'assignment_gaussian', val_g))
            
            # Save category
            cat = request.form.get(f'cat_{idx}')
            if cat:
                existing_cat = db.execute('SELECT id FROM pg_student_classifications WHERE student_id=%s AND module_id=%s', (student_id, id)).fetchone()
                if existing_cat:
                    db.execute('UPDATE pg_student_classifications SET category=%s WHERE id=%s', (cat, existing_cat['id']))
                else:
                    db.execute('INSERT INTO pg_student_classifications (student_id, module_id, category) VALUES (%s, %s, %s)', (student_id, id, cat))
    
    db.commit()
    flash('Assignment marks and categories saved!')
    return redirect(url_for('pg_module_dashboard', id=id, step='assignment', amode='single'))

@app.route('/pg/module/<int:id>/preview/assignment<int:num>', methods=['POST'])
def pg_preview_assignment(id, num):
    """Parse multi-column assignment format and store in session for inline preview"""
    db = get_db()
    module = db.execute('SELECT m.*, b.start_year, b.end_year FROM pg_modules m JOIN pg_batches b ON m.batch_id = b.id WHERE m.id = %s', (id,)).fetchone()
    if not module:
        return "Module not found", 404
    
    paste_text = request.form.get('paste_text', '')
    
    if not paste_text.strip():
        flash('No data pasted!')
        return redirect(url_for('pg_module_dashboard', id=id, step='assignment', amode='multi', _anchor=f'tab{num}'))
    
    lines = paste_text.strip().split('\n')
    lines = [l.strip() for l in lines if l.strip()]  # Remove empty lines
    
    # Skip header lines
    start_idx = 0
    for i, line in enumerate(lines):
        if line.upper() in ['USN', 'NAME', 'ASSIGNMENT', 'MAX MARKS', 'MARKS SCORED', 'CONVERTED', 'REMARKS']:
            start_idx = i + 1
            continue
        if line.upper() == 'Q12':
            start_idx = i + 1
            break
    
    # Process student data
    student_lines = lines[start_idx:]
    parsed_data = []
    
    # Each student has 18 fields
    chunk_size = 18
    for i in range(0, len(student_lines), chunk_size):
        if i + 6 > len(student_lines):
            break
        
        usn = student_lines[i].strip()
        if not usn or not usn[0].isalpha() or not any(c.isdigit() for c in usn):
            continue
        
        try:
            name = student_lines[i + 1].strip() if i + 1 < len(student_lines) else ''
            # max_marks at i+3
            marks_scored = float(student_lines[i + 4].strip()) if i + 4 < len(student_lines) else 0
            converted = float(student_lines[i + 5].strip()) if i + 5 < len(student_lines) else 0
            
            q_values = []
            for q_idx in range(7, min(19, len(student_lines) - i)):
                try:
                    q_values.append(float(student_lines[i + q_idx].strip()))
                except:
                    q_values.append(0)
            
            student = db.execute('SELECT id FROM pg_students WHERE usn=%s AND batch_id=%s', (usn, module['batch_id'])).fetchone()
            student_id = student['id'] if student else None
            
            parsed_data.append({
                'usn': usn,
                'name': name,
                'marks_scored': marks_scored,
                'converted': converted,
                'q_values': q_values,
                'student_id': student_id
            })
        except:
            continue
    
    if not parsed_data:
        flash('Could not parse any student data. Check format.')
    else:
        # Store in session
        preview_data = session.get(f'preview_data_{id}', {})
        preview_data[str(num)] = parsed_data
        session[f'preview_data_{id}'] = preview_data
        
    return redirect(url_for('pg_module_dashboard', id=id, step='assignment', amode='multi') + f'#tab{num}')

@app.route('/pg/module/<int:id>/confirm/assignment<int:num>', methods=['POST'])
def pg_confirm_assignment(id, num):
    """Confirm and save assignment marks from inline preview"""
    db = get_db()
    mark_column = request.form.get('mark_column', 'marks_scored')
    
    # Get data from session
    preview_data = session.get(f'preview_data_{id}', {})
    data = preview_data.get(str(num), [])
    
    if not data:
        flash('Session expired or no data found. Please parse again.')
        return redirect(url_for('pg_module_dashboard', id=id, step='assignment', amode='multi') + f'#tab{num}')
    
    count = 0
    for item in data:
        student_id = item.get('student_id')
        if not student_id:
            continue
        
        marks = item.get(mark_column, 0)
        try:
            marks = float(marks)
        except:
            marks = 0
            
        # Prepare metadata (Q values and remarks) for main assignment mark
        metadata = {
            'q_values': item.get('q_values', []),
            'remarks': item.get('remarks', '')
        }
        metadata_json = json.dumps(metadata)
        
        # Store Scored, Converted, and Main (Selected)
        for mtype, val, meta in [
            (f'assignment{num}_scored', item.get('marks_scored', 0), None),
            (f'assignment{num}_converted', item.get('converted', 0), None),
            (f'assignment{num}', marks, metadata_json)
        ]:
            existing = db.execute('SELECT id FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (student_id, id, mtype)).fetchone()
            if existing:
                if meta:
                    db.execute('UPDATE pg_marks SET value=%s, ai_prediction=%s WHERE id=%s', (val, meta, existing['id']))
                else:
                    db.execute('UPDATE pg_marks SET value=%s WHERE id=%s', (val, existing['id']))
            else:
                if meta:
                    db.execute('INSERT INTO pg_marks (student_id, module_id, mark_type, value, ai_prediction) VALUES (%s, %s, %s, %s, %s)', (student_id, id, mtype, val, meta))
                else:
                    db.execute('INSERT INTO pg_marks (student_id, module_id, mark_type, value) VALUES (%s, %s, %s, %s)', (student_id, id, mtype, val))
        count += 1
    
    db.commit()
    
    # Clear preview data for this assignment
    if str(num) in preview_data:
        del preview_data[str(num)]
        session[f'preview_data_{id}'] = preview_data
        
    flash(f'✅ Assignment {num} imported! {count} students saved using {mark_column.replace("_", " ").title()}.')
    return redirect(url_for('pg_module_dashboard', id=id, step='assignment', amode='multi') + f'#tab{num}')

@app.route('/pg/module/<int:id>/cancel_preview')
def pg_cancel_preview(id):
    """Clear preview processing data"""
    session.pop(f'preview_data_{id}', None)
    return redirect(url_for('pg_module_dashboard', id=id, step='assignment', amode='multi'))

@app.route('/pg/module/<int:id>/delete/assignment<int:num>')
def pg_delete_assignment(id, num):
    """Delete all marks for a specific assignment number"""
    db = get_db()
    
    # Delete Scored, Converted, and Main marks
    types = [f'assignment{num}', f'assignment{num}_scored', f'assignment{num}_converted']
    for t in types:
        db.execute('DELETE FROM pg_marks WHERE module_id=%s AND mark_type=%s', (id, t))
    
    db.commit()
    flash(f'🗑️ Deleted all Assignment {num} marks.')
    return redirect(url_for('pg_module_dashboard', id=id, step='assignment', amode='multi') + f'#tab{num}')

@app.route('/pg/module/<int:id>/manual/assignment<int:num>', methods=['POST'])
def pg_manual_multi_assignment(id, num):
    """Parse multi-column assignment format and save marks
    
    Format: Each student has 18 lines:
    0: USN, 1: NAME, 2: ASSIGNMENT#, 3: MAX, 4: MARKS_SCORED, 5: CONVERTED, 
    6: REMARKS, 7-17: Q1-Q11, 18: Q12
    """
    db = get_db()
    module = db.execute('SELECT batch_id FROM pg_modules WHERE id=%s', (id,)).fetchone()
    paste_text = request.form.get('paste_text', '')
    mark_col = request.form.get('mark_col', 'scored')  # 'scored' or 'converted'
    
    if not paste_text.strip():
        flash('No data pasted!')
        return redirect(url_for('pg_module_dashboard', id=id, step='assignment', amode='multi'))
    
    lines = paste_text.strip().split('\n')
    lines = [l.strip() for l in lines if l.strip()]  # Remove empty lines
    
    # Skip header lines (look for lines like "USN", "NAME", etc.)
    start_idx = 0
    for i, line in enumerate(lines):
        if line.upper() in ['USN', 'NAME', 'ASSIGNMENT', 'MAX MARKS', 'MARKS SCORED', 'CONVERTED', 'REMARKS']:
            start_idx = i + 1
            continue
        # If we hit a Q12 line, next should be first student USN
        if line.upper() == 'Q12':
            start_idx = i + 1
            break
    
    # Process student data (18 lines per student)
    student_lines = lines[start_idx:]
    count = 0
    
    # Each student has 18 fields on separate lines
    chunk_size = 18
    for i in range(0, len(student_lines), chunk_size):
        if i + 5 >= len(student_lines):
            break  # Not enough lines for a complete student
        
        usn = student_lines[i].strip()
        # Validate USN format (starts with letter, contains numbers)
        if not usn or not usn[0].isalpha() or not any(c.isdigit() for c in usn):
            continue
        
        try:
            # mark_col is now a line offset (e.g., '4' for MARKS SCORED, '5' for CONVERTED)
            col_offset = int(mark_col) if mark_col.isdigit() else 4  # Default to MARKS SCORED
            marks = float(student_lines[i + col_offset].strip())
        except:
            marks = 0
        
        # Find student by USN
        student = db.execute('SELECT id FROM pg_students WHERE usn=%s AND batch_id=%s', (usn, module['batch_id'])).fetchone()
        if not student:
            continue
        
        # Save to pg_marks with mark_type = assignment1/2/3
        mark_type = f'assignment{num}'
        existing = db.execute('SELECT id FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (student['id'], id, mark_type)).fetchone()
        if existing:
            db.execute('UPDATE pg_marks SET value=%s WHERE id=%s', (marks, existing['id']))
        else:
            db.execute('INSERT INTO pg_marks (student_id, module_id, mark_type, value) VALUES (%s, %s, %s, %s)', (student['id'], id, mark_type, marks))
        count += 1
    
    db.commit()
    flash(f'Assignment {num} parsed! {count} students updated.')
    return redirect(url_for('pg_module_dashboard', id=id, step='assignment', amode='multi'))

@app.route('/pg/module/<int:id>/consolidate', methods=['POST'])
def pg_consolidate_assignments(id):
    """Consolidate 3 assignments (120 total) into scaled 50-mark assignment
    
    Uses the user-selected column (Marks Scored, Converted, or Gaussian) for each assignment
    Saves to 'assignment' (Actual) or 'assignment_gaussian' based on button clicked
    """
    db = get_db()
    module = db.execute('SELECT batch_id FROM pg_modules WHERE id=%s', (id,)).fetchone()
    students = db.execute('SELECT id, usn FROM pg_students WHERE batch_id=%s', (module['batch_id'],)).fetchall()
    
    # Get column selection from form
    a1_col = request.form.get('a1_col', 'scored')  # 'scored', 'converted', or 'gaussian'
    a2_col = request.form.get('a2_col', 'scored')
    a3_col = request.form.get('a3_col', 'scored')
    consolidate_type = request.form.get('consolidate_type', 'actual')  # 'actual' or 'gaussian'
    
    # Determine mark types based on selection
    def get_mark_type(num, col):
        if col == 'scored':
            return f'assignment{num}_scored'
        elif col == 'converted':
            return f'assignment{num}_converted'
        else:  # gaussian
            return f'assignment{num}_gaussian'
    
    a1_type = get_mark_type(1, a1_col)
    a2_type = get_mark_type(2, a2_col)
    a3_type = get_mark_type(3, a3_col)
    
    # Target mark type based on button clicked
    target_mark_type = 'assignment_gaussian' if consolidate_type == 'gaussian' else 'assignment'
    
    count = 0
    for s in students:
        # Get assignment marks from selected columns (with fallback to main assignment mark)
        m1 = db.execute('SELECT value FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (s['id'], id, a1_type)).fetchone()
        if not m1:
            m1 = db.execute('SELECT value FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (s['id'], id, 'assignment1')).fetchone()
        
        m2 = db.execute('SELECT value FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (s['id'], id, a2_type)).fetchone()
        if not m2:
            m2 = db.execute('SELECT value FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (s['id'], id, 'assignment2')).fetchone()
        
        m3 = db.execute('SELECT value FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (s['id'], id, a3_type)).fetchone()
        if not m3:
            m3 = db.execute('SELECT value FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (s['id'], id, 'assignment3')).fetchone()
        
        a1 = m1['value'] if m1 else 0
        a2 = m2['value'] if m2 else 0
        a3 = m3['value'] if m3 else 0
        
        total_120 = a1 + a2 + a3
        # Scale: (total_120 / 120) * 50, using ceiling
        scaled_50 = math.ceil((total_120 / 120) * 50)
        
        # Save to pg_marks using target mark type
        existing = db.execute('SELECT id FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (s['id'], id, target_mark_type)).fetchone()
        if existing:
            db.execute('UPDATE pg_marks SET value=%s WHERE id=%s', (scaled_50, existing['id']))
        else:
            db.execute('INSERT INTO pg_marks (student_id, module_id, mark_type, value) VALUES (%s, %s, %s, %s)', (s['id'], id, target_mark_type, scaled_50))
        count += 1
    
    col_names = f"A1:{a1_col}, A2:{a2_col}, A3:{a3_col}"
    save_type = "Gaussian" if consolidate_type == 'gaussian' else "Actual"
    db.commit()
    flash(f'Consolidated {count} students ({save_type}) using {col_names}! Saved to {target_mark_type}.')
    return redirect(url_for('pg_module_dashboard', id=id, step='assignment', amode='multi') + '#tabC')

@app.route('/pg/module/<int:id>/gaussian/assignment<int:num>', methods=['POST'])
def pg_gaussian_assignment(id, num):
    """Gaussian assignment for A1, A2, A3 with Classification Constraints"""
    db = get_db()
    module = db.execute('SELECT batch_id, settings FROM pg_modules WHERE id=%s', (id,)).fetchone()
    students = db.execute('SELECT id, usn FROM pg_students WHERE batch_id=%s ORDER BY usn', (module['batch_id'],)).fetchall()
    
    # helper: fetch marks dict
    marks_data = {}
    rows = db.execute('SELECT student_id, mark_type, value FROM pg_marks WHERE module_id=%s', (id,)).fetchall()
    for r in rows:
        marks_data[(r['student_id'], r['mark_type'])] = {'value': r['value']}

    # Load Settings
    settings_json = module['settings']
    settings = json.loads(settings_json) if settings_json else {}
    mode = settings.get('mode', 'auto')
    ranges = settings.get('ranges', {
        'bright': {'min': 30, 'max': 40},
        'average': {'min': 18, 'max': 29},
        'poor': {'min': 0, 'max': 17}
    })

    # Get student classifications if any
    try:
        classifications = {row['student_id']: row['category'] for row in db.execute('SELECT student_id, category FROM pg_student_classifications WHERE module_id=%s', (id,)).fetchall()}
    except:
        classifications = {}

    import random
    count = 0
    
    # Check for Previous Module for Predict Mode
    prev_module = db.execute('SELECT id FROM pg_modules WHERE batch_id=%s AND id < %s ORDER BY id DESC LIMIT 1', (module['batch_id'], id)).fetchone()
    prev_marks_map = {}
    if mode == 'predict_prev' and prev_module:
        # Fetch Total Marks from previous module (Assignment + SEE)
        # Using a simple aggregation: sum of all marks for that module
        rows = db.execute('SELECT student_id, SUM(value) as total FROM pg_marks WHERE module_id=%s GROUP BY student_id', (prev_module['id'],)).fetchall()
        prev_marks_map = {r['student_id']: r['total'] for r in rows}
        if not prev_marks_map:
            mode = 'auto' # Fallback if no marks found
    
    # Sort students by Rank if Auto/Predict
    student_list = []
    for s in students:
        p_score = prev_marks_map.get(s['id'], 0)
        student_list.append({'id': s['id'], 'usn': s['usn'], 'prev': p_score})
    
    if mode == 'auto':
        # Generate B+ Peak Distribution for A1
        n = len(students)
        # O=5, A+=10, A=20, B+=40, B=20, C=5
        counts = {
            'O': max(1, int(n * 0.05)),
            'A+': max(1, int(n * 0.10)),
            'A': max(1, int(n * 0.20)),
            'B+': max(1, int(n * 0.40)),
            'B': max(1, int(n * 0.20)),
            'C': max(0, int(n * 0.05))
        }
        # Fill remainder to B+
        curr = sum(counts.values())
        if n > curr: counts['B+'] += (n - curr)
            
        target_grades = []
        for g in ['O', 'A+', 'A', 'B+', 'B', 'C']:
            target_grades.extend([g] * counts[g])
        target_grades = target_grades[:n]
        random.shuffle(target_grades) # Shuffle since no history to sort by
        
        grade_ranges = {'O':(35,40), 'A+':(30,34), 'A':(25,29), 'B+':(20,24), 'B':(15,19), 'C':(0,14)} # Scaled for 40 marks
        
        for i, s in enumerate(students):
            sid = s['id']
            grade = target_grades[i]
            t_min, t_max = grade_ranges.get(grade, (0,40))
            val = random.randint(t_min, t_max)
            
            # Save
            existing = db.execute('SELECT id FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (sid, id, f'assignment{num}_gaussian')).fetchone()
            meta = json.dumps({'method': 'auto_curve'})
            if existing:
                db.execute('UPDATE pg_marks SET value=%s, ai_prediction=%s WHERE id=%s', (val, meta, existing['id']))
            else:
                db.execute('INSERT INTO pg_marks (student_id, module_id, mark_type, value, ai_prediction) VALUES (%s, %s, %s, %s, %s)', (sid, id, f'assignment{num}_gaussian', val, meta))
            count += 1
        db.commit()
        return redirect(url_for('pg_module_dashboard', id=id, step='assignment', amode='multi') + f'#tab{num}')

    # MANUAL / SEQUENTIAL MODE
    max_val = 40 # Max marks for individual assignment
    for s in students:
        sid = s['id']
        category = classifications.get(sid, 'none')
        
        # Determine Target Range from Settings
        if category == 'bright':
            target_min, target_max = ranges['bright']['min'], ranges['bright']['max']
        elif category == 'average':
            target_min, target_max = ranges['average']['min'], ranges['average']['max']
        elif category == 'poor':
            target_min, target_max = ranges['poor']['min'], ranges['poor']['max']
        else:
            target_min, target_max = 0, max_val 
            
        def clamp_cat(val):
            if category != 'none':
                return max(target_min, min(target_max, val))
            else:
                return max(0, min(max_val, val))

        marks = {}
        # Force fetch fresh marks for this student
        # Because we might have just updated them in a previous loop? No, this is all one request.
        # But we need to be sure we are looking at the right keys.
        # marks_data keys are (student_id, mark_type).
        
        # Check A1
        a1_entry = marks_data.get((sid, 'assignment1'), {})
        if not a1_entry: a1_entry = marks_data.get((sid, 'assignment1_gaussian'), {})
        if a1_entry and a1_entry.get('value', 0) > 0:
            marks['a1'] = a1_entry['value']
            
        # Check A2
        a2_entry = marks_data.get((sid, 'assignment2'), {})
        if not a2_entry: a2_entry = marks_data.get((sid, 'assignment2_gaussian'), {})
        if a2_entry and a2_entry.get('value', 0) > 0:
            marks['a2'] = a2_entry['value']
            
        final_val = 0
        prediction_meta = None
        
        if num == 1:
            curr = marks_data.get((sid, f'assignment{num}'), {}).get('value', 0)
            if curr == 0:
                # Manual Mode or Predict Mode
                if mode == 'predict_prev' and prev_marks_map:
                    # Predict based on Prev Total
                    # Prev Total Max ~150 (50 Assign + 100 SEE)? Or scaled?
                    # Let's assume Prev Total is decent proxy.
                    # Scale: (Prev / Max_Prev) * 40
                    # We don't know Max Prev easily without query. Let's assume 150.
                    # Actually, simple rank-based approach is safer but user wants direct prediction.
                    # Let's use direct proportional mapping with noise.
                    prev_val = prev_marks_map.get(sid, 0)
                    # Map 0-150 -> 0-40
                    base_mark = (prev_val / 150) * max_val
                    final_val = base_mark + random.randint(-4, 4)
                    final_val = max(0, min(max_val, final_val))
                else:
                     # Check Categories (Manual)
                    cat = classifications.get(sid, 'none')
                    if cat == 'bright':
                        final_val = random.randint(ranges['bright']['min'], ranges['bright']['max'])
                    elif cat == 'average':
                        final_val = random.randint(ranges['average']['min'], ranges['average']['max'])
                    elif cat == 'poor':
                        final_val = random.randint(ranges['poor']['min'], ranges['poor']['max'])
                    else:
                        final_val = random.randint(int(max_val*0.4), int(max_val*0.8))
                final_val = clamp_cat(final_val)
                prediction_meta = {"method": "category_gen" if category!='none' else "gaussian_gen"}
                
        elif num == 2:
            # Force update (removed curr==0 check)
            if 'a1' in marks and marks['a1'] > 0:
                base = marks['a1']
                noise = random.randint(-2, 2)
                final_val = base + noise
                # Clamp to range (0-40 usually)
                final_val = max(0, min(40, final_val))
                if mode == 'manual': final_val = clamp_cat(final_val)
                prediction_meta = {"method": "pattern_a1", "src": "a1", "cat": category}
            else:
                    # Fallback to pure generation
                    if mode == 'auto':
                        final_val = int(random.gauss(28,5))
                        final_val = max(0, min(40, final_val))
                    else:
                        final_val = int(random.uniform(target_min, target_max)) if category!='none' else int(random.gauss(28,5))
                        final_val = clamp_cat(final_val)
                    prediction_meta = {"method": "category_gen"}

        elif num == 3:
             # Force update (removed curr==0 check)
             if 'a1' in marks and 'a2' in marks and (marks['a1'] > 0 or marks['a2'] > 0):
                base = (marks['a1'] + marks['a2']) / 2
                noise = random.randint(-1, 2)
                final_val = base + noise
                final_val = max(0, min(40, final_val))
                if mode == 'manual': final_val = clamp_cat(final_val)
                prediction_meta = {"method": "pattern_avg", "src": "a1,a2"}
             elif 'a1' in marks:
                    final_val = marks['a1'] # Use A1 if A2 missing
                    if mode == 'manual': final_val = clamp_cat(final_val)
                    prediction_meta = {"method": "pattern_a1_fallback", "src": "a1"}
             else:
                    if mode == 'auto':
                        final_val = int(random.gauss(28,5))
                        final_val = max(0, min(40, final_val))
                    else:
                        final_val = int(random.uniform(target_min, target_max)) if category!='none' else int(random.gauss(28,5))
                        final_val = clamp_cat(final_val)
                    prediction_meta = {"method": "category_gen"}
        
        if final_val >= 0: # Allow 0 assignments
            # Final Cap at 47
            final_val = min(final_val, 47)
            
            existing = db.execute('SELECT id FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (sid, id, f'assignment{num}_gaussian')).fetchone()
            if existing:
                db.execute('UPDATE pg_marks SET value=%s, ai_prediction=%s WHERE id=%s', (final_val, json.dumps(prediction_meta), existing['id']))
            else:
                db.execute('INSERT INTO pg_marks (student_id, module_id, mark_type, value, ai_prediction) VALUES (%s, %s, %s, %s, %s)', (sid, id, f'assignment{num}_gaussian', final_val, json.dumps(prediction_meta)))
            count += 1
            
    db.commit()
    return redirect(url_for('pg_module_dashboard', id=id, step='assignment', amode='multi') + f'#tab{num}')

@app.route('/pg/module/<int:id>/gaussian', methods=['POST'])
def pg_gaussian_assign(id):
    """Assign SEE marks using Gaussian distribution for PG"""
    print("=" * 50)
    print("PG GAUSSIAN ASSIGN CALLED - ID:", id)
    print("=" * 50)
    import random
    db = get_db()
    module = db.execute('SELECT batch_id FROM pg_modules WHERE id=%s', (id,)).fetchone()
    students = db.execute('SELECT s.id, s.usn FROM pg_students s WHERE s.batch_id=%s ORDER BY s.usn', (module['batch_id'],)).fetchall()
    student_data = []
    for s in students:
        m = db.execute('SELECT value FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (s['id'], id, 'assignment')).fetchone()
        assign_val = m['value'] if m else 0
        student_data.append({'id': s['id'], 'usn': s['usn'], 'assignment': assign_val})
    student_data.sort(key=lambda x: x['assignment'], reverse=True)
    n = len(student_data)
    if n == 0:
        flash('No students found!')
        return redirect(url_for('pg_module_dashboard', id=id, step='see'))
    # Build grade distribution ensuring B grade is present
    # User Request: "If assignment is 0, assign below 50 (Fail/C). If assignment is good, assign good marks."
    # So we MUST have C grades for the poor performers.
    # New Plan: O=5%, A+=10%, A=20%, B+=40%, B=20%, C=5%
    counts = {
        'O': max(1, int(n * 0.05)),
        'A+': max(1, int(n * 0.10)),
        'A': max(1, int(n * 0.20)),
        'B+': max(1, int(n * 0.40)),
        'B': max(1, int(n * 0.20)),
        'C': max(0, int(n * 0.05)) # Restore C for poor performers
    }
    
    # Calculate remainder
    current_assigned = sum(counts.values())
    remainder = n - current_assigned
    
    # Distribute remainder to B (safe average)
    if remainder > 0:
        counts['B'] += remainder
    if counts['B'] < 1 and n >= 5:
        if counts['B+'] > 1: counts['B+'] -= 1
        counts['B'] += 1

    # Create target grades list sorted O -> C
    target_grades = []
    # Note: Order matters! We assign Top Assignment -> Top Grade (O).
    # Bottom Assignment -> Bottom Grade (C).
    for grade in ['O', 'A+', 'A', 'B+', 'B', 'C']:
        target_grades.extend([grade] * counts[grade])
    
    # Sort students by assignment marks (descending)
    # CRITICAL: Ensure assignment is float for correct sorting!
    for s in student_data:
        try:
            s['assignment'] = float(s['assignment'])
        except:
            s['assignment'] = 0.0
            
    student_data.sort(key=lambda x: x['assignment'], reverse=True)
    
    # Safety truncation
    target_grades = target_grades[:n]
    
    # Grade to Total Score ranges
    grade_ranges = {
        'O': (91, 100),
        'A+': (81, 90),
        'A': (71, 80),
        'B+': (61, 70),
        'B': (50, 60),
        'C': (0, 49)
    }

    for i, student in enumerate(student_data):
        grade = target_grades[i]
        
        # LOGIC OVERRIDE:
        # User said: "if its 0 then assign below 50" (Fail)
        if student['assignment'] <= 1.0: # Very low assignment
            grade = 'C' # Force C
        
        # User said: "make sure its above 50" (Pass) if they did okay?
        # Let's say if Assignment > 20, we try to pass them.
        elif grade == 'C' and student['assignment'] > 25:
             grade = 'B' # Bump to B if they did decent in assignment but fell into C bucket
        
        min_total, max_total = grade_ranges[grade]
        
        # Determine target total for this student - aim for middle of bracket
        target_total = random.randint(min_total, max_total)
        
        # Calculate raw SEE needed
        needed_scaled_see = target_total - student['assignment']
        needed_scaled_see = max(0, min(50, needed_scaled_see))
        
        # Find raw_see
        raw_see = (needed_scaled_see * 2) - 1
        
        # Apply Logic:
        # If Grade is C (Fail), allow low marks.
        # If Grade is >= B (Pass), enforce min 50.
        if grade == 'C':
            raw_see = min(48, raw_see) # Force Fail
        else:
            raw_see = max(50, min(100, raw_see)) # Force Pass
        
        # Verify the final grade matches target
        actual_scaled = math.ceil((raw_see / 100) * 50)
        actual_total = student['assignment'] + actual_scaled
        actual_grade = calculate_pg_grade(actual_total, raw_see)
        
        # If grade doesn't match, adjust raw_see
        attempts = 0
        while actual_grade != grade and attempts < 20:
            if actual_total > max_total:
                # If target is C, we want to go LOWER
                if grade == 'C' or raw_see > 50:
                    raw_see -= 2 
                else: 
                    break 
            elif actual_total < min_total:
                # If target is Pass, we want to go HIGHER
                if grade != 'C':
                    raw_see += 2
                elif raw_see < 48:
                    raw_see += 2
                else:
                    break
            else:
                break
            
            # Re-Apply Limits inside loop
            if grade == 'C':
                raw_see = max(0, min(48, raw_see))
            else:
                raw_see = max(50, min(100, raw_see))
                
            actual_scaled = math.ceil((raw_see / 100) * 50)
            actual_total = student['assignment'] + actual_scaled
            actual_grade = calculate_pg_grade(actual_total, raw_see)
            attempts += 1
        
        print(f"  {student['usn']}: Assign={student['assignment']}, TargetGrade={grade}, RawSEE={raw_see}, FinalTotal={actual_total}, FinalGrade={actual_grade}")
        
        existing = db.execute('SELECT id FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (student['id'], id, 'see_gaussian')).fetchone()
        if existing:
            db.execute('UPDATE pg_marks SET value=%s WHERE id=%s', (raw_see, existing['id']))
        else:
            db.execute('INSERT INTO pg_marks (student_id, module_id, mark_type, value) VALUES (%s, %s, %s, %s)', (student['id'], id, 'see_gaussian', raw_see))
    db.commit()
    flash(f'Gaussian SEE marks assigned to {n} students!')
    return redirect(url_for('pg_module_dashboard', id=id, step='see'))

@app.route('/pg/module/<int:id>/parse/<step>', methods=['POST'])
def pg_parse_text(id, step):
    text = request.form.get('paste_text', '')
    if not text.strip():
        flash('No text provided')
        return redirect(url_for('pg_module_dashboard', id=id, step=step))
    max_marks = 50 if step == 'assignment' else 100
    prompt = f'Parse student marks. Return ONLY JSON: [{{"usn": "...", "marks": {max_marks}}}]. No markdown.\nData:\n{text}'
    try:
        txt = get_gemini_response(prompt)
        data = json.loads(txt.replace('```json', '').replace('```', '').strip())
        db = get_db()
        module = db.execute('SELECT batch_id FROM pg_modules WHERE id=%s', (id,)).fetchone()
        students = {s['usn'].upper(): s['id'] for s in db.execute('SELECT id, usn FROM pg_students WHERE batch_id=%s', (module['batch_id'],)).fetchall()}
        count = 0
        for item in data:
            usn = str(item.get('usn', '')).upper()
            marks = item.get('marks', 0)
            student_id = students.get(usn)
            if not student_id:
                for db_usn, sid in students.items():
                    if usn.endswith(db_usn[-5:]) or db_usn.endswith(usn[-5:]):
                        student_id = sid
                        break
            if student_id:
                existing = db.execute('SELECT id FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (student_id, id, step)).fetchone()
                if existing:
                    db.execute('UPDATE pg_marks SET value=%s WHERE id=%s', (marks, existing['id']))
                else:
                    db.execute('INSERT INTO pg_marks (student_id, module_id, mark_type, value) VALUES (%s, %s, %s, %s)', (student_id, id, step, marks))
                count += 1
        db.commit()
        flash(f'Imported {count} marks!')
    except Exception as e:
        flash(f'Parse failed: {e}')
    return redirect(url_for('pg_module_dashboard', id=id, step=step))

@app.route('/pg/module/<int:id>/manual/<step>', methods=['POST'])
def pg_manual_marks(id, step):
    """Parse marks data - supports CSV, tab-separated, or space-separated"""
    text = request.form.get('paste_text', '')
    if not text.strip():
        flash('Please paste marks data')
        return redirect(url_for('pg_module_dashboard', id=id, step=step))
    
    max_marks = 50 if step == 'assignment' else 100
    db = get_db()
    module = db.execute('SELECT batch_id FROM pg_modules WHERE id=%s', (id,)).fetchone()
    students = {s['usn'].upper(): s['id'] for s in db.execute('SELECT id, usn FROM pg_students WHERE batch_id=%s', (module['batch_id'],)).fetchall()}
    
    count = 0
    lines = text.strip().split('\n')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Detect format: CSV (comma), TSV (tab), or space-separated
        if ',' in line:
            parts = [p.strip() for p in line.split(',')]
        elif '\t' in line:
            parts = [p.strip() for p in line.split('\t')]
        else:
            parts = re.split(r'\s{2,}', line)
            if len(parts) == 1:
                parts = line.split(' ')
        
        if len(parts) >= 2:
            usn = parts[0].strip().upper()
            
            # Skip header rows
            if usn in ['USN', 'REG', '_USN', 'REGISTER', 'REGISTRATION', 'NEW_USN']:
                continue
            
            # Remove leading underscore if present
            if usn.startswith('_'):
                usn = usn[1:]
            
            # Get marks from last column (TOTAL)
            try:
                marks = float(parts[-1].strip())
            except:
                continue
            
            # Clamp marks to valid range
            marks = max(0, min(marks, max_marks))
            
            # Find student (exact or suffix match)
            student_id = students.get(usn)
            if not student_id:
                for db_usn, sid in students.items():
                    if usn.endswith(db_usn[-5:]) or db_usn.endswith(usn[-5:]):
                        student_id = sid
                        break
            
            if student_id:
                existing = db.execute('SELECT id FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (student_id, id, step)).fetchone()
                if existing:
                    db.execute('UPDATE pg_marks SET value=%s WHERE id=%s', (marks, existing['id']))
                else:
                    db.execute('INSERT INTO pg_marks (student_id, module_id, mark_type, value) VALUES (%s, %s, %s, %s)', (student_id, id, step, marks))
                count += 1
    
    db.commit()
    flash(f'Imported {count} {step} marks!')
    return redirect(url_for('pg_module_dashboard', id=id, step=step))



@app.route('/pg/module/<int:id>/gaussian/single', methods=['POST'])
def pg_gaussian_single(id):
    """Gaussian assignment for Single Assignment (Max 50) with Grade-Based Distribution
    
    Grade Ranges (scaled from 100 to 50):
    - O: 46-50 (91-100 scaled)
    - A+: 41-45 (81-90 scaled)
    - A: 36-40 (71-80 scaled)
    - B+: 31-35 (61-70 scaled)
    - B: 25-30 (50-60 scaled)
    - C: <25 (<50 scaled) - FAIL
    
    Target Distribution for 25-30 students:
    - O: ~4 students (13%)
    - A+: ~5-7 students (20%)
    - A: ~8-10 students (33%) 
    - B+: ~12-15 students (highest, but distributed across A/B+)
    - B: ~5-7 students (20%)
    - C: ~2-3 students (only for 0-mark students)
    
    Categories:
    - Bright → O + A+ range (41-50)
    - Average → A + B+ range (31-40)
    - Poor → B range (25-30)
    """
    import random
    db = get_db()
    module = db.execute('SELECT batch_id, settings FROM pg_modules WHERE id=%s', (id,)).fetchone()
    students_raw = db.execute('SELECT id FROM pg_students WHERE batch_id=%s', (module['batch_id'],)).fetchall()
    students = [s['id'] for s in students_raw]
    n = len(students)
    
    # Get classifications
    try:
        classifications = {row['student_id']: row['category'] for row in db.execute('SELECT student_id, category FROM pg_student_classifications WHERE module_id=%s', (id,)).fetchall()}
    except:
        classifications = {}
    
    # Grade mark ranges (for 50-mark scale) - MAX 48
    grade_ranges = {
        'O': (46, 48),      # Max 48, not 50
        'A+': (41, 45),
        'A': (36, 40),
        'B+': (31, 35),
        'B': (25, 30),
        'C': (0, 24)
    }
    
    # Category to grades mapping
    cat_grades = {
        'bright': ['O', 'A+'],      # Bright students get O or A+
        'average': ['A', 'B+'],     # Average students get A or B+
        'poor': ['B'],              # Poor students get B
        'none': ['A', 'B+', 'B']    # No category - distributed towards B+/A
    }
    
    count = 0
    for sid in students:
        category = classifications.get(sid, 'none')
        allowed_grades = cat_grades.get(category, cat_grades['none'])
        
        # Pick a random grade from allowed grades (weighted for B+ peak)
        if category == 'bright':
            # 30% O, 70% A+ (less O, more A+)
            grade = random.choices(['O', 'A+'], weights=[30, 70])[0]
        elif category == 'average':
            # 35% A, 65% B+ (shift towards B+)
            grade = random.choices(['A', 'B+'], weights=[35, 65])[0]
        elif category == 'poor':
            # 100% B
            grade = 'B'
        else:
            # No category - STRONGLY B+ peaked distribution
            # 10% A, 60% B+, 30% B (B+ is the clear peak)
            grade = random.choices(['A', 'B+', 'B'], weights=[10, 60, 30])[0]
        
        # Get mark range for selected grade
        min_mark, max_mark = grade_ranges[grade]
        
        # Generate random mark within grade range
        val = random.randint(min_mark, max_mark)
        
        # Final safety: ensure min 25 (no C grades unless forced)
        val = max(25, val)
        
        existing = db.execute('SELECT id FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (sid, id, 'assignment_gaussian')).fetchone()
        meta = json.dumps({'method': 'gaussian_single', 'grade': grade, 'category': category})
        if existing:
             db.execute('UPDATE pg_marks SET value=%s, ai_prediction=%s WHERE id=%s', (val, meta, existing['id']))
        else:
             db.execute('INSERT INTO pg_marks (student_id, module_id, mark_type, value, ai_prediction) VALUES (%s, %s, %s, %s, %s)', (sid, id, 'assignment_gaussian', val, meta))
        count += 1
            
    db.commit()
    return redirect(url_for('pg_module_dashboard', id=id, step='assignment', amode='single'))

@app.route('/pg/module/<int:id>/gaussian/see', methods=['POST'])
def pg_gaussian_see(id):
    """Predict SEE marks based on Total Assignment Marks (Auto B+ Max or Manual)"""
    db = get_db()
    module = db.execute('SELECT batch_id, settings FROM pg_modules WHERE id=%s', (id,)).fetchone()
    students_data = db.execute('SELECT * FROM pg_students WHERE batch_id=%s ORDER BY usn', (module['batch_id'],)).fetchall()
    
    # Load Settings
    settings_json = module['settings']
    settings = json.loads(settings_json) if settings_json else {}
    mode = settings.get('mode', 'auto')
    auto_source = settings.get('auto_source', 'gaussian')  # 'gaussian' or 'actual'
    predict_source = settings.get('predict_source', 'overall')  # 'assignment', 'see', or 'overall'
    
    # Load SEE Ranges (Default 75-95, 45-74, 10-44)
    ranges = settings.get('see_ranges', {
        'bright': {'min': 75, 'max': 95},
        'average': {'min': 45, 'max': 74},
        'poor': {'min': 10, 'max': 44}
    })
    
    # Fetch consolidated assignment marks based on auto_source setting
    assign_mark_type = 'assignment_gaussian' if auto_source == 'gaussian' else 'assignment'
    assign_vals = {r['student_id']: r['value'] for r in db.execute('SELECT student_id, value FROM pg_marks WHERE module_id=%s AND mark_type=%s', (id, assign_mark_type)).fetchall()}
    
    # Fallback: If no Gaussian assignment, use actual assignment
    if auto_source == 'gaussian' and not assign_vals:
        assign_vals = {r['student_id']: r['value'] for r in db.execute('SELECT student_id, value FROM pg_marks WHERE module_id=%s AND mark_type=%s', (id, 'assignment')).fetchall()}
    
    try:
        classifications = {row['student_id']: row['category'] for row in db.execute('SELECT student_id, category FROM pg_student_classifications WHERE module_id=%s', (id,)).fetchall()}
    except:
        classifications = {}

    import random
    count = 0
    
    # Prepare Data for Curve Fitting (Rank based)
    student_list = []
    
    import math
    
    # Prev Module Data for Predict Mode
    predict_module_id = settings.get('predict_module_id', '')
    if predict_module_id:
        prev_module = db.execute('SELECT id FROM pg_modules WHERE id=%s', (predict_module_id,)).fetchone()
    else:
        prev_module = db.execute('SELECT id FROM pg_modules WHERE batch_id=%s AND id < %s ORDER BY id DESC LIMIT 1', (module['batch_id'], id)).fetchone()
        
    prev_marks_map = {}
    if mode == 'predict_prev' and prev_module:
        # Fetch marks based on predict_source setting
        if predict_source == 'assignment':
            # Use assignment_gaussian if available, else assignment
            rows = db.execute('SELECT student_id, value as total FROM pg_marks WHERE module_id=%s AND mark_type IN (%s, %s)', (prev_module['id'], 'assignment', 'assignment_gaussian')).fetchall()
            prev_marks_map = {r['student_id']: r['total'] for r in rows}
        elif predict_source == 'see':
            # Use see_gaussian if available, else see
            rows = db.execute('SELECT student_id, value as total FROM pg_marks WHERE module_id=%s AND mark_type IN (%s, %s)', (prev_module['id'], 'see', 'see_gaussian')).fetchall()
            prev_marks_map = {r['student_id']: r['total'] for r in rows}
        else:  # overall
            # Sum Assignment (50) + SEE Scaled (50)
            # Fetch assignment
            assign_rows = db.execute('SELECT student_id, value FROM pg_marks WHERE module_id=%s AND mark_type IN (%s, %s)', (prev_module['id'], 'assignment', 'assignment_gaussian')).fetchall()
            amd = {r['student_id']: r['value'] for r in assign_rows}
            
            # Fetch SEE (100)
            see_rows = db.execute('SELECT student_id, value FROM pg_marks WHERE module_id=%s AND mark_type IN (%s, %s)', (prev_module['id'], 'see', 'see_gaussian')).fetchall()
            smd = {r['student_id']: r['value'] for r in see_rows}
            
            # Calculate Total (Max 100)
            for sid, aval in amd.items():
                sval = smd.get(sid, 0)
                sval_scaled = math.ceil((sval / 100) * 50)
                prev_marks_map[sid] = aval + sval_scaled
                
        if not prev_marks_map: mode = 'auto'

    for s in students_data:
        student_list.append({'id': s['id'], 'usn': s['usn'], 'assign': assign_vals.get(s['id'], 0), 'cat': classifications.get(s['id'], 'none'), 'prev': prev_marks_map.get(s['id'], 0)})
    
    # Sort by Assignment Score Descending
    student_list.sort(key=lambda x: x['assign'], reverse=True)
    n = len(student_list)
    
    target_grades = {} # sid -> grade
    TARGET_TOTALS = {} # sid -> target_total_100
    
    if mode == 'auto':
        # AUTO: Enforcement of B+ Peak Curve
        counts = {
            'O': max(1, int(n * 0.05)),
            'A+': max(1, int(n * 0.10)),
            'A': max(1, int(n * 0.20)),
            'B+': max(1, int(n * 0.40)), # Ensure this is Max
            'B': max(1, int(n * 0.20)),
            'C': max(0, int(n * 0.05))
        }
        # Remainder to B+
        curr = sum(counts.values())
        if n > curr: counts['B+'] += (n - curr)
        
        # Grade Ranges (Total / 100)
        # O: 91-100, A+: 81-90, A: 71-80, B+: 61-70, B: 50-60, C: 0-49
        g_ranges = {'O':(91,100), 'A+':(81,90), 'A':(71,80), 'B+':(61,70), 'B':(50,60), 'C':(40,49)}
        
        g_list = []
        for g in ['O', 'A+', 'A', 'B+', 'B', 'C']:
            g_list.extend([g] * counts[g])
        
        # Assign Grades based on Rank
        for i, student in enumerate(student_list):
            grade = g_list[i] if i < len(g_list) else 'C'
            t_min, t_max = g_ranges[grade]
            # Randomize within target grade
            target_total = random.randint(t_min, t_max)
            TARGET_TOTALS[student['id']] = target_total
            
    for student in student_list:
        sid = student['id']
        assign_score = student['assign']
        
        if mode == 'auto':
            # Back-calculate SEE
            # Target Total = Assign + (SEE/2)
            
            # LOGIC OVERRIDE: Low Assignment -> Ensure Pass (Total 50)
            if assign_score < 1:
                target_total = random.randint(50, 52) # Just pass
                needed_scaled_see = target_total - assign_score
                needed_raw_see = needed_scaled_see * 2
            else:
                target_total = TARGET_TOTALS.get(sid, 60)
                needed_scaled_see = target_total - assign_score
                needed_raw_see = needed_scaled_see * 2
                
                # Add some noise to avoid identical marks
                needed_raw_see += random.randint(-2, 2)
            
            # Clamp
            final_see = max(0, min(100, needed_raw_see))
            
            # Enforce SEE passed/failed constraints
            if target_total >= 50:
                final_see = max(50, final_see) # Must pass SEE to pass overall
            else:
                final_see = min(49, final_see) # Must fail SEE to fail overall
            
        elif mode == 'predict_prev':
            # PREDICT PREV MODE
            # Use prev module performance to drive SEE
            if assign_score < 1:
                final_see = random.randint(50, 60) # Rescue
            else:
                prev_val = student['prev']
                
                # Determine Max Score Base for Prediction
                # Assignment max 50. SEE max 100. Overall max 100.
                max_score = 50 if predict_source == 'assignment' else 100
                
                # Calculate Ratio (0.0 to 1.0)
                ratio = prev_val / max_score if max_score > 0 else 0
                
                # Target SEE (Max 95)
                # Map Ratio to SEE Range
                base_see = ratio * 95
                
                final_see = int(base_see + random.randint(-4, 6))
                final_see = max(0, min(95, final_see))
                
                # We do not strictly enforce pass/fail based on a total here, 
                # but if prev_val was high, ratio is high, final_see should be high.
                target_total = assign_score + math.ceil((final_see / 100) * 50)
                if target_total >= 50 and final_see < 50:
                    final_see = random.randint(50, 55)

        else:
            # MANUAL MODE
            if assign_score < 1:
                 # Override for manual too
                 final_see = random.randint(90, 100) # Boost to Pass
            else:
                # Use Categories (Bright -> High SEE, etc.)
                cat = student['cat']
                if cat == 'bright':
                    final_see = random.randint(ranges['bright']['min'], ranges['bright']['max'])
                elif cat == 'average':
                    final_see = random.randint(ranges['average']['min'], ranges['average']['max'])
                elif cat == 'poor':
                    final_see = random.randint(ranges['poor']['min'], ranges['poor']['max'])
                else:
                    # If no category, just extrapolate linear
                    final_see = assign_score * 2
                    final_see += random.randint(-5, 5)
            
            final_see = max(0, min(100, final_see))
            target_total = assign_score + math.ceil((final_see / 100) * 50)
            if cat != 'poor' and target_total >= 50:
                final_see = max(50, final_see)
            elif cat == 'poor':
                final_see = min(49, final_see)
        
        # Save to 'see_gaussian'
        existing = db.execute('SELECT id FROM pg_marks WHERE student_id=%s AND module_id=%s AND mark_type=%s', (sid, id, 'see_gaussian')).fetchone()
        if existing:
            db.execute('UPDATE pg_marks SET value=%s WHERE id=%s', (final_see, existing['id']))
        else:
            db.execute('INSERT INTO pg_marks (student_id, module_id, mark_type, value) VALUES (%s, %s, %s, %s)', (sid, id, 'see_gaussian', final_see))
        count += 1
        
    db.commit()
    flash(f'SEE Gaussian ({mode.title()}) applied for {count} students!')
    return redirect(url_for('pg_module_dashboard', id=id, step='see'))


@app.route('/pg/module/<int:id>/classify/<step>', methods=['GET', 'POST'])
def pg_classify_students(id, step):
    """Classify students as Bright, Average, Poor for Gaussian constraints"""
    db = get_db()
    
    # Ensure table exists
    db.execute('''CREATE TABLE IF NOT EXISTS pg_student_classifications (
        id SERIAL PRIMARY KEY,
        student_id INTEGER,
        module_id INTEGER,
        category TEXT
    )''')
    
    module = db.execute('SELECT m.*, b.start_year, b.end_year FROM pg_modules m JOIN pg_batches b ON m.batch_id = b.id WHERE m.id = %s', (id,)).fetchone()
    # Fetch all previous modules for dropdown
    available_prev_modules = db.execute('SELECT id, title, code FROM pg_modules WHERE batch_id=%s AND id < %s ORDER BY id DESC', (module['batch_id'], id)).fetchall()
    prev_module = available_prev_modules[0] if available_prev_modules else None
    
    students = db.execute('SELECT * FROM pg_students WHERE batch_id = %s ORDER BY usn', (module['batch_id'],)).fetchall()
    
    if request.method == 'POST':
        # Save Settings
        mode = request.form.get('logic_mode', 'auto')
        
        # Parse ranges
        try:
            if step == 'see':
                # SAVE SEE SETTINGS
                see_ranges = {
                    'bright': {'min': int(request.form.get('bright_min', 75)), 'max': int(request.form.get('bright_max', 95))},
                    'average': {'min': int(request.form.get('avg_min', 45)), 'max': int(request.form.get('avg_max', 74))},
                    'poor': {'min': int(request.form.get('poor_min', 10)), 'max': int(request.form.get('poor_max', 44))}
                }
                auto_source = request.form.get('auto_source', 'gaussian')  # 'gaussian' or 'actual'
                predict_source = request.form.get('predict_source', 'overall')  # 'assignment', 'see', or 'overall'
                predict_module_id = request.form.get('predict_module_id', '') # Selected Module ID
                if predict_module_id: predict_module_id = int(predict_module_id)
                elif prev_module: predict_module_id = prev_module['id']
                
                # Load existing to preserve other settings
                old_settings = json.loads(module['settings']) if module['settings'] else {}
                old_settings['mode'] = mode
                old_settings['see_ranges'] = see_ranges
                old_settings['auto_source'] = auto_source
                old_settings['predict_source'] = predict_source
                old_settings['predict_module_id'] = predict_module_id
                settings = old_settings
            else:
                # SAVE ASSIGNMENT SETTINGS
                ranges = {
                    'bright': {'min': int(request.form.get('bright_min', 30)), 'max': int(request.form.get('bright_max', 40))},
                    'average': {'min': int(request.form.get('avg_min', 18)), 'max': int(request.form.get('avg_max', 29))},
                    'poor': {'min': int(request.form.get('poor_min', 0)), 'max': int(request.form.get('poor_max', 17))}
                }
                # Load existing to preserve other settings
                old_settings = json.loads(module['settings']) if module['settings'] else {}
                old_settings['mode'] = mode
                old_settings['ranges'] = ranges
                settings = old_settings
            
            db.execute('UPDATE pg_modules SET settings=%s WHERE id=%s', (json.dumps(settings), id))
        except Exception as e:
            flash(f'Error saving settings: {e}')
        
        for s in students:
            cat = request.form.get(f'cat_{s["id"]}', 'none')
            # Update or Insert
            existing = db.execute('SELECT id FROM pg_student_classifications WHERE student_id=%s AND module_id=%s', (s['id'], id)).fetchone()
            if existing:
                db.execute('UPDATE pg_student_classifications SET category=%s WHERE id=%s', (cat, existing['id']))
            else:
                db.execute('INSERT INTO pg_student_classifications (student_id, module_id, category) VALUES (%s, %s, %s)', (s['id'], id, cat))
        db.commit()
        flash('Settings & Classifications saved!')
        if step == 'single':
            return redirect(url_for('pg_module_dashboard', id=id, step='assignment', amode='single'))
        else:
            return redirect(url_for('pg_module_dashboard', id=id, step='assignment', amode='multi') + '#tab' + step[-1])
    
    # Load Settings
    settings_json = module['settings']
    settings = json.loads(settings_json) if settings_json else {}
    mode = settings.get('mode', 'auto')
    
    if step == 'see':
        # Load SEE Ranges (Default 75-95, 45-74, 10-44)
        current_ranges = settings.get('see_ranges', {
            'bright': {'min': 75, 'max': 95},
            'average': {'min': 45, 'max': 74},
            'poor': {'min': 10, 'max': 44}
        })
        range_max = 100
        title_suffix = "(Max 100 - SEE)"
        auto_source = settings.get('auto_source', 'gaussian')  # 'gaussian' or 'actual'
        predict_source = settings.get('predict_source', 'overall')  # 'assignment', 'see', or 'overall'
        predict_module_id = settings.get('predict_module_id', prev_module['id'] if prev_module else '')
    else:
        # Load Assignment Ranges (Default 30-40, 18-29, 0-17)
        current_ranges = settings.get('ranges', {
            # Updated for Max 50 scale (scaled from 100):
            # O: 46-50, A+: 41-45, A: 36-40, B+: 31-35, B: 25-30, C: <25
            # Bright → A range, Average → B+ range (peak), Poor → B range
            'bright': {'min': 36, 'max': 45},
            'average': {'min': 31, 'max': 35},
            'poor': {'min': 25, 'max': 30}
        })
        range_max = 50
        title_suffix = "(Max 50 - Assignment)"
    
    # Enable Dictionary access for classifications
    # classifications = {row['student_id']: row['category'] for row in db.execute('SELECT student_id, category FROM pg_student_classifications WHERE module_id=%s', (id,)).fetchall()}
    
    student_rows = ''
    for s in students:
        # Get current category
        cat_res = db.execute('SELECT category FROM pg_student_classifications WHERE student_id=%s AND module_id=%s', (s['id'], id)).fetchone()
        cat = cat_res['category'] if cat_res else 'none'
        
        student_rows += f'''
        <tr style="border-bottom:1px solid #e2e8f0;">
            <td style="padding:0.75rem;"><strong>{s['usn']}</strong></td>
            <td style="padding:0.75rem;">{s['name']}</td>
            <td style="padding:0.75rem;">
                <div style="display:flex; gap:15px; align-items:center;">
                    <label style="cursor:pointer; display:flex; align-items:center; gap:4px;">
                        <input type="radio" name="cat_{s['id']}" value="bright" {'checked' if cat=='bright' else ''}> 
                        <span style="color:#16a34a;">🌟 Bright</span>
                    </label>
                    <label style="cursor:pointer; display:flex; align-items:center; gap:4px;">
                        <input type="radio" name="cat_{s['id']}" value="average" {'checked' if cat=='average' else ''}> 
                        <span style="color:#2563eb;">📊 Average</span>
                    </label>
                    <label style="cursor:pointer; display:flex; align-items:center; gap:4px;">
                        <input type="radio" name="cat_{s['id']}" value="poor" {'checked' if cat=='poor' else ''}> 
                        <span style="color:#dc2626;">⚠️ Poor</span>
                    </label>
                    <!-- allow clearing -->
                    <label style="cursor:pointer; display:flex; align-items:center; gap:4px; font-size:0.85rem; color:#6b7280; margin-left:10px;">
                        <input type="radio" name="cat_{s['id']}" value="none" {'checked' if cat=='none' else ''}> Clear
                    </label>
                </div>
            </td>
        </tr>
        '''

    content = f'''
    <div class="container">
        <div class="mb-3">
             <h1 class="page-title mt-2">⚙️ Gaussian Settings { '(SEE)' if step=='see' else '' }</h1>
             <p class="page-subtitle">Configure Logic & Categories</p>
        </div>
        <form method="post">
            <div class="card">
                <!-- Logic Mode -->
                <div style="background:#f0fdf4; padding:1.5rem; border-radius:8px; margin-bottom:1.5rem; border:1px solid #86efac;">
                    <h5 style="margin-top:0;">1. Logic Mode</h5>
                    <div style="display:flex; gap:2rem; flex-wrap:wrap;">
                        <label style="cursor:pointer; display:flex; align-items:center;">
                            <input type="radio" name="logic_mode" value="auto" {'checked' if mode=='auto' else ''} style="transform:scale(1.2); margin-right:8px;"> 
                            <div>
                                <strong>Auto (Gaussian Curve)</strong><br>
                                <small class="text-muted">Strictly follows B+ Max Curve. Ignores categories.</small>
                            </div>
                        </label>
                        <label style="cursor:pointer; display:flex; align-items:center;">
                            <input type="radio" name="logic_mode" value="manual" {'checked' if mode=='manual' else ''} style="transform:scale(1.2); margin-right:8px;"> 
                            <div>
                                <strong>Manual (Categories)</strong><br>
                                <small class="text-muted">Strict ranges based on class</small>
                            </div>
                        </label>
                    </div>
                    ''' + (f'''
                    <div style="margin-top:1.5rem; border-top:1px dashed #86efac; padding-top:1rem;">
                         <label style="cursor:pointer; display:flex; align-items:center;">
                            <input type="radio" name="logic_mode" value="predict_prev" {'checked' if mode=='predict_prev' else ''} style="transform:scale(1.2); margin-right:8px;"> 
                            <div>
                                <strong>Predict based on Previous Module</strong><br>
                                <small class="text-muted">Uses performance from the last module to predict results.</small>
                            </div>
                        </label>
                    </div>
                    ''' if available_prev_modules else '') + '''
                </div>
                
                ''' + (f'''
                <!-- SEE Auto Sub-Options -->
                <div id="auto-options" style="background:#ecfdf5; padding:1rem; border-radius:8px; margin-bottom:1.5rem; border:1px solid #10b981;">
                    <h5 style="margin-top:0; color:#059669;">1a. Auto Mode - Rank Students By:</h5>
                    <div style="display:flex; gap:2rem;">
                        <label style="cursor:pointer; display:flex; align-items:center;">
                            <input type="radio" name="auto_source" value="gaussian" {'checked' if auto_source=='gaussian' else ''} style="margin-right:8px;">
                            <span style="color:#059669;"><strong>Gaussian Assignment</strong></span>
                            <small class="text-muted" style="margin-left:5px;">(from consolidation)</small>
                        </label>
                        <label style="cursor:pointer; display:flex; align-items:center;">
                            <input type="radio" name="auto_source" value="actual" {'checked' if auto_source=='actual' else ''} style="margin-right:8px;">
                            <span style="color:#2563eb;"><strong>Actual Assignment</strong></span>
                            <small class="text-muted" style="margin-left:5px;">(original marks)</small>
                        </label>
                    </div>
                </div>
                
                <!-- SEE Predict Sub-Options -->
                <div id="predict-options" style="background:#eff6ff; padding:1rem; border-radius:8px; margin-bottom:1.5rem; border:1px solid #3b82f6;">
                    <h5 style="margin-top:0; color:#2563eb;">1b. Predict Mode - Configuration:</h5>
                    
                    <div style="margin-bottom:1rem;">
                         <label style="display:block; margin-bottom:0.5rem; font-weight:bold;">Select Previous Module:</label>
                         <select name="predict_module_id" class="form-control" style="max-width:400px;">
                             {''.join([f'<option value="{Pm["id"]}" {"selected" if str(Pm["id"])==str(predict_module_id) else ""}>{Pm["code"]} - {Pm["title"]}</option>' for Pm in available_prev_modules])}
                         </select>
                    </div>
                    
                    <label style="display:block; margin-bottom:0.5rem; font-weight:bold;">Predict Based On:</label>
                    <div style="display:flex; gap:2rem; flex-wrap:wrap;">
                        <label style="cursor:pointer; display:flex; align-items:center;">
                            <input type="radio" name="predict_source" value="assignment" {'checked' if predict_source=='assignment' else ''} style="margin-right:8px;">
                            <span><strong>Assignment Only</strong></span>
                        </label>
                        <label style="cursor:pointer; display:flex; align-items:center;">
                            <input type="radio" name="predict_source" value="see" {'checked' if predict_source=='see' else ''} style="margin-right:8px;">
                            <span><strong>SEE Only</strong></span>
                        </label>
                        <label style="cursor:pointer; display:flex; align-items:center;">
                            <input type="radio" name="predict_source" value="overall" {'checked' if predict_source=='overall' else ''} style="margin-right:8px;">
                            <span><strong>Overall (Assignment + SEE)</strong></span>
                        </label>
                    </div>
                </div>
                ''' if step == 'see' else '') + f'''
                
                <div id="manual-section" style="display:none;">
                    <!-- Ranges -->
                    <div style="background:#f8fafc; padding:1.5rem; border-radius:8px; margin-bottom:1.5rem; border:1px solid #e2e8f0;">
                        <h5 style="margin-top:0;">2. Mark Ranges {title_suffix}</h5>
                        <small class="text-muted d-block mb-2">Define the min/max marks for each category in Manual Mode.</small>
                        <div style="display:flex; gap:2rem; flex-wrap:wrap;">
                            <div style="background:white; padding:1rem; border-radius:8px; border-left:4px solid #16a34a; box-shadow:0 2px 4px rgba(0,0,0,0.05);">
                                <strong style="color:#16a34a;">Bright</strong>
                                <div style="margin-top:0.5rem; display:flex; gap:0.5rem; align-items:center;">
                                    <input type="number" name="bright_min" value="{current_ranges['bright']['min']}" style="width:60px; padding:4px;" min="0" max="{range_max}">
                                    <span>to</span>
                                    <input type="number" name="bright_max" value="{current_ranges['bright']['max']}" style="width:60px; padding:4px;" min="0" max="{range_max}">
                                </div>
                            </div>
                             <div style="background:white; padding:1rem; border-radius:8px; border-left:4px solid #2563eb; box-shadow:0 2px 4px rgba(0,0,0,0.05);">
                                <strong style="color:#2563eb;">Average</strong>
                                <div style="margin-top:0.5rem; display:flex; gap:0.5rem; align-items:center;">
                                    <input type="number" name="avg_min" value="{current_ranges['average']['min']}" style="width:60px; padding:4px;" min="0" max="{range_max}">
                                    <span>to</span>
                                    <input type="number" name="avg_max" value="{current_ranges['average']['max']}" style="width:60px; padding:4px;" min="0" max="{range_max}">
                                </div>
                            </div>
                             <div style="background:white; padding:1rem; border-radius:8px; border-left:4px solid #dc2626; box-shadow:0 2px 4px rgba(0,0,0,0.05);">
                                <strong style="color:#dc2626;">Poor</strong>
                                <div style="margin-top:0.5rem; display:flex; gap:0.5rem; align-items:center;">
                                    <input type="number" name="poor_min" value="{current_ranges['poor']['min']}" style="width:60px; padding:4px;" min="0" max="{range_max}">
                                    <span>to</span>
                                    <input type="number" name="poor_max" value="{current_ranges['poor']['max']}" style="width:60px; padding:4px;" min="0" max="{range_max}">
                                </div>
                            </div>
                        </div>
                    </div>
                    
                    <h5 style="margin-bottom:1rem;">3. Classify Students</h5>
                    <table style="width:100%; border-collapse:collapse;">
                        <tr style="background:#f1f5f9; text-align:left;">
                            <th style="padding:0.75rem;">USN</th>
                            <th style="padding:0.75rem;">Name</th>
                            <th style="padding:0.75rem;">Category</th>
                        </tr>
                        {student_rows}
                    </table>
                </div>
                
                <div class="mt-3">
                    <button class="btn btn-primary">💾 Save All Settings</button>
                    <a href="/pg/module/{id}?step={ 'see' if step=='see' else 'assignment' }" class="btn btn-outline" style="margin-left:10px;">Cancel</a>
                </div>
            </div>
        </form>
    </div>
    <div style="text-align: center; margin-top: 3rem; margin-bottom: 2rem;">
        <a href="/pg/module/{id}?step={ 'see' if step=='see' else 'assignment' }" class="btn-back">⬅️ Back to { 'SEE' if step=='see' else 'Assignment' }</a>
    </div>
    <script>
    function updateVisibility() {{
        const mode = document.querySelector('input[name="logic_mode"]:checked').value;
        const autoSection = document.getElementById('auto-options');
        const predictSection = document.getElementById('predict-options');
        const manualSection = document.getElementById('manual-section');
        
        if(autoSection) autoSection.style.display = (mode === 'auto') ? 'block' : 'none';
        if(predictSection) predictSection.style.display = (mode === 'predict_prev') ? 'block' : 'none';
        if(manualSection) manualSection.style.display = (mode === 'manual') ? 'block' : 'none';
    }}
    
    // Add event listeners to radio buttons
    document.querySelectorAll('input[name="logic_mode"]').forEach(radio => {{
        radio.addEventListener('change', updateVisibility);
    }});
    
    // Initial call
    updateVisibility();
    </script>'''
    return render_template_string(base_html(f'Classify Students - CAB', content))



# Initialize database tables on startup (works under both gunicorn and direct run)
init_db()

if __name__ == '__main__':
    PORT = int(os.environ.get('PORT', 5000))
    
    # Print console banner
    print("\n" + "="*60)
    print("  CAB - Course Assessment Board")
    print("="*60)
    print(f"\n  URL: http://localhost:{PORT}")
    print("\n  Login: hod / 123")
    print("="*60 + "\n")
    
    app.run(host='0.0.0.0', port=PORT, debug=True)