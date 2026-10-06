"""
Internal web API endpoints for the study framework.
Provides dashboard functionality, user management, and data visualization.
"""

import logging
import os
import json
import time
import io
import csv
import shutil
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

from flask import Flask, request, jsonify, render_template, Response, send_from_directory, send_file, session, redirect, url_for
from flask_restful import Resource, Api
from markupsafe import Markup

from study_framework_core.core.config import get_config
from study_framework_core.core.handlers import get_db, verify_admin_login, get_available_modules
from study_framework_core.core.dashboard import DashboardBase
from study_framework_core.core import mindguard


def setup_internal_web_logging():
    """Setup logging for internal web endpoints."""
    config = get_config()
    
    logging.basicConfig(
        level=getattr(logging, config.logging.level.upper()),
        format=config.logging.format,
        filename=config.get_log_file_path('internal_web')
    )

# Create a concrete dashboard implementation for internal web
class SimpleDashboard(DashboardBase):
    def _get_custom_columns(self):
        return []
    
    def generate_custom_row_data(self, user_data, date_str):
        return dict()


class HealthCheck(Resource):
    """Health check endpoint for internal web."""
    def get(self):
        return {'status': 'healthy', 'service': 'study-framework-internal'}, 200


class SessionDebug(Resource):
    """Debug endpoint to check session state."""
    def get(self):
        # Check for session corruption and clean up if needed
        if session and not session.get('admin_logged_in') and len(session.keys()) > 0:
            logging.warning("Detected corrupted session, clearing it")
            session.clear()
        
        session_info = {
            'session_keys': list(session.keys()),
            'admin_logged_in': session.get('admin_logged_in', 'NOT_FOUND'),
            'admin_username': session.get('admin_username', 'NOT_FOUND'),
            'session_permanent': session.permanent,
            'session_modified': session.modified,
            'request_headers': dict(request.headers),
            'request_host': request.host,
            'request_scheme': request.scheme,
            'session_cookie': request.cookies.get('session', 'NOT_FOUND')
        }
        return session_info, 200


class InternalWebBase:
    """Base class for internal web functionality."""
    
    def __init__(self, app: Flask, dashboard: DashboardBase):
        self.app = app
        self.dashboard = dashboard
        self.api = Api(app, prefix='/internal_web')
        # Setup logging if not already configured
        if not logging.getLogger().handlers:
            setup_internal_web_logging()
        self.setup_routes()
        self.setup_session_config()
    
    def setup_session_config(self):
        """Setup session configuration."""
        # Use a consistent secret key to prevent session invalidation on app restarts
        # In production, this should be stored in environment variables
        self.app.secret_key = 'bean-study-internal-web-secret-key-2024'
        self.app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=15)  # Session lasts 15 minutes
        
        # Configure session security based on environment
        # In production with proper HTTPS, this should be True
        # For now, set to False to allow HTTP (since X-Forwarded-Proto is http)
        self.app.config['SESSION_COOKIE_SECURE'] = False
        
        self.app.config['SESSION_COOKIE_HTTPONLY'] = True
        self.app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
        self.app.config['SESSION_COOKIE_DOMAIN'] = None  # Allow all domains
        self.app.config['SESSION_COOKIE_PATH'] = '/'
        self.app.config['SESSION_REFRESH_EACH_REQUEST'] = True  # Refresh session on each request
        
        # Add debugging for session configuration
        logging.info(f"Session configuration:")
        logging.info(f"  SECRET_KEY: {self.app.secret_key[:10]}... (consistent)")
        logging.info(f"  SECURE: {self.app.config['SESSION_COOKIE_SECURE']}")
        logging.info(f"  HTTPONLY: {self.app.config['SESSION_COOKIE_HTTPONLY']}")
        logging.info(f"  SAMESITE: {self.app.config['SESSION_COOKIE_SAMESITE']}")
        logging.info(f"  LIFETIME: {self.app.config['PERMANENT_SESSION_LIFETIME']}")
        logging.info(f"  NOTE: SESSION_COOKIE_SECURE set to False because X-Forwarded-Proto is http")
    
    def setup_routes(self):
        """Setup internal web routes."""
        # Health check route
        self.api.add_resource(HealthCheck, '/health')
        self.api.add_resource(SessionDebug, '/session-debug')
        
        # Authentication routes
        self.api.add_resource(LoginPage, '/login')
        self.api.add_resource(LoginHandler, '/login-handler')
        self.api.add_resource(LogoutHandler, '/logout')
        self.api.add_resource(LandingPage, '/')
        
        # Dashboard routes (now require authentication)
        self.api.add_resource(ViewDashboard, '/dashboard')
        self.api.add_resource(ViewDashboardDate, '/dashboard/<date>')
        self.api.add_resource(ViewUserDetail, '/dashboard/view/<user>/<date>')
        self.api.add_resource(PatientPage, '/patient/<user>')
        self.api.add_resource(PatientData, '/patient/<user>/data')
        self.api.add_resource(ViewAnnouncement, '/dashboard/announcement')
        
        # Data download routes
        self.api.add_resource(DownloadCompliance, '/download_compliance')
        self.api.add_resource(DownloadEmaAll, '/download_ema_all')
        
        # EMA Schedule routes
        self.api.add_resource(Users, '/ema-schedule/users')
        self.api.add_resource(ReplaceEMA, '/ema-schedule/replace-ema')
        self.api.add_resource(AddUserFile, '/ema-schedule/add-user')
        self.api.add_resource(DownloadUserFile, '/ema-schedule/download/<string:user>')
        self.api.add_resource(EmaFrontend, '/ema-schedule')
        self.api.add_resource(ConfigFrontend, '/config-schedule')
        
        # User Management Routes
        self.api.add_resource(UserManagementFrontend, '/user-management')
        self.api.add_resource(CreateSingleUser, '/user-management/create-single')
        self.api.add_resource(CreateMultipleUsers, '/user-management/create-multiple')
        self.api.add_resource(GetUsers, '/user-management/users')
        self.api.add_resource(ExportUsers, '/user-management/export')
        self.api.add_resource(UpdateUserEmail, '/user-management/update-email')


def handle_response(message, status):
    """Create standardized response."""
    return {'message': message, "status": status}


def require_auth(f):
    """Decorator to require authentication for internal web endpoints."""
    def decorated_function(*args, **kwargs):
        logging.info(f"Auth check for {f.__name__} - Session keys: {list(session.keys())}")
        logging.info(f"Session admin_logged_in: {session.get('admin_logged_in', 'NOT_FOUND')}")
        logging.info(f"Session admin_username: {session.get('admin_username', 'NOT_FOUND')}")
        
        # Check if session is valid and user is logged in
        if 'admin_logged_in' not in session or not session.get('admin_logged_in'):
            logging.warning(f"User not logged in, redirecting to login from {f.__name__}")
            return redirect('/internal_web/login')
        
        # Validate session data integrity
        if 'admin_username' not in session:
            logging.warning(f"Session corrupted - missing admin_username, clearing session")
            session.clear()
            return redirect('/internal_web/login')
        
        # Refresh session on each request to prevent timeout
        session.permanent = True
        session.modified = True
        logging.info(f"Session refreshed for {f.__name__} - User: {session.get('admin_username')}")
        
        return f(*args, **kwargs)
    return decorated_function


class LoginPage(Resource):
    """Login page for internal web access."""
    def get(self):
        if 'admin_logged_in' in session:
            return redirect('/internal_web/')
        
        return Response(
            render_template('login.html'),
            mimetype='text/html'
        )


class LoginHandler(Resource):
    """Handle login form submission."""
    def post(self):
        try:
            data = request.get_json()
            username = data.get('username')
            password = data.get('password')
            
            if not username or not password:
                return {'success': False, 'error': 'Username and password are required'}, 400
            
            # Verify credentials
            result = verify_admin_login(username, password)
            
            if result['success']:
                session['admin_logged_in'] = True
                session['admin_username'] = username
                session.permanent = True  # Make session permanent
                session.modified = True
                
                logging.info(f"Login successful for {username}")
                logging.info(f"Session after login: {dict(session)}")
                logging.info(f"Request headers: {dict(request.headers)}")
                
                return {'success': True, 'redirect': '/internal_web/'}, 200
            else:
                return {'success': False, 'error': result['error']}, 401
                
        except Exception as e:
            logging.error(f"Login error: {e}")
            return {'success': False, 'error': 'Login failed'}, 500


class LogoutHandler(Resource):
    """Handle logout."""
    def get(self):
        session.clear()
        return redirect('/internal_web/login')


class LandingPage(Resource):
    """Landing page showing all available modules."""
    def get(self):
        if 'admin_logged_in' not in session:
            return redirect('/internal_web/login')
        
        modules = get_available_modules()
        
        return Response(
            render_template('landing_page.html', modules=modules, username=session.get('admin_username')),
            mimetype='text/html'
        )


def dashboard_participants(db, config, date_timestamp):
    """Every participant, so one with no data yet still shows (as zeros) instead of vanishing."""
    uids = {u['uid'] for u in db[config.collections.USERS].find({}, {'uid': 1}) if u.get('uid')}
    uids |= {s['uid'] for s in db[config.collections.DAILY_SUMMARY].find({'date': date_timestamp}, {'uid': 1}) if s.get('uid')}
    return [{'uid': uid} for uid in sorted(uids)]


class ViewDashboard(Resource):
    """View dashboard for a specific date."""
    def get(self):
        if 'admin_logged_in' not in session:
            return redirect('/internal_web/login')
        
        # Refresh session on each request
        session.permanent = True
        session.modified = True
        
        logging.info(f"ViewDashboard accessed by {session.get('admin_username')}")
        
        # Today in the study's time zone (the server runs on UTC, which is already tomorrow in the evening)
        today_date = datetime.now(ZoneInfo(mindguard.TIMEZONE)).date()
        date_str = today_date.strftime("%m-%d-%y")
        
        # Get users who have daily summaries for this date
        db = get_db()
        config = get_config()
        
        # Convert date to timestamp
        date_timestamp = int(datetime.strptime(date_str, "%m-%d-%y").timestamp())
        
        # Find users who have daily summaries for this date
        users_with_data = dashboard_participants(db, config, date_timestamp)
        
        logging.info(f"Found {len(users_with_data)} users with daily summaries for {date_str}")
        
        # Generate dashboard context
        dashboard = SimpleDashboard()
        context = dashboard.get_template_context(None, date_str, users_with_data)  # No token needed
        
        return Response(
            render_template('dashboard_base.html', **context),
            mimetype='text/html'
        )


class ViewDashboardDate(Resource):
    """View dashboard for a specific date."""
    def get(self, date):
        if 'admin_logged_in' not in session:
            return redirect('/internal_web/login')
        
        # Refresh session on each request
        session.permanent = True
        session.modified = True
        
        logging.info(f"ViewDashboardDate accessed by {session.get('admin_username')}, date: {date}")
        
        try:
            # Parse date
            request_date = datetime.strptime(date, "%m-%d-%y").date()
            date_str = request_date.strftime("%m-%d-%y")
            
            # Get users who have daily summaries for this date
            db = get_db()
            config = get_config()
            
            # Convert date to timestamp
            date_timestamp = int(datetime.strptime(date_str, "%m-%d-%y").timestamp())
            
            # Find users who have daily summaries for this date
            users_with_data = dashboard_participants(db, config, date_timestamp)
            
            logging.info(f"Found {len(users_with_data)} users with daily summaries for {date_str}")
            
            # Generate dashboard context
            dashboard = SimpleDashboard()
            context = dashboard.get_template_context(None, date_str, users_with_data)  # No token needed
            
            return Response(
                render_template('dashboard_base.html', **context),
                mimetype='text/html'
            )
            
        except ValueError as e:
            logging.error(f"Error parsing date: {e}")
            return {"message": "Bad request", "status": 400}, 400


class ViewUserDetail(Resource):
    """View detailed user information."""
    def get(self, user, date):
        logging.info(f"ViewUserDetail accessed - Session: {dict(session)}")
        if 'admin_logged_in' not in session:
            logging.warning(f"User not logged in, redirecting to login. Session keys: {list(session.keys())}")
            return redirect('/internal_web/login')
        
        config = get_config()
        
        try:
            current_date = datetime.strptime(date, "%m-%d-%y")
            previous_date = (current_date - timedelta(days=1)).strftime("%m-%d-%y")
            next_date = (current_date + timedelta(days=1)).strftime("%m-%d-%y")
            
            # Generate plots on-the-fly
            from study_framework_core.core.processing_scripts import DataProcessor
            processor = DataProcessor()
            plots = processor.generate_user_plots(user, date)
            
            # Log plot generation results for debugging
            logging.info(f"Generated plots for {user} on {date}: {list(plots.keys())}")
            logging.info(f"Daily plot length: {len(plots.get('daily_plot', ''))}")
            logging.info(f"Weekly trends length: {len(plots.get('weekly_trends', ''))}")
            
            # Combine plots into a single HTML content
            plot_content = f"""
            <div class="plot-section mb-4">
                <h3 class="section-title">
                    <i class="fas fa-chart-line me-2"></i>Daily Activity
                </h3>
                <div class="plot-container">
                    {plots.get('daily_plot', '<p>No daily data available</p>')}
                </div>
            </div>
            
            <div class="plot-section">
                <h3 class="section-title">
                    <i class="fas fa-chart-bar me-2"></i>Weekly Trends
                </h3>
                <div class="plot-container">
                    {plots.get('weekly_trends', '<p>No weekly data available</p>')}
                </div>
            </div>
            """
            
            return Response(
                render_template('user_detail.html', 
                              user=user, date=date,
                              plot_content=Markup(plot_content), 
                              mindguard_checkins=mindguard.checkins_for_day(
                                  get_db(), user, current_date.strftime("%Y-%m-%d")),
                              previous_date=previous_date,
                              next_date=next_date), 
                mimetype='text/html'
            )
                
        except ValueError as e:
            return {"message": "Invalid date format", "status": 400}, 400
        except Exception as e:
            logging.error(f"Error generating plots for {user} on {date}: {e}")
            return {"message": f"Error generating plots: {str(e)}", "status": 500}, 500





class ViewAnnouncement(Resource):
    """View and manage announcements."""
    def get(self):
        if 'admin_logged_in' not in session:
            return redirect('/internal_web/login')
        
        logging.info(f"ViewAnnouncement accessed by {session.get('admin_username')}")
        
        # Get users from database
        db = get_db()
        users = db['users'].find()
        
        template = render_template("user_announcement.html", token=token, users=users)
        return Response(template, mimetype="text/html")
    
    def post(self):
        if 'admin_logged_in' not in session:
            return redirect('/internal_web/login')
        
        logging.info(f"ViewAnnouncement accessed using post by {session.get('admin_username')}")
        
        config = get_config()
        data = request.json
        if data['pass_key'] == config.security.announcement_pass_key:
            selected_users = data['selected_users']
            announcement = data['announcement']
            alert_message = data['alert_message']
            debug_device = data.get('debug_device', False)
            
            # Get users from database
            db = get_db()
            users_collection = db['users']
            
            response_data = {}
            for user_uid in selected_users:
                user_info = users_collection.find_one({"uid": user_uid})
                if user_info:
                    push_token = user_info.get('push_token', '')
                    try:
                        # Generate announcement (placeholder)
                        logging.info(f"Generated announcement for user {user_uid}: {alert_message}")
                        response_data[user_uid] = "success"
                    except Exception as e:
                        logging.error(f"Failed to generate announcement for user {user_uid}: {e}")
                        response_data[user_uid] = "failure"
            
            return handle_response(response_data, 200), 200
        else:
            return handle_response({"error": "wrong passkey"}, 403), 403


class DownloadCompliance(Resource):
    """Download compliance data as CSV."""
    def get(self):
        if 'admin_logged_in' not in session:
            return redirect('/internal_web/login')
        
        logging.info(f"Accessing Download Page by {session.get('admin_username')}")
        template = render_template("compliance_downloader.html")
        return Response(template, mimetype="text/html")
    
    def post(self):
        logging.info("Accessing Download csv with request")
        data = request.json
        
        start_date = data['start_date']
        end_date = data['end_date']
        type = data['type']
        
        # Get database
        db = get_db()
        
        # Convert date strings to Unix timestamps
        start_time_stamp = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp())
        end_time_stamp = int(datetime.strptime(end_date, "%Y-%m-%d").timestamp())
        
        # Fetch records from MongoDB
        config = get_config()
        records = db[config.collections.DAILY_SUMMARY].find({'date': {'$gte': start_time_stamp, '$lte': end_time_stamp}})
        
        if type == 'aggregation':
            # Aggregate metrics per user
            user_metrics = defaultdict(lambda: {
                'total_distance': 0,
                'total_garmin_on': 0,
                'total_phone_on': 0,
                'total_garmin_worn': 0,
                'total_days': 0
            })
            
            for record in records:
                uid = record.get('uid', 'unknown')
                user_metrics[uid]['total_distance'] += record.get('distance', 0)
                user_metrics[uid]['total_garmin_on'] += record.get('garmin_on_duration', 0)
                
                # Get phone duration from nested location object
                location_data = record.get('location', {})
                phone_duration = location_data.get('duration_hours', 0.0)
                user_metrics[uid]['total_phone_on'] += phone_duration
                
                user_metrics[uid]['total_garmin_worn'] += record.get('garmin_wear_duration', 0)
                user_metrics[uid]['total_days'] += 1
            
            # Prepare CSV
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow([
                'User ID', 'Total Distance', 'Total Garmin On', 'Total Phone On', 'Total Garmin Worn',
                'Total Days'
            ])
            
            for uid, metrics in user_metrics.items():
                writer.writerow([
                    uid,
                    metrics['total_distance'],
                    metrics['total_garmin_on'],
                    metrics['total_phone_on'],
                    metrics['total_garmin_worn'],
                    metrics['total_days']
                ])
            
            output.seek(0)
            filename = f"aggregated_compliance_{start_date}_to_{end_date}.csv"
            
            return Response(
                output,
                mimetype="text/csv",
                headers={"Content-Disposition": f"attachment;filename={filename}"}
            )
        
        elif type == 'daily':
            # Daily records
            records = db[config.collections.DAILY_SUMMARY].find({'date': {'$gte': start_time_stamp, '$lte': end_time_stamp}}).sort("uid", 1)
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow([
                'User ID', 'Date', 'Distance', 'Garmin On', 'Phone On', 'Garmin Worn'
            ])
            
            for record in records:
                uid = record.get('uid', 'unknown')
                distance = record.get('distance', 0)
                garmin_on = record.get('garmin_on_duration', 0)
                
                # Get phone duration from nested location object
                location_data = record.get('location', {})
                phone_on = location_data.get('duration_hours', 0.0)
                
                garmin_worn = record.get('garmin_wear_duration', 0)
                date = datetime.fromtimestamp(record['date']).strftime("%Y-%m-%d")
                
                writer.writerow([
                    uid, date, distance, garmin_on, phone_on, garmin_worn
                ])
            
            output.seek(0)
            filename = f"daily_compliance_{start_date}_to_{end_date}.csv"
            
            return Response(
                output,
                mimetype="text/csv",
                headers={"Content-Disposition": f"attachment;filename={filename}"}
            )


class Users(Resource):
    """Get list of users with EMA status."""
    def get(self):
        if 'admin_logged_in' not in session:
            return redirect('/internal_web/login')
        
        config = get_config()
        users = []
        
        for user in os.listdir(config.paths.ema_file_path):
            user_path = os.path.join(config.paths.ema_file_path, user)
            ema_path = os.path.join(user_path, 'ema.json')
            
            if os.path.isdir(user_path):
                try:
                    with open(ema_path, 'r') as f:
                        data = json.load(f)
                        is_active = bool(data)
                except (json.JSONDecodeError, FileNotFoundError):
                    is_active = False
                
                users.append({'name': user, 'status': 'active' if is_active else 'inactive'})
        
        return jsonify(users)


class ReplaceEMA(Resource):
    """Replace EMA file for a user."""
    def post(self):
        if 'admin_logged_in' not in session:
            return redirect('/internal_web/login')
        
        config = get_config()
        user = request.form['user']
        file = request.files['file']
        password = request.form.get('password')
        
        if password != config.security.announcement_pass_key:
            return jsonify({'error': 'Invalid password'}), 403
        
        user_path = os.path.join(config.paths.ema_file_path, user)
        if os.path.exists(user_path):
            file.save(os.path.join(user_path, 'ema.json'))
            return jsonify({'message': 'EMA file replaced successfully!'})
        
        return jsonify({'error': 'User does not exist'}), 404


class AddUser(Resource):
    """Add a new user."""
    def post(self):
        if 'admin_logged_in' not in session:
            return redirect('/internal_web/login')
        
        config = get_config()
        user = request.form['user']
        file = request.files['file']
        password = request.form.get('password')
        
        if password != config.security.announcement_pass_key:
            return jsonify({'error': 'Invalid password'}), 403
        
        user_path = os.path.join(config.paths.ema_file_path, user)
        if not os.path.exists(user_path):
            os.makedirs(user_path)
            file.save(os.path.join(user_path, 'ema.json'))
            return jsonify({'message': 'User added successfully!'})
        
        return jsonify({'error': 'User already exists'}), 400


class DownloadEMA(Resource):
    """Download EMA file for a user."""
    def get(self, user):
        if 'admin_logged_in' not in session:
            return redirect('/internal_web/login')
        
        config = get_config()
        user_path = os.path.join(config.paths.ema_file_path, user)
        
        if os.path.exists(user_path):
            return send_from_directory(user_path, 'ema.json', as_attachment=True)
        
        return jsonify({'error': 'User does not exist'}), 404


class EmaFrontend(Resource):
    """EMA schedule frontend."""
    def get(self):
        if 'admin_logged_in' not in session:
            return redirect('/internal_web/login')
        template = render_template("ema-schedule.html")
        return Response(template, mimetype="text/html")


class ConfigFrontend(Resource):
    """Config schedule frontend."""
    def get(self):
        if 'admin_logged_in' not in session:
            return redirect('/internal_web/login')
        
        template = render_template("config-schedule.html")
        return Response(template, mimetype="text/html")


class DownloadEmaAll(Resource):
    """Download all EMA data with authentication."""
    def get(self):
        if 'admin_logged_in' not in session:
            return redirect('/internal_web/login')
        
        logging.info(f"Accessing EMA download by {session.get('admin_username')}")
        template = render_template("ema_downloader.html")
        return Response(template, mimetype="text/html")

    def post(self):
        VALID_USERNAME = "connect_ra"
        VALID_PASSWORD = "connect123"
        data = request.get_json()
        username = data.get("username")
        password = data.get("password")
        daily_diaries = data.get("daily_diary", False)
        start_date_str = data.get("start_date")
        end_date_str = data.get("end_date")

        if username != VALID_USERNAME or password != VALID_PASSWORD:
            logging.warning("Unauthorized access attempt")
            return {"message": "Invalid credentials"}, 401

        # Convert the start and end date from string to datetime objects
        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
        except ValueError:
            return {"message": "Invalid date format. Please use YYYY-MM-DD."}, 400

        logging.info(f"Authorized access to EMA data with date range: {start_date} to {end_date}")

        # Get configuration
        config = get_config()
        zip_file_path = os.path.join(config.paths.base_dir, "extras", "secure_data.zip")
        folder_to_zip = config.paths.active_sensing_upload_path

        # Create extras directory if it doesn't exist
        os.makedirs(os.path.dirname(zip_file_path), exist_ok=True)

        if daily_diaries:
            if os.path.exists(zip_file_path):
                os.remove(zip_file_path)

            # Create a temporary directory to store filtered files
            temp_dir = os.path.join(config.paths.base_dir, "extras", "temp_filtered_data")
            os.makedirs(temp_dir, exist_ok=True)

            # Walk through the folder and filter files based on creation date
            for root, dirs, files in os.walk(folder_to_zip):
                for file in files:
                    file_path = os.path.join(root, file)
                    # Get the last modification time of the file
                    file_mod_time = datetime.fromtimestamp(os.path.getmtime(file_path))

                    # Check if the file's modification time is within the date range
                    if start_date <= file_mod_time <= end_date:
                        # Maintain the folder structure in the temp directory
                        relative_path = os.path.relpath(root, folder_to_zip)
                        destination_path = os.path.join(temp_dir, relative_path)

                        # Ensure the destination path exists before copying
                        os.makedirs(destination_path, exist_ok=True)

                        # Copy the filtered file to the temporary directory
                        shutil.copy2(file_path, destination_path)

            # Create a zip file of the filtered data maintaining folder structure
            shutil.make_archive(zip_file_path.replace('.zip', ''), 'zip', temp_dir)

            # Clean up the temporary directory
            shutil.rmtree(temp_dir)
            logging.info(f"Sending diaries along with EMAs")

            return send_file(zip_file_path, as_attachment=True, download_name="secure_data.zip")
        else:
            temp_dir = os.path.join(config.paths.base_dir, "extras", "temp_answers")
            os.makedirs(temp_dir, exist_ok=True)

            # Walk through the main folder and collect 'ema_responses' folders
            for root, dirs, files in os.walk(folder_to_zip):
                if "ema_responses" in dirs:  # Look for the 'ema_responses' folder
                    answers_folder = os.path.join(root, "ema_responses")
                    # Determine the relative path from the base folder to preserve the structure
                    relative_path = os.path.relpath(root, folder_to_zip)
                    destination_path = os.path.join(temp_dir, relative_path, "ema_responses")

                    # Ensure the destination path exists before copying
                    os.makedirs(os.path.dirname(destination_path), exist_ok=True)

                    # Copy the 'ema_responses' folder to the new destination
                    shutil.copytree(answers_folder, destination_path)

            # Create a zip file of the temp directory maintaining folder structure
            shutil.make_archive(zip_file_path.replace('.zip', ''), 'zip', temp_dir)

            # Clean up the temporary directory
            shutil.rmtree(temp_dir)
            logging.info(f"Sending only EMAs")
            return send_file(zip_file_path, as_attachment=True, download_name="secure_data.zip")


class UserManagementFrontend(Resource):
    """User management frontend."""
    def get(self):
        if 'admin_logged_in' not in session:
            return redirect('/internal_web/login')
        
        template = render_template("user_management.html")
        return Response(template, mimetype="text/html")


class CreateSingleUser(Resource):
    """Create a single participant."""
    def post(self):
        if 'admin_logged_in' not in session:
            return redirect('/internal_web/login')
        
        try:
            data = request.get_json()
            
            # Validate required fields
            uid = data.get('uid')
            if not uid:
                return {'success': False, 'error': 'UID is required'}, 400
            
            email = data.get('email')
            study_id = data.get('study_id', 'A1')
            
            # Create participant
            from study_framework_core.core.handlers import create_user
            result = create_user(uid, email, study_id)
            
            if result['success']:
                return result, 200
            else:
                return result, 400
                
        except Exception as e:
            logging.error(f"Error creating single participant: {e}")
            return {'success': False, 'error': str(e)}, 500


class CreateMultipleUsers(Resource):
    """Create multiple participants with sequential IDs."""
    def post(self):
        if 'admin_logged_in' not in session:
            return redirect('/internal_web/login')
        
        try:
            data = request.get_json()
            
            # Validate required fields
            user_prefix = data.get('user_prefix')
            num_users = data.get('num_users')
            
            if not user_prefix or not num_users:
                return {'success': False, 'error': 'User prefix and number of users are required'}, 400
            
            if not isinstance(num_users, int) or num_users <= 0 or num_users > 100:
                return {'success': False, 'error': 'Number of users must be between 1 and 100'}, 400
            
            start_id = data.get('start_id', 1)
            email_base = data.get('email_base')
            study_id = data.get('study_id', 'A1')
            
            # Create multiple participants
            from study_framework_core.core.handlers import create_multiple_users
            result = create_multiple_users(user_prefix, num_users, start_id, email_base, study_id)
            
            if result['success']:
                return result, 200
            else:
                return result, 400
                
        except Exception as e:
            logging.error(f"Error creating multiple participants: {e}")
            return {'success': False, 'error': str(e)}, 500


class GetUsers(Resource):
    """Get all participants."""
    def get(self):
        if 'admin_logged_in' not in session:
            return redirect('/internal_web/login')
        
        try:
            from study_framework_core.core.handlers import get_all_users
            result = get_all_users()
            
            if result['success']:
                return result, 200
            else:
                return result, 400
                
        except Exception as e:
            logging.error(f"Error getting participants: {e}")
            return {'success': False, 'error': str(e)}, 500


class UpdateUserEmail(Resource):
    """Set or change a participant's email; Mind Guard matches check-ins to participants by it."""
    def post(self):
        if 'admin_logged_in' not in session:
            return login_redirect()
        data = request.get_json(silent=True) or {}
        uid = data.get('uid')
        email = (data.get('email') or '').strip()
        if not uid:
            return {'success': False, 'error': 'UID is required'}, 400
        if email and ('@' not in email or len(email) > 254):
            return {'success': False, 'error': 'Not an email address'}, 400
        config = get_config()
        db = get_db()
        if email and db[config.collections.USERS].find_one(
                {'uid': {'$ne': uid}, 'email': {'$regex': f'^{re.escape(email)}$', '$options': 'i'}}):
            return {'success': False, 'error': 'Another participant already has this email'}, 400
        update = {'$set': {'email': email}} if email else {'$unset': {'email': ''}}
        result = db[config.collections.USERS].update_one({'uid': uid}, update)
        if result.matched_count == 0:
            return {'success': False, 'error': f'No participant {uid}'}, 404
        # Check-ins that arrived before the email was set are linked now rather than on their next post.
        if email:
            db[mindguard.COLLECTION].update_many(
                {'uid': None, 'email': email.lower()}, {'$set': {'uid': uid}})
        return {'success': True, 'uid': uid, 'email': email or None}, 200


class PatientPage(Resource):
    """/internal_web/patient/<uid>[?date=YYYY-MM-DD]: one participant's day, wearable data beside Mind Guard.

    The page asks /patient/<uid>/data for the day in the viewer's own time zone, so the day and every
    time shown follow the browser, not the server.
    """
    def get(self, user):
        if 'admin_logged_in' not in session:
            return redirect('/internal_web/login')
        date = request.args.get('date', '')
        if date:
            try:
                datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                return {"message": "date must be YYYY-MM-DD", "status": 400}, 400
        config = get_config()
        db = get_db()
        if not db[config.collections.USERS].find_one({'uid': user}, {'_id': 1}):
            return {"message": f"No participant {user}", "status": 404}, 404
        participants = sorted(u['uid'] for u in db[config.collections.USERS].find({}, {'uid': 1}) if u.get('uid'))
        return Response(render_template('patient.html', uid=user, initial_date=date, participants=participants),
                        mimetype='text/html')


# Garmin collections shown on the patient page: (collection, value field, label, unit)
PATIENT_SERIES = [
    ('GARMIN_HR', 'heart_rate', 'Heart rate', 'bpm'),
    ('GARMIN_STRESS', 'heart_rate', 'Stress', 'score'),        # stress score is stored under heart_rate
    ('GARMIN_RESPIRATION', 'respiration', 'Respiration', 'brpm'),
    ('GARMIN_STEPS', 'steps', 'Steps', 'steps'),
]


def _epoch_seconds(ts):
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return None
    return ts / 1000 if ts > 1e11 else ts       # some sources write milliseconds


class PatientData(Resource):
    """GET /internal_web/patient/<uid>/data?start=<ISO>&end=<ISO>: everything the patient page shows for
    [start, end), the viewer's local day sent as UTC instants."""
    def get(self, user):
        if 'admin_logged_in' not in session:
            return login_redirect()
        try:
            start = datetime.fromisoformat(request.args['start'].replace('Z', '+00:00'))
            end = datetime.fromisoformat(request.args['end'].replace('Z', '+00:00'))
        except (KeyError, ValueError):
            return {'success': False, 'error': 'start and end must be ISO 8601 instants'}, 400
        if start.tzinfo is None or end.tzinfo is None or not (timedelta(0) < end - start <= timedelta(days=2)):
            return {'success': False, 'error': 'start/end must carry a time zone and span at most 2 days'}, 400
        config = get_config()
        db = get_db()
        profile = db[config.collections.USERS].find_one({'uid': user}, {'_id': 0, 'uid': 1, 'email': 1, 'created_at': 1})
        if not profile:
            return {'success': False, 'error': f'No participant {user}'}, 404
        code = db[config.collections.USER_CODE_MAPPINGS].find_one({'uid': user}, {'_id': 0, 'uid_code': 1})
        ping = db[config.collections.USER_PINGS].find_one({'uid': user}, {'_id': 0}, sort=[('_id', -1)])
        lo, hi = start.timestamp(), end.timestamp()
        series = []
        for coll, field, label, unit in PATIENT_SERIES:
            # Timestamps may be seconds or milliseconds; query both ranges.
            docs = db[getattr(config.collections, coll)].find(
                {'uid': user, '$or': [{'timestamp': {'$gte': lo, '$lt': hi}},
                                      {'timestamp': {'$gte': lo * 1000, '$lt': hi * 1000}}]},
                {'_id': 0, 'timestamp': 1, field: 1}).sort('timestamp', 1).limit(20000)
            points = [[_epoch_seconds(d.get('timestamp')), d.get(field)] for d in docs]
            series.append({'key': coll.lower(), 'label': label, 'unit': unit,
                           'points': [p for p in points if p[0] is not None and p[1] is not None]})
        start_iso = start.astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        end_iso = end.astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        return {
            'success': True,
            'participant': {
                'uid': profile['uid'],
                'email': profile.get('email'),
                'uid_code': (code or {}).get('uid_code'),
                'created_at': _epoch_seconds(profile.get('created_at')),
                'last_phone_contact': _epoch_seconds((ping or {}).get('timestamp') or (ping or {}).get('time')),
            },
            'series': series,
            'checkins': mindguard.checkins_between(db, user, start_iso, end_iso),
        }, 200


class ExportUsers(Resource):
    """Export participants to CSV."""
    def get(self):
        if 'admin_logged_in' not in session:
            return redirect('/internal_web/login')
        
        try:
            from study_framework_core.core.handlers import export_users_csv
            result = export_users_csv()
            
            if result['success']:
                return Response(
                    result['csv_content'],
                    mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment;filename={result['filename']}"}
                )
            else:
                return result, 400
                
        except Exception as e:
            logging.error(f"Error exporting participants: {e}")
            return {'success': False, 'error': str(e)}, 500


class ReplaceEMA(Resource):
    """Replace EMA file for a user."""
    def __init__(self, ema_type: str = "ema"):
        self.ema_type = ema_type
        self.config = get_config()
        self.db = get_db()
        
    def post(self):
        if 'admin_logged_in' not in session:
            return redirect('/internal_web/login')
        
        try:
            user = request.form['user']
            file = request.files['file']
            password = request.form.get('password')
            
            # Verify password
            if password != self.config.security.announcement_pass_key:
                return jsonify({'error': 'Invalid password'}), 403
            
            # Determine base directory and file name
            if self.ema_type == "ema":
                base_dir = self.config.paths.ema_file_path
                file_name = 'ema.json'
            else:
                base_dir = self.config.paths.config_dir
                file_name = 'config.json'
            
            # Create user directory if it doesn't exist
            user_path = os.path.join(base_dir, user)
            os.makedirs(user_path, exist_ok=True)
            
            # Save the file
            file_path = os.path.join(user_path, file_name)
            file.save(file_path)
            
            logging.info(f"Replaced {self.ema_type} file for user {user}")
            return jsonify({'message': f'{self.ema_type.upper()} file replaced successfully!'})
            
        except Exception as e:
            logging.error(f"Error replacing {self.ema_type} file: {e}")
            return jsonify({'error': f'Failed to replace {self.ema_type} file'}), 500


class AddUserFile(Resource):
    """Add a new user with EMA or config file."""
    def __init__(self, ema_type: str = "ema"):
        self.ema_type = ema_type
        self.config = get_config()
        self.db = get_db()
        
    def post(self):
        if 'admin_logged_in' not in session:
            return redirect('/internal_web/login')
        
        try:
            user = request.form['user']
            file = request.files['file']
            password = request.form.get('password')
            
            # Verify password
            if password != self.config.security.announcement_pass_key:
                return jsonify({'error': 'Invalid password'}), 403
            
            # Determine base directory and file name
            if self.ema_type == "ema":
                base_dir = self.config.paths.ema_file_path
                file_name = 'ema.json'
            else:
                base_dir = self.config.paths.config_dir
                file_name = 'config.json'
            
            # Check if user directory already exists
            user_path = os.path.join(base_dir, user)
            if os.path.exists(user_path):
                return jsonify({'error': 'User already exists'}), 400
            
            # Create user directory and save file
            os.makedirs(user_path, exist_ok=True)
            file_path = os.path.join(user_path, file_name)
            file.save(file_path)
            
            logging.info(f"Added {self.ema_type} file for new user {user}")
            return jsonify({'message': f'User added successfully with {self.ema_type} file!'})
            
        except Exception as e:
            logging.error(f"Error adding user with {self.ema_type} file: {e}")
            return jsonify({'error': f'Failed to add user with {self.ema_type} file'}), 500


class DownloadUserFile(Resource):
    """Download EMA or config file for a user."""
    def __init__(self, ema_type: str = "ema"):
        self.ema_type = ema_type
        self.config = get_config()
        self.db = get_db()
        
    def get(self, user):
        if 'admin_logged_in' not in session:
            return redirect('/internal_web/login')
        
        try:
            # Determine base directory and file name
            if self.ema_type == "ema":
                base_dir = self.config.paths.ema_file_path
                file_name = 'ema.json'
            else:
                base_dir = self.config.paths.config_dir
                file_name = 'config.json'
            
            user_path = os.path.join(base_dir, user)
            file_path = os.path.join(user_path, file_name)
            
            if os.path.exists(file_path):
                return send_from_directory(user_path, file_name, as_attachment=True)
            else:
                return jsonify({'error': 'User file does not exist'}), 404
                
        except Exception as e:
            logging.error(f"Error downloading {self.ema_type} file for user {user}: {e}")
            return jsonify({'error': f'Failed to download {self.ema_type} file'}), 500
