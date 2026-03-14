import os
import json
import ssl
import sqlite3
import shutil
from decimal import Decimal
from datetime import datetime
from functools import wraps
from urllib.parse import urlparse, unquote
from urllib import error as urllib_error
from urllib import request as urllib_request

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify, g
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

try:
    import certifi
except ImportError:
    certifi = None

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

try:
    import pg8000.dbapi as pg_dbapi
except ImportError:
    pg_dbapi = None

app = Flask(__name__)
app.secret_key = os.environ.get('SESSION_SECRET', 'dev-secret-key-change-in-production')

# app.config['SESSION_COOKIE_SAMESITE'] = 'None'
# app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True

IS_VERCEL = bool(os.environ.get('VERCEL'))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DATA_DIR = '/tmp/internsphere' if IS_VERCEL else BASE_DIR

UPLOAD_FOLDER = os.path.join(RUNTIME_DATA_DIR, 'uploads')
ALLOWED_EXTENSIONS = {'pdf', 'doc', 'docx', 'txt'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
USE_POSTGRES = DATABASE_URL.startswith(('postgres://', 'postgresql://'))
DATABASE = os.environ.get('DATABASE_PATH', os.path.join(RUNTIME_DATA_DIR, 'jobportal.db'))

# Ollama API setup
OLLAMA_HOST = os.environ.get('OLLAMA_HOST', 'https://ollama.com').rstrip('/')
OLLAMA_API_KEY = (os.environ.get('OLLAMA_API_KEY') or '').strip() or None
OLLAMA_MODEL = os.environ.get('OLLAMA_MODEL', 'gpt-oss:20b')
OLLAMA_TIMEOUT = int(os.environ.get('OLLAMA_TIMEOUT', '60'))
PLACEHOLDER_OLLAMA_KEYS = {'your_ollama_api_key', 'ollama_api_key'}
LAST_OLLAMA_ERROR = None

if OLLAMA_API_KEY in PLACEHOLDER_OLLAMA_KEYS:
    OLLAMA_API_KEY = None


def _ollama_api_url(path: str) -> str:
    base = OLLAMA_HOST
    if base.endswith('/api'):
        return f'{base}{path}'
    return f'{base}/api{path}'


def _using_ollama_cloud() -> bool:
    return 'ollama.com' in OLLAMA_HOST


def ollama_available() -> bool:
    if _using_ollama_cloud():
        return bool(OLLAMA_API_KEY)
    return True


def _ollama_key_error_message() -> str:
    return 'Set OLLAMA_API_KEY in .env to a real Ollama Cloud key.'


def _set_ollama_error(message: str | None):
    global LAST_OLLAMA_ERROR
    LAST_OLLAMA_ERROR = message


def _get_ollama_ssl_context():
    if not OLLAMA_HOST.startswith('https://'):
        return None
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


def _ollama_headers():
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }
    if OLLAMA_API_KEY:
        headers['Authorization'] = f'Bearer {OLLAMA_API_KEY}'
    return headers


def ollama_chat(messages, *, model: str | None = None, temperature: float = 0.2):
    if not ollama_available():
        return None

    _set_ollama_error(None)
    payload = {
        'model': model or OLLAMA_MODEL,
        'messages': messages,
        'stream': False,
        'options': {'temperature': temperature},
    }

    request_data = json.dumps(payload).encode('utf-8')
    http_request = urllib_request.Request(
        _ollama_api_url('/chat'),
        data=request_data,
        headers=_ollama_headers(),
        method='POST',
    )

    try:
        with urllib_request.urlopen(
            http_request,
            timeout=OLLAMA_TIMEOUT,
            context=_get_ollama_ssl_context(),
        ) as response:
            data = json.loads(response.read().decode('utf-8'))
    except urllib_error.HTTPError as error:
        error_body = error.read().decode('utf-8', errors='ignore')
        if error.code == 401:
            _set_ollama_error('Ollama Cloud rejected the API key. Please verify OLLAMA_API_KEY.')
        elif error.code == 404:
            _set_ollama_error(f'Ollama model "{model or OLLAMA_MODEL}" was not found.')
        else:
            _set_ollama_error(f'Ollama API error ({error.code}). Please try again in a moment.')
        print(f'Ollama API error ({error.code}): {error_body}')
        return None
    except ssl.SSLError as error:
        _set_ollama_error(f'Ollama SSL error: {error}')
        print(f'Ollama SSL error: {error}')
        return None
    except Exception as error:
        _set_ollama_error(f'Ollama request error: {error}')
        print(f'Ollama request error: {error}')
        return None

    return data.get('message', {}).get('content', '')


def current_timestamp() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


class DictRow(dict):
    def __init__(self, columns, values):
        super().__init__(zip(columns, values))
        self._values = list(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class PostgresCursorWrapper:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def description(self):
        return self._cursor.description

    @property
    def lastrowid(self):
        return getattr(self._cursor, 'lastrowid', None)

    def _wrap_row(self, row):
        if row is None or not self._cursor.description:
            return row
        columns = [column[0] for column in self._cursor.description]
        values = [int(value) if isinstance(value, Decimal) and value == int(value) else value for value in row]
        return DictRow(columns, values)

    def fetchone(self):
        return self._wrap_row(self._cursor.fetchone())

    def fetchall(self):
        rows = self._cursor.fetchall()
        return [self._wrap_row(row) for row in rows]


class PostgresConnectionWrapper:
    def __init__(self, connection):
        self._connection = connection

    def execute(self, query, params=None):
        cursor = self._connection.cursor()
        cursor.execute(query.replace('?', '%s'), params or ())
        return PostgresCursorWrapper(cursor)

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()


def _postgres_connect():
    if not pg_dbapi:
        raise RuntimeError('pg8000 is required when DATABASE_URL points to Postgres.')

    parsed = urlparse(DATABASE_URL)
    return pg_dbapi.connect(
        user=unquote(parsed.username or ''),
        password=unquote(parsed.password or ''),
        host=parsed.hostname or 'localhost',
        port=parsed.port or 5432,
        database=(parsed.path or '/').lstrip('/'),
        ssl_context=ssl.create_default_context(),
    )


def format_salary_inr(amount):
    if amount in (None, ''):
        return ''
    try:
        return f"INR {int(amount):,}"
    except (TypeError, ValueError):
        return str(amount)


def demo_password_hash():
    cached_hash = getattr(app, '_demo_password_hash', None)
    if cached_hash:
        return cached_hash

    app._demo_password_hash = generate_password_hash('demo123')
    return app._demo_password_hash


def seed_runtime_database():
    if USE_POSTGRES or not IS_VERCEL or os.path.exists(DATABASE):
        return

    source_db = os.path.join(BASE_DIR, 'jobportal.db')
    os.makedirs(os.path.dirname(DATABASE), exist_ok=True)
    if os.path.exists(source_db):
        shutil.copyfile(source_db, DATABASE)


app_initialized = False


def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        if USE_POSTGRES:
            db = g._database = PostgresConnectionWrapper(_postgres_connect())
        else:
            db = g._database = sqlite3.connect(DATABASE)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys = ON')
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


@app.context_processor
def inject_ai_config():
    provider_label = 'Ollama Cloud' if _using_ollama_cloud() else 'Ollama'
    return {
        'ai_provider_label': provider_label,
        'ai_model_name': OLLAMA_MODEL,
        'ai_enabled': ollama_available(),
        'is_vercel_deployment': IS_VERCEL,
        'currency_label': 'INR',
    }


@app.template_filter('inr')
def inr_filter(amount):
    return format_salary_inr(amount)


def initialize_application():
    global app_initialized
    if app_initialized:
        return

    seed_runtime_database()
    with app.app_context():
        init_db()
        seed_sample_data()
    app_initialized = True


@app.before_request
def ensure_app_initialized():
    initialize_application()


def init_db():
    with app.app_context():
        db = get_db()
        statements = []
        if USE_POSTGRES:
            statements = [
                '''
                CREATE TABLE IF NOT EXISTS users (
                    id BIGSERIAL PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    user_type TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                ''',
                '''
                CREATE TABLE IF NOT EXISTS job_seeker_profiles (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    full_name TEXT,
                    phone TEXT,
                    location TEXT,
                    title TEXT,
                    bio TEXT,
                    skills TEXT,
                    experience TEXT,
                    education TEXT,
                    resume_path TEXT,
                    resume_text TEXT,
                    ai_insights TEXT,
                    profile_photo TEXT,
                    linkedin_url TEXT,
                    portfolio_url TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                ''',
                '''
                CREATE TABLE IF NOT EXISTS recruiter_profiles (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    full_name TEXT,
                    phone TEXT,
                    company_name TEXT,
                    company_description TEXT,
                    company_website TEXT,
                    company_logo TEXT,
                    industry TEXT,
                    company_size TEXT,
                    location TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                ''',
                '''
                CREATE TABLE IF NOT EXISTS jobs (
                    id BIGSERIAL PRIMARY KEY,
                    recruiter_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT,
                    job_type TEXT,
                    salary_min INTEGER,
                    salary_max INTEGER,
                    description TEXT,
                    requirements TEXT,
                    benefits TEXT,
                    skills_required TEXT,
                    experience_level TEXT,
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                ''',
                '''
                CREATE TABLE IF NOT EXISTS applications (
                    id BIGSERIAL PRIMARY KEY,
                    job_id BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    seeker_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    cover_letter TEXT,
                    status TEXT DEFAULT 'pending',
                    recruiter_notes TEXT,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(job_id, seeker_id)
                )
                ''',
                '''
                CREATE TABLE IF NOT EXISTS saved_jobs (
                    id BIGSERIAL PRIMARY KEY,
                    job_id BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    seeker_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(job_id, seeker_id)
                )
                ''',
            ]
        else:
            statements = [
                '''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    user_type TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                ''',
                '''
                CREATE TABLE IF NOT EXISTS job_seeker_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE NOT NULL,
                    full_name TEXT,
                    phone TEXT,
                    location TEXT,
                    title TEXT,
                    bio TEXT,
                    skills TEXT,
                    experience TEXT,
                    education TEXT,
                    resume_path TEXT,
                    resume_text TEXT,
                    ai_insights TEXT,
                    profile_photo TEXT,
                    linkedin_url TEXT,
                    portfolio_url TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                ''',
                '''
                CREATE TABLE IF NOT EXISTS recruiter_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE NOT NULL,
                    full_name TEXT,
                    phone TEXT,
                    company_name TEXT,
                    company_description TEXT,
                    company_website TEXT,
                    company_logo TEXT,
                    industry TEXT,
                    company_size TEXT,
                    location TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                ''',
                '''
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recruiter_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT,
                    job_type TEXT,
                    salary_min INTEGER,
                    salary_max INTEGER,
                    description TEXT,
                    requirements TEXT,
                    benefits TEXT,
                    skills_required TEXT,
                    experience_level TEXT,
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (recruiter_id) REFERENCES users(id)
                )
                ''',
                '''
                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL,
                    seeker_id INTEGER NOT NULL,
                    cover_letter TEXT,
                    status TEXT DEFAULT 'pending',
                    recruiter_notes TEXT,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (job_id) REFERENCES jobs(id),
                    FOREIGN KEY (seeker_id) REFERENCES users(id),
                    UNIQUE(job_id, seeker_id)
                )
                ''',
                '''
                CREATE TABLE IF NOT EXISTS saved_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL,
                    seeker_id INTEGER NOT NULL,
                    saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (job_id) REFERENCES jobs(id),
                    FOREIGN KEY (seeker_id) REFERENCES users(id),
                    UNIQUE(job_id, seeker_id)
                )
                ''',
            ]

        for statement in statements:
            db.execute(statement)
        db.commit()


def _create_sample_recruiters(db, start_index=0, total=50):
    recruiter_templates = [
        ('Aarav', 'Mehta', 'TechNova', 'Technology', '11-50', 'Bengaluru', 'Platform Engineer'),
        ('Ishita', 'Sharma', 'Finverse', 'Fintech', '51-200', 'Mumbai', 'Product Designer'),
        ('Rohan', 'Patel', 'HealthGrid', 'Healthcare', '51-200', 'Hyderabad', 'Data Analyst'),
        ('Ananya', 'Singh', 'RetailSpark', 'Retail', '11-50', 'Delhi', 'Growth Marketer'),
        ('Kabir', 'Gupta', 'CloudNest', 'SaaS', '201-500', 'Pune', 'Frontend Developer'),
        ('Meera', 'Nair', 'EduBloom', 'EdTech', '11-50', 'Chennai', 'Customer Success Specialist'),
        ('Vivaan', 'Joshi', 'LogiCore', 'Logistics', '51-200', 'Ahmedabad', 'Operations Manager'),
        ('Siya', 'Reddy', 'GreenGrid', 'Climate Tech', '11-50', 'Gurugram', 'Business Analyst'),
        ('Arjun', 'Kapoor', 'MediaMint', 'Media', '201-500', 'Noida', 'Content Strategist'),
        ('Diya', 'Verma', 'SecureStack', 'Cybersecurity', '51-200', 'Kolkata', 'Security Engineer'),
    ]

    password = demo_password_hash()
    for index in range(start_index, total):
        first_name, last_name, brand, industry, company_size, location, role = recruiter_templates[index % len(recruiter_templates)]
        company_name = f'{brand} Labs {index + 1}'
        email = f'sample.recruiter{index + 1}@internsphere.demo'
        full_name = f'{first_name} {last_name} {index + 1}'
        recruiter_user = db.execute('INSERT INTO users (email, password, user_type) VALUES (?, ?, ?) RETURNING id', (email, password, 'recruiter')).fetchone()
        recruiter_id = recruiter_user['id']
        db.execute(
            '''
            INSERT INTO recruiter_profiles (
                user_id, full_name, phone, company_name, company_description,
                company_website, industry, company_size, location, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                recruiter_id,
                full_name,
                f'+91-90000{index:05d}',
                company_name,
                f'{company_name} hires across engineering, product, sales, and operations roles in India.',
                f'https://{brand.lower()}labs{index + 1}.example.com',
                industry,
                company_size,
                location,
                current_timestamp(),
            ),
        )
        db.execute(
            '''
            INSERT INTO jobs (
                recruiter_id, title, company, location, job_type, salary_min, salary_max,
                description, requirements, benefits, skills_required, experience_level, is_active, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                recruiter_id,
                role,
                company_name,
                location,
                ['full-time', 'remote', 'contract', 'internship'][index % 4],
                400000 + (index * 15000),
                650000 + (index * 18000),
                f'Join {company_name} to build high-impact products for customers across India.',
                'Strong communication, role-specific problem solving, and collaborative execution.',
                'Health cover, flexible work, learning budget, and performance bonus.',
                'Python, SQL, Communication, Problem Solving',
                ['entry', 'mid', 'senior'][index % 3],
                1,
                current_timestamp(),
            ),
        )


def _create_sample_seekers(db, start_index=0, total=50):
    seeker_templates = [
        ('Priya', 'Iyer', 'Frontend Developer', 'Bengaluru', 'React, JavaScript, CSS, Figma', '2 years building web apps', 'B.Tech Computer Science'),
        ('Rahul', 'Malhotra', 'Backend Developer', 'Pune', 'Python, Flask, PostgreSQL, APIs', '3 years building backend services', 'B.E. Information Technology'),
        ('Sneha', 'Kulkarni', 'UI/UX Designer', 'Mumbai', 'Figma, UX Research, Wireframing, Prototyping', '2 years in product design', 'B.Des Interaction Design'),
        ('Aditya', 'Saxena', 'Data Analyst', 'Hyderabad', 'SQL, Excel, Power BI, Python', '2 years in analytics', 'B.Sc Statistics'),
        ('Kavya', 'Menon', 'Product Manager', 'Chennai', 'Roadmapping, Analytics, Stakeholder Management', '4 years in product delivery', 'MBA Product Management'),
        ('Neel', 'Chopra', 'DevOps Engineer', 'Noida', 'AWS, Docker, CI/CD, Linux', '3 years in cloud operations', 'B.Tech Electronics'),
        ('Pooja', 'Bansal', 'Marketing Specialist', 'Delhi', 'SEO, Performance Marketing, Content Strategy', '2 years in digital marketing', 'BBA Marketing'),
        ('Nikhil', 'Das', 'Operations Associate', 'Kolkata', 'Operations, Excel, Vendor Management, Reporting', '1 year in operations', 'B.Com'),
        ('Tanvi', 'Jain', 'Customer Success Manager', 'Ahmedabad', 'CRM, Communication, Onboarding, Retention', '3 years in customer success', 'BA English'),
        ('Yash', 'Agarwal', 'Security Analyst', 'Gurugram', 'SIEM, Networking, Incident Response, Python', '2 years in security operations', 'B.Tech Cybersecurity'),
    ]

    password = demo_password_hash()
    for index in range(start_index, total):
        first_name, last_name, title, location, skills, experience, education = seeker_templates[index % len(seeker_templates)]
        email = f'sample.seeker{index + 1}@internsphere.demo'
        full_name = f'{first_name} {last_name} {index + 1}'
        seeker_user = db.execute('INSERT INTO users (email, password, user_type) VALUES (?, ?, ?) RETURNING id', (email, password, 'seeker')).fetchone()
        seeker_id = seeker_user['id']
        db.execute(
            '''
            INSERT INTO job_seeker_profiles (
                user_id, full_name, phone, location, title, bio, skills, experience,
                education, resume_text, linkedin_url, portfolio_url, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                seeker_id,
                full_name,
                f'+91-80000{index:05d}',
                location,
                title,
                f'{full_name} is actively exploring their next role and is available for interviews.',
                skills,
                experience,
                education,
                f'Resume summary for {full_name}: {skills}. {experience}.',
                f'https://linkedin.com/in/sample-seeker-{index + 1}',
                f'https://portfolio.example.com/sample-seeker-{index + 1}',
                current_timestamp(),
            ),
        )


def _create_specialist_sample_jobs(db):
    recruiters = db.execute(
        '''
        SELECT u.id, rp.company_name, rp.location
        FROM users u
        JOIN recruiter_profiles rp ON rp.user_id = u.id
        WHERE u.email LIKE 'sample.recruiter%@internsphere.demo'
        ORDER BY u.id
        LIMIT 20
        '''
    ).fetchall()

    existing_pairs = {
        (row['recruiter_id'], row['title'])
        for row in db.execute(
            '''
            SELECT recruiter_id, title
            FROM jobs
            WHERE title IN ('Full Stack Developer', 'Java Developer')
              AND recruiter_id IN (
                  SELECT id FROM users WHERE email LIKE 'sample.recruiter%@internsphere.demo'
              )
            '''
        ).fetchall()
    }

    role_templates = [
        {
            'title': 'Full Stack Developer',
            'job_type': 'full-time',
            'salary_min': 700000,
            'salary_max': 1100000,
            'description': 'Build end-to-end product features across React frontends, APIs, and cloud deployments.',
            'requirements': '2+ years of experience with modern JavaScript, backend APIs, databases, and production debugging.',
            'benefits': 'Hybrid work, health insurance, learning stipend, and performance bonus.',
            'skills_required': 'React, Node.js, Python, SQL, REST APIs, Docker',
            'experience_level': 'mid',
        },
        {
            'title': 'Java Developer',
            'job_type': 'full-time',
            'salary_min': 650000,
            'salary_max': 1050000,
            'description': 'Design and ship backend services in Java for customer-facing products and internal platforms.',
            'requirements': '2+ years of experience with Java, Spring Boot, relational databases, and API development.',
            'benefits': 'Remote flexibility, insurance cover, certification budget, and paid leave.',
            'skills_required': 'Java, Spring Boot, MySQL, REST APIs, Microservices, Git',
            'experience_level': 'mid',
        },
    ]

    for index, recruiter in enumerate(recruiters):
        template = role_templates[index % len(role_templates)]
        role_key = (recruiter['id'], template['title'])
        if role_key in existing_pairs:
            continue

        db.execute(
            '''
            INSERT INTO jobs (
                recruiter_id, title, company, location, job_type, salary_min, salary_max,
                description, requirements, benefits, skills_required, experience_level, is_active, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                recruiter['id'],
                template['title'],
                recruiter['company_name'],
                recruiter['location'],
                template['job_type'],
                template['salary_min'] + (index * 10000),
                template['salary_max'] + (index * 12000),
                template['description'],
                template['requirements'],
                template['benefits'],
                template['skills_required'],
                template['experience_level'],
                1,
                current_timestamp(),
            ),
        )


def seed_sample_data():
    db = get_db()
    recruiter_count = db.execute(
        "SELECT COUNT(*) FROM users WHERE email LIKE 'sample.recruiter%@internsphere.demo'"
    ).fetchone()[0]
    seeker_count = db.execute(
        "SELECT COUNT(*) FROM users WHERE email LIKE 'sample.seeker%@internsphere.demo'"
    ).fetchone()[0]

    if recruiter_count < 50:
        _create_sample_recruiters(db, start_index=recruiter_count, total=50)
    if seeker_count < 50:
        _create_sample_seekers(db, start_index=seeker_count, total=50)
    _create_specialist_sample_jobs(db)
    db.commit()


def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)

    return decorated_function


def seeker_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('login'))
        if session.get('user_type') != 'seeker':
            flash('This page is only accessible to job seekers.', 'error')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)

    return decorated_function


def recruiter_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('login'))
        if session.get('user_type') != 'recruiter':
            flash('This page is only accessible to recruiters.', 'error')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)

    return decorated_function


# ---------- OLLAMA HELPERS ----------

def _extract_json_from_text(text: str):
    """
    LLM responses sometimes wrap JSON in extra text or markdown.
    This tries to safely extract the JSON part.
    """
    if not text:
        return None

    try:
        # First try direct parse
        return json.loads(text)
    except Exception:
        pass

    # Try to extract the first {...} block
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None


def analyze_resume_with_ai(resume_text: str):
    if not ollama_available() or not resume_text:
        return None

    prompt = f"""You are an expert HR consultant and resume analyst.
Analyze this resume and respond ONLY with valid JSON.

Resume:
{resume_text[:4000]}

Required JSON format:
{{
  "overall_score": 1-100,
  "strengths": ["3-5 key strengths"],
  "improvements": ["3-5 areas to improve"],
  "skills_detected": ["skills"],
  "experience_level": "entry" | "mid" | "senior" | "executive",
  "suggested_job_titles": ["3-5 job titles"],
  "industry_fit": ["2-3 industries"],
  "keywords_missing": ["missing keywords"],
  "summary": "2-3 sentence summary"
}}
"""

    try:
        content = ollama_chat(
            [
                {
                    'role': 'system',
                    'content': 'Return strict JSON only. Do not include markdown fences or commentary.',
                },
                {'role': 'user', 'content': prompt},
            ],
            temperature=0.2,
        )
        if not content:
            if not LAST_OLLAMA_ERROR:
                _set_ollama_error('Ollama returned an empty response.')
            return None
        result = _extract_json_from_text(content)
        if not result:
            _set_ollama_error('Ollama returned a response, but it was not valid JSON.')
            return None
        return result
    except Exception as error:
        print(f'AI analysis error: {error}')
        return None


def get_job_recommendations(seeker_profile):
    if not ollama_available() or not seeker_profile:
        return []

    try:
        profile_text = f"""
Title: {seeker_profile.get('title', '')}
Skills: {seeker_profile.get('skills', '')}
Experience: {seeker_profile.get('experience', '')}
Location: {seeker_profile.get('location', '')}
"""

        db = get_db()
        jobs = db.execute(
            '''
            SELECT id, title, company, location, job_type, description, skills_required, experience_level
            FROM jobs WHERE is_active = 1 ORDER BY created_at DESC LIMIT 20
        '''
        ).fetchall()

        if not jobs:
            return []

        jobs_text = "\n".join(
            [
                f"Job {j['id']}: {j['title']} at {j['company']} - {j['location']} "
                f"- Skills: {j['skills_required']}"
                for j in jobs
            ]
        )

        prompt = f"""
You are a job matching engine.

Match this job seeker to the best jobs from the available list.
Return up to 5 recommendations and only use job IDs from the provided jobs.

Candidate Profile:
{profile_text}

Available Jobs:
{jobs_text}

Return ONLY valid JSON in this format:
{{
  "recommendations": [
    {{"job_id": number, "match_score": 1-100, "reason": "brief explanation"}}
  ]
}}
"""

        content = ollama_chat(
            [
                {
                    'role': 'system',
                    'content': 'Return strict JSON only. Do not include markdown fences or commentary.',
                },
                {'role': 'user', 'content': prompt},
            ],
            temperature=0.15,
        )
        if not content:
            return []

        result = _extract_json_from_text(content)
        if not result:
            return []

        return result.get("recommendations", [])

    except Exception as error:
        print(f'Recommendation error: {error}')
        return []

# ---------- ROUTES ----------

@app.route('/')
def index():
    db = get_db()
    featured_jobs = db.execute(
        '''
        SELECT j.*, r.company_name, r.company_logo 
        FROM jobs j 
        LEFT JOIN recruiter_profiles r ON j.recruiter_id = r.user_id
        WHERE j.is_active = 1 
        ORDER BY j.created_at DESC LIMIT 6
    '''
    ).fetchall()

    stats = {
        'jobs': db.execute('SELECT COUNT(*) FROM jobs WHERE is_active = 1').fetchone()[0],
        'companies': db.execute('SELECT COUNT(DISTINCT recruiter_id) FROM jobs').fetchone()[0],
        'seekers': db.execute("SELECT COUNT(*) FROM users WHERE user_type = 'seeker'").fetchone()[0],
    }

    return render_template('index.html', featured_jobs=featured_jobs, stats=stats)


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        user_type = request.form.get('user_type', 'seeker')

        if not email or not password:
            flash('Email and password are required.', 'error')
            return render_template('register.html')

        if password != confirm_password:
            flash('Passwords do not match.', 'error')
            return render_template('register.html')

        if len(password) < 6:
            flash('Password must be at least 6 characters.', 'error')
            return render_template('register.html')

        db = get_db()
        existing = db.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
        if existing:
            flash('Email already registered.', 'error')
            return render_template('register.html')

        hashed_password = generate_password_hash(password)
        user_id = db.execute(
            'INSERT INTO users (email, password, user_type) VALUES (?, ?, ?) RETURNING id',
            (email, hashed_password, user_type),
        ).fetchone()['id']

        if user_type == 'seeker':
            db.execute('INSERT INTO job_seeker_profiles (user_id) VALUES (?)', (user_id,))
        else:
            db.execute('INSERT INTO recruiter_profiles (user_id) VALUES (?)', (user_id,))

        db.commit()

        session['user_id'] = user_id
        session['user_type'] = user_type
        session['email'] = email

        flash('Registration successful! Please complete your profile.', 'success')
        return redirect(url_for('profile'))

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()

        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['user_type'] = user['user_type']
            session['email'] = user['email']
            flash('Welcome back!', 'success')
            return redirect(url_for('dashboard'))

        flash('Invalid email or password.', 'error')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))


@app.route('/dashboard')
@login_required
def dashboard():
    db = get_db()
    user_type = session.get('user_type')
    user_id = session.get('user_id')

    if user_type == 'seeker':
        profile = db.execute('SELECT * FROM job_seeker_profiles WHERE user_id = ?', (user_id,)).fetchone()

        applications = db.execute(
            '''
            SELECT a.*, j.title, j.company, j.location 
            FROM applications a 
            JOIN jobs j ON a.job_id = j.id 
            WHERE a.seeker_id = ? 
            ORDER BY a.applied_at DESC LIMIT 10
        ''',
            (user_id,),
        ).fetchall()

        saved_jobs = db.execute(
            '''
            SELECT j.*, s.saved_at 
            FROM saved_jobs s 
            JOIN jobs j ON s.job_id = j.id 
            WHERE s.seeker_id = ? 
            ORDER BY s.saved_at DESC LIMIT 5
        ''',
            (user_id,),
        ).fetchall()

        ai_insights = None
        if profile and profile['ai_insights']:
            try:
                ai_insights = json.loads(profile['ai_insights'])
            except Exception:
                ai_insights = None

        return render_template(
            'dashboard_seeker.html',
            profile=profile,
            applications=applications,
            saved_jobs=saved_jobs,
            ai_insights=ai_insights,
        )

    else:
        profile = db.execute('SELECT * FROM recruiter_profiles WHERE user_id = ?', (user_id,)).fetchone()

        jobs = db.execute(
            '''
            SELECT j.*, 
                   (SELECT COUNT(*) FROM applications WHERE job_id = j.id) as application_count
            FROM jobs j 
            WHERE j.recruiter_id = ? 
            ORDER BY j.created_at DESC
        ''',
            (user_id,),
        ).fetchall()

        recent_applications = db.execute(
            '''
            SELECT a.*, j.title as job_title, p.full_name as applicant_name, p.title as applicant_title
            FROM applications a 
            JOIN jobs j ON a.job_id = j.id 
            LEFT JOIN job_seeker_profiles p ON a.seeker_id = p.user_id
            WHERE j.recruiter_id = ? 
            ORDER BY a.applied_at DESC LIMIT 10
        ''',
            (user_id,),
        ).fetchall()

        stats = {
            'total_jobs': len(jobs),
            'active_jobs': sum(1 for j in jobs if j['is_active']),
            'total_applications': sum(j['application_count'] for j in jobs),
            'pending_applications': db.execute(
                '''
                SELECT COUNT(*) FROM applications a 
                JOIN jobs j ON a.job_id = j.id 
                WHERE j.recruiter_id = ? AND a.status = 'pending'
            ''',
                (user_id,),
            ).fetchone()[0],
        }

        return render_template(
            'dashboard_recruiter.html',
            profile=profile,
            jobs=jobs,
            recent_applications=recent_applications,
            stats=stats,
        )


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    db = get_db()
    user_type = session.get('user_type')
    user_id = session.get('user_id')

    if user_type == 'seeker':
        if request.method == 'POST':
            full_name = request.form.get('full_name', '')
            phone = request.form.get('phone', '')
            location = request.form.get('location', '')
            title = request.form.get('title', '')
            bio = request.form.get('bio', '')
            skills = request.form.get('skills', '')
            experience = request.form.get('experience', '')
            education = request.form.get('education', '')
            linkedin_url = request.form.get('linkedin_url', '')
            portfolio_url = request.form.get('portfolio_url', '')

            db.execute(
                '''
                UPDATE job_seeker_profiles 
                SET full_name=?, phone=?, location=?, title=?, bio=?, skills=?, 
                    experience=?, education=?, linkedin_url=?, portfolio_url=?, updated_at=?
                WHERE user_id=?
            ''',
                (
                    full_name,
                    phone,
                    location,
                    title,
                    bio,
                    skills,
                    experience,
                    education,
                    linkedin_url,
                    portfolio_url,
                    current_timestamp(),
                    user_id,
                ),
            )
            db.commit()

            flash('Profile updated successfully!', 'success')
            return redirect(url_for('profile'))

        profile = db.execute('SELECT * FROM job_seeker_profiles WHERE user_id = ?', (user_id,)).fetchone()
        return render_template('profile_seeker.html', profile=profile)

    else:
        if request.method == 'POST':
            full_name = request.form.get('full_name', '')
            phone = request.form.get('phone', '')
            company_name = request.form.get('company_name', '')
            company_description = request.form.get('company_description', '')
            company_website = request.form.get('company_website', '')
            industry = request.form.get('industry', '')
            company_size = request.form.get('company_size', '')
            location = request.form.get('location', '')

            db.execute(
                '''
                UPDATE recruiter_profiles 
                SET full_name=?, phone=?, company_name=?, company_description=?, 
                    company_website=?, industry=?, company_size=?, location=?, updated_at=?
                WHERE user_id=?
            ''',
                (
                    full_name,
                    phone,
                    company_name,
                    company_description,
                    company_website,
                    industry,
                    company_size,
                    location,
                    current_timestamp(),
                    user_id,
                ),
            )
            db.commit()

            flash('Profile updated successfully!', 'success')
            return redirect(url_for('profile'))

        profile = db.execute('SELECT * FROM recruiter_profiles WHERE user_id = ?', (user_id,)).fetchone()
        return render_template('profile_recruiter.html', profile=profile)


@app.route('/upload-resume', methods=['POST'])
@seeker_required
def upload_resume():
    if 'resume' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['resume']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    if file and allowed_file(file.filename):
        filename = secure_filename(
            f"{session['user_id']}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{file.filename}"
        )
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        resume_text = ""
        if filename.endswith('.txt'):
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                resume_text = f.read()
        else:
            # TODO: later: extract text from PDF/DOC
            resume_text = f"[Resume file: {file.filename}]"

        db = get_db()
        db.execute(
            '''
            UPDATE job_seeker_profiles 
            SET resume_path=?, resume_text=?, updated_at=?
            WHERE user_id=?
        ''',
            (filepath, resume_text, current_timestamp(), session['user_id']),
        )
        db.commit()

        return jsonify({'success': True, 'filename': filename})

    return jsonify({'error': 'Invalid file type'}), 400


@app.route('/analyze-resume', methods=['POST'])
@seeker_required
def analyze_resume():
    db = get_db()
    profile = db.execute(
        'SELECT resume_text FROM job_seeker_profiles WHERE user_id = ?',
        (session['user_id'],),
    ).fetchone()

    if not profile or not profile['resume_text']:
        return jsonify({'success': False, 'error': 'Please upload a resume first'})

    if not ollama_available():
        if _using_ollama_cloud():
            return jsonify(
                {
                    'success': False,
                    'error': f'AI analysis is not available. {_ollama_key_error_message()}',
                }
            )
        return jsonify(
            {
                'success': False,
                'error': 'AI analysis is not available. Start Ollama locally or configure OLLAMA_HOST.',
            }
        )

    insights = analyze_resume_with_ai(profile['resume_text'])

    if insights:
        db.execute(
            '''
            UPDATE job_seeker_profiles 
            SET ai_insights=?, updated_at=?
            WHERE user_id=?
        ''',
            (json.dumps(insights), current_timestamp(), session['user_id']),
        )
        db.commit()

        return jsonify({'success': True, 'insights': insights})

    return jsonify(
        {
            'success': False,
            'error': LAST_OLLAMA_ERROR or 'Resume analysis did not return valid JSON. Please try again.',
        }
    ), 500


@app.route('/jobs')
def jobs_list():
    db = get_db()

    search = request.args.get('search', '')
    location = request.args.get('location', '')
    job_type = request.args.get('job_type', '')
    experience = request.args.get('experience', '')

    query = '''
        SELECT j.*, r.company_name, r.company_logo 
        FROM jobs j 
        LEFT JOIN recruiter_profiles r ON j.recruiter_id = r.user_id
        WHERE j.is_active = 1
    '''
    params = []

    if search:
        query += ' AND (j.title LIKE ? OR j.company LIKE ? OR j.description LIKE ?)'
        search_param = f'%{search}%'
        params.extend([search_param, search_param, search_param])

    if location:
        query += ' AND j.location LIKE ?'
        params.append(f'%{location}%')

    if job_type:
        query += ' AND j.job_type = ?'
        params.append(job_type)

    if experience:
        query += ' AND j.experience_level = ?'
        params.append(experience)

    query += ' ORDER BY j.created_at DESC'

    jobs = db.execute(query, params).fetchall()

    return render_template(
        'jobs.html',
        jobs=jobs,
        search=search,
        location=location,
        job_type=job_type,
        experience=experience,
    )


@app.route('/job/<int:job_id>')
def job_detail(job_id):
    db = get_db()

    job = db.execute(
        '''
        SELECT j.*, r.company_name, r.company_description, r.company_website, 
               r.company_logo, r.location as company_location
        FROM jobs j 
        LEFT JOIN recruiter_profiles r ON j.recruiter_id = r.user_id
        WHERE j.id = ?
    ''',
        (job_id,),
    ).fetchone()

    if not job:
        flash('Job not found.', 'error')
        return redirect(url_for('jobs_list'))

    has_applied = False
    is_saved = False
    if session.get('user_id') and session.get('user_type') == 'seeker':
        application = db.execute(
            'SELECT id FROM applications WHERE job_id = ? AND seeker_id = ?',
            (job_id, session['user_id']),
        ).fetchone()
        has_applied = application is not None

        saved = db.execute(
            'SELECT id FROM saved_jobs WHERE job_id = ? AND seeker_id = ?',
            (job_id, session['user_id']),
        ).fetchone()
        is_saved = saved is not None

    similar_jobs = db.execute(
        '''
        SELECT j.*, r.company_name 
        FROM jobs j 
        LEFT JOIN recruiter_profiles r ON j.recruiter_id = r.user_id
        WHERE j.is_active = 1 AND j.id != ? 
        ORDER BY RANDOM() LIMIT 3
    ''',
        (job_id,),
    ).fetchall()

    return render_template(
        'job_detail.html',
        job=job,
        has_applied=has_applied,
        is_saved=is_saved,
        similar_jobs=similar_jobs,
    )


@app.route('/apply/<int:job_id>', methods=['POST'])
@seeker_required
def apply_job(job_id):
    db = get_db()

    existing = db.execute(
        'SELECT id FROM applications WHERE job_id = ? AND seeker_id = ?',
        (job_id, session['user_id']),
    ).fetchone()

    if existing:
        return jsonify({'error': 'You have already applied to this job'}), 400

    cover_letter = request.form.get('cover_letter', '')

    db.execute(
        'INSERT INTO applications (job_id, seeker_id, cover_letter) VALUES (?, ?, ?)',
        (job_id, session['user_id'], cover_letter),
    )
    db.commit()

    return jsonify({'success': True, 'message': 'Application submitted successfully!'})


@app.route('/save-job/<int:job_id>', methods=['POST'])
@seeker_required
def save_job(job_id):
    db = get_db()

    existing = db.execute(
        'SELECT id FROM saved_jobs WHERE job_id = ? AND seeker_id = ?',
        (job_id, session['user_id']),
    ).fetchone()

    if existing:
        db.execute('DELETE FROM saved_jobs WHERE id = ?', (existing['id'],))
        db.commit()
        return jsonify({'success': True, 'saved': False, 'message': 'Job removed from saved'})

    db.execute(
        'INSERT INTO saved_jobs (job_id, seeker_id) VALUES (?, ?)',
        (job_id, session['user_id']),
    )
    db.commit()

    return jsonify({'success': True, 'saved': True, 'message': 'Job saved successfully!'})


@app.route('/post-job', methods=['GET', 'POST'])
@recruiter_required
def post_job():
    if request.method == 'POST':
        db = get_db()

        recruiter = db.execute(
            'SELECT company_name FROM recruiter_profiles WHERE user_id = ?',
            (session['user_id'],),
        ).fetchone()
        company = recruiter['company_name'] if recruiter else ''

        title = request.form.get('title', '')
        location = request.form.get('location', '')
        job_type = request.form.get('job_type', '')
        salary_min = request.form.get('salary_min', type=int)
        salary_max = request.form.get('salary_max', type=int)
        description = request.form.get('description', '')
        requirements = request.form.get('requirements', '')
        benefits = request.form.get('benefits', '')
        skills_required = request.form.get('skills_required', '')
        experience_level = request.form.get('experience_level', '')

        db.execute(
            '''
            INSERT INTO jobs (recruiter_id, title, company, location, job_type, 
                              salary_min, salary_max, description, requirements, 
                              benefits, skills_required, experience_level)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
            (
                session['user_id'],
                title,
                company,
                location,
                job_type,
                salary_min,
                salary_max,
                description,
                requirements,
                benefits,
                skills_required,
                experience_level,
            ),
        )
        db.commit()

        flash('Job posted successfully!', 'success')
        return redirect(url_for('dashboard'))

    return render_template('post_job.html')


@app.route('/edit-job/<int:job_id>', methods=['GET', 'POST'])
@recruiter_required
def edit_job(job_id):
    db = get_db()

    job = db.execute(
        'SELECT * FROM jobs WHERE id = ? AND recruiter_id = ?',
        (job_id, session['user_id']),
    ).fetchone()

    if not job:
        flash('Job not found.', 'error')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        title = request.form.get('title', '')
        location = request.form.get('location', '')
        job_type = request.form.get('job_type', '')
        salary_min = request.form.get('salary_min', type=int)
        salary_max = request.form.get('salary_max', type=int)
        description = request.form.get('description', '')
        requirements = request.form.get('requirements', '')
        benefits = request.form.get('benefits', '')
        skills_required = request.form.get('skills_required', '')
        experience_level = request.form.get('experience_level', '')
        is_active = 1 if request.form.get('is_active') else 0

        db.execute(
            '''
            UPDATE jobs 
            SET title=?, location=?, job_type=?, salary_min=?, salary_max=?,
                description=?, requirements=?, benefits=?, skills_required=?,
                experience_level=?, is_active=?, updated_at=?
            WHERE id=? AND recruiter_id=?
        ''',
            (
                title,
                location,
                job_type,
                salary_min,
                salary_max,
                description,
                requirements,
                benefits,
                skills_required,
                experience_level,
                is_active,
                current_timestamp(),
                job_id,
                session['user_id'],
            ),
        )
        db.commit()

        flash('Job updated successfully!', 'success')
        return redirect(url_for('dashboard'))

    return render_template('edit_job.html', job=job)


@app.route('/job/<int:job_id>/applications')
@recruiter_required
def job_applications(job_id):
    db = get_db()

    job = db.execute(
        'SELECT * FROM jobs WHERE id = ? AND recruiter_id = ?',
        (job_id, session['user_id']),
    ).fetchone()

    if not job:
        flash('Job not found.', 'error')
        return redirect(url_for('dashboard'))

    applications = db.execute(
        '''
        SELECT a.*, p.full_name, p.title, p.location, p.skills, p.resume_path, p.ai_insights
        FROM applications a 
        JOIN job_seeker_profiles p ON a.seeker_id = p.user_id
        WHERE a.job_id = ?
        ORDER BY a.applied_at DESC
    ''',
        (job_id,),
    ).fetchall()

    return render_template('job_applications.html', job=job, applications=applications)


@app.route('/update-application-status/<int:app_id>', methods=['POST'])
@recruiter_required
def update_application_status(app_id):
    db = get_db()

    application = db.execute(
        '''
        SELECT a.* FROM applications a 
        JOIN jobs j ON a.job_id = j.id 
        WHERE a.id = ? AND j.recruiter_id = ?
    ''',
        (app_id, session['user_id']),
    ).fetchone()

    if not application:
        return jsonify({'error': 'Application not found'}), 404

    status = request.form.get('status', 'pending')
    notes = request.form.get('notes', '')

    db.execute(
        '''
        UPDATE applications SET status=?, recruiter_notes=?, updated_at=?
        WHERE id=?
    ''',
        (status, notes, current_timestamp(), app_id),
    )
    db.commit()

    return jsonify({'success': True, 'message': 'Status updated'})


@app.route('/candidates')
@recruiter_required
def candidates():
    db = get_db()

    search = request.args.get('search', '')
    location = request.args.get('location', '')
    skills = request.args.get('skills', '')

    query = '''
        SELECT p.*, u.email 
        FROM job_seeker_profiles p 
        JOIN users u ON p.user_id = u.id
        WHERE p.full_name IS NOT NULL
    '''
    params = []

    if search:
        query += ' AND (p.full_name LIKE ? OR p.title LIKE ? OR p.bio LIKE ?)'
        search_param = f'%{search}%'
        params.extend([search_param, search_param, search_param])

    if location:
        query += ' AND p.location LIKE ?'
        params.append(f'%{location}%')

    if skills:
        query += ' AND p.skills LIKE ?'
        params.append(f'%{skills}%')

    query += ' ORDER BY p.updated_at DESC'

    candidates = db.execute(query, params).fetchall()

    return render_template(
        'candidates.html',
        candidates=candidates,
        search=search,
        location=location,
        skills=skills,
    )


@app.route('/candidate/<int:user_id>')
@recruiter_required
def candidate_profile(user_id):
    db = get_db()

    profile = db.execute(
        '''
        SELECT p.*, u.email 
        FROM job_seeker_profiles p 
        JOIN users u ON p.user_id = u.id
        WHERE p.user_id = ?
    ''',
        (user_id,),
    ).fetchone()

    if not profile:
        flash('Candidate not found.', 'error')
        return redirect(url_for('candidates'))

    ai_insights = None
    if profile['ai_insights']:
        try:
            ai_insights = json.loads(profile['ai_insights'])
        except Exception:
            ai_insights = None

    return render_template('candidate_profile.html', profile=profile, ai_insights=ai_insights)


@app.route('/api/job-suggestions', methods=['POST'])
@seeker_required
def api_job_suggestions():
    db = get_db()
    profile = db.execute(
        'SELECT * FROM job_seeker_profiles WHERE user_id = ?',
        (session['user_id'],),
    ).fetchone()

    if not profile:
        return jsonify({'success': False, 'error': 'Profile not found'})

    if not ollama_available():
        if _using_ollama_cloud():
            return jsonify({'success': False, 'error': _ollama_key_error_message()})
        return jsonify({'success': False, 'error': 'Start Ollama locally or configure OLLAMA_HOST first.'})

    recommendations = get_job_recommendations(dict(profile))

    if recommendations:
        job_ids = [r['job_id'] for r in recommendations]
        placeholders = ','.join('?' * len(job_ids))
        jobs = db.execute(
            f'''
            SELECT j.*, r.company_name 
            FROM jobs j 
            LEFT JOIN recruiter_profiles r ON j.recruiter_id = r.user_id
            WHERE j.id IN ({placeholders}) AND j.is_active = 1
        ''',
            job_ids,
        ).fetchall()

        jobs_dict = {j['id']: dict(j) for j in jobs}
        enriched_recommendations = []
        for rec in recommendations:
            if rec['job_id'] in jobs_dict:
                rec['job'] = jobs_dict[rec['job_id']]
                enriched_recommendations.append(rec)

        return jsonify({'success': True, 'recommendations': enriched_recommendations})

    return jsonify({'success': True, 'recommendations': []})


if __name__ == '__main__':
    initialize_application()
    port = int(os.environ.get('PORT', '5000'))
    debug = os.environ.get('FLASK_DEBUG', 'true').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)
