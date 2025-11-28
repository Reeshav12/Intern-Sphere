# JobConnect - Job Portal with AI Resume Analysis

## Overview
JobConnect is a LinkedIn-like job portal platform where job seekers can apply for jobs and recruiters can post positions and find candidates. The platform features AI-powered resume analysis using OpenAI to provide insights and job recommendations.

## Project Architecture

### Technology Stack
- **Backend**: Python Flask
- **Database**: SQLite
- **Frontend**: HTML, CSS, JavaScript (Vanilla)
- **AI**: OpenAI GPT-5 for resume analysis and job recommendations

### Directory Structure
```
├── app.py                 # Main Flask application
├── templates/             # Jinja2 HTML templates
│   ├── base.html          # Base template with navigation
│   ├── index.html         # Homepage
│   ├── login.html         # Login page
│   ├── register.html      # Registration page
│   ├── dashboard_seeker.html    # Job seeker dashboard
│   ├── dashboard_recruiter.html # Recruiter dashboard
│   ├── profile_seeker.html      # Seeker profile edit
│   ├── profile_recruiter.html   # Recruiter profile edit
│   ├── jobs.html          # Job listings page
│   ├── job_detail.html    # Single job details
│   ├── post_job.html      # Post new job form
│   ├── edit_job.html      # Edit job form
│   ├── job_applications.html    # View applicants for a job
│   ├── candidates.html    # Search candidates
│   └── candidate_profile.html   # View candidate profile
├── static/
│   ├── css/style.css      # All CSS styles
│   └── js/main.js         # JavaScript utilities
├── uploads/               # Uploaded resumes storage
└── jobportal.db           # SQLite database
```

### Database Schema
- **users**: Stores authentication data (email, password, user_type)
- **job_seeker_profiles**: Extended profile for job seekers
- **recruiter_profiles**: Company and recruiter information
- **jobs**: Job postings
- **applications**: Job applications
- **saved_jobs**: Bookmarked jobs by seekers

### Key Features
1. **User Authentication**: Registration and login for job seekers and recruiters
2. **Job Seeker Features**:
   - Profile management with resume upload
   - AI resume analysis with scores and suggestions
   - Job search with filters
   - Job applications with cover letters
   - Save/bookmark jobs
   - AI-powered job recommendations

3. **Recruiter Features**:
   - Company profile setup
   - Post and manage job listings
   - View and filter applicants
   - Search candidate database
   - Update application status

4. **AI Features** (requires OPENAI_API_KEY):
   - Resume analysis with overall score
   - Strengths and improvement areas
   - Skill detection
   - Experience level assessment
   - Job title suggestions
   - Job matching recommendations

### Environment Variables
- `SESSION_SECRET`: Flask session secret key
- `OPENAI_API_KEY`: OpenAI API key for AI features

### Running the Application
The application runs on port 5000 with the Flask development server.

## Recent Changes
- Initial project setup (November 2025)
- Created complete job portal with all core features
- Implemented AI resume analysis using OpenAI GPT-5
- Added responsive UI with modern CSS styling

## User Preferences
- Simple, clean UI preferred
- Using SQLite for simplicity
- Python Flask as the backend framework
