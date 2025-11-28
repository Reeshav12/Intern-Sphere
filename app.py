import os
import json
import sqlite3
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, g
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from openai import OpenAI

app = Flask(__name__)
app.secret_key = os.environ.get('SESSION_SECRET', 'dev-secret-key-change-in-production')

app.config['SESSION_COOKIE_SAMESITE'] = 'None'
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True

UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'pdf', 'doc', 'docx', 'txt'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

DATABASE = 'jobportal.db'

# the newest OpenAI model is "gpt-5" which was released August 7, 2025.
# do not change this unless explicitly requested by the user
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
openai_client = None
if OPENAI_API_KEY:
    openai_client = OpenAI(api_key=OPENAI_API_KEY)

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                user_type TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
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
            );
            
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
            );
            
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
            );
            
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
            );
            
            CREATE TABLE IF NOT EXISTS saved_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                seeker_id INTEGER NOT NULL,
                saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (job_id) REFERENCES jobs(id),
                FOREIGN KEY (seeker_id) REFERENCES users(id),
                UNIQUE(job_id, seeker_id)
            );
        ''')
        db.commit()

def allowed_file(filename):
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

def analyze_resume_with_ai(resume_text):
    if not openai_client or not resume_text:
        return None
    
    try:
        prompt = f"""Analyze this resume and provide detailed insights in JSON format:

Resume:
{resume_text[:4000]}

Provide a JSON response with:
{{
    "overall_score": (number 1-100),
    "strengths": ["list of 3-5 key strengths"],
    "improvements": ["list of 3-5 areas to improve"],
    "skills_detected": ["list of technical and soft skills found"],
    "experience_level": "entry/mid/senior/executive",
    "suggested_job_titles": ["3-5 job titles this person would be good for"],
    "industry_fit": ["2-3 industries this person fits well"],
    "keywords_missing": ["important keywords that could be added"],
    "summary": "2-3 sentence professional summary of the candidate"
}}"""

        response = openai_client.chat.completions.create(
            model="gpt-5",
            messages=[
                {"role": "system", "content": "You are an expert HR consultant and resume analyst. Provide constructive, actionable feedback."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=2048
        )
        
        content = response.choices[0].message.content
        return json.loads(content) if content else None
    except Exception as e:
        print(f"AI Analysis error: {e}")
        return None

def get_job_recommendations(seeker_profile):
    if not openai_client or not seeker_profile:
        return []
    
    try:
        profile_text = f"""
        Title: {seeker_profile.get('title', '')}
        Skills: {seeker_profile.get('skills', '')}
        Experience: {seeker_profile.get('experience', '')}
        Location: {seeker_profile.get('location', '')}
        """
        
        db = get_db()
        jobs = db.execute('''
            SELECT id, title, company, location, job_type, description, skills_required, experience_level
            FROM jobs WHERE is_active = 1 ORDER BY created_at DESC LIMIT 20
        ''').fetchall()
        
        if not jobs:
            return []
        
        jobs_text = "\n".join([f"Job {j['id']}: {j['title']} at {j['company']} - {j['location']} - Skills: {j['skills_required']}" for j in jobs])
        
        prompt = f"""Based on this job seeker's profile, rank the best matching jobs and explain why:

Candidate Profile:
{profile_text}

Available Jobs:
{jobs_text}

Return JSON with top 5 matches:
{{
    "recommendations": [
        {{"job_id": number, "match_score": 1-100, "reason": "brief explanation"}}
    ]
}}"""

        response = openai_client.chat.completions.create(
            model="gpt-5",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_completion_tokens=1024
        )
        
        content = response.choices[0].message.content
        if not content:
            return []
        result = json.loads(content)
        return result.get('recommendations', [])
    except Exception as e:
        print(f"Recommendation error: {e}")
        return []

@app.route('/')
def index():
    db = get_db()
    featured_jobs = db.execute('''
        SELECT j.*, r.company_name, r.company_logo 
        FROM jobs j 
        LEFT JOIN recruiter_profiles r ON j.recruiter_id = r.user_id
        WHERE j.is_active = 1 
        ORDER BY j.created_at DESC LIMIT 6
    ''').fetchall()
    
    stats = {
        'jobs': db.execute('SELECT COUNT(*) FROM jobs WHERE is_active = 1').fetchone()[0],
        'companies': db.execute('SELECT COUNT(DISTINCT recruiter_id) FROM jobs').fetchone()[0],
        'seekers': db.execute("SELECT COUNT(*) FROM users WHERE user_type = 'seeker'").fetchone()[0]
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
        cursor = db.execute(
            'INSERT INTO users (email, password, user_type) VALUES (?, ?, ?)',
            (email, hashed_password, user_type)
        )
        user_id = cursor.lastrowid
        
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
        
        applications = db.execute('''
            SELECT a.*, j.title, j.company, j.location 
            FROM applications a 
            JOIN jobs j ON a.job_id = j.id 
            WHERE a.seeker_id = ? 
            ORDER BY a.applied_at DESC LIMIT 10
        ''', (user_id,)).fetchall()
        
        saved_jobs = db.execute('''
            SELECT j.*, s.saved_at 
            FROM saved_jobs s 
            JOIN jobs j ON s.job_id = j.id 
            WHERE s.seeker_id = ? 
            ORDER BY s.saved_at DESC LIMIT 5
        ''', (user_id,)).fetchall()
        
        ai_insights = None
        if profile and profile['ai_insights']:
            try:
                ai_insights = json.loads(profile['ai_insights'])
            except:
                pass
        
        recommendations = []
        if profile:
            profile_dict = dict(profile)
            recommendations = get_job_recommendations(profile_dict)
            
            if recommendations:
                job_ids = [r['job_id'] for r in recommendations]
                placeholders = ','.join('?' * len(job_ids))
                recommended_jobs = db.execute(f'''
                    SELECT * FROM jobs WHERE id IN ({placeholders}) AND is_active = 1
                ''', job_ids).fetchall()
                
                jobs_dict = {j['id']: dict(j) for j in recommended_jobs}
                for rec in recommendations:
                    if rec['job_id'] in jobs_dict:
                        rec['job'] = jobs_dict[rec['job_id']]
        
        return render_template('dashboard_seeker.html', 
                             profile=profile, 
                             applications=applications,
                             saved_jobs=saved_jobs,
                             ai_insights=ai_insights,
                             recommendations=recommendations)
    else:
        profile = db.execute('SELECT * FROM recruiter_profiles WHERE user_id = ?', (user_id,)).fetchone()
        
        jobs = db.execute('''
            SELECT j.*, 
                   (SELECT COUNT(*) FROM applications WHERE job_id = j.id) as application_count
            FROM jobs j 
            WHERE j.recruiter_id = ? 
            ORDER BY j.created_at DESC
        ''', (user_id,)).fetchall()
        
        recent_applications = db.execute('''
            SELECT a.*, j.title as job_title, p.full_name as applicant_name, p.title as applicant_title
            FROM applications a 
            JOIN jobs j ON a.job_id = j.id 
            LEFT JOIN job_seeker_profiles p ON a.seeker_id = p.user_id
            WHERE j.recruiter_id = ? 
            ORDER BY a.applied_at DESC LIMIT 10
        ''', (user_id,)).fetchall()
        
        stats = {
            'total_jobs': len(jobs),
            'active_jobs': sum(1 for j in jobs if j['is_active']),
            'total_applications': sum(j['application_count'] for j in jobs),
            'pending_applications': db.execute('''
                SELECT COUNT(*) FROM applications a 
                JOIN jobs j ON a.job_id = j.id 
                WHERE j.recruiter_id = ? AND a.status = 'pending'
            ''', (user_id,)).fetchone()[0]
        }
        
        return render_template('dashboard_recruiter.html',
                             profile=profile,
                             jobs=jobs,
                             recent_applications=recent_applications,
                             stats=stats)

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
            
            db.execute('''
                UPDATE job_seeker_profiles 
                SET full_name=?, phone=?, location=?, title=?, bio=?, skills=?, 
                    experience=?, education=?, linkedin_url=?, portfolio_url=?, updated_at=?
                WHERE user_id=?
            ''', (full_name, phone, location, title, bio, skills, experience, education,
                  linkedin_url, portfolio_url, datetime.now(), user_id))
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
            
            db.execute('''
                UPDATE recruiter_profiles 
                SET full_name=?, phone=?, company_name=?, company_description=?, 
                    company_website=?, industry=?, company_size=?, location=?, updated_at=?
                WHERE user_id=?
            ''', (full_name, phone, company_name, company_description, company_website,
                  industry, company_size, location, datetime.now(), user_id))
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
        filename = secure_filename(f"{session['user_id']}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{file.filename}")
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        resume_text = ""
        if filename.endswith('.txt'):
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                resume_text = f.read()
        else:
            resume_text = f"[Resume file: {file.filename}]"
        
        db = get_db()
        db.execute('''
            UPDATE job_seeker_profiles 
            SET resume_path=?, resume_text=?, updated_at=?
            WHERE user_id=?
        ''', (filepath, resume_text, datetime.now(), session['user_id']))
        db.commit()
        
        return jsonify({'success': True, 'filename': filename})
    
    return jsonify({'error': 'Invalid file type'}), 400

@app.route('/analyze-resume', methods=['POST'])
@seeker_required
def analyze_resume():
    db = get_db()
    profile = db.execute('SELECT resume_text FROM job_seeker_profiles WHERE user_id = ?', 
                        (session['user_id'],)).fetchone()
    
    if not profile or not profile['resume_text']:
        return jsonify({'error': 'Please upload a resume first'}), 400
    
    if not openai_client:
        return jsonify({'error': 'AI analysis is not available. Please configure OpenAI API key.'}), 400
    
    insights = analyze_resume_with_ai(profile['resume_text'])
    
    if insights:
        db.execute('''
            UPDATE job_seeker_profiles 
            SET ai_insights=?, updated_at=?
            WHERE user_id=?
        ''', (json.dumps(insights), datetime.now(), session['user_id']))
        db.commit()
        
        return jsonify({'success': True, 'insights': insights})
    
    return jsonify({'error': 'Failed to analyze resume'}), 500

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
    
    return render_template('jobs.html', jobs=jobs, 
                         search=search, location=location, 
                         job_type=job_type, experience=experience)

@app.route('/job/<int:job_id>')
def job_detail(job_id):
    db = get_db()
    
    job = db.execute('''
        SELECT j.*, r.company_name, r.company_description, r.company_website, r.company_logo, r.location as company_location
        FROM jobs j 
        LEFT JOIN recruiter_profiles r ON j.recruiter_id = r.user_id
        WHERE j.id = ?
    ''', (job_id,)).fetchone()
    
    if not job:
        flash('Job not found.', 'error')
        return redirect(url_for('jobs_list'))
    
    has_applied = False
    is_saved = False
    if session.get('user_id') and session.get('user_type') == 'seeker':
        application = db.execute(
            'SELECT id FROM applications WHERE job_id = ? AND seeker_id = ?',
            (job_id, session['user_id'])
        ).fetchone()
        has_applied = application is not None
        
        saved = db.execute(
            'SELECT id FROM saved_jobs WHERE job_id = ? AND seeker_id = ?',
            (job_id, session['user_id'])
        ).fetchone()
        is_saved = saved is not None
    
    similar_jobs = db.execute('''
        SELECT j.*, r.company_name 
        FROM jobs j 
        LEFT JOIN recruiter_profiles r ON j.recruiter_id = r.user_id
        WHERE j.is_active = 1 AND j.id != ? 
        ORDER BY RANDOM() LIMIT 3
    ''', (job_id,)).fetchall()
    
    return render_template('job_detail.html', job=job, has_applied=has_applied, 
                         is_saved=is_saved, similar_jobs=similar_jobs)

@app.route('/apply/<int:job_id>', methods=['POST'])
@seeker_required
def apply_job(job_id):
    db = get_db()
    
    existing = db.execute(
        'SELECT id FROM applications WHERE job_id = ? AND seeker_id = ?',
        (job_id, session['user_id'])
    ).fetchone()
    
    if existing:
        return jsonify({'error': 'You have already applied to this job'}), 400
    
    cover_letter = request.form.get('cover_letter', '')
    
    db.execute(
        'INSERT INTO applications (job_id, seeker_id, cover_letter) VALUES (?, ?, ?)',
        (job_id, session['user_id'], cover_letter)
    )
    db.commit()
    
    return jsonify({'success': True, 'message': 'Application submitted successfully!'})

@app.route('/save-job/<int:job_id>', methods=['POST'])
@seeker_required
def save_job(job_id):
    db = get_db()
    
    existing = db.execute(
        'SELECT id FROM saved_jobs WHERE job_id = ? AND seeker_id = ?',
        (job_id, session['user_id'])
    ).fetchone()
    
    if existing:
        db.execute('DELETE FROM saved_jobs WHERE id = ?', (existing['id'],))
        db.commit()
        return jsonify({'success': True, 'saved': False, 'message': 'Job removed from saved'})
    
    db.execute(
        'INSERT INTO saved_jobs (job_id, seeker_id) VALUES (?, ?)',
        (job_id, session['user_id'])
    )
    db.commit()
    
    return jsonify({'success': True, 'saved': True, 'message': 'Job saved successfully!'})

@app.route('/post-job', methods=['GET', 'POST'])
@recruiter_required
def post_job():
    if request.method == 'POST':
        db = get_db()
        
        recruiter = db.execute('SELECT company_name FROM recruiter_profiles WHERE user_id = ?',
                              (session['user_id'],)).fetchone()
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
        
        db.execute('''
            INSERT INTO jobs (recruiter_id, title, company, location, job_type, 
                            salary_min, salary_max, description, requirements, 
                            benefits, skills_required, experience_level)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (session['user_id'], title, company, location, job_type, salary_min,
              salary_max, description, requirements, benefits, skills_required, experience_level))
        db.commit()
        
        flash('Job posted successfully!', 'success')
        return redirect(url_for('dashboard'))
    
    return render_template('post_job.html')

@app.route('/edit-job/<int:job_id>', methods=['GET', 'POST'])
@recruiter_required
def edit_job(job_id):
    db = get_db()
    
    job = db.execute('SELECT * FROM jobs WHERE id = ? AND recruiter_id = ?',
                    (job_id, session['user_id'])).fetchone()
    
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
        
        db.execute('''
            UPDATE jobs SET title=?, location=?, job_type=?, salary_min=?, salary_max=?,
                          description=?, requirements=?, benefits=?, skills_required=?,
                          experience_level=?, is_active=?, updated_at=?
            WHERE id=? AND recruiter_id=?
        ''', (title, location, job_type, salary_min, salary_max, description,
              requirements, benefits, skills_required, experience_level, is_active,
              datetime.now(), job_id, session['user_id']))
        db.commit()
        
        flash('Job updated successfully!', 'success')
        return redirect(url_for('dashboard'))
    
    return render_template('edit_job.html', job=job)

@app.route('/job/<int:job_id>/applications')
@recruiter_required
def job_applications(job_id):
    db = get_db()
    
    job = db.execute('SELECT * FROM jobs WHERE id = ? AND recruiter_id = ?',
                    (job_id, session['user_id'])).fetchone()
    
    if not job:
        flash('Job not found.', 'error')
        return redirect(url_for('dashboard'))
    
    applications = db.execute('''
        SELECT a.*, p.full_name, p.title, p.location, p.skills, p.resume_path, p.ai_insights
        FROM applications a 
        JOIN job_seeker_profiles p ON a.seeker_id = p.user_id
        WHERE a.job_id = ?
        ORDER BY a.applied_at DESC
    ''', (job_id,)).fetchall()
    
    return render_template('job_applications.html', job=job, applications=applications)

@app.route('/update-application-status/<int:app_id>', methods=['POST'])
@recruiter_required
def update_application_status(app_id):
    db = get_db()
    
    application = db.execute('''
        SELECT a.* FROM applications a 
        JOIN jobs j ON a.job_id = j.id 
        WHERE a.id = ? AND j.recruiter_id = ?
    ''', (app_id, session['user_id'])).fetchone()
    
    if not application:
        return jsonify({'error': 'Application not found'}), 404
    
    status = request.form.get('status', 'pending')
    notes = request.form.get('notes', '')
    
    db.execute('''
        UPDATE applications SET status=?, recruiter_notes=?, updated_at=?
        WHERE id=?
    ''', (status, notes, datetime.now(), app_id))
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
    
    return render_template('candidates.html', candidates=candidates,
                         search=search, location=location, skills=skills)

@app.route('/candidate/<int:user_id>')
@recruiter_required
def candidate_profile(user_id):
    db = get_db()
    
    profile = db.execute('''
        SELECT p.*, u.email 
        FROM job_seeker_profiles p 
        JOIN users u ON p.user_id = u.id
        WHERE p.user_id = ?
    ''', (user_id,)).fetchone()
    
    if not profile:
        flash('Candidate not found.', 'error')
        return redirect(url_for('candidates'))
    
    ai_insights = None
    if profile['ai_insights']:
        try:
            ai_insights = json.loads(profile['ai_insights'])
        except:
            pass
    
    return render_template('candidate_profile.html', profile=profile, ai_insights=ai_insights)

@app.route('/api/job-suggestions', methods=['POST'])
@seeker_required
def get_job_suggestions():
    db = get_db()
    profile = db.execute('SELECT * FROM job_seeker_profiles WHERE user_id = ?',
                        (session['user_id'],)).fetchone()
    
    if not profile:
        return jsonify({'error': 'Profile not found'}), 404
    
    recommendations = get_job_recommendations(dict(profile))
    
    if recommendations:
        job_ids = [r['job_id'] for r in recommendations]
        placeholders = ','.join('?' * len(job_ids))
        jobs = db.execute(f'''
            SELECT j.*, r.company_name 
            FROM jobs j 
            LEFT JOIN recruiter_profiles r ON j.recruiter_id = r.user_id
            WHERE j.id IN ({placeholders}) AND j.is_active = 1
        ''', job_ids).fetchall()
        
        jobs_dict = {j['id']: dict(j) for j in jobs}
        for rec in recommendations:
            if rec['job_id'] in jobs_dict:
                rec['job'] = jobs_dict[rec['job_id']]
        
        return jsonify({'success': True, 'recommendations': recommendations})
    
    return jsonify({'success': True, 'recommendations': []})

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)
