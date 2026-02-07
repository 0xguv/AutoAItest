import os
import tempfile
import redis
from rq import Queue, Worker
from flask import Flask, request, render_template, redirect, url_for, send_from_directory, flash, jsonify
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from faster_whisper import WhisperModel
import logging
import subprocess
from datetime import datetime, date

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', 'uploads')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'a_very_secret_key_that_should_be_in_env') # Needed for Flask-Login
# Database configuration - supports PostgreSQL (Railway) or SQLite (local)
database_url = os.environ.get('DATABASE_URL') or os.environ.get('DATABASE_PUBLIC_URL')
if database_url and database_url.startswith('postgres://'):
    # Convert postgres:// to postgresql:// for SQLAlchemy compatibility
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url or 'sqlite:///app.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# Configuration for generating external URLs correctly in production
app.config['SERVER_NAME'] = os.environ.get('FLASK_SERVER_NAME') # e.g., 'your-domain.railway.app'
app.config['PREFERRED_URL_SCHEME'] = os.environ.get('FLASK_PREFERRED_URL_SCHEME', 'https')

# Configure Redis Queue
app.config['REDIS_URL'] = os.environ.get('REDIS_URL', 'redis://localhost:6379')

# OAuth Configuration
app.config['GOOGLE_CLIENT_ID'] = os.environ.get('GOOGLE_CLIENT_ID')
app.config['GOOGLE_CLIENT_SECRET'] = os.environ.get('GOOGLE_CLIENT_SECRET')

app.config['APPLE_CLIENT_ID'] = os.environ.get('APPLE_CLIENT_ID')
app.config['APPLE_CLIENT_SECRET'] = os.environ.get('APPLE_CLIENT_SECRET') # Apple requires a client secret generated from a .p8 key

app.config['DISCORD_CLIENT_ID'] = os.environ.get('DISCORD_CLIENT_ID')
app.config['DISCORD_CLIENT_SECRET'] = os.environ.get('DISCORD_CLIENT_SECRET')


os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login' # Specify the login view
login_manager.login_message_category = 'error' # Make login required messages appear as errors


# Initialize Authlib OAuth
from authlib.integrations.flask_client import OAuth
oauth = OAuth(app)

oauth.register(
    name='google',
    client_id=app.config.get('GOOGLE_CLIENT_ID'),
    client_secret=app.config.get('GOOGLE_CLIENT_SECRET'),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

# Apple OAuth (more complex setup, often requires JWT for client_secret)
# This is a basic placeholder, actual Apple integration can be more involved
oauth.register(
    name='apple',
    client_id=app.config.get('APPLE_CLIENT_ID'),
    client_secret=app.config.get('APPLE_CLIENT_SECRET'), # This will likely need to be a dynamically generated JWT
    server_metadata_url='https://appleid.apple.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'name email'},
    # Apple uses a specific "response_mode=form_post" for auth code, may need customization
)

oauth.register(
    name='discord',
    client_id=app.config.get('DISCORD_CLIENT_ID'),
    client_secret=app.config.get('DISCORD_CLIENT_SECRET'),
    api_base_url='https://discord.com/api/',
    access_token_url='https://discord.com/api/oauth2/token',
    authorize_url='https://discord.com/api/oauth2/authorize',
    client_kwargs={'scope': 'identify email'}
)

# Initialize Redis and RQ queue
redis_conn = redis.from_url(app.config['REDIS_URL'])
q = Queue(connection=redis_conn)

# Define model storage directory within /tmp for Vercel
# Vercel's /tmp directory is cleared between invocations, but cached for cold starts
MODEL_DIR = os.path.join(tempfile.gettempdir(), "faster_whisper_models")
os.makedirs(MODEL_DIR, exist_ok=True)

# Global variable to hold the model, loaded once
_faster_whisper_model = None

def load_faster_whisper_model(model_size="base", device="cpu", compute_type="int8"):
    global _faster_whisper_model
    if _faster_whisper_model is None:
        app.logger.info(f"Loading faster-whisper model '{model_size}' to {MODEL_DIR}...")
        _faster_whisper_model = WhisperModel(model_size, device=device, compute_type=compute_type, download_root=MODEL_DIR)
        app.logger.info(f"Faster-whisper model '{model_size}' loaded successfully.")
    return _faster_whisper_model

# model = whisper.load_model("base") # REMOVE THIS LINE - model loaded via function


class User(UserMixin, db.Model):
    __tablename__ = 'user' # Explicitly define table name
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=True) # Username is now nullable
    password_hash = db.Column(db.String(256), nullable=True) # Password can be null for OAuth users
    email = db.Column(db.String(120), unique=True, nullable=False) # Email is now required and unique for all users
    oauth_provider = db.Column(db.String(50), nullable=True)
    oauth_id = db.Column(db.String(256), nullable=True)
    is_subscribed = db.Column(db.Boolean, default=False)
    daily_tries_count = db.Column(db.Integer, default=0)
    last_try_date = db.Column(db.Date, default=date.min) # Store as date
    
    # Tiered limits
    max_duration_minutes = db.Column(db.Integer, default=1) # Default for unregistered/free
    max_daily_tries = db.Column(db.Integer, default=2) # Default for unregistered/free

    # Relationship for jobs
    processing_jobs = db.relationship('VideoProcessingJob', backref='user', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def get_max_duration(self):
        return self.max_duration_minutes

    def get_max_daily_tries(self):
        return self.max_daily_tries

class VideoProcessingJob(db.Model):
    id = db.Column(db.String(36), primary_key=True) # Use RQ job_id as primary key
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    original_video_filepath = db.Column(db.String(256), nullable=False)
    generated_srt_filepath = db.Column(db.String(256), nullable=True) # Nullable until transcription is done
    edited_srt_filepath = db.Column(db.String(256), nullable=True) # Path to user-edited SRT
    output_video_filepath = db.Column(db.String(256), nullable=True) # Path to final burned video
    original_filename = db.Column(db.String(256), nullable=False) # Original filename uploaded by user
    status = db.Column(db.String(50), default='pending') # pending, transcribed, editing, burning, completed, failed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    resolution = db.Column(db.String(20), nullable=True) # Store selected resolution
    language = db.Column(db.String(10), nullable=True) # Store selected language

    def __repr__(self):
        return f"<VideoProcessingJob {self.id} - {self.status}>"



@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))



class UsageLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    date = db.Column(db.Date, nullable=False, default=date.today)
    videos_processed = db.Column(db.Integer, default=0)

    user = db.relationship('User', backref=db.backref('usage_logs', lazy=True))

    __table_args__ = (db.UniqueConstraint('user_id', 'date', name='_user_date_uc'),)

    def __repr__(self):
        return f'<UsageLog {self.user.username} {self.date} - {self.videos_processed} videos>'


# Auto-create database tables on startup (for Railway deployment)
with app.app_context():
    try:
        db.create_all()
        app.logger.info("Database tables created successfully (or already exist)")
    except Exception as e:
        app.logger.error(f"Error creating database tables: {e}")


def seconds_to_srt_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int((seconds % 60) // 1)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"

def transcribe_video_task(user_id, original_filepath, filename, language, user_max_duration):
    from rq import get_current_job
    from app import app, db, User, UsageLog, seconds_to_srt_time, load_faster_whisper_model, get_video_duration, MODEL_DIR, os, subprocess, logging, date, tempfile
    
    with app.app_context():
        current_job_id = get_current_job().id
        user = User.query.get(user_id)
        if not user:
            app.logger.error(f"User with ID {user_id} not found for transcription job {current_job_id}")
            return {"status": "failed", "error": "User not found"}

        app.logger.info(f"Starting video transcription for job {current_job_id}, user {user_id}, file {filename}")
        
        try:
            video_duration = get_video_duration(original_filepath)
            if video_duration is None:
                app.logger.error(f"Could not determine video duration for transcription job {current_job_id}. Skipping processing.")
                os.remove(original_filepath)
                return {"status": "failed", "error": "Could not determine video duration. Is ffprobe installed?"}

            if video_duration > user_max_duration * 60:
                app.logger.error(f"Video duration ({video_duration / 60:.1f} min) exceeds limit of {user_max_duration} minutes for transcription job {current_job_id}. Skipping processing.")
                os.remove(original_filepath)
                return {"status": "failed", "error": f"Video duration ({video_duration / 60:.1f} min) exceeds your limit of {user_max_duration} minutes."}
            
            audio_filename_base = os.path.splitext(filename)[0]
            # Use a temporary directory for audio extraction
            temp_dir = tempfile.mkdtemp(dir=app.config['UPLOAD_FOLDER'])
            audio_filepath = os.path.join(temp_dir, f"{audio_filename_base}.mp3")

            ffmpeg_audio_command = ["ffmpeg", "-i", original_filepath, "-y", audio_filepath]
            app.logger.info(f"Running FFmpeg audio extraction for job {current_job_id}: {' '.join(ffmpeg_audio_command)}")
            subprocess.run(ffmpeg_audio_command, check=True, capture_output=True)

            model_ft = load_faster_whisper_model()
            segments, info = model_ft.transcribe(audio_filepath, beam_size=5, language=language if language else None)
            
            srt_content = ""
            for i, segment in enumerate(segments):
                start_time = seconds_to_srt_time(segment.start)
                end_time = seconds_to_srt_time(segment.end)
                text = segment.text.strip()
                srt_content += f"{i+1}\n{start_time} --> {end_time}\n{text}\n\n"

            # Save SRT in UPLOAD_FOLDER alongside the original video (for now)
            srt_filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{current_job_id}_{audio_filename_base}.srt")
            with open(srt_filepath, "w", encoding="utf-8") as f:
                f.write(srt_content)
            
            # Clean up audio file and temp directory
            os.remove(audio_filepath)
            os.rmdir(temp_dir)

            # Update the VideoProcessingJob entry
            job_entry = VideoProcessingJob.query.get(current_job_id)
            if job_entry:
                job_entry.generated_srt_filepath = srt_filepath
                job_entry.status = 'transcribed'
                db.session.commit()
            else:
                app.logger.error(f"VideoProcessingJob with ID {current_job_id} not found after transcription.")

            return {
                "status": "transcribed",
                "original_video_filepath": original_filepath,
                "generated_srt_filepath": srt_filepath
            }

        except subprocess.CalledProcessError as e:
            # Update job status to failed
            job_entry = VideoProcessingJob.query.get(current_job_id)
            if job_entry:
                job_entry.status = 'failed'
                db.session.commit()
            app.logger.error(f"FFmpeg command failed for transcription job {current_job_id} with exit code {e.returncode}")
            app.logger.error(f"FFmpeg stdout: {e.stdout.decode(errors='ignore')}")
            app.logger.error(f"FFmpeg stderr: {e.stderr.decode(errors='ignore')}")
            if os.path.exists(original_filepath):
                os.remove(original_filepath)
            return {"status": "failed", "error": f"FFmpeg audio extraction error: {e.stderr.decode(errors='ignore')}"}
        except Exception as e:
            # Update job status to failed
            job_entry = VideoProcessingJob.query.get(current_job_id)
            if job_entry:
                job_entry.status = 'failed'
                db.session.commit()
            app.logger.error(f"An unexpected error occurred for transcription job {current_job_id}: {e}")
            if os.path.exists(original_filepath):
                os.remove(original_filepath)
            return {"status": "failed", "error": f"An unexpected error occurred during transcription: {e}"}


@app.cli.command("init-db")
def init_db_command():
    """Drops and creates the database tables."""
    with app.app_context():
        db.drop_all()
        db.create_all()
    print("Dropped and initialized the database.")

@app.route('/')
def index():
    message = request.args.get('message')
    return render_template('index.html', message=message)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        if not email or not password:
            flash('Email and password are required!', 'error')
            return redirect(url_for('register'))
        
        # Basic email validation (can be more robust with regex)
        if '@' not in email or '.' not in email:
            flash('Invalid email address!', 'error')
            return redirect(url_for('register'))

        # Password validation
        if len(password) < 5:
            flash('Password must be at least 5 characters long!', 'error')
            return redirect(url_for('register'))
        if len(password) > 10:
            flash('Password cannot exceed 10 characters!', 'error')
            return redirect(url_for('register'))
        if not any(char.isdigit() for char in password):
            flash('Password must contain at least one number!', 'error')
            return redirect(url_for('register'))

        if User.query.filter_by(email=email).first():
            flash('Email already registered!', 'error')
            return redirect(url_for('register'))
        
        # Generate a username from email, or leave it null.
        # For simplicity, let's set username to be the part before '@' in email.
        # This can be made more sophisticated later if usernames are still desired.
        username_from_email = email.split('@')[0]
        
        new_user = User(email=email, username=username_from_email, max_duration_minutes=5, max_daily_tries=5)
        new_user.set_password(password)
        db.session.add(new_user)
        db.session.commit()
        login_user(new_user) # Log in the user immediately after registration
        return redirect(url_for('index', message='Registration successful! Welcome!')) # Redirect to index with message
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first() # Find user by email
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('index', message='Success! Logged in successfully!'))
        else:
            flash('Invalid email or password.', 'error') # Update flash message
            return redirect(url_for('login'))
    return render_template('login.html')

@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        email = request.form.get('email')
        flash('If an account with that email exists, you will receive a password reset link.', 'info')
        return redirect(url_for('login'))
    return render_template('forgot_password.html')



# Helper function to find or create a user from OAuth data
def create_or_login_oauth_user(oauth_provider, oauth_id, email, username):
    user = User.query.filter_by(oauth_provider=oauth_provider, oauth_id=oauth_id).first()
    if user:
        login_user(user)
        return True

    # Try to link by email if user exists with this email but not this OAuth provider
    if email:
        existing_user_by_email = User.query.filter_by(email=email).first()
        if existing_user_by_email:
            # Link existing account
            existing_user_by_email.oauth_provider = oauth_provider
            existing_user_by_email.oauth_id = oauth_id
            db.session.commit()
            login_user(existing_user_by_email)
            return True

    # Create new user
    # Generate a unique username from email if OAuth provider doesn't provide one
    if not username and email:
        base_username = email.split('@')[0]
    elif not username: # Fallback if neither username nor email is present
        base_username = "oauth_user"
    else:
        base_username = username
        
    generated_username = base_username
    counter = 1
    # Ensure generated_username is unique (only if username is still unique=True)
    # The User model is still unique=True for username, so keep this check.
    while User.query.filter_by(username=generated_username).first():
        generated_username = f"{base_username}{counter}"
        counter += 1
    
    new_user = User(
        username=generated_username, # Use generated_username
        email=email,
        oauth_provider=oauth_provider,
        oauth_id=oauth_id,
        max_duration_minutes=5, # Default limits for new users
        max_daily_tries=5
    )
    db.session.add(new_user)
    db.session.commit()
    login_user(new_user)
    return True

# OAuth login routes
@app.route('/login/<name>')
def oauth_login(name):
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    client = oauth.create_client(name)
    if not client:
        flash(f'OAuth client "{name}" not configured.', 'error')
        return redirect(url_for('login'))
    
    redirect_uri = url_for(f'auth_{name}_callback', _external=True)
    return client.authorize_redirect(redirect_uri)

# Google OAuth Callback
@app.route('/auth/google/callback')
def auth_google_callback():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    try:
        token = oauth.google.authorize_access_token()
        user_info = oauth.google.parse_id_token(token)
        
        email = user_info.get('email')
        username = user_info.get('name') # Google provides 'name'
        oauth_id = user_info.get('sub') # Google's unique user ID
        
        if create_or_login_oauth_user('google', oauth_id, email, username):
            flash('Successfully logged in with Google!', 'success')
            return redirect(url_for('index'))
        
        flash('Could not log in with Google.', 'error')
        return redirect(url_for('login'))
    except Exception as e:
        flash(f'Google login failed: {e}', 'error')
        app.logger.error(f"Google OAuth error: {e}")
        return redirect(url_for('login'))

# Discord OAuth Callback
@app.route('/auth/discord/callback')
def auth_discord_callback():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    try:
        token = oauth.discord.authorize_access_token()
        # Discord returns user info directly in the token response if scope 'identify' is used
        user_info = oauth.discord.get('users/@me').json()
        
        email = user_info.get('email')
        username = user_info.get('username') # Discord provides 'username'
        oauth_id = user_info.get('id') # Discord's unique user ID

        if create_or_login_oauth_user('discord', oauth_id, email, username):
            flash('Successfully logged in with Discord!', 'success')
            return redirect(url_for('index'))

        flash('Could not log in with Discord.', 'error')
        return redirect(url_for('login'))
    except Exception as e:
        flash(f'Discord login failed: {e}', 'error')
        app.logger.error(f"Discord OAuth error: {e}")
        return redirect(url_for('login'))

# Apple OAuth Callback (Note: Apple requires POST for authorization code exchange)
@app.route('/auth/apple/callback', methods=['GET', 'POST'])
def auth_apple_callback():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        try:
            # Apple often sends the authorization code as a form post parameter
            # Authlib handles this with authorize_access_token which expects GET params,
            # but if it's a POST, we need to extract the 'code' and 'id_token' manually if Authlib doesn't handle it
            # For simplicity, Authlib's authorize_access_token should still work if it can read POST body.
            token = oauth.apple.authorize_access_token()
            user_info = oauth.apple.parse_id_token(token)

            email = user_info.get('email')
            # Apple might not provide a 'name' directly, can construct from 'given_name'/'family_name' or use email part
            username = user_info.get('email', '').split('@')[0] 
            oauth_id = user_info.get('sub') # Apple's unique user ID

            if create_or_login_oauth_user('apple', oauth_id, email, username):
                flash('Successfully logged in with Apple!', 'success')
                return redirect(url_for('index'))
            
            flash('Could not log in with Apple.', 'error')
            return redirect(url_for('login'))
        except Exception as e:
            flash(f'Apple login failed: {e}', 'error')
            app.logger.error(f"Apple OAuth error: {e}")
            return redirect(url_for('login'))
    else: # GET request, usually for errors or initial redirect if not form_post
        flash('Apple login requires POST callback, please try again.', 'error')
        return redirect(url_for('login'))


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index', message="Info! You have been logged out."))

@app.route('/upgrade')
@login_required
def upgrade():
    # Placeholder for subscription logic - disabled for now
    # In a real app, this would integrate with a payment gateway
    # current_user.is_subscribed = True
    # current_user.max_duration_minutes = 60 # 1 hour
    # current_user.max_daily_tries = -1 # Unlimited
    # db.session.commit()
    return redirect(url_for('index', message="Info! Upgrade feature coming soon!"))

@app.route('/profile')
@login_required
def profile():
    return render_template('profile.html')

@app.route('/terms')
def terms():
    return render_template('terms.html')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

@app.route('/api/user_usage')
@login_required
def user_usage_data():
    # Fetch usage data for the current user
    # Order by date to ensure correct plotting
    usage_data = UsageLog.query.filter_by(user_id=current_user.id).order_by(UsageLog.date).all()

    dates = [log.date.strftime('%Y-%m-%d') for log in usage_data]
    videos_processed = [log.videos_processed for log in usage_data]

    return jsonify({
        'dates': dates,
        'videos_processed': videos_processed
    })

# Define a function to encapsulate the video processing logic
# This function will be enqueued by RQ
def burn_subtitles_task(user_id, original_video_filepath, srt_filepath, filename_for_output, resolution):
    from rq import get_current_job
    from app import app, db, User, UsageLog, seconds_to_srt_time, load_faster_whisper_model, get_video_duration, MODEL_DIR, os, subprocess, logging, date
    
    with app.app_context():
        current_job_id = get_current_job().id
        user = User.query.get(user_id)
        if not user:
            app.logger.error(f"User with ID {user_id} not found for burning job {current_job_id}")
            return {"status": "failed", "error": "User not found"}

        app.logger.info(f"Starting video burning for job {current_job_id}, user {user_id}, file {filename_for_output}")
        
        try:
            # Check if original video file still exists
            if not os.path.exists(original_video_filepath):
                app.logger.error(f"Original video file not found for burning job {current_job_id}: {original_video_filepath}")
                # Attempt to delete SRT if it exists
                if os.path.exists(srt_filepath):
                    os.remove(srt_filepath)
                return {"status": "failed", "error": "Original video file not found. It might have been deleted or moved."}
            
            # Check if SRT file exists
            if not os.path.exists(srt_filepath):
                app.logger.error(f"SRT file not found for burning job {current_job_id}: {srt_filepath}")
                # Attempt to delete original video if it exists
                if os.path.exists(original_video_filepath):
                    os.remove(original_video_filepath)
                return {"status": "failed", "error": "SRT file not found. It might have been deleted or moved."}

            output_video_filename = f"subtitled_{filename_for_output}"
            output_video_filepath = os.path.join(app.config['UPLOAD_FOLDER'], output_video_filename)
            srt_filepath_for_filter = srt_filepath.replace('\\', '/').replace(':', '\\:') # Handle Windows paths for FFmpeg

            vf_filters = [f"subtitles='{srt_filepath_for_filter}'"]

            if resolution != 'original':
                width, height = resolution.split('x')
                vf_filters.append(f"scale={width}x{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2")
            
            ffmpeg_burn_command = [
                "ffmpeg",
                "-i", original_video_filepath,
                "-y",
                "-vf", ",".join(vf_filters),
                "-preset", "ultrafast",
                "-threads", "1",
                output_video_filepath
            ]
            
            app.logger.info(f"Running FFmpeg burn command for job {current_job_id}: {' '.join(ffmpeg_burn_command)}")
            
            subprocess.run(
                ffmpeg_burn_command,
                check=True,
                capture_output=True
            )

            # Clean up original uploaded file and SRT file after processing
            if os.path.exists(original_video_filepath):
                os.remove(original_video_filepath)
            if os.path.exists(srt_filepath):
                os.remove(srt_filepath)

            # Record usage in UsageLog
            today = date.today()
            usage_log = UsageLog.query.filter_by(user_id=user.id, date=today).first()
            if usage_log:
                usage_log.videos_processed += 1
            else:
                usage_log = UsageLog(user_id=user.id, date=today, videos_processed=1)
                db.session.add(usage_log)
            db.session.commit()

            # Increment daily tries count for the user only on successful completion
            if user.max_daily_tries != -1: # Only for users with limited tries
                user.daily_tries_count += 1
                user.last_try_date = date.today() # Update last_try_date to today for this successful try
                db.session.commit()

            video_download_url = f"/download/{output_video_filename}"
            
            # Update the VideoProcessingJob entry
            job_entry = VideoProcessingJob.query.get(current_job_id)
            if job_entry:
                job_entry.output_video_filepath = output_video_filepath
                job_entry.status = 'completed'
                db.session.commit()
            else:
                app.logger.error(f"VideoProcessingJob with ID {current_job_id} not found after burning.")

            return {
                "status": "completed",
                "video_url": video_download_url
            }

        except subprocess.CalledProcessError as e:
            # Update job status to failed
            job_entry = VideoProcessingJob.query.get(current_job_id)
            if job_entry:
                job_entry.status = 'failed'
                db.session.commit()
            app.logger.error(f"FFmpeg command failed for burning job {current_job_id} with exit code {e.returncode}")
            app.logger.error(f"FFmpeg stdout: {e.stdout.decode(errors='ignore')}")
            app.logger.error(f"FFmpeg stderr: {e.stderr.decode(errors='ignore')}")
            # Clean up original video and srt if burning failed
            if os.path.exists(original_video_filepath):
                os.remove(original_video_filepath)
            if os.path.exists(srt_filepath):
                os.remove(srt_filepath)
            return {"status": "failed", "error": f"FFmpeg burning error: {e.stderr.decode(errors='ignore')}"}
        except Exception as e:
            # Update job status to failed
            job_entry = VideoProcessingJob.query.get(current_job_id)
            if job_entry:
                job_entry.status = 'failed'
                db.session.commit()
            app.logger.error(f"An unexpected error occurred for burning job {current_job_id}: {e}")
            # Clean up original video and srt if burning failed
            if os.path.exists(original_video_filepath):
                os.remove(original_video_filepath)
            if os.path.exists(srt_filepath):
                os.remove(srt_filepath)
            return {"status": "failed", "error": f"An unexpected error occurred during burning: {e}"}


def get_video_duration(filepath):
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            filepath
        ]
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return float(result.stdout.strip())
    except subprocess.CalledProcessError as e:
        app.logger.error(f"FFprobe failed: {e.stderr}")
        return None
    except Exception as e:
        app.logger.error(f"Error getting video duration: {e}")
        return None

@app.route('/upload', methods=['POST'])
@login_required # This decorator requires user to be logged in to upload
def upload_file():
    # Determine user limits
    user_max_duration = current_user.get_max_duration()
    user_max_tries = current_user.get_max_daily_tries()
    
    # Reset daily tries if date changed
    if current_user.last_try_date != date.today():
        current_user.daily_tries_count = 0
        current_user.last_try_date = date.today()
        db.session.commit()

    if user_max_tries != -1 and current_user.daily_tries_count >= user_max_tries:
        return jsonify({"status": "error", "message": f"Daily upload limit reached ({user_max_tries}). Upgrade or try again tomorrow."}), 403

    if 'video_file' not in request.files:
        return redirect(url_for('index', message="Error: No file part in the request."))
    file = request.files['video_file']
    if file.filename == '':
        return redirect(url_for('index', message="Error: No selected file."))
    if file:
        filename = secure_filename(file.filename)
        # Generate a unique filename to avoid conflicts and store temporarily
        unique_filename = f"{os.urandom(16).hex()}_{filename}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
        
        # Extract resolution from form
        resolution = request.form.get('resolution', 'original')
        language = request.form.get('language', None) # New: Get language from form

        try:
            # ==> DIAGNOSTIC LOGGING <==
            upload_folder = app.config['UPLOAD_FOLDER']
            app.logger.info(f"Attempting to save file to: {filepath}")
            app.logger.info(f"Upload folder is: {upload_folder}")
            if not os.path.isdir(upload_folder):
                app.logger.error(f"Upload folder '{upload_folder}' does not exist or is not a directory.")
            else:
                app.logger.info(f"Upload folder '{upload_folder}' exists.")
            
            file.save(filepath)

            # ==> DIAGNOSTIC LOGGING <==
            if os.path.exists(filepath):
                app.logger.info(f"SUCCESS: File saved and found at {filepath}")
            else:
                app.logger.error(f"FAILURE: File not found at {filepath} immediately after save.")


            # Enqueue the video processing task
            job = q.enqueue(
                'app.transcribe_video_task', # Enqueue the new transcription task
                current_user.id,
                filepath,
                filename, # Pass original filename for output naming
                language,
                user_max_duration,
                job_timeout='1h' # Allow up to 1 hour for video processing
            )
            app.logger.info(f"Video transcription task enqueued with job ID: {job.id}")

            # Create a new VideoProcessingJob entry
            new_job_entry = VideoProcessingJob(
                id=job.id,
                user_id=current_user.id,
                original_video_filepath=filepath,
                original_filename=filename,
                status='pending',
                resolution=resolution, # Store resolution from upload form
                language=language # Store language from upload form
            )
            db.session.add(new_job_entry)
            db.session.commit()

            return jsonify({"status": "success", "job_id": job.id})

        except Exception as e:
            app.logger.error(f"An error occurred during file upload or enqueue: {e}")
            if os.path.exists(filepath):
                os.remove(filepath)
            return jsonify({"status": "error", "message": f"An unexpected error occurred: {e}"}), 500


@app.route('/download/<filename>')
def download_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=True)

@app.route('/api/job_status/<job_id>')
@login_required
def job_status(job_id):
    job_entry = VideoProcessingJob.query.filter_by(id=job_id, user_id=current_user.id).first()

    if not job_entry:
        return jsonify({"status": "error", "message": "Job not found or unauthorized access."}), 404

    # Fetch real-time status from RQ for jobs that are still processing
    rq_job = q.fetch_job(job_id)
    rq_status = rq_job.get_status() if rq_job else 'unknown' # Get status from RQ, fallback to unknown

    # Prefer DB status for 'transcribed', 'burning', 'completed', 'failed' states
    # Use RQ status for 'queued', 'started', 'deferred'
    if job_entry.status in ['transcribed', 'burning', 'completed', 'failed', 'editing']:
        status_to_report = job_entry.status
    else:
        status_to_report = rq_status


    if status_to_report == 'transcribed':
        return jsonify({
            "status": "transcribed",
            "progress_message": "Transcription completed. Redirecting to editor.",
            "redirect_url": url_for('edit_video', job_id=job_id)
        })
    elif status_to_report == 'completed':
        if job_entry.output_video_filepath and os.path.exists(job_entry.output_video_filepath):
            return jsonify({
                "status": "completed",
                "result": {"video_url": url_for('download_file', filename=os.path.basename(job_entry.output_video_filepath))},
                "progress_message": "Video processing completed successfully."
            })
        else:
            return jsonify({
                "status": "failed",
                "error": "Completed job, but output video file not found.",
                "progress_message": "Processing failed."
            })
    elif status_to_report == 'failed':
        # Retrieve error info from RQ job if available, otherwise rely on DB status
        error_message = job_entry.status_message if hasattr(job_entry, 'status_message') else "Processing failed." # Assuming status_message field if added
        if rq_job and rq_job.is_failed:
             error_message = str(rq_job.exc_info)
        return jsonify({
            "status": "failed",
            "error": error_message,
            "progress_message": "Processing failed."
        })
    else: # pending, started, deferred, unknown, etc.
        return jsonify({
            "status": status_to_report,
            "progress_message": f"Job is currently {status_to_report}."
        })

@app.route('/edit/<job_id>')
@login_required
def edit_video(job_id):
    job_entry = VideoProcessingJob.query.filter_by(id=job_id, user_id=current_user.id).first()

    if not job_entry:
        flash('Job not found or unauthorized.', 'error')
        return redirect(url_for('index'))

    if job_entry.status != 'transcribed' and job_entry.status != 'editing': # Allow re-editing
        flash(f'Video is not ready for editing (current status: {job_entry.status}).', 'error')
        return redirect(url_for('index'))

    video_url = url_for('download_file', filename=os.path.basename(job_entry.original_video_filepath))
    srt_url = url_for('download_file', filename=os.path.basename(job_entry.generated_srt_filepath))
    
    # Read SRT content directly to pass to the template for initial display
    srt_content = ""
    try:
        with open(job_entry.generated_srt_filepath, "r", encoding="utf-8") as f:
            srt_content = f.read()
    except FileNotFoundError:
        flash('Generated SRT file not found.', 'error')
        return redirect(url_for('index'))

    job_entry.status = 'editing' # Update status to indicate it's being edited
    db.session.commit()

    return render_template(
        'editor.html',
        job_id=job_id,
        video_url=video_url,
        srt_content=srt_content,
        original_filename=job_entry.original_filename,
        resolution=job_entry.resolution, # Pass resolution for later burning
        language=job_entry.language # Pass language for potential re-transcription if needed
    )

@app.route('/api/queue_stats')
def queue_stats():
    # Get queued jobs
    queued_jobs = q.count
    # Get active jobs (jobs currently being worked on by a worker)
    started_jobs = q.started_job_registry.count
    # Get total workers by querying RQ
    total_workers = len(Worker.all(connection=redis_conn))
    
    return jsonify({
        'queued_jobs': queued_jobs,
        'started_jobs': started_jobs,
        'total_workers': total_workers
    })

@app.route('/save_and_burn', methods=['POST'])
@login_required
def save_and_burn():
    data = request.get_json()
    job_id = data.get('job_id')
    srt_content = data.get('srt_content')
    positional_data = data.get('positional_data') # Not directly used for burning, but good to store if needed later
    resolution = data.get('resolution')
    # language = data.get('language') # Language is already stored in VideoProcessingJob or passed to transcribe_video_task

    if not all([job_id, srt_content, resolution]):
        return jsonify({"status": "error", "message": "Missing required data."}), 400

    job_entry = VideoProcessingJob.query.filter_by(id=job_id, user_id=current_user.id).first()

    if not job_entry:
        return jsonify({"status": "error", "message": "Job not found or unauthorized."}), 404
    
    # Ensure the job is in a state that allows saving/burning
    if job_entry.status not in ['transcribed', 'editing']:
        return jsonify({"status": "error", "message": f"Job is not in a state to be edited or burned ({job_entry.status})."}), 400

    try:
        # Overwrite the generated SRT file with the edited content
        edited_srt_filepath = job_entry.generated_srt_filepath # Use the same file for now
        with open(edited_srt_filepath, "w", encoding="utf-8") as f:
            f.write(srt_content)
        
        job_entry.edited_srt_filepath = edited_srt_filepath # Point to the edited SRT (which is the same file)
        job_entry.status = 'burning'
        db.session.commit()

        # Enqueue the burn_subtitles_task
        burn_job = q.enqueue(
            'app.burn_subtitles_task',
            current_user.id,
            job_entry.original_video_filepath,
            edited_srt_filepath,
            job_entry.original_filename, # Pass original filename for output naming
            resolution,
            job_timeout='1h'
        )
        app.logger.info(f"Burn subtitles task enqueued with job ID: {burn_job.id}")

        return jsonify({"status": "success", "job_id": burn_job.id, "message": "Subtitles saved and burning process started."})

    except Exception as e:
        app.logger.error(f"Error saving edited SRT and enqueuing burn task for job {job_id}: {e}")
        job_entry.status = 'failed'
        db.session.commit()
        return jsonify({"status": "error", "message": f"Failed to save and burn subtitles: {e}"}), 500

if __name__ == '__main__':
    app.run(debug=True)