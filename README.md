# InternSphere

InternSphere is a Flask-based job portal for job seekers and recruiters. It includes account management, profile creation, job posting, applications, saved jobs, resume upload, and AI-powered resume analysis plus job recommendations through Ollama.

## Features

- Job seeker and recruiter authentication
- Recruiter company profiles and job posting flow
- Job seeker profiles with resume upload
- Save job and apply to job flows
- Recruiter-side candidate and application review
- Ollama-powered resume analysis
- On-demand job recommendations

## Tech Stack

- Backend: Flask
- Database: SQLite
- Frontend: Jinja templates, vanilla JavaScript, custom CSS
- AI provider: Ollama Cloud by default, local Ollama supported

## Project Structure

```text
.
├── api/                # Vercel entrypoint
├── static/             # Local static assets
├── templates/          # Jinja templates
├── uploads/            # Local uploaded resumes
├── app.py              # Main Flask app
├── build.py            # Copies static assets to public/ for Vercel
├── vercel.json         # Vercel routing/runtime config
├── requirements.txt
└── README.md
```

## Local Development

### 1. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create your environment file

```bash
cp .env.example .env
```

Set at least:

```env
SESSION_SECRET=change-this-secret
OLLAMA_HOST=https://ollama.com
OLLAMA_API_KEY=your_ollama_api_key
OLLAMA_MODEL=gpt-oss:20b
OLLAMA_TIMEOUT=60
PORT=5000
FLASK_DEBUG=true
```

### 4. Run the app

```bash
python app.py
```

Open `http://127.0.0.1:5000`

If port `5000` is already busy:

```bash
PORT=5001 python app.py
```

## Ollama Configuration

### Ollama Cloud

Default setup uses Ollama Cloud:

```env
OLLAMA_HOST=https://ollama.com
OLLAMA_API_KEY=your_ollama_api_key
OLLAMA_MODEL=gpt-oss:20b
```

### Local Ollama

You can switch to a local Ollama server without changing code:

```env
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=gpt-oss:20b
```

## Deploy to Vercel

This repo now includes the files needed for a Vercel deployment:

- `vercel.json`
- `api/index.py`
- `build.py`

### Important limitation

Vercel is fine for a demo of this app, but the current SQLite database is not production-safe there.

- Vercel functions run on ephemeral storage.
- The app uses `/tmp/internsphere/jobportal.db` on Vercel so writes work during execution.
- Data is not guaranteed to persist across deployments, cold starts, or scaling events.

For production, move the database to Postgres, Neon, Supabase, PlanetScale, or another managed database.

### Deploy steps

#### Option 1: Deploy from GitHub

1. Push this repository to GitHub.
2. Go to [vercel.com](https://vercel.com/) and create a new project.
3. Import the GitHub repository.
4. In Vercel project settings, add these environment variables:

```env
SESSION_SECRET=your-production-secret
OLLAMA_HOST=https://ollama.com
OLLAMA_API_KEY=your_ollama_api_key
OLLAMA_MODEL=gpt-oss:20b
OLLAMA_TIMEOUT=60
FLASK_DEBUG=false
```

5. Deploy.

Vercel will:

- run `python build.py`
- copy `static/` into `public/static/`
- package the Flask app from `api/index.py`
- include `templates/` and the seed `jobportal.db` with the Python function bundle

The project pins Python through `pyproject.toml`, so `vercel.json` does not need a separate `runtime` override.

#### Option 2: Deploy with the Vercel CLI

```bash
npm i -g vercel
vercel
```

Then add the same environment variables in the Vercel dashboard or through the CLI.

## GitHub Setup

To publish this project on GitHub:

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<your-username>/InternSphere.git
git push -u origin main
```

If this repository already exists locally, just set the remote and push.

## Production Recommendations

If you want this app to be production-ready, the next upgrades should be:

1. Replace SQLite with Postgres.
2. Store uploaded resumes in cloud storage such as S3, Cloudinary, or Vercel Blob.
3. Add PDF/DOCX text extraction so resume analysis works beyond `.txt` uploads.
4. Move session storage and secrets management to a production-safe setup.
5. Add CSRF protection and stricter input validation.

## Current Limitations

- PDF and DOC/DOCX resumes upload successfully, but only `.txt` resumes are fully analyzable right now.
- AI analysis and recommendations require a valid Ollama API key or a reachable local Ollama server.
- Vercel deployment is best treated as a demo unless the database and file storage are moved off local disk.
