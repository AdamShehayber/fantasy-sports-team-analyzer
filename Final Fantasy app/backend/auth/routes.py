from flask import Blueprint, render_template, request, redirect, url_for, flash, session, make_response, current_app
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.orm import Session
from sqlalchemy import select
from datetime import datetime, timedelta
import secrets
import hashlib
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from extensions import limiter
from models import get_engine, User, LeagueSettings, SessionLog

auth_bp = Blueprint('auth', __name__)

def get_session():
    return Session(get_engine())

def generate_remember_token():
    """Generate a secure remember me token"""
    return secrets.token_urlsafe(32)

def hash_remember_token(token):
    """Hash the remember token for secure storage"""
    return hashlib.sha256(token.encode()).hexdigest()

def create_session_log(user_id, remember_me_used=False):
    """Create a session log entry for analytics"""
    with get_session() as s:
        session_log = SessionLog(
            user_id=user_id,
            ip_address=request.environ.get('REMOTE_ADDR', 'unknown'),
            user_agent=request.headers.get('User-Agent', 'unknown')[:500],
            remember_me_used=remember_me_used
        )
        s.add(session_log)
        s.commit()
        return session_log.id

def update_session_log_logout(session_log_id):
    """Update session log with logout time and duration"""
    with get_session() as s:
        session_log = s.get(SessionLog, session_log_id)
        if session_log:
            session_log.logout_time = datetime.utcnow()
            if session_log.login_time:
                duration = (session_log.logout_time - session_log.login_time).total_seconds()
                session_log.session_duration = int(duration)
            s.commit()

def regenerate_session():
    """Regenerate session ID for security"""
    # Store current session data
    user_id = session.get('user_id')
    user_email = session.get('user_email')
    session_log_id = session.get('session_log_id')
    
    # Clear and regenerate session
    session.clear()
    session.permanent = True
    
    # Restore session data
    if user_id:
        session['user_id'] = user_id
        session['user_email'] = user_email
        session['session_log_id'] = session_log_id
        session['last_activity'] = datetime.utcnow().isoformat()

def check_remember_me():
    """Check if user has valid remember me cookie"""
    remember_token = request.cookies.get('remember_token')
    if not remember_token:
        return None
    
    with get_session() as s:
        # Find user with matching hashed token
        hashed_token = hash_remember_token(remember_token)
        user = s.scalar(select(User).where(User.remember_token == hashed_token))
        if user:
            return user
    return None

@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        # Validation
        if not email or not password:
            flash('Email and password are required.', 'error')
            return render_template('auth/register.html')
        
        if password != confirm_password:
            flash('Passwords do not match.', 'error')
            return render_template('auth/register.html')
        
        if len(password) < 6:
            flash('Password must be at least 6 characters long.', 'error')
            return render_template('auth/register.html')
        
        # Check if user already exists
        with get_session() as s: #104
            existing_user = s.scalar(select(User).where(User.email == email))
            if existing_user:
                flash('Email already registered. Please login instead.', 'error')
                return render_template('auth/register.html')
            
            # Create new user
            password_hash = generate_password_hash(password)  # 111
            user = User(email=email, password_hash=password_hash)
            s.add(user)
            s.flush()  # Get the user ID
            
            # Create default league settings
            league_settings = LeagueSettings(
                user_id=user.id,
                scoring_type="PPR",
                teams=12,
                qb_slots=1,
                rb_slots=2,
                wr_slots=2,
                te_slots=1,
                flex_slots=1,
                k_slots=1,
                d_st_slots=1
            )
            s.add(league_settings)
            s.commit()
            
            flash('Account created successfully! Please login.', 'success')
            return redirect(url_for('auth.login'))
    
    return render_template('auth/register.html')

@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")  # Rate limit login attempts
def login():
    # Check for remember me cookie first
    if 'user_id' not in session:
        remembered_user = check_remember_me()
        if remembered_user:
            session['user_id'] = remembered_user.id
            session['user_email'] = remembered_user.email
            session.permanent = True
            session['last_activity'] = datetime.utcnow().isoformat()
            
            # Create session log for remember me login
            session_log_id = create_session_log(remembered_user.id, remember_me_used=True)
            session['session_log_id'] = session_log_id
            
            # Update user login stats
            with get_session() as s:
                user = s.get(User, remembered_user.id)
                user.last_login = datetime.utcnow()
                user.login_count += 1
                s.commit()
            
            flash(f'Welcome back, {remembered_user.email}!', 'success')
            # Send user to the page they were trying to access, when safe (not login/register)
            ref = request.headers.get('Referer')
            try:
                from urllib.parse import urlparse
                u = urlparse(ref or '')
                # Avoid redirecting back to login/register regardless of blueprint prefix
                login_path = url_for('auth.login')
                register_path = url_for('auth.register')
                if (
                    ref
                    and u.netloc == request.host
                    and u.path
                    and not u.path.startswith(login_path)
                    and not u.path.startswith(register_path)
                ):
                    return redirect(ref)
            except Exception:
                pass
            return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        remember_me = request.form.get('remember_me') == 'on'   #j
        
        if not email or not password:
            flash('Email and password are required.', 'error')
            return render_template('auth/login.html')
        
        with get_session() as s:
            user = s.scalar(select(User).where(User.email == email))
            if user and check_password_hash(user.password_hash, password):
                # Regenerate session for security
                regenerate_session()
                
                # Set session data
                session['user_id'] = user.id #197
                session['user_email'] = user.email
                session.permanent = True
                session['last_activity'] = datetime.utcnow().isoformat()
                
                # Create session log
                session_log_id = create_session_log(user.id, remember_me_used=remember_me)
                session['session_log_id'] = session_log_id
                
                # Update user login stats
                user.last_login = datetime.utcnow()
                user.login_count += 1
                
                # Handle remember me
                # Prefer redirecting back to the page the user came from (not login/register)
                ref = request.headers.get('Referer')
                try:
                    from urllib.parse import urlparse
                    u = urlparse(ref or '')
                    default_resp = make_response(redirect(url_for('dashboard')))
                    # Avoid redirecting back to login/register regardless of blueprint prefix
                    login_path = url_for('auth.login')
                    register_path = url_for('auth.register')
                    if (
                        ref
                        and u.netloc == request.host
                        and u.path
                        and not u.path.startswith(login_path)
                        and not u.path.startswith(register_path)
                    ):
                        default_resp = make_response(redirect(ref))
                except Exception:
                    default_resp = make_response(redirect(url_for('dashboard')))
                response = default_resp   #aa
                if remember_me:
                    remember_token = generate_remember_token()
                    hashed_token = hash_remember_token(remember_token)
                    user.remember_token = hashed_token
                    
                    # Set remember me cookie (30 days)
                    response.set_cookie(
                        'remember_token',
                        remember_token,
                        max_age=30*24*60*60,  # 30 days
                        secure=current_app.config.get('SESSION_COOKIE_SECURE', False),
                        httponly=True,
                        samesite='Lax'
                    )     #end
                
                s.commit()
                flash(f'Welcome back, {user.email}!', 'success')
                return response
            else:
                flash('Invalid email or password.', 'error')
                return render_template('auth/login.html')
    
    return render_template('auth/login.html')

@auth_bp.route('/logout')
def logout():
    # Update session log before clearing session
    session_log_id = session.get('session_log_id')
    if session_log_id:
        update_session_log_logout(session_log_id)
    
    # Clear remember me token if exists
    user_id = session.get('user_id')
    if user_id:
        with get_session() as s:
            user = s.get(User, user_id)
            if user:
                user.remember_token = None
                s.commit()
    
    # Clear session and cookie
    session.clear()
    response = make_response(redirect(url_for('home')))
    response.set_cookie('remember_token', '', expires=0)
    
    flash('You have been logged out.', 'info')
    return response

def login_required(f):
    """Decorator to require login for protected routes with sliding window timeout"""
    from functools import wraps
    
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            # Check for remember me cookie
            remembered_user = check_remember_me()
            if remembered_user:
                session['user_id'] = remembered_user.id
                session['user_email'] = remembered_user.email
                session.permanent = True
                session['last_activity'] = datetime.utcnow().isoformat()
                
                # Create session log for remember me login
                session_log_id = create_session_log(remembered_user.id, remember_me_used=True)
                session['session_log_id'] = session_log_id
                
                # Update user login stats
                with get_session() as s:
                    user = s.get(User, remembered_user.id)
                    user.last_login = datetime.utcnow()
                    user.login_count += 1
                    s.commit()
            else:
                flash('Please login to access this page.', 'error')
                return redirect(url_for('auth.login'))
        
        # Check for session timeout with sliding window
        last_activity_str = session.get('last_activity')
        if last_activity_str:
            try:
                last_activity = datetime.fromisoformat(last_activity_str)
                timeout_minutes = current_app.config.get('SESSION_TIMEOUT_MINUTES', 30)
                
                if datetime.utcnow() - last_activity > timedelta(minutes=timeout_minutes):
                    # Session expired
                    session_log_id = session.get('session_log_id')
                    if session_log_id:
                        update_session_log_logout(session_log_id)
                    
                    session.clear()
                    flash('Your session has expired. Please login again.', 'warning')
                    return redirect(url_for('auth.login'))
                else:
                    # Update last activity (sliding window)
                    session['last_activity'] = datetime.utcnow().isoformat()
            except (ValueError, TypeError):
                # Invalid timestamp, require re-login
                session.clear()
                flash('Please login to access this page.', 'error')
                return redirect(url_for('auth.login'))
        else:
            # No last activity timestamp, set it
            session['last_activity'] = datetime.utcnow().isoformat()
        
        return f(*args, **kwargs)
    return decorated_function
