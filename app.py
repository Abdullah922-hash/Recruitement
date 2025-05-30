import os
import base64
import pickle
import datetime
import sqlite3
import streamlit as st
import pandas as pd
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import openai
from dotenv import load_dotenv
import re
from pdfminer.high_level import extract_text as extract_pdf_text
from docx import Document
import shutil
import json
from google.oauth2.credentials import Credentials
import urllib.request
from github import Github  # Requires PyGithub: pip install PyGithub

# Streamlit page config
st.set_page_config(page_title="AI Recruitment", layout="wide")

# Custom CSS (unchanged for brevity, same as original)
st.markdown("""
<style>
    /* Same CSS as original */
</style>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
""", unsafe_allow_html=True)

# Constants
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
RESUME_FOLDER = "Resumes"  # Local folder
JD_FOLDER = "JDs"  # Local folder
DATABASE = "/tmp/recruitment.db"
GITHUB_REPO = "Abdullah922-hash/Recruitement"
GITHUB_DB_PATH = "recruitment.db"
GITHUB_RESUME_PATH = "Resumes"
GITHUB_JD_PATH = "JDs"

load_dotenv()

# GitHub API setup (optional, enable if syncing with GitHub)
def github_setup():
    github_token = st.secrets.get("github", {}).get("token", None)
    if github_token:
        g = Github(github_token)
        repo = g.get_repo(GITHUB_REPO)
        return repo
    return None

def github_upload_file(repo, file_path, github_path, commit_message="Update file"):
    try:
        with open(file_path, "rb") as file:
            content = file.read()
        repo.create_file(github_path, commit_message, content, branch="main")
    except Exception as e:
        st.warning(f"Failed to upload {file_path} to GitHub: {e}")

def github_download_file(repo, github_path, local_path):
    try:
        contents = repo.get_contents(github_path)
        with open(local_path, "wb") as file:
            file.write(contents.decoded_content)
    except Exception as e:
        st.warning(f"Failed to download {github_path} from GitHub: {e}")

def authenticate_gmail():
    try:
        oauth_credentials = {
            "client_id": st.secrets["google_oauth"]["client_id"],
            "client_secret": st.secrets["google_oauth"]["client_secret"],
            "token_uri": st.secrets["google_oauth"]["token_uri"],
            "auth_uri": st.secrets["google_oauth"]["auth_uri"],
            "redirect_uris": st.secrets["google_oauth"]["redirect_uris"]
        }
        token_info = json.loads(st.secrets["gmail_token"]["token_json"])
        creds = Credentials.from_authorized_user_info(token_info)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                st.error("Token expired and can't be refreshed.")
                st.stop()
        service = build('gmail', 'v1', credentials=creds)
        return service
    except Exception as e:
        st.error(f"Gmail authentication failed: {e}")
        st.stop()

def search_emails(service, subject_text="", after_date="", before_date=""):
    query = f'subject:"{subject_text}"'
    if after_date:
        query += f' after:{after_date}'
    if before_date:
        query += f' before:{before_date}'
    results = service.users().messages().list(userId='me', q=query).execute()
    return results.get('messages', [])

def download_attachments(service, messages, destination_folder=RESUME_FOLDER):
    os.makedirs(destination_folder, exist_ok=True)
    downloaded = 0
    for message in messages:
        try:
            msg = service.users().messages().get(userId='me', id=message['id']).execute()
            for part in msg['payload'].get('parts', []):
                if part.get('filename') and part.get('body') and part.get('body').get('attachmentId'):
                    attachment_id = part['body']['attachmentId']
                    attachment = service.users().messages().attachments().get(
                        userId='me', messageId=message['id'], id=attachment_id
                    ).execute()
                    file_data = base64.urlsafe_b64decode(attachment['data'].encode('UTF-8'))
                    attachment_filename = part['filename']
                    attachment_path = os.path.join(destination_folder, attachment_filename)
                    with open(attachment_path, 'wb') as f:
                        f.write(file_data)
                    # Optional: Upload to GitHub
                    repo = github_setup()
                    if repo:
                        github_upload_file(repo, attachment_path, f"{GITHUB_RESUME_PATH}/{attachment_filename}")
                    downloaded += 1
        except Exception:
            continue
    return downloaded

# Resume Extraction
EMAIL_REGEX = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
MOBILE_REGEX = r'(?:\+92|0)?3\d{9}\b'
NAME_REGEX = r'\b(?:[A-Z][a-z]+|[A-Z]{2,})(?:\s(?:[A-Z][a-z]+|[A-Z]{2,})){1,3}\b'

def extract_text_from_docx(path):
    doc = Document(path)
    return '\n'.join([para.text for para in doc.paragraphs])

def extract_info_from_text(text):
    email = re.findall(EMAIL_REGEX, text)
    mobile = re.findall(MOBILE_REGEX, text)
    names = re.findall(NAME_REGEX, text)
    return {
        'name': names[0] if names else 'Not found',
        'email': email[0] if email else 'Not found',
        'mobile': mobile[0] if mobile else 'Not found'
    }

def extract_job_title_from_filename(jd_path):
    filename = os.path.basename(jd_path)
    if "application for" in filename.lower():
        return filename.split("for", 1)[-1].replace('.docx', '').replace('.doc', '').replace('.pdf', '').strip()
    return "Not found"

def extract_resume_info(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    try:
        if ext == '.pdf':
            text = extract_pdf_text(file_path)
        elif ext == '.docx':
            text = extract_text_from_docx(file_path)
        else:
            raise ValueError("Unsupported file type. Only PDF and DOCX are supported.")
        info = extract_info_from_text(text)
        info['file_name'] = os.path.basename(file_path)
        info['text'] = text
        return info
    except Exception:
        return None

def analyze_resume_with_gpt(resume_info, job_description):
    openai.api_key = st.secrets["openai"]["OPENAI_API_KEY"]
    if not openai.api_key:
        st.error("OpenAI API key not found.")
        return "Score: 0\nRecommendation: Analysis failed due to missing API key\nStrengths: None\nGaps: None"
    resume_text = resume_info.get('text', '')
    if not resume_text:
        resume_text = f"Name: {resume_info.get('name', 'Not found')}\nEmail: {resume_info.get('email', 'Not found')}\nMobile: {resume_info.get('mobile', 'Not found')}"
    prompt = f"""
You are an expert HR recruiter specializing in data science hiring. Your task is to critically evaluate a candidate's resume against a job description and assign a realistic score out of 10.

Job Description:
{job_description}

Candidate Resume:
{resume_text}

Instructions:
1. Compare the candidate's skills, experience, and qualifications to the job description's requirements.
2. Assign a score (0-10) based on the match:
   - 8-10: Excellent match (meets most or all requirements).
   - 5-7: Moderate match (meets some requirements, minor gaps).
   - 0-4: Poor match (significant gaps or irrelevant experience).
3. Provide a concise summary in the following format:
   - Score: [Number, e.g., 7.5]
   - Recommendation: [One-line summary, e.g., "Suitable for the role with minor upskilling."]
   - Strengths: [One-line summary, e.g., "Strong Python and ML experience."]
   - Gaps: [One-line summary, e.g., "Lacks cloud computing expertise."]

Ensure the score reflects the actual fit, avoiding inflated ratings unless fully justified.
"""
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4" if os.getenv("USE_GPT4", "0") == "1" else "gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are an expert HR recruiter analyzing resumes."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=500
        )
        return response['choices'][0]['message']['content'].strip()
    except Exception as e:
        st.error(f"GPT analysis failed: {str(e)}")
        return f"Score: 0\nRecommendation: Analysis failed due to {str(e)}\nStrengths: None\nGaps: None"

def init_db():
    # Download database from GitHub
    try:
        repo = github_setup()
        if repo:
            github_download_file(repo, GITHUB_DB_PATH, DATABASE)
        else:
            urllib.request.urlretrieve(f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_DB_PATH}", DATABASE)
    except Exception as e:
        st.warning(f"Error downloading database: {e}")

    # Connect to local database
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()

    # Create tables
    c.execute('''CREATE TABLE IF NOT EXISTS analysis (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        email TEXT UNIQUE,
        mobile TEXT,
        strengths TEXT,
        gaps TEXT,
        recommendation TEXT,
        score REAL,
        status TEXT,
        resume_path TEXT,
        job_title TEXT,
        date_added DATE DEFAULT CURRENT_DATE
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS analysis2 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        email TEXT,
        mobile TEXT,
        strengths TEXT,
        gaps TEXT,
        recommendation TEXT,
        score REAL,
        status TEXT,
        resume_path TEXT,
        job_title TEXT,
        date_added DATE DEFAULT CURRENT_DATE
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS admin (
        username TEXT PRIMARY KEY,
        password TEXT
    )''')

    # Insert default admin user
    c.execute("SELECT * FROM admin WHERE username = ?", ("admin",))
    if not c.fetchone():
        c.execute("INSERT INTO admin (username, password) VALUES (?, ?)", ("admin", "123"))
        conn.commit()
    conn.close()

def store_analysis(name, email, mobile, strengths, score, recommendation, gaps, resume_path, job_title):
    status = "Shortlisted" if float(score) >= 5 else "Rejected"
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    today_date = datetime.date.today()
    c.execute('''
        SELECT COUNT(*) FROM analysis
        WHERE name = ? AND email = ? AND mobile = ? AND date_added = ?
    ''', (name, email, mobile, today_date))
    if c.fetchone()[0] > 0:
        print("Duplicate entry for the same person on the same day! No data inserted.")
    else:
        c.execute('''INSERT INTO analysis (name, email, mobile, strengths, gaps, recommendation, score, status, resume_path, job_title, date_added)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_DATE)''',
                  (name, email, mobile, strengths, gaps, recommendation, score, status, resume_path, job_title))
        # Optional: Upload updated database to GitHub
        repo = github_setup()
        if repo:
            github_upload_file(repo, DATABASE, GITHUB_DB_PATH, "Update database")
    conn.commit()
    conn.close()

def store_quick_analysis(name, email, mobile, strengths, score, recommendation, gaps, resume_path, job_title):
    status = "Shortlisted" if float(score) >= 5 else "Rejected"
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    today_date = datetime.date.today()
    c.execute('''
        SELECT COUNT(*) FROM analysis2
        WHERE name = ? AND email = ? AND mobile = ? AND date_added = ?
    ''', (name, email, mobile, today_date))
    if c.fetchone()[0] > 0:
        print("Duplicate entry for the same person on the same day! No data inserted.")
    else:
        c.execute('''INSERT INTO analysis2 (name, email, mobile, strengths, gaps, recommendation, score, status, resume_path, job_title, date_added)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_DATE)''',
                  (name, email, mobile, strengths, gaps, recommendation, score, status, resume_path, job_title))
        # Optional: Upload updated database to GitHub
        repo = github_setup()
        if repo:
            github_upload_file(repo, DATABASE, GITHUB_DB_PATH, "Update database")
    conn.commit()
    conn.close()

def is_resume_processed(resume_path, job_title):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM analysis WHERE resume_path = ? AND job_title = ?', (resume_path, job_title))
    count = c.fetchone()[0]
    conn.close()
    return count > 0

def is_resume_processed_quick(resume_path, job_title):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM analysis2 WHERE resume_path = ? AND job_title = ?', (resume_path, job_title))
    count = c.fetchone()[0]
    conn.close()
    return count > 0

def load_data():
    try:
        with sqlite3.connect(DATABASE) as conn:
            if st.session_state.page == "quick_analysis":
                df = pd.read_sql_query("SELECT * FROM analysis2", conn)
            else:
                df = pd.read_sql_query("SELECT * FROM analysis", conn)
            return df.sort_values(by='id', ascending=False).head(20)
    except Exception as e:
        st.error(f"Failed to load data: {e}")
        return pd.DataFrame()

def normalize_folder_name(text):
    return re.sub(r'\W+', '_', text.strip().lower())

# Streamlit UI
init_db()
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "username" not in st.session_state:
    st.session_state.username = None
if "page" not in st.session_state:
    st.session_state.page = "dashboard"

if not st.session_state.logged_in:
    st.markdown('<div class="header"><h1>AI Recruitment</h1></div>', unsafe_allow_html=True)
    st.title("Login")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Login"):
            conn = sqlite3.connect(DATABASE)
            c = conn.cursor()
            c.execute("SELECT * FROM admin WHERE username=? AND password=?", (username, password))
            result = c.fetchone()
            conn.close()
            if result:
                st.session_state.logged_in = True
                st.session_state.username = username
                st.success("Login successful")
                st.rerun()
            else:
                st.error("Invalid credentials")
    st.stop()

# Sidebar
st.sidebar.title("AI Recruitment")
if st.session_state.page != "change_password":
    page = st.sidebar.radio("Navigation", ["Dashboard", "Process Gmail", "Quick Analysis"], key="nav_radio")
    st.session_state.page = page.lower().replace(" ", "_")
else:
    st.sidebar.radio("Navigation", ["Dashboard", "Process Gmail", "Quick Analysis"], key="nav_radio", disabled=True)

if st.sidebar.button("Logout"):
    st.session_state.logged_in = False
    st.session_state.username = None
    st.session_state.page = "dashboard"
    st.rerun()

with st.sidebar.expander("Change Password"):
    if st.button("Change Password"):
        st.session_state.page = "change_password"
        st.rerun()

def change_password_page():
    st.markdown("<script>window.scrollTo(0, 0);</script>", unsafe_allow_html=True)
    st.markdown('<div class="header"><h1>Change Password</h1></div>', unsafe_allow_html=True)
    with st.container():
        with st.form("change_password_form", clear_on_submit=True):
            current_password = st.text_input("Current Password", type="password")
            new_password = st.text_input("New Password", type="password")
            confirm_password = st.text_input("Confirm New Password", type="password")
            submit_button = st.form_submit_button("Update Password", use_container_width=True)
            if submit_button:
                conn = sqlite3.connect(DATABASE)
                c = conn.cursor()
                if st.session_state.username:
                    c.execute(
                        "SELECT * FROM admin WHERE username=? AND password=?",
                        (st.session_state.username, current_password)
                    )
                    result = c.fetchone()
                    if result:
                        if new_password == confirm_password:
                            c.execute(
                                "UPDATE admin SET password=? WHERE username=?",
                                (new_password, st.session_state.username)
                            )
                            conn.commit()
                            # Upload updated database to GitHub
                            repo = github_setup()
                            if repo:
                                github_upload_file(repo, DATABASE, GITHUB_DB_PATH, "Update database")
                            st.success("Password updated successfully. Please log in again.")
                            st.session_state.logged_in = False
                            st.session_state.username = None
                            st.session_state.page = "dashboard"
                            st.rerun()
                        else:
                            st.error("New passwords do not match.")
                    else:
                        st.error("Current password is incorrect.")
                else:
                    st.error("No user session found.")
                conn.close()
    if st.button("Back"):
        st.session_state.page = "dashboard"
        st.rerun()

if st.session_state.page != "change_password":
    st.markdown('<div class="header"><h1>AI Recruitment</h1></div>', unsafe_allow_html=True)

if st.session_state.page == "change_password":
    change_password_page()

elif st.session_state.page == "dashboard":
    st.title("Recruitment Dashboard")
    with st.form("filter_form"):
        col1, col2, col3 = st.columns([1, 1, 1.5])
        start_date = col1.date_input("Start Date", datetime.date.today() - datetime.timedelta(days=30))
        end_date = col2.date_input("End Date", datetime.date.today())
        subject_filter = col3.text_input("Filter by Job Title", value="")
        status_filter = col1.selectbox("Status", ["All", "Shortlisted"], index=0, key="status_filter")
        top_scorers_filter = col3.selectbox(
            "Top Scorers",
            ["All", "Top 3", "Top 5", "Top 10"],
            index=0,
            key="top_scorers_filter"
        )
        submit_button = st.form_submit_button("Show Results")
    if submit_button:
        df = load_data()
        df = df.loc[:, ~df.columns.str.contains('^Unnamed')]
        if 'date_added' in df.columns:
            df['date_added'] = pd.to_datetime(df['date_added'], errors='coerce')
            filtered_df = df[
                (df['date_added'].dt.date >= start_date) &
                (df['date_added'].dt.date <= end_date)
            ]
        else:
            filtered_df = df
        if subject_filter:
            filtered_df = filtered_df[filtered_df['job_title'].str.contains(subject_filter, case=False, na=False)]
        if status_filter != "All":
            filtered_df = filtered_df[filtered_df['status'] == status_filter]
        if top_scorers_filter != "All":
            n = int(top_scorers_filter.split()[1])
            filtered_df = filtered_df.sort_values('score', ascending=False).head(n)
        mcol1, mcol2, mcol3 = st.columns(3)
        with mcol1:
            st.metric("Total Resumes", len(filtered_df))
        with mcol2:
            st.metric("Shortlisted", len(filtered_df[filtered_df['status'] == "Shortlisted"]))
        with mcol3:
            st.metric("Rejected", len(filtered_df[filtered_df['status'] == "Rejected"]))
        if not filtered_df.empty:
            for index, row in filtered_df.iterrows():
                with st.expander(f"Report - {row['name']} ({row['job_title']})"):
                    col1, col2 = st.columns([1, 3])
                    col1.markdown('<span class="label">Name</span>', unsafe_allow_html=True)
                    col2.markdown(f'<span class="value">{row["name"]}</span>', unsafe_allow_html=True)
                    col1, col2 = st.columns([1, 3])
                    col1.markdown('<span class="label">Email</span>', unsafe_allow_html=True)
                    col2.markdown(f'<span class="value">{row["email"]}</span>', unsafe_allow_html=True)
                    col1, col2 = st.columns([1, 3])
                    col1.markdown('<span class="label">Mobile</span>', unsafe_allow_html=True)
                    col2.markdown(f'<span class="value">{row["mobile"]}</span>', unsafe_allow_html=True)
                    col1, col2 = st.columns([1, 3])
                    col1.markdown('<span class="label">Score</span>', unsafe_allow_html=True)
                    col2.markdown(f'<span class="value">{row["score"]}</span>', unsafe_allow_html=True)
                    col1, col2 = st.columns([1, 3])
                    col1.markdown('<span class="label">Recommendation</span>', unsafe_allow_html=True)
                    col2.markdown(f'<span class="value">{row["recommendation"]}</span>', unsafe_allow_html=True)
                    col1, col2 = st.columns([1, 3])
                    col1.markdown('<span class="label">Gaps</span>', unsafe_allow_html=True)
                    col2.markdown(f'<span class="value">{row["gaps"]}</span>', unsafe_allow_html=True)
                    col1, col2 = st.columns([1, 3])
                    col1.markdown('<span class="label">Strengths</span>', unsafe_allow_html=True)
                    col2.markdown(f'<span class="value">{row.get("strengths", "Not Available")}</span>', unsafe_allow_html=True)
                    col1, col2 = st.columns([1, 3])
                    col1.markdown('<span class="label">Status</span>', unsafe_allow_html=True)
                    col2.markdown(f'<span class="value">{row["status"]}</span>', unsafe_allow_html=True)
                    col1, col2 = st.columns([1, 3])
                    col1.markdown('<span class="label">Job Title</span>', unsafe_allow_html=True)
                    col2.markdown(f'<span class="value">{row["job_title"]}</span>', unsafe_allow_html=True)
                    resume_path = row.get('resume_path', None)
                    if resume_path and os.path.exists(resume_path):
                        with open(resume_path, "rb") as file:
                            st.download_button(
                                label="📄 Download Resume",
                                data=file,
                                file_name=os.path.basename(resume_path),
                                mime="application/octet-stream",
                                key=f"download_resume_{index}"
                            )
                    else:
                        st.error("Resume file not found or path missing in database.")
        else:
            st.info("No results found matching the filters.")

elif st.session_state.page == "process_gmail":
    st.title("Process Gmail Resumes")
    subject = st.text_input("Email Subject")
    col1, col2 = st.columns(2)
    start_date = col1.date_input("Start Date", datetime.date.today() - datetime.timedelta(days=7))
    end_date = col2.date_input("End Date", datetime.date.today())
    st.subheader("Upload Job Descriptions")
    uploaded_files = st.file_uploader("Upload JD files", type=["txt", "docx", "pdf"], accept_multiple_files=True)
    if uploaded_files:
        os.makedirs(JD_FOLDER, exist_ok=True)
        for uploaded_file in uploaded_files:
            save_path = os.path.join(JD_FOLDER, uploaded_file.name)
            with open(save_path, "wb") as f:
                f.write(uploaded_file.getbuffer())
            # Optional: Upload to GitHub
            repo = github_setup()
            if repo:
                github_upload_file(repo, save_path, f"{GITHUB_JD_PATH}/{uploaded_file.name}")
        st.success(f"Uploaded {len(uploaded_files)} JD file(s) to {JD_FOLDER}")
    if st.button("Fetch Resumes"):
        service = authenticate_gmail()
        after_date = start_date.strftime("%Y/%m/%d")
        before_date = (end_date + datetime.timedelta(days=1)).strftime("%Y/%m/%d")
        folder_name = subject.lower().replace(" ", "_").strip()
        if not folder_name or "application_for" not in folder_name:
            st.error("Please enter a valid subject starting with 'Application for' (e.g., 'Application for Data Scientist').")
        else:
            resume_subfolder = os.path.join(RESUME_FOLDER, folder_name)
            os.makedirs(resume_subfolder, exist_ok=True)
            messages = search_emails(service, subject_text=subject, after_date=after_date, before_date=before_date)
            downloaded = download_attachments(service, messages, destination_folder=resume_subfolder)
            st.success(f"Downloaded {downloaded} resumes to {resume_subfolder}.")
    if st.button("Process Resumes"):
        with st.spinner("Processing resumes..."):
            jd_files = [f for f in os.listdir(JD_FOLDER) if os.path.isfile(os.path.join(JD_FOLDER, f))]
            if not jd_files:
                st.error(f"No job description files found in {JD_FOLDER}.")
            else:
                total_processed = 0
                total_failed = 0
                processed_jds = 0
                for jd_filename in jd_files:
                    jd_path = os.path.join(JD_FOLDER, jd_filename)
                    try:
                        base_name = os.path.splitext(jd_filename)[0]
                        folder_name = normalize_folder_name(base_name)
                        resume_subfolder = os.path.join(RESUME_FOLDER, folder_name)
                        if not os.path.exists(resume_subfolder):
                            continue
                        job_title = extract_job_title_from_filename(jd_path)
                        if job_title == "Not found":
                            continue
                        ext = os.path.splitext(jd_path)[1].lower()
                        if ext == '.txt':
                            job_description = open(jd_path, 'r', encoding='utf-8').read()
                        elif ext == '.docx':
                            job_description = extract_text_from_docx(jd_path)
                        elif ext == '.pdf':
                            job_description = extract_pdf_text(jd_path)
                        else:
                            continue
                        if not job_description:
                            continue
                        processed = 0
                        failed = 0
                        for filename in os.listdir(resume_subfolder):
                            resume_path = os.path.join(resume_subfolder, filename)
                            if is_resume_processed(resume_path, job_title):
                                continue
                            resume_info = extract_resume_info(resume_path)
                            if not resume_info or resume_info['name'] == 'Not found':
                                failed += 1
                                continue
                            result = analyze_resume_with_gpt(resume_info, job_description)
                            if not result:
                                failed += 1
                                continue
                            score = 0
                            strengths = ""
                            recommendation = ""
                            gaps = ""
                            for line in result.splitlines():
                                if "score" in line.lower():
                                    try:
                                        match = re.search(r'score.*?:\s*(\d+\.?\d*)', line, re.IGNORECASE)
                                        if match:
                                            score = float(match.group(1))
                                    except:
                                        pass
                                elif "strengths" in line.lower():
                                    strengths = line.split(":", 1)[-1].strip()
                                elif "recommendation" in line.lower():
                                    recommendation = line.split(":", 1)[-1].strip()
                                elif "gap" in line.lower():
                                    gaps = line.split(":", 1)[-1].strip()
                            name = resume_info.get('name', 'Not found')
                            email = resume_info.get('email', 'Not found')
                            mobile = resume_info.get('mobile', 'Not found')
                            store_analysis(
                                name, email, mobile,
                                strengths, score, recommendation, gaps,
                                resume_path, job_title
                            )
                            processed += 1
                        total_processed += processed
                        total_failed += failed
                        processed_jds += 1
                    except Exception as e:
                        st.warning(f"Error processing {jd_filename}: {e}")
                        continue
                if processed_jds == 0:
                    st.error(f"No resume subfolders found for any job descriptions in {JD_FOLDER}.")
                else:
                    st.success(f"Total: Processed {total_processed} resumes. Failed: {total_failed}.")

elif st.session_state.page == "quick_analysis":
    st.title("Quick Resume Analysis")
    if 'data' not in st.session_state:
        st.session_state.data = None
    if 'process_successful' not in st.session_state:
        st.session_state.process_successful = False
    uploaded_jd = st.file_uploader("Upload Job Description", type=["pdf", "doc", "docx"])
    uploaded_resumes = st.file_uploader("Upload Resumes", type=["pdf", "doc", "docx"], accept_multiple_files=True)
    if st.button("Process Resumes"):
        with st.spinner("Processing resumes..."):
            if uploaded_jd and uploaded_resumes:
                try:
                    os.makedirs(JD_FOLDER, exist_ok=True)
                    os.makedirs(RESUME_FOLDER, exist_ok=True)
                    jd_path = os.path.join(JD_FOLDER, uploaded_jd.name)
                    with open(jd_path, "wb") as f:
                        f.write(uploaded_jd.read())
                    # Optional: Upload to GitHub
                    repo = github_setup()
                    if repo:
                        github_upload_file(repo, jd_path, f"{GITHUB_JD_PATH}/{uploaded_jd.name}")
                    if jd_path.endswith(('.docx', '.doc')):
                        jd_text = extract_text_from_docx(jd_path)
                    elif jd_path.endswith('.pdf'):
                        jd_text = extract_pdf_text(jd_path)
                    else:
                        raise ValueError("Unsupported Job Description file format.")
                    for uploaded_resume in uploaded_resumes:
                        resume_path = os.path.join(RESUME_FOLDER, uploaded_resume.name)
                        with open(resume_path, "wb") as f:
                            f.write(uploaded_resume.read())
                        # Optional: Upload to GitHub
                        if repo:
                            github_upload_file(repo, resume_path, f"{GITHUB_RESUME_PATH}/{uploaded_resume.name}")
                        job_title = extract_job_title_from_filename(jd_path)
                        if is_resume_processed_quick(resume_path, job_title):
                            continue
                        resume_info = extract_resume_info(resume_path)
                        if not resume_info:
                            continue
                        result = analyze_resume_with_gpt(resume_info, jd_text)
                        if not result:
                            continue
                        score = 0
                        recommendation = ""
                        gaps = ""
                        strengths = ""
                        for line in result.splitlines():
                            if "score" in line.lower():
                                try:
                                    match = re.search(r'score.*?:\s*(\d+\.?\d*)', line, re.IGNORECASE)
                                    score = float(match.group(1)) if match else 0
                                except:
                                    score = 0
                            elif "recommendation" in line.lower():
                                recommendation = line.split(":", 1)[-1].strip()
                            elif "gap" in line.lower():
                                gaps = line.split(":", 1)[-1].strip()
                            elif "strength" in line.lower():
                                strengths = line.split(":", 1)[-1].strip()
                        name = resume_info.get('name', 'Not found')
                        email = resume_info.get('email', 'Not found')
                        mobile = resume_info.get('mobile', 'Not found')
                        store_quick_analysis(
                            name,
                            email,
                            mobile,
                            strengths,
                            score,
                            recommendation,
                            gaps,
                            resume_path,
                            job_title
                        )
                    st.success("Quick Analysis results saved successfully!")
                    st.session_state.process_successful = True
                    st.session_state.data = load_data()
                except Exception as e:
                    st.error(f"Failed to process resumes: {e}")
            else:
                st.error("Please upload both Job Description and at least one Resume to proceed.")
    st.subheader("Filtered Results")
    if st.session_state.data is None:
        st.session_state.data = load_data()
    df = st.session_state.data
    if df is not None and not df.empty:
        with st.form("filter_form"):
            col1, col2, col3 = st.columns([1, 1, 1.5])
            start_date = col1.date_input("Start Date", datetime.date.today() - datetime.timedelta(days=30))
            end_date = col2.date_input("End Date", datetime.date.today())
            subject_filter = col3.text_input("Filter by Job Title", value="")
            status_filter = col1.selectbox("Status", ["All", "Shortlisted"], index=0, key="status_filter")
            top_scorers_filter = col3.selectbox(
                "Top Scorers",
                ["All", "Top 3", "Top 5", "Top 10"],
                index=0,
                key="top_scorers_filter"
            )
            submit_button = st.form_submit_button("Show Results")
        if submit_button:
            df = load_data()
            df = df.loc[:, ~df.columns.str.contains('^Unnamed')]
            if 'date_added' in df.columns:
                df['date_added'] = pd.to_datetime(df['date_added'], errors='coerce')
                filtered_df = df[
                    (df['date_added'].dt.date >= start_date) &
                    (df['date_added'].dt.date <= end_date)
                ]
            else:
                filtered_df = df
            if subject_filter:
                filtered_df = filtered_df[filtered_df['job_title'].str.contains(subject_filter, case=False, na=False)]
            if status_filter != "All":
                filtered_df = filtered_df[filtered_df['status'] == status_filter]
            if top_scorers_filter != "All":
                n = int(top_scorers_filter.split()[1])
                filtered_df = filtered_df.sort_values('score', ascending=False).head(n)
            mcol1, mcol2, mcol3 = st.columns(3)
            with mcol1:
                st.metric("Total Resumes", len(filtered_df))
            with mcol2:
                st.metric("Shortlisted", len(filtered_df[filtered_df['status'] == "Shortlisted"]))
            with mcol3:
                st.metric("Rejected", len(filtered_df[filtered_df['status'] == "Rejected"]))
            if not filtered_df.empty:
                for index, row in filtered_df.iterrows():
                    with st.expander(f"Report - {row['name']} ({row['job_title']})"):
                        col1, col2 = st.columns([1, 3])
                        col1.markdown('<span class="label">Name</span>', unsafe_allow_html=True)
                        col2.markdown(f'<span class="value">{row["name"]}</span>', unsafe_allow_html=True)
                        col1, col2 = st.columns([1, 3])
                        col1.markdown('<span class="label">Email</span>', unsafe_allow_html=True)
                        col2.markdown(f'<span class="value">{row["email"]}</span>', unsafe_allow_html=True)
                        col1, col2 = st.columns([1, 3])
                        col1.markdown('<span class="label">Mobile</span>', unsafe_allow_html=True)
                        col2.markdown(f'<span class="value">{row["mobile"]}</span>', unsafe_allow_html=True)
                        col1, col2 = st.columns([1, 3])
                        col1.markdown('<span class="label">Score</span>', unsafe_allow_html=True)
                        col2.markdown(f'<span class="value">{row["score"]}</span>', unsafe_allow_html=True)
                        col1, col2 = st.columns([1, 3])
                        col1.markdown('<span class="label">Recommendation</span>', unsafe_allow_html=True)
                        col2.markdown(f'<span class="value">{row["recommendation"]}</span>', unsafe_allow_html=True)
                        col1, col2 = st.columns([1, 3])
                        col1.markdown('<span class="label">Gaps</span>', unsafe_allow_html=True)
                        col2.markdown(f'<span class="value">{row["gaps"]}</span>', unsafe_allow_html=True)
                        col1, col2 = st.columns([1, 3])
                        col1.markdown('<span class="label">Strengths</span>', unsafe_allow_html=True)
                        col2.markdown(f'<span class="value">{row.get("strengths", "Not Available")}</span>', unsafe_allow_html=True)
                        col1, col2 = st.columns([1, 3])
                        col1.markdown('<span class="label">Status</span>', unsafe_allow_html=True)
                        col2.markdown(f'<span class="value">{row["status"]}</span>', unsafe_allow_html=True)
                        col1, col2 = st.columns([1, 3])
                        col1.markdown('<span class="label">Job Title</span>', unsafe_allow_html=True)
                        col2.markdown(f'<span class="value">{row["job_title"]}</span>', unsafe_allow_html=True)
                        resume_path = row.get('resume_path', None)
                        if resume_path and os.path.exists(resume_path):
                            with open(resume_path, "rb") as file:
                                st.download_button(
                                    label="📄 Download Resume",
                                    data=file,
                                    file_name=os.path.basename(resume_path),
                                    mime="application/octet-stream",
                                    key=f"download_resume_quick_{index}"
                                )
                        else:
                            st.error("Resume file not found or path missing in database.")
            else:
                st.info("No results found matching the filters.")
    else:
        st.info("No data available. Please process resumes to view results.")
