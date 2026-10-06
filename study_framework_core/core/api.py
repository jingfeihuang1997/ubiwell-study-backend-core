"""
Base API classes for the study framework.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from flask import Flask, request, jsonify, send_file
from flask_restful import Resource, Api
from marshmallow import ValidationError
import logging
import os
import time
import json
from datetime import datetime
from werkzeug.utils import secure_filename

# Import core handlers and schemas
from study_framework_core.core.handlers import (
    login_check, login_code_check, save_info, save_user_ping,
    check_end_date, save_file, save_logfile, allowed_file
)
from study_framework_core.core.mindguard import MindGuardCheckin
from study_framework_core.core.schemas import (
    LoginSchema, LoginCodeSchema, UserInfoSchema, UserPingSchema
)


def setup_api_logging():
    """Setup logging for API endpoints."""
    from study_framework_core.core.config import get_config
    config = get_config()
    
    logging.basicConfig(
        level=getattr(logging, config.logging.level.upper()),
        format=config.logging.format,
        filename=config.get_log_file_path('api')
    )

def handle_response(message, status):
    """Create standardized response object."""
    response_obj = dict()
    response_obj['message'] = message
    response_obj['status'] = status
    return response_obj


class APIBase(ABC):
    """
    Base class for API functionality.
    
    This class defines the interface that all study-specific APIs must implement.
    """
    
    def __init__(self, app: Flask):
        self.app = app
        self.api = Api(app, prefix='/api/v1')
        self.setup_routes()
    
    @abstractmethod
    def setup_routes(self):
        """Setup API routes. Override in study-specific implementations."""
        pass
    
    @abstractmethod
    def verify_user_login(self, credentials: Dict[str, Any]) -> Dict[str, Any]:
        """
        Verify user login credentials.
        
        Args:
            credentials: User credentials
            
        Returns:
            Dictionary with verification result
        """
        pass
    
    @abstractmethod
    def upload_phone_logs(self, user_id: str, logs: Dict[str, Any]) -> Dict[str, Any]:
        """
        Upload phone logs for a user.
        
        Args:
            user_id: User identifier
            logs: Phone logs data
            
        Returns:
            Dictionary with upload result
        """
        pass
    
    @abstractmethod
    def upload_phone_data(self, user_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Upload phone data for a user.
        
        Args:
            user_id: User identifier
            data: Phone data
            
        Returns:
            Dictionary with upload result
        """
        pass


class CoreAPIEndpoints:
    """
    Core API endpoints that are common across all studies.
    
    These endpoints provide standard functionality for user authentication,
    data upload, and status checking.
    """
    
    def __init__(self, api: Api, auth_key: str):
        self.api = api
        self.auth_key = auth_key
        # Setup logging if not already configured
        if not logging.getLogger().handlers:
            setup_api_logging()
        self.setup_core_routes()
    
    def setup_core_routes(self):
        """Setup core API routes that are common to all studies."""
        self.api.add_resource(Default, '/')
        self.api.add_resource(HealthCheck, '/health')
        self.api.add_resource(Login, '/credentials/check')
        self.api.add_resource(LoginCode, '/credentials/checkCode')
        self.api.add_resource(UserInfo, '/user/info/update')
        self.api.add_resource(PhonePing, '/user/status/ping')
        self.api.add_resource(UploadFile, '/data/upload')
        self.api.add_resource(UploadLogFile, '/data/uploadLog')
        self.api.add_resource(UploadDailyDiary, '/data/daily-diary')
        self.api.add_resource(UploadEma, '/data/ema-response')
        self.api.add_resource(RequestEmaFile, '/data/ema-request')
        self.api.add_resource(UploadJSON, '/upload-news/')
        self.api.add_resource(MindGuardCheckin, '/mindguard/checkin')
    
    def handle_response(self, data: Any, status_code: int) -> Dict[str, Any]:
        """Standard response handler."""
        return {
            'status': 'success' if status_code < 400 else 'error',
            'code': status_code,
            'data': data
        }


class Default(Resource):
    """Default API endpoint."""
    def get(self):
        return {'message': 'Study Framework API', 'version': '1.0.0'}, 200


class HealthCheck(Resource):
    """Health check endpoint for API."""
    def get(self):
        return {'status': 'healthy', 'service': 'study-framework-api'}, 200


class Login(Resource):
    """User login verification endpoint."""
    def get(self):
        return handle_response('Bad Request. Use POST', 405), 405

    def post(self):
        code = 403
        resp = handle_response('not permitted to make API Calls', code)
        try:
            # Get configuration
            from study_framework_core.core.config import get_config
            config = get_config()
            
            # Validate request schema
            reques = LoginSchema().load(request.json)
            uid = str(request.json['uid'])
            password = str(request.json['password'])
            device = str(request.json['device'])
            
            if 'auth_key' in request.json:
                if request.json['auth_key'] == config.security.auth_key:
                    # Check for special test user
                    if uid.__contains__('varunm'):
                        code = 200
                        resp = handle_response({'info': 'user_handler success', 'uid': request.json['uid']}, code)
                    else:
                        # Real login check
                        login_type = login_check(uid, password, device)
                        if login_type == 1:
                            code = 200
                            resp = handle_response(
                                {'info': 'user_handler success', 'uid': request.json['uid']}, code)
                        elif login_type == 2:
                            code = 200
                            resp = handle_response(
                                {'info': 'user_handler success', 'uid': request.json['uid']}, code)
                        else:
                            code = 401
                            resp = handle_response({'info': 'user_handler failed', 'uid': request.json['uid']}, code)
        except ValidationError as err:
            code = 400
            resp = handle_response(err.messages, code)
        except Exception as e:
            code = 500
            resp = handle_response(f'Login failed: {str(e)}', code)
        return resp, code


class LoginCode(Resource):
    """User login code verification endpoint."""
    def get(self):
        return handle_response('Bad Request. Use POST', 405), 405

    def post(self):
        logging.info("LoginCode fetching config")
        code = 403
        entry_code = ""
        entry_uid = ""

        resp = handle_response('not permitted to make API Calls', code)
        try:
            # Get configuration
            from study_framework_core.core.config import get_config
            config = get_config()
            
            logging.info(request.json)
            reques = LoginCodeSchema().load(request.json)
            if 'code' in request.json:
                entry_code = str(request.json['code'])
            if "uid" in request.json:
                entry_uid = str(request.json['uid'])

            device = str(request.json['device'])
            if 'auth_key' in request.json:
                if request.json['auth_key'] == config.security.auth_key:
                    if (entry_code == "" ):
                        uid = entry_uid
                        login_type = 1
                    else:
                        login_type, uid = login_code_check(entry_code, device)

                    logging.info(login_type)
                    logging.info(uid)

                    if login_type == 1:
                        code = 200
                        # Load user-specific config file
                        config_file_path = os.path.join(config.paths.base_dir, "config-files/")

                        file_path = config_file_path + str(uid) + '/' + "config.json"
                        if not os.path.exists(file_path):
                            file_path = config_file_path + "global" + '/' + "config.json"

                        with open(file_path) as f:
                            user_config = json.load(f)

                        # Get latest app version
                        from study_framework_core.core.handlers import get_latest_app_version
                        latest_app_version = get_latest_app_version()

                        resp = handle_response(
                            {
                                'info': 'user_handler success', 
                                'uid': uid, 
                                'config': user_config,
                                'latest_app_version': latest_app_version
                            }, code)

                    else:
                        code = 401
                        # Get latest app version
                        from study_framework_core.core.handlers import get_latest_app_version
                        latest_app_version = get_latest_app_version()
                        
                        resp = handle_response({
                            'info': 'user_handler failed', 
                            'uid': 'invalid', 
                            'config': 'invalid',
                            'latest_app_version': latest_app_version
                        }, code)
        except ValidationError as err:
            code = 400
            resp = handle_response(err.messages, code)
        except Exception as e:
            code = 500
            resp = handle_response(f'Code verification failed: {str(e)}', code)
        return resp, code


class UserInfo(Resource):
    """User information update endpoint."""
    def post(self):
        code = 403
        resp = handle_response('not permitted to make API Calls', code)
        try:
            # Get configuration
            from study_framework_core.core.config import get_config
            config = get_config()
            
            reques = UserInfoSchema().load(request.json)
            if 'auth_key' in request.json:
                uid = str(request.json['uid'])
                info_key = str(request.json['info_key'])
                info_value = str(request.json['info_value'])
                if request.json['auth_key'] == config.security.auth_key:
                    code = 200
                    save_info(uid, info_key, info_value)
                    resp = handle_response({'info': 'saved user info', 'uid': request.json['uid']}, code)
        except ValidationError as err:
            code = 400
            resp = handle_response(err.messages, code)
        except Exception as e:
            code = 500
            resp = handle_response(f'User info update failed: {str(e)}', code)
        return resp, code


class PhonePing(Resource):
    """Phone status ping endpoint."""
    def post(self):
        code = 403
        resp = handle_response('not permitted to make API Calls', code)
        try:
            # Get configuration
            from study_framework_core.core.config import get_config
            config = get_config()
            
            reques = UserPingSchema().load(request.json)
            if 'auth_key' in request.json:
                uid = str(request.json['uid'])
                device = 'ios'
                device_type = 'unknown'
                if 'device' in request.json:
                    device = request.json['device']
                if 'device_type' in request.json:
                    device_type = request.json['device_type']
                logging.info("Ping user {0} -- {1}".format(uid, device))
                if request.json['auth_key'] == config.security.auth_key:
                    code = 200
                    save_user_ping(uid, device, device_type)
                    resp = handle_response({'info': 'pinged', 'uid': request.json['uid']}, code)
        except ValidationError as err:
            code = 400
            resp = handle_response(err.messages, code)
        except Exception as e:
            code = 500
            resp = handle_response(f'Phone ping failed: {str(e)}', code)
        return resp, code


class UploadFile(Resource):
    """File upload endpoint."""
    def post(self):
        received_auth_key = request.values.get('auth_key')
        received_auth_key = str(received_auth_key).replace("\r\n", "").replace("\r", "").replace("\n", "")
        uid = request.values.get('uid')
        uid = str(uid).replace("\r\n", "").replace("\r", "").replace("\n", "")
        
        logging.info("/data/upload: uid = {0}".format(uid))

        if 'file' in request.files:
            file = request.files['file']
            if file.filename == '':
                resp = handle_response("Bad Request", 400)
                return resp, 400
            if file and allowed_file(file.filename):
                # Get configuration
                from study_framework_core.core.config import get_config
                config = get_config()
                
                if received_auth_key == config.security.auth_key:
                    if check_end_date(uid):
                        code = 200
                        save_file(uid, file)
                        resp = handle_response({'info': 'upload successful', 'uid': request.values['uid']}, code)
                        logging.info("/data/upload: uid = {0}; resp = {1}".format(uid, resp))
                    else:
                        code = 403
                        resp = handle_response({'info': 'study completed', 'uid': request.values['uid']}, 403)
                else:
                    code = 403
                    resp = handle_response('not permitted to make API Calls', code)
            else:
                code = 400
                resp = handle_response("Bad Request - Invalid file type", 400)
        else:
            code = 400
            resp = handle_response("Bad Request - No file", 400)

        return resp, code


class UploadLogFile(Resource):
    """Log file upload endpoint."""
    def post(self):
        code = 403
        resp = handle_response('not permitted to make API Calls', code)
        received_auth_key = request.values.get('auth_key')
        received_auth_key = str(received_auth_key).replace("\r\n", "").replace("\r", "").replace("\n", "")
        uid = request.values.get('uid')
        uid = str(uid).replace("\r\n", "").replace("\r", "").replace("\n", "")
        
        logging.info("/data/uploadLog: uid = {0}".format(uid))

        if 'file' in request.files:
            file = request.files['file']
            if file.filename == '':
                resp = handle_response("Bad Request", 400)
                return resp, 400
            if file and allowed_file(file.filename):
                # Get configuration
                from study_framework_core.core.config import get_config
                config = get_config()
                
                if received_auth_key == config.security.auth_key:
                    if check_end_date(uid):
                        code = 200
                        save_logfile(uid, file)
                        resp = handle_response({'info': 'Logfile upload successful', 'uid': request.values['uid']}, code)
                        logging.info("/data/uploadLog: uid = {0}; resp = {1}".format(uid, resp))
                    else:
                        code = 403
                        resp = handle_response({'info': 'study completed', 'uid': request.values['uid']}, 403)
                else:
                    code = 403
                    resp = handle_response('not permitted to make API Calls', code)
            else:
                code = 400
                resp = handle_response("Bad Request - Invalid file type", 400)
        else:
            code = 400
            resp = handle_response("Bad Request - No file", 400)

        return resp, code


class UploadDailyDiary(Resource):
    """Daily diary upload endpoint."""
    def post(self):
        received_auth_key = request.values.get('auth_key')
        received_auth_key = str(received_auth_key).replace("\r\n", "").replace("\r", "").replace("\n", "")
        uid = request.values.get('uid')
        uid = str(uid).replace("\r\n", "").replace("\r", "").replace("\n", "")
        
        logging.info("/data/daily-diary: uid = {0}".format(uid))

        if 'file' in request.files:
            file = request.files['file']
            if file.filename == '':
                resp = handle_response("Bad Request", 400)
                return resp, 400
            if file and allowed_file(file.filename):
                # Get configuration
                from study_framework_core.core.config import get_config
                config = get_config()
                
                if received_auth_key == config.security.auth_key:
                    if check_end_date(uid):
                        code = 200
                        from study_framework_core.core.handlers import save_daily_diary_file
                        save_daily_diary_file(uid, file)
                        resp = handle_response({'info': 'upload successful', 'uid': request.values['uid']}, code)
                        logging.info("/data/daily-diary: uid = {0}; resp = {1}".format(uid, resp))
                    else:
                        code = 403
                        resp = handle_response({'info': 'study completed', 'uid': request.values['uid']}, 403)
                else:
                    code = 403
                    resp = handle_response('not permitted to make API Calls', code)
            else:
                code = 400
                resp = handle_response("Bad Request - Invalid file type", 400)
        else:
            code = 400
            resp = handle_response("Bad Request - No file", 400)

        return resp, code


class UploadEma(Resource):
    """EMA upload endpoint."""
    def post(self):
        received_auth_key = request.values.get('auth_key')
        received_auth_key = str(received_auth_key).replace("\r\n", "").replace("\r", "").replace("\n", "")
        uid = request.values.get('uid')
        uid = str(uid).replace("\r\n", "").replace("\r", "").replace("\n", "")
        
        logging.info("/data/ema-response: uid = {0}".format(uid))

        if 'file' in request.files:
            file = request.files['file']
            if file.filename == '':
                resp = handle_response("Bad Request", 400)
                return resp, 400
            if file and allowed_file(file.filename):
                # Get configuration
                from study_framework_core.core.config import get_config
                config = get_config()
                
                if received_auth_key == config.security.auth_key:
                    if check_end_date(uid):
                        code = 200
                        from study_framework_core.core.handlers import save_ema_file
                        save_ema_file(uid, file)
                        resp = handle_response({'info': 'upload successful', 'uid': request.values['uid']}, code)
                        logging.info("/data/ema-response: uid = {0}; file = {1}".format(uid, file))
                    else:
                        code = 403
                        resp = handle_response({'info': 'study completed', 'uid': request.values['uid']}, 403)
                else:
                    code = 403
                    resp = handle_response('not permitted to make API Calls', code)
            else:
                code = 400
                resp = handle_response("Bad Request - Invalid file type", 400)
        else:
            code = 400
            resp = handle_response("Bad Request - No file", 400)

        return resp, code


class RequestEmaFile(Resource):
    """Request EMA file endpoint."""
    def post(self):
        received_auth_key = request.json['auth_key']
        received_auth_key = str(received_auth_key).replace("\r\n", "").replace("\r", "").replace("\n", "")
        uid = request.json['uid']
        uid = str(uid).replace("\r\n", "").replace("\r", "").replace("\n", "")
        
        logging.info("/data/ema_request: uid = {0}".format(uid))

        # Get configuration
        from study_framework_core.core.config import get_config
        config = get_config()
        
        if received_auth_key == config.security.auth_key:
            # Look for user-specific EMA file
            file_path = os.path.join(config.paths.ema_file_path, str(uid), "ema.json")
            if not os.path.exists(file_path):
                file_path = os.path.join(config.paths.ema_file_path, "global", "ema.json")

            # Send the file as a response
            return send_file(file_path, as_attachment=True)
        else:
            # Handle unauthorized access
            code = 403
            resp = handle_response(f'not permitted to make API Calls', code)
            return resp, code





class UploadJSON(Resource):
    """Upload JSON data endpoint."""
    def post(self):
        try:
            if not request.is_json:
                return {'error': 'Content-Type must be application/json'}, 400

            body = request.get_json()
            uid = body.get('uid')
            data = body.get('data')
            data_type = body.get('data_type', 'news_task')

            if not uid or not data:
                return {'error': 'Both "uid" and "data" fields are required'}, 400

            # Save JSON data
            from study_framework_core.core.handlers import save_json_data
            success = save_json_data(uid, data, data_type)
            
            if success:
                return {'message': f'File saved for user {uid}'}, 200
            else:
                return {'error': 'Failed to save data'}, 500
                
        except Exception as e:
            return {'error': str(e)}, 500
