"""
Base dashboard classes for the study framework.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional
from dataclasses import dataclass
import logging
import os
import json
from datetime import datetime, timedelta


@dataclass
class DashboardColumn:
    """Represents a column in the dashboard table."""
    name: str
    header: str
    width: Optional[str] = None
    sortable: bool = True
    filterable: bool = True


class DashboardBase(ABC):
    """
    Base class for dashboard functionality.
    
    This class defines the interface that all study-specific dashboards must implement.
    Study-specific dashboards can extend this class to add custom columns and functionality.
    """
    
    def __init__(self):
        self._core_columns = self._get_core_columns()
        self._custom_columns = self._get_custom_columns()
        self.logger = logging.getLogger(self.__class__.__name__)
        
        # NEW: Performance optimization - cache column definitions
        self._all_columns_cache = None
    
    def _get_core_columns(self) -> List[DashboardColumn]:
        """Get the core columns that are always present in all studies."""
        return [
            DashboardColumn("user", "User", width="10%"),
            DashboardColumn("phone_duration", "Phone Duration", width="12%"),
            DashboardColumn("garmin_worn", "Garmin Worn", width="10%"),
            DashboardColumn("garmin_on", "Garmin On", width="10%"),
            DashboardColumn("distance_traveled", "Distance Traveled", width="12%"),
            DashboardColumn("details", "Details", width="8%"),
        ]
    
    @abstractmethod
    def _get_custom_columns(self) -> List[DashboardColumn]:
        """
        Get study-specific custom columns.
        
        Override this method in study-specific implementations to add custom columns.
        """
        return []
    
    def get_all_columns(self) -> List[DashboardColumn]:
        """Get all columns (core + custom) with caching for performance."""
        if self._all_columns_cache is None:
            self._all_columns_cache = self._core_columns + self._custom_columns
        return self._all_columns_cache
    
    def generate_core_row_data(self, user_data: Dict[str, Any], date_str: str) -> Dict[str, Any]:
        """
        Generate data for core columns.
        
        Args:
            user_data: User data from database
            date_str: Date string in format "mm-dd-yy"
            
        Returns:
            Dictionary with core column data
        """
        # Get configuration
        from study_framework_core.core.config import get_config
        config = get_config()
        
        # Get database connection
        from study_framework_core.core.handlers import get_db
        db = get_db()
        
        uid = user_data.get('uid', '')
        
        # Convert date string to timestamp
        try:
            date_obj = datetime.strptime(date_str, "%m-%d-%y")
            date_timestamp = int(date_obj.timestamp())
        except ValueError:
            self.logger.error(f"Invalid date format: {date_str}")
            date_timestamp = int(datetime.now().timestamp())
        
        # Get daily summary for the specified date
        daily_summary = db[config.collections.DAILY_SUMMARY].find_one({'uid': uid, "date": date_timestamp})
        
        core_data = {
            'user': uid,
            'phone_duration': 0.0,
            'garmin_worn': 0.0,
            'garmin_on': 0.0,
            'distance_traveled': 0.0,
            'details': f"<a href='/internal_web/patient/{uid}?date={datetime.strptime(date_str, '%m-%d-%y').strftime('%Y-%m-%d')}' class='btn-details'>View Details</a>"
        }
        
        if daily_summary:
            # Get location data from nested structure
            location_data = daily_summary.get("location", {})
            core_data.update({
                'phone_duration': location_data.get("duration_hours", 0.0),
                'garmin_worn': daily_summary.get("garmin_wear_duration", 0.0),
                'garmin_on': daily_summary.get("garmin_on_duration", 0.0),
                'distance_traveled': location_data.get("distance_traveled", 0.0)
            })
        
        return core_data
    
    @abstractmethod
    def generate_custom_row_data(self, user_data: Dict[str, Any], date_str: str) -> Dict[str, Any]:
        """
        Generate data for custom columns.
        
        Args:
            user_data: User data from database
            date_str: Date string in format "mm-dd-yy"
            
        Returns:
            Dictionary with custom column data
        """
        return {}
    
    def generate_row_data(self, user_data: Dict[str, Any], date_str: str) -> Dict[str, Any]:
        """Generate complete row data including core and custom columns."""
        try:
            core_data = self.generate_core_row_data(user_data, date_str)
            custom_data = self.generate_custom_row_data(user_data, date_str)
            return {**core_data, **custom_data}
        except Exception as e:
            self.logger.error(f"Error generating row data for user {user_data.get('uid', 'unknown')}: {e}")
            return {"error": f"Data generation failed: {str(e)}"}
    
    def get_template_context(self, token: str, date_str: str, users: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Get template context for rendering the dashboard.
        
        Args:
            token: Authentication token
            date_str: Date string
            users: List of users
            
        Returns:
            Dictionary with template context
        """
        # Get configuration
        from study_framework_core.core.config import get_config
        config = get_config()
        
        # Get database connection
        from study_framework_core.core.handlers import get_db
        db = get_db()
        
        # Generate rows HTML
        rows_html = ''
        for user in users:
            uid = user['uid']
            row_data = self.generate_row_data(user, date_str)
            
            if 'error' in row_data:
                self.logger.error(f"Skipping user {uid} due to error: {row_data['error']}")
                continue
            
            rows_html += '<tr>'
            for column in self.get_all_columns():
                value = row_data.get(column.name, '')
                if isinstance(value, float):
                    value = f"{value:.2f}"
                rows_html += f'<td>{value}</td>'
            rows_html += '</tr>\n'
        
        # Calculate navigation dates
        try:
            current_date = datetime.strptime(date_str, "%m-%d-%y")
            previous_date = (current_date - timedelta(days=1)).strftime("%m-%d-%y")
            next_date = (current_date + timedelta(days=1)).strftime("%m-%d-%y")
            most_recent_date = datetime.now().date().strftime("%m-%d-%y")
            
            # Don't show next date if it's today or future
            if current_date.date() >= datetime.now().date() - timedelta(days=1):
                next_date = None
        except ValueError:
            previous_date = None
            next_date = None
            most_recent_date = datetime.now().date().strftime("%m-%d-%y")
        
        # Calculate statistics
        active_users = 0
        total_ema_responses = 0
        total_garmin_hours = 0
        garmin_users = 0
        
        for user in users:
            uid = user['uid']
            user_row_data = self.generate_row_data(user, date_str)
            
            # Count active users (those with any data)
            if (user_row_data.get('phone_duration', 0) > 0 or 
                user_row_data.get('garmin_worn', 0) > 0 or 
                user_row_data.get('garmin_on', 0) > 0):
                active_users += 1
            
            # Count EMA responses (simplified - you might want to get this from actual data)
            if 'ema_responses' in user_row_data:
                total_ema_responses += len(user_row_data['ema_responses'])
            
            # Calculate average Garmin hours
            garmin_hours = user_row_data.get('garmin_on', 0)
            if garmin_hours > 0:
                total_garmin_hours += garmin_hours
                garmin_users += 1
        
        avg_garmin_hours = total_garmin_hours / garmin_users if garmin_users > 0 else 0
        
        return {
            'rows_html': rows_html,
            'date_html': date_str,
            'token': token,
            'user': None,
            'previous_date': previous_date,
            'next_date': next_date,
            'most_recent_date': most_recent_date,
            'columns': self.get_all_columns(),
            'refresh_config': self.get_refresh_config(),
            'users': users,
            'active_users': active_users,
            'total_ema_responses': total_ema_responses,
            'avg_garmin_hours': avg_garmin_hours,
            # Extension points for study-specific customizations
            'custom_css': self.get_custom_css(),
            'custom_content': self.get_custom_content(),
            'custom_scripts': self.get_custom_scripts(),
            'column_icons': self.get_column_icons()
        }
    
    # NEW: Performance optimization methods
    def clear_cache(self):
        """Clear the column cache (useful for testing or dynamic updates)."""
        self._all_columns_cache = None
    
    def get_column_by_name(self, name: str) -> Optional[DashboardColumn]:
        """Get a specific column by name."""
        for column in self.get_all_columns():
            if column.name == name:
                return column
        return None
    
    # NEW: Validation methods
    def validate_user_data(self, user_data: Dict[str, Any]) -> bool:
        """Validate that user data has required fields."""
        required_fields = ['uid']
        return all(field in user_data for field in required_fields)
    
    def get_column_count(self) -> int:
        """Get the total number of columns."""
        return len(self.get_all_columns())
    
    # NEW: Real-time refresh capability
    def enable_real_time_refresh(self, interval_seconds: int = 30):
        """Enable real-time dashboard refresh."""
        self.refresh_interval = interval_seconds
        self.logger.info(f"Real-time refresh enabled with {interval_seconds}s interval")
    
    def get_refresh_config(self) -> Dict[str, Any]:
        """Get refresh configuration for the frontend."""
        return {
            "enabled": hasattr(self, 'refresh_interval'),
            "interval": getattr(self, 'refresh_interval', None),
            "endpoint": "/api/dashboard/refresh"
        }
    
    # Extension points for study-specific customizations
    def get_custom_css(self) -> str:
        """
        Get custom CSS for study-specific styling.
        
        Override this method in study-specific implementations to add custom CSS.
        
        Returns:
            String containing custom CSS
        """
        return ""
    
    def get_custom_content(self) -> str:
        """
        Get custom HTML content to inject into the dashboard.
        
        Override this method in study-specific implementations to add custom content.
        
        Returns:
            String containing custom HTML content
        """
        return ""
    
    def get_custom_scripts(self) -> str:
        """
        Get custom JavaScript for study-specific functionality.
        
        Override this method in study-specific implementations to add custom scripts.
        
        Returns:
            String containing custom JavaScript
        """
        return ""
    
    def get_column_icons(self) -> Dict[str, str]:
        """
        Get custom column icons for study-specific columns.
        
        Override this method in study-specific implementations to add custom column icons.
        
        Returns:
            Dictionary mapping column names to icon classes
        """
        return {}
